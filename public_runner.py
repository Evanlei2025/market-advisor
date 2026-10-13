"""Prepare the public site, then notify after a verified Pages deployment."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import date, datetime
from pathlib import Path
from time import sleep
from zoneinfo import ZoneInfo

import requests

import public_brief
import public_market
import public_sources
import public_store
import public_schedule

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "public_data" / "state.json"
SITE = ROOT / "public_site"
URL = "https://Evanlei2025.github.io/market-advisor/"
TZ = ZoneInfo("Asia/Shanghai")


def _session_gap(last_asof, expected, fetcher):
    """Count verified sessions between two dates for delayed-run protection."""
    if not last_asof or expected <= last_asof:
        return 0
    calendar = fetcher.trade_calendar()
    if calendar is None or "trade_date" not in calendar:
        raise ValueError("verified trading calendar unavailable for delayed-run check")
    dates = public_market.pd.to_datetime(calendar["trade_date"], errors="coerce").dt.date.dropna()
    return int(((dates > date.fromisoformat(last_asof)) &
                (dates <= date.fromisoformat(expected))).sum())


def _candidate_events(now, before, older=None):
    # Opportunity entry needs two successive dates; exit and overheating notify immediately.
    events = []
    if not before:
        return events
    for name, item in now["sectors"].items():
        prev = before["sectors"].get(name)
        if not prev or prev["state"] == "资料不足" or item["state"] == "资料不足":
            continue
        old = (older or {}).get("sectors", {}).get(name)
        if item["state"] == "优先研究":
            if (prev["state"] != "优先研究" or old is None
                    or old["state"] == "优先研究"):
                continue
            before_state = old["state"] if old else "等待确认"
        elif prev["state"] != item["state"] and (prev["state"] == "优先研究" or item["state"] == "暂缓追涨"):
            before_state = prev["state"]
        else:
            continue
        events.append({"id": f"{now['asof']}:{name}:{before_state}:{item['state']}",
                       "entity": name, "before": before_state, "after": item["state"],
                       "relative20_pp": item["relative20_pp"]})
    return events


def _recently_notified(store, entity, asof):
    from datetime import date
    current = date.fromisoformat(asof)
    return any(0 <= (current - date.fromisoformat(record["asof"])).days < 7
               and any(event["entity"] == entity for event in record.get("events", []))
               for record in store["alerts"])


def prepare(store, research, weekly_due=False, suppress_alerts=False):
    import copy
    store = copy.deepcopy(store)
    previous = store["snapshots"][-1] if store["snapshots"] else None
    asof = research["asof"]
    if previous and asof < previous["asof"] and not weekly_due:
        raise ValueError("historical snapshot precedes latest state; backfill must run in date order")
    if not previous or previous["asof"] < asof:
        events = [] if suppress_alerts else _candidate_events(research, previous,
                                   store["snapshots"][-2] if len(store["snapshots"]) >= 2 else None)
        events = [event for event in events if not _recently_notified(store, event["entity"], asof)]
        store["snapshots"].append(research)
        store["snapshots"] = store["snapshots"][-65:]
        if events:
            key = "alert:" + asof
            text = public_brief.alert(research, events)
            store["pending"][key] = {"body": text, "url": URL + "alerts/" + asof + ".html",
                                     "recipients": {}, "kind": "alert"}
            store["alerts"].append({"asof": asof, "body": text, "events": events})
            recent = store["alerts"][-20:]
            retained_ids = set(store["pending"])
            retained = [entry for entry in store["alerts"]
                        if "alert:" + entry["asof"] in retained_ids]
            retained_dates = {entry["asof"] for entry in retained}
            store["alerts"] = retained + [entry for entry in recent
                                           if entry["asof"] not in retained_dates]
        # Set the reference price on the first observation after publication.
        for entry in store["observations"]:
            if not entry.get("published_at"):
                continue
            current = research["sectors"].get(entry["entity"])
            if (current and "reference_close" not in entry and
                    current["data"]["date"] > entry["published_asof"]):
                entry["reference_close"] = current["data"]["close"]
                entry["reference_date"] = current["data"]["date"]
                entry["reference_benchmark"] = research["assets"][public_market.BENCHMARK]["close"]
                entry["path"] = [{"date": current["data"]["date"],
                                  "close": current["data"]["close"],
                                  "benchmark": research["assets"][public_market.BENCHMARK]["close"]}]
            elif current and entry.get("reference_close") and current["data"]["date"] > entry["reference_date"]:
                entry.setdefault("path", []).append({
                    "date": current["data"]["date"], "close": current["data"]["close"],
                    "benchmark": research["assets"][public_market.BENCHMARK]["close"]})
                entry["path"] = entry["path"][:61]
    if weekly_due:
        wk = public_brief.week_id(research["date"])
        if wk not in store["weeks"]:
            earlier_wk = max((key for key in store["weeks"] if key < wk), default=None)
            earlier_asof = store["week_asof"].get(earlier_wk) if earlier_wk else None
            earlier = next((s for s in reversed(store["snapshots"])
                            if s["asof"] == earlier_asof), None)
            body = public_brief.weekly(research, earlier, store["observations"])
            store["weeks"][wk] = body
            store["week_asof"][wk] = research["asof"]
            store["pending"]["weekly:" + wk] = {"body": brief_message(research, earlier, wk),
                                                 "url": URL + "weeks/" + wk + ".html",
                                                 "recipients": {}, "kind": "weekly"}
            for name, item in public_brief.highlighted(research):
                store["observations"].append({"entity": name, "asof": item["data"]["date"],
                                               "week": wk, "published_at": None,
                                               "deployment_id": None})
            store["observations"] = store["observations"][-150:]
            keep_weeks = set(event_id[7:] for event_id in store["pending"]
                             if event_id.startswith("weekly:"))
            ordered_weeks = sorted(store["weeks"])
            keep_weeks.update(ordered_weeks[-104:])
            store["weeks"] = {key: store["weeks"][key] for key in sorted(keep_weeks)}
            store["week_asof"] = {k: store["week_asof"][k] for k in store["weeks"]}
    store["site_version"] = public_brief.site_version(store["weeks"], store["alerts"])
    page_versions = public_brief.page_versions(store["weeks"], store["alerts"])
    store["prepared_pages"] = page_versions
    store["deployed"] = {k: v for k, v in store["deployed"].items() if k in store["weeks"]}
    store["deployed_pages"] = {k: v for k, v in store["deployed_pages"].items() if k in page_versions}
    for event_id, event in store["pending"].items():
        if event_id not in page_versions:
            raise ValueError("pending event has no public page")
        event["page_version"] = page_versions[event_id]
    return store


def brief_message(research, previous, wk):
    picks = public_brief.highlighted(research)
    lines = [f"市场机会与风险周报 {wk}｜截至{research['asof']}",
             "较上期：" + public_brief.recap(research, previous, [])[0],
             "宽基：" + "、".join(f"{name}近5日{item['returns']['5']:+.1f}%"
                              for name, item in research["assets"].items()),
             "行业关注：" + ("、".join(name for name, _ in picks) if picks else "暂无同时满足条件的方向"),
             "判断边界：行业研究筛选不等于确定买点；详见证据与反证。"]
    return "\n".join(lines)


def prepare_run(state_path=STATE, output=SITE, today=None, weekly_due=None, collector=None, fetcher=None,
                context=None, backfill=False):
    if context is None and today is None:
        context = public_schedule.resolve()
    if context:
        today = public_schedule.parse_date(context["logical_date"])
        weekly_due = context["kind"] == "weekly"
        backfill = context.get("backfill", False)
    else:
        today = public_schedule.parse_date(today)
    state = public_store.load(state_path)
    weekly_due = (today.weekday() == 5 or not state["weeks"]) if weekly_due is None else weekly_due
    # A Saturday digest uses the most recent complete daily observation.
    if fetcher is None and (collector is None or (weekly_due and today.weekday() >= 5 and state["snapshots"])):
        fetcher = public_sources.DataFetcher()
    if collector is None or collector is public_market.collect:
        expected = public_market.expected_session(today, fetcher)
        previous_asof = state["snapshots"][-1]["asof"] if state["snapshots"] else None
        if previous_asof and expected < previous_asof and not weekly_due:
            raise ValueError("historical snapshot precedes latest state; backfill must run in date order")
        if previous_asof and _session_gap(previous_asof, expected, fetcher) > 1:
            raise ValueError(f"需要补跑历史日期：最新快照 {previous_asof}，目标行情 {expected}；请逐日显式 backfill")
        if context and not backfill:
            completed = public_schedule.completed_day(datetime.fromisoformat(context["observed_at"]).astimezone(TZ))
            last_completed = public_market.expected_session(completed, fetcher)
            if _session_gap(expected, last_completed, fetcher) > 1:
                raise ValueError(f"需要补跑历史日期：任务 {today} 已延迟超过一个交易日；禁止自动跳过")
    if weekly_due and today.weekday() >= 5 and state["snapshots"]:
        expected = public_market.expected_session(today, fetcher)
        matching = next((item for item in reversed(state["snapshots"]) if item["asof"] == expected), None)
        if matching:
            research = dict(matching, date=today.isoformat())
        else:
            raise ValueError(f"周报 {public_brief.week_id(today.isoformat())} 缺少截至 {expected} 的快照，需要补跑历史日期")
    else:
        raw = (collector or public_market.collect)(today=today, fetcher=fetcher)
        research = public_market.research(raw)
    if weekly_due and public_brief.week_id(research["date"]) not in state["weeks"]:
        import public_ai
        research["editor_note"] = public_ai.explain(research)
    if weekly_due and today.weekday() >= 5 and research["asof"] == today.isoformat():
        raise ValueError("weekly edition cannot use a weekend market date")
    if collector is None:
        expected = public_market.expected_session(today, fetcher)
        if research["asof"] != expected:
            raise ValueError(f"行情日期{research['asof']}未对齐最近交易日{expected}")
    updated = prepare(state, research, weekly_due, suppress_alerts=backfill)
    if not updated["weeks"]:
        raise ValueError("no public weekly page to deploy")
    public_store.validate(updated)
    public_brief.render_site(output, updated["weeks"], updated["alerts"])
    public_store.save(state_path, updated)
    return updated


def recipients_from_config(config):
    recipients = config.get("recipients")
    if not isinstance(recipients, list):
        # One-release migration bridge: accept the old secret in memory only.
        # No client name, endpoint, or legacy config is written to public state.
        recipients = []
        global_push = config.get("push", {})
        if isinstance(global_push, dict):
            recipients.append({"id": "legacy-global", "channel": global_push.get("channel"),
                               "key": global_push.get("serverchan_key"),
                               "webhook": global_push.get("wecom_webhook")})
        for client in config.get("clients", {}).values():
            if isinstance(client, dict) and isinstance(client.get("push"), dict):
                push = client["push"]
                recipients.append({"id": "legacy-channel", "channel": push.get("channel"),
                                   "key": push.get("serverchan_key"),
                                   "webhook": push.get("wecom_webhook")})
    if not isinstance(recipients, list) or not recipients:
        raise ValueError("no public delivery recipients configured")
    out = {}
    endpoints = set()
    for item in recipients:
        alias = str(item.get("id", ""))
        channel = item.get("channel")
        endpoint = item.get("key") if channel == "serverchan" else item.get("webhook")
        if not alias or not alias.replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid recipient alias")
        if channel not in ("serverchan", "wecom") or not endpoint:
            raise ValueError("invalid recipient channel")
        if (channel, endpoint) in endpoints:
            raise ValueError("duplicate recipient or destination")
        # Only anonymous IDs go into the public Git ledger, never customer names.
        public_id = "receiver-" + hashlib.sha256((channel + ":" + endpoint).encode()).hexdigest()[:16]
        out[public_id] = {"channel": channel, "endpoint": endpoint}
        endpoints.add((channel, endpoint))
    return out


def send(recipient, title, text, transport=requests.post):
    if recipient["channel"] == "serverchan":
        url = "https://sctapi.ftqq.com/" + recipient["endpoint"] + ".send"
        payload = {"title": title, "desp": text}
    else:
        url = recipient["endpoint"]
        payload = {"msgtype": "markdown", "markdown": {"content": "### " + title + "\n" + text}}
    response = transport(url, json=payload, timeout=15)
    response.raise_for_status()
    result = response.json()
    ok = result.get("code") == 0 if recipient["channel"] == "serverchan" else result.get("errcode") == 0
    if not ok:
        raise RuntimeError("provider rejected delivery")


def _delivery_recipients(delivery_json):
    source = delivery_json or os.environ.get("ADVISOR_DELIVERY_JSON", "")
    if not source:
        raise ValueError("ADVISOR_DELIVERY_JSON missing; refusing to read client configuration")
    return recipients_from_config(json.loads(source))


def verify_live_run(state_path=STATE, output=SITE, transport=requests.get, attempts=6,
                    wait=sleep):
    """Verify the public manifest and every page before recording deployment."""
    state = public_store.load(state_path)
    manifest = public_brief.validate_manifest(output)
    if not state["weeks"] or manifest["pages"] != state["prepared_pages"]:
        raise ValueError("public site manifest differs from prepared state")
    if not isinstance(attempts, int) or attempts < 1:
        raise ValueError("positive verification attempts required")

    def fetch(name):
        response = transport(URL + name, params={"v": manifest["site_id"]},
                             headers={"Cache-Control": "no-cache"}, timeout=(5, 15))
        response.raise_for_status()
        return response

    for attempt in range(attempts):
        try:
            if fetch("manifest.json").json() != manifest:
                raise ValueError("public manifest differs from deployed artifact")
            for name, digest in sorted(manifest["files"].items()):
                if hashlib.sha256(fetch(name).content).hexdigest() != digest:
                    raise ValueError("public page content differs: " + name)
            # Confirm the manifest still identifies the same publication after fetching pages.
            if fetch("manifest.json").json() != manifest:
                raise ValueError("public manifest changed during verification")
            return manifest["site_id"]
        except (requests.RequestException, ValueError) as exc:
            error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            if attempt + 1 < attempts:
                wait(10)
    raise RuntimeError("public Pages verification failed: " + error) from None


def mark_deployed(state_path=STATE, persist=public_store.save, output=SITE, deployment_id=None,
                  published_at=None):
    state = public_store.load(state_path)
    if not state["weeks"]:
        raise ValueError("no weekly edition to mark deployed")
    manifest = public_brief.validate_manifest(output)
    expected = state["prepared_pages"]
    if "weekly:" + max(state["weeks"]) not in expected:
        raise ValueError("weekly page has not been prepared")
    if manifest["pages"] != expected:
        raise ValueError("deployed site manifest differs from prepared state")
    if not deployment_id:
        raise ValueError("verified Pages deployment id required")
    published_at = published_at or datetime.now(TZ).isoformat()
    latest_asof = state["snapshots"][-1]["asof"] if state["snapshots"] else state["week_asof"][max(state["weeks"])]
    # If deployment was delayed, observations must start after its actual date.
    boundary = max(latest_asof, datetime.fromisoformat(published_at).astimezone(TZ).date().isoformat())
    for event_id, version in expected.items():
        record = state["deployed_pages"].get(event_id)
        if record is None or record["page_version"] != version:
            state["deployed_pages"][event_id] = {"page_version": version,
                "deployment_id": str(deployment_id), "published_at": published_at}
        if event_id.startswith("weekly:"):
            wk = event_id[7:]
            state["deployed"][wk] = state["week_asof"][wk]
            record = state["deployed_pages"][event_id]
            for entry in state["observations"]:
                if entry["week"] == wk and not entry.get("published_at"):
                    entry.update(published_at=record["published_at"],
                                 deployment_id=record["deployment_id"], published_asof=boundary)
    persist(state_path, state)


def require_deployed(state, event_id, event):
    record = state["deployed_pages"].get(event_id)
    version = state["prepared_pages"].get(event_id)
    if (not version or version != event.get("page_version")
            or not record or record.get("page_version") != version):
        raise ValueError("event page is not deployed at the pending version")
    if event["kind"] == "weekly":
        wk = event_id[7:]
        if state["deployed"].get(wk) != state["week_asof"].get(wk):
            raise ValueError("weekly edition is not deployed")


def eligible_events(alias, state_path=STATE, delivery_json=None):
    recipients = _delivery_recipients(delivery_json)
    if alias not in recipients:
        raise ValueError("invalid recipient alias")
    state = public_store.load(state_path)
    out = []
    for event_id, event in state["pending"].items():
        require_deployed(state, event_id, event)
        if alias not in event["recipients"]:
            out.append(event_id)
    return out


def reserve_run(alias, attempt, event_id, state_path=STATE, delivery_json=None, persist=public_store.save):
    state = public_store.load(state_path)
    recipients = _delivery_recipients(delivery_json)
    if alias not in recipients or not attempt or event_id not in state["pending"]:
        raise ValueError("invalid delivery attempt")
    event = state["pending"][event_id]
    require_deployed(state, event_id, event)
    if alias in event["recipients"]:
        raise ValueError("delivery already reserved")
    if attempt in event.get("retired_attempts", {}).get(alias, []):
        raise ValueError("retry requires a new attempt")
    content = event["body"] + "\n" + event["url"]
    if len(content.encode("utf-8")) > 3500:
        raise ValueError("public push exceeds byte limit")
    event["recipients"][alias] = {"status": "reserved", "attempt": attempt}
    persist(state_path, state)


def notify_run(alias, attempt, event_id, state_path=STATE, delivery_json=None, sender=send,
               persist=public_store.save):
    state = public_store.load(state_path)
    recipients = _delivery_recipients(delivery_json)
    if alias not in recipients or not attempt or event_id not in state["pending"]:
        raise ValueError("invalid delivery attempt")
    event = state["pending"][event_id]
    require_deployed(state, event_id, event)
    if event["recipients"].get(alias) != {"status": "reserved", "attempt": attempt}:
        raise ValueError("no reserved delivery for this attempt")
    content = event["body"] + "\n" + event["url"]
    if len(content.encode("utf-8")) > 3500:
        raise ValueError("public push exceeds byte limit")
    # Consume the reservation durably before contacting the provider.
    event["recipients"][alias] = {"status": "unknown", "attempt": attempt}
    persist(state_path, state)
    try:
        sender(recipients[alias], "市场周报" if event["kind"] == "weekly" else "市场变化", content)
    except Exception:
        raise RuntimeError("delivery status unknown for " + alias) from None
    event["recipients"][alias] = {"status": "accepted", "attempt": attempt}
    if all(event["recipients"].get(name, {}).get("status") == "accepted" for name in recipients):
        state["delivered"][event_id] = datetime.now(TZ).isoformat()
        del state["pending"][event_id]
    state["delivered"] = dict(sorted(state["delivered"].items())[-300:])
    persist(state_path, state)


def reconcile_run(alias, event_id, status, state_path=STATE, delivery_json=None,
                  persist=public_store.save):
    """Manually resolve a provider-unknown notification before resuming automation."""
    state = public_store.load(state_path)
    recipients = _delivery_recipients(delivery_json)
    if alias not in recipients or event_id not in state["pending"]:
        raise ValueError("invalid reconciliation target")
    if status not in ("accepted", "retry"):
        raise ValueError("status must be accepted or retry")
    event = state["pending"][event_id]
    record = event["recipients"].get(alias)
    if not record or record["status"] == "accepted":
        raise ValueError("no unresolved delivery to reconcile")
    if status == "accepted":
        event["recipients"][alias] = dict(record, status="accepted", reconciled=True)
    else:
        event.setdefault("retired_attempts", {}).setdefault(alias, []).append(record["attempt"])
        event["recipients"].pop(alias, None)
    if status == "accepted" and all(event["recipients"].get(name, {}).get("status") == "accepted" for name in recipients):
        state["delivered"][event_id] = datetime.now(TZ).isoformat()
        del state["pending"][event_id]
    state["delivered"] = dict(sorted(state["delivered"].items())[-300:])
    persist(state_path, state)


def persist_ledger(state_path, state, run=subprocess.run):
    """The CLI requires a successful remote ledger push before any send."""
    relative = Path(state_path).resolve().relative_to(ROOT).as_posix()
    public_store.save(state_path, state)

    def git(*args, allowed=(0,)):
        result = run(["git", *args], cwd=ROOT, capture_output=True, text=True)
        if result.returncode not in allowed:
            raise RuntimeError("public delivery ledger Git operation failed: " + args[0])
        return result.returncode

    git("add", "--", relative)
    if git("diff", "--cached", "--quiet", "--", relative, allowed=(0, 1)) == 1:
        git("commit", "--only", "-m", "public: persist notification ledger", "--", relative)
    git("push", "origin", "HEAD:master")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", nargs="?", default="prepare",
                        choices=["prepare", "verify-live", "deployed", "recipients", "events", "reserve", "notify", "reconcile"])
    parser.add_argument("--weekly", action="store_true", help="Publish an edition outside the Saturday schedule")
    parser.add_argument("--logical-date")
    parser.add_argument("--run-kind", choices=["auto", "daily", "weekly"], default="auto")
    parser.add_argument("--backfill", action="store_true", help="Explicit historical recovery; suppress new change notifications")
    parser.add_argument("--context", default=os.environ.get("PUBLIC_CONTEXT_FILE"))
    parser.add_argument("--recipient")
    parser.add_argument("--attempt")
    parser.add_argument("--event")
    parser.add_argument("--status")
    parser.add_argument("--deployment-id")
    args = parser.parse_args()
    if args.phase == "prepare":
        if args.context:
            context = json.loads(Path(args.context).read_text(encoding="utf-8"))
        else:
            context = public_schedule.resolve(kind="weekly" if args.weekly else args.run_kind,
                                              logical_date=args.logical_date, backfill=args.backfill)
        prepare_run(context=context)
    elif args.phase == "verify-live":
        print("Verified public Pages site " + verify_live_run())
    elif args.phase == "deployed":
        mark_deployed(deployment_id=args.deployment_id)
    elif args.phase == "recipients":
        for alias in _delivery_recipients(None):
            print(alias)
    elif args.phase == "events":
        for event_id in eligible_events(args.recipient):
            print(event_id)
    elif args.phase == "reserve":
        reserve_run(args.recipient, args.attempt, args.event, persist=persist_ledger)
    elif args.phase == "reconcile":
        reconcile_run(args.recipient, args.event, args.status, persist=persist_ledger)
    else:
        notify_run(args.recipient, args.attempt, args.event, persist=persist_ledger)


if __name__ == "__main__":
    main()
