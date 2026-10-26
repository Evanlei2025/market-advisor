"""Generate one self-contained local market report and exit."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import time as monotonic_time
import uuid
import webbrowser
from zoneinfo import ZoneInfo

import public_brief
import public_market

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
TZ = ZoneInfo("Asia/Shanghai")
DATA_CUTOFF = time(19, 35)
MAX_RUNTIME_SECONDS = 600
PROGRAM_VERSION = "local-1"


def freeze_time(value=None):
    value = value or datetime.now(TZ)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("generation time must include a timezone")
    return value.astimezone(TZ)


def market_date_ceiling(generated_at):
    generated_at = freeze_time(generated_at)
    if generated_at.timetz().replace(tzinfo=None) < DATA_CUTOFF:
        return generated_at.date() - timedelta(days=1)
    return generated_at.date()


def _collect_snapshot(generated_at):
    from public_sources import DataFetcher

    return public_market.collect(today=market_date_ceiling(generated_at),
                                 fetcher=DataFetcher(), generated_at=generated_at)


def _collect_worker(connection, generated_at_text, collector):
    try:
        generated_at = datetime.fromisoformat(generated_at_text).astimezone(TZ)
        snapshot = collector(generated_at) if collector else _collect_snapshot(generated_at)
        if not isinstance(snapshot, dict):
            raise ValueError("collector did not return a research snapshot")
        _validate_market_snapshot(snapshot)
        snapshot["date"] = generated_at.date().isoformat()
        snapshot["generated_at"] = generated_at.isoformat()
        research = public_market.research(snapshot)
        research["generated_at"] = generated_at.isoformat()
        connection.send({"ok": True, "snapshot": snapshot, "research": research})
    except BaseException as error:
        try:
            connection.send({"ok": False, "error": f"{type(error).__name__}: {error}"})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


def _validate_market_snapshot(snapshot):
    asof = snapshot.get("asof")
    try:
        if date.fromisoformat(asof).isoformat() != asof:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("market snapshot has no valid as-of date") from None
    assets = snapshot.get("assets")
    sectors = snapshot.get("sectors")
    if not isinstance(assets, dict) or "沪深300" not in assets:
        raise ValueError("沪深300基准行情缺失，不能生成报告")
    if len(assets) < 3:
        raise ValueError("同日宽基/债券指数覆盖不足，不能生成报告")
    if any(item.get("date") != asof for item in assets.values()):
        raise ValueError("指数日期未与沪深300对齐，不能生成报告")
    if not isinstance(sectors, dict) or len(sectors) < 20:
        raise ValueError("申万一级行业行情覆盖不足，不能生成报告")
    if any(item.get("date") != asof for item in sectors.values()):
        raise ValueError("行业行情日期未与沪深300对齐，不能生成报告")


def collect_with_timeout(generated_at, timeout_seconds=MAX_RUNTIME_SECONDS, collector=None):
    if timeout_seconds <= 0:
        raise ValueError("runtime budget must be positive")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    worker = context.Process(target=_collect_worker,
                             args=(child, freeze_time(generated_at).isoformat(), collector),
                             name="market-report-collector")
    deadline = monotonic_time.monotonic() + timeout_seconds
    worker.start()
    child.close()
    message = None
    try:
        while monotonic_time.monotonic() < deadline:
            if parent.poll(0.1):
                message = parent.recv()
                break
            if not worker.is_alive():
                if parent.poll(0.1):
                    message = parent.recv()
                break
        if message is None and worker.is_alive():
            worker.terminate()
            worker.join(3)
            if worker.is_alive() and hasattr(worker, "kill"):
                worker.kill()
                worker.join(3)
            raise TimeoutError(f"data collection exceeded {timeout_seconds} seconds; worker stopped")
        worker.join(3)
        if message is None:
            raise RuntimeError("data collection worker exited without a result")
        if not message.get("ok"):
            raise RuntimeError("data collection failed: " + message.get("error", "unknown error"))
        if worker.exitcode not in (0, None):
            raise RuntimeError(f"data collection worker exited with code {worker.exitcode}")
        return message["snapshot"], message["research"]
    finally:
        parent.close()
        if worker.is_alive():
            worker.terminate()
            worker.join(3)


@contextmanager
def output_lock(output_root):
    output_root.mkdir(parents=True, exist_ok=True)
    lock_path = output_root / ".local-report.lock"
    lock_file = lock_path.open("a+b")
    try:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("another report run is already using this output folder") from None
        else:
            import fcntl
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("another report run is already using this output folder") from None
        yield
    finally:
        try:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_file.close()


def _valid_snapshot(snapshot, research, html_text, markdown_text):
    if not snapshot.get("asof") or research.get("asof") != snapshot["asof"]:
        raise ValueError("research snapshot has an invalid market date")
    if len(snapshot.get("assets", {})) < 3 or len(snapshot.get("sectors", {})) < 20:
        raise ValueError("research snapshot does not meet market coverage requirements")
    if not html_text.startswith("<!doctype html>") or "</html>" not in html_text:
        raise ValueError("HTML report validation failed")
    if snapshot["asof"] not in html_text or snapshot["generated_at"] not in html_text:
        raise ValueError("HTML report is missing its date metadata")
    if "# " not in markdown_text or snapshot["asof"] not in markdown_text:
        raise ValueError("Markdown report validation failed")


def _unique_run_folder(output_root, generated_at):
    base = generated_at.strftime("%Y-%m-%d_%H%M%S_%f")
    candidate = output_root / base
    sequence = 1
    while candidate.exists():
        candidate = output_root / f"{base}_{sequence:02d}"
        sequence += 1
    return candidate


def generate(output_root=REPORTS, generated_at=None, timeout_seconds=MAX_RUNTIME_SECONDS,
             collector=None, open_report=False):
    generated_at = freeze_time(generated_at)
    output_root = Path(output_root).resolve()
    started = monotonic_time.monotonic()
    deadline = started + timeout_seconds
    with output_lock(output_root):
        remaining = deadline - monotonic_time.monotonic()
        snapshot, research = collect_with_timeout(generated_at, remaining, collector)
        title, body = public_brief.local_text(research)
        html_text = public_brief.html_page(title, body)
        markdown_text = public_brief.markdown_page(title, body)
        payload = {
            "schema": 1,
            "program_version": PROGRAM_VERSION,
            "generated_at": generated_at.isoformat(),
            "asof": snapshot["asof"],
            "snapshot": snapshot,
            "research": research,
        }
        json_text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        _valid_snapshot(snapshot, research, html_text, markdown_text)
        if monotonic_time.monotonic() > deadline:
            raise TimeoutError("report runtime budget exceeded before saving output")

        staging = Path(tempfile.mkdtemp(prefix=".report-staging-", dir=output_root))
        try:
            (staging / "report.html").write_text(html_text, encoding="utf-8", newline="\n")
            (staging / "report.md").write_text(markdown_text, encoding="utf-8", newline="\n")
            (staging / "research.json").write_text(json_text, encoding="utf-8", newline="\n")
            json.loads((staging / "research.json").read_text(encoding="utf-8"))
            final_folder = _unique_run_folder(output_root, generated_at)
            os.replace(staging, final_folder)
        except Exception:
            if staging.exists():
                import shutil
                shutil.rmtree(staging)
            raise

        latest = output_root / "latest.html"
        latest_tmp = output_root / (".latest-" + uuid.uuid4().hex + ".tmp")
        try:
            latest_tmp.write_text(html_text, encoding="utf-8", newline="\n")
            os.replace(latest_tmp, latest)
        finally:
            if latest_tmp.exists():
                latest_tmp.unlink()

    report_path = final_folder / "report.html"
    markdown_path = final_folder / "report.md"
    snapshot_path = final_folder / "research.json"
    print(f"报告 HTML：{report_path}")
    print(f"报告 Markdown：{markdown_path}")
    print(f"研究快照：{snapshot_path}")
    print(f"最新报告：{latest}")
    if open_report:
        webbrowser.open(latest.as_uri())
    return {"folder": final_folder, "html": report_path, "markdown": markdown_path,
            "snapshot": snapshot_path, "latest": latest}


def main(argv=None):
    parser = argparse.ArgumentParser(description="按需生成一次本地市场简报")
    parser.add_argument("--open", action="store_true", help="成功后在浏览器打开 latest.html")
    args = parser.parse_args(argv)
    try:
        generate(open_report=args.open)
    except Exception as error:
        print(f"本次未成功生成报告：{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
