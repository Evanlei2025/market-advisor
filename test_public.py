"""Offline contract tests for the public research and publication path."""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import public_brief
import public_health
import public_market
import public_runner
import public_sources
import public_store


def fixture(day="2026-08-12", state="优先研究"):
    asset = {"date": day, "close": 100, "returns": {"5": 1, "20": 2, "60": 4}, "drawdown60": -2}
    sector = {"date": day, "close": 150, "returns": {"5": 2, "20": 6, "60": 9},
              "drawdown60": -4, "pe": 19, "participation": 1.3}
    return {"date": day, "asof": day, "assets": {"沪深300": asset},
            "sectors": {"电子": {"state": state, "relative20_pp": 4,
                               "evidence": ["相对领先", "成交确认"], "risk": "回撤-4%",
                               "invalidate": "相对强度跌破2pp", "data": sector}},
            "bond_y10": None, "cash_shibor": None, "gold": None, "quality": []}


def deploy_fixture(state, day="2026-08-12"):
    with tempfile.TemporaryDirectory() as tmp:
        path, site = Path(tmp, "state.json"), Path(tmp, "site")
        public_store.save(path, state)
        public_brief.render_site(site, state["weeks"], state["alerts"])
        public_runner.mark_deployed(path, output=site, deployment_id="test-pages",
                                    published_at=day + "T19:35:00+08:00")
        return public_store.load(path)


def delivery_config(count=1):
    config = json.dumps({"recipients": [
        {"id": f"test{i}", "channel": "serverchan", "key": f"synthetic{i}"}
        for i in range(count)]})
    return config, list(public_runner.recipients_from_config(json.loads(config)))


class NewsFetcher(public_sources.DataFetcher):
    """Use the real news adapter with an offline AkShare-shaped response."""

    def __init__(self, rows, day="2026-08-12"):
        class FakeAk:
            def stock_info_global_cls(self):
                return rows
        super().__init__(ak=FakeAk())
        self.day = day
        self.dates = pd.bdate_range(end=day, periods=70)
        self.names = [f"industry{i}" for i in range(20)]

    def trade_calendar(self):
        return pd.DataFrame({"trade_date": [self.day]})

    def index_daily(self, *args, **kwargs):
        return pd.DataFrame({"date": self.dates, "close": range(100, 170)})

    def industry_universe(self):
        return pd.DataFrame({"指数代码": self.names, "指数名称": self.names})

    def industry_history(self, code):
        return pd.DataFrame({"日期": self.dates, "收盘": range(100, 170)})

    def industry_analysis(self, day):
        return pd.DataFrame()

    def bond_rates(self):
        return None

    def shibor(self):
        return None

    def gold_daily(self):
        return None

    def cpi(self):
        return None

    def ppi(self):
        return None


class FixRegressionTests(unittest.TestCase):
    def live_fixture(self, tmp):
        path, site = Path(tmp, "state.json"), Path(tmp, "site")
        state = public_runner.prepare(public_store.empty_state(), fixture(), True)
        state = public_runner.prepare(state, fixture("2026-08-13", "暂缓追涨"))
        public_store.save(path, state)
        public_brief.render_site(site, state["weeks"], state["alerts"])
        manifest = public_brief.validate_manifest(site)
        return path, site, manifest

    def live_response(self, manifest, content=b"", error=None):
        class Response:
            def raise_for_status(self):
                if error:
                    raise error

            def json(self):
                return manifest

        response = Response()
        response.content = content
        return response

    def test_live_verification_checks_manifest_and_all_public_pages_without_state_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, site, manifest = self.live_fixture(tmp)
            before = path.read_bytes()
            fetched = []

            def transport(url, **kwargs):
                name = url.removeprefix(public_runner.URL)
                fetched.append(name)
                self.assertEqual(kwargs["params"], {"v": manifest["site_id"]})
                self.assertIn("no-cache", kwargs["headers"]["Cache-Control"])
                return self.live_response(manifest, (site / name).read_bytes())

            site_id = public_runner.verify_live_run(path, site, transport=transport,
                                                    wait=lambda seconds: self.fail("unexpected retry"))
            self.assertEqual(site_id, manifest["site_id"])
            self.assertEqual(fetched, ["manifest.json", *sorted(manifest["files"]), "manifest.json"])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(public_store.load(path)["deployed_pages"], {})

    def test_live_verification_waits_for_manifest_propagation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, site, manifest = self.live_fixture(tmp)
            fetched, waits = [], []

            def transport(url, **kwargs):
                name = url.removeprefix(public_runner.URL)
                fetched.append(name)
                return self.live_response({} if len(fetched) == 1 else manifest, (site / name).read_bytes())

            public_runner.verify_live_run(path, site, transport=transport, attempts=2, wait=waits.append)
            self.assertEqual(waits, [10])
            self.assertEqual(fetched[:2], ["manifest.json", "manifest.json"])

    def test_live_verification_blocks_stale_manifest_bad_page_http_failure_and_publication_race(self):
        for mode in ("manifest", "content", "http", "race"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                path, site, manifest = self.live_fixture(tmp)
                before = path.read_bytes()
                fetched, waits = [], []

                def transport(url, **kwargs):
                    name = url.removeprefix(public_runner.URL)
                    fetched.append(name)
                    is_final = len(fetched) % (len(manifest["files"]) + 2) == 0
                    bad_manifest = mode == "manifest" or (mode == "race" and is_final)
                    return self.live_response({} if bad_manifest else manifest,
                                              b"stale" if mode == "content" else (site / name).read_bytes(),
                                              public_runner.requests.HTTPError("unavailable") if mode == "http" else None)

                with self.assertRaisesRegex(RuntimeError, "public Pages verification failed"):
                    public_runner.verify_live_run(path, site, transport=transport, attempts=2, wait=waits.append)
                self.assertEqual(waits, [10])
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(public_store.load(path)["deployed_pages"], {})

    def test_live_verification_rejects_local_state_mismatch_without_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, site, manifest = self.live_fixture(tmp)
            state = public_store.load(path)
            state["prepared_pages"]["weekly:2026-W33"] = "0" * 64
            state["pending"]["weekly:2026-W33"]["page_version"] = "0" * 64
            public_store.save(path, state)
            with self.assertRaisesRegex(ValueError, "manifest differs"):
                public_runner.verify_live_run(path, site, transport=lambda *a, **k: self.fail("request before local validation"))

    def test_live_verification_cli_and_workflow_order(self):
        with patch("sys.argv", ["public_runner.py", "verify-live"]), patch("public_runner.verify_live_run", return_value="fake-id") as call:
            with patch("builtins.print"):
                public_runner.main()
            call.assert_called_once_with()
        workflow = (public_runner.ROOT / ".github/workflows/daily-report.yml").read_text(encoding="utf-8")
        order = [workflow.index(text) for text in ("uses: actions/deploy-pages@v4",
                 "run: python public_runner.py verify-live", "name: Record verified Pages deployment",
                 "name: Send subscribed notifications")]
        self.assertEqual(order, sorted(order))

    def test_weekend_first_launch_week_equals_latest_due_week(self):
        cases = [("2026-10-10T19:35:00+08:00", "2026-10-09"),
                 ("2026-10-11T22:17:00+08:00", "2026-10-09"),
                 ("2027-01-03T22:17:00+08:00", "2026-12-31")]
        for timestamp, asof in cases:
            with self.subTest(timestamp=timestamp):
                now = datetime.fromisoformat(timestamp)
                research = dict(fixture(asof), date=now.date().isoformat())
                state = deploy_fixture(public_runner.prepare(public_store.empty_state(), research, True), now.date().isoformat())
                state["pending"].clear()
                self.assertEqual(max(state["weeks"]), public_health.due_week(now))
                public_health.assert_current(state, now, asof)

    def test_template_upgrade_loads_delivered_history(self):
        state = deploy_fixture(public_runner.prepare(public_store.empty_state(), fixture(), True))
        event_id = "weekly:2026-W33"
        state["delivered"][event_id] = "2026-08-12T19:36:00+08:00"
        del state["pending"][event_id]
        state = public_runner.prepare(state, fixture("2026-08-13"))
        observation = copy.deepcopy(state["observations"][0])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "state.json")
            public_store.save(path, state)
            original = public_brief.html_page
            with patch("public_brief.html_page", side_effect=lambda *a, **k: original(*a, **k) + "<!-- v2 -->"):
                loaded = public_store.load(path)
                updated = public_runner.prepare(loaded, fixture("2026-08-13"), True)
                redeployed = deploy_fixture(updated, "2026-08-14")
            self.assertEqual(redeployed["observations"][0], observation)
            self.assertEqual(redeployed["delivered"], state["delivered"])
            self.assertNotIn(event_id, redeployed["pending"])

    def test_timeout_cannot_send_same_attempt_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "state.json")
            public_store.save(path, deploy_fixture(public_runner.prepare(public_store.empty_state(), fixture(), True)))
            config, (alias,) = delivery_config()
            event = "weekly:2026-W33"
            sent = []
            def timeout(*args):
                sent.append(args)
                raise TimeoutError("uncertain")
            public_runner.reserve_run(alias, "run-1", event, path, config)
            with self.assertRaisesRegex(RuntimeError, "unknown"):
                public_runner.notify_run(alias, "run-1", event, path, config, sender=timeout)
            with self.assertRaises(ValueError):
                public_runner.notify_run(alias, "run-1", event, path, config, sender=timeout)
            self.assertEqual(len(sent), 1)

    def test_monday_does_not_forgive_missed_saturday(self):
        state = deploy_fixture(public_runner.prepare(public_store.empty_state(), fixture("2026-08-07"), True),
                               "2026-08-07")
        state["pending"].clear()
        state = public_runner.prepare(state, fixture("2026-08-17"), False)
        with self.assertRaisesRegex(ValueError, "2026-W33"):
            public_health.assert_current(state, datetime(2026, 8, 17, 22, 17, tzinfo=public_runner.TZ), "2026-08-17")

    def test_akshare_date_time_news_survives_collection(self):
        rows = pd.DataFrame({"标题": ["制造业统计信息更新公告"], "内容": ["内容"],
                             "发布日期": [date(2026, 8, 12)], "发布时间": [time(9, 30)]})
        snapshot = public_market.collect(date(2026, 8, 12), NewsFetcher(rows))
        self.assertEqual(len(snapshot["news"]), 1)
        self.assertEqual(snapshot["news"][0]["published"], "2026-08-12T09:30:00+08:00")

    def run_notification_shell(self, mode):
        bash = shutil.which("bash")
        if not bash:
            git = shutil.which("git")
            candidates = [Path("C:/Program Files/Git/bin/bash.exe")]
            if git:
                candidates.append(Path(git).parent.parent / "bin/bash.exe")
            bash = next((str(p) for p in candidates if p.is_file()), None)
        if not bash:
            self.fail("Bash is required for the notification workflow contract test")
        workflow = (public_runner.ROOT / ".github/workflows/daily-report.yml").read_text(encoding="utf-8")
        block = workflow.split("      - name: Send subscribed notifications\n", 1)[1]
        block = block.split("        run: |\n", 1)[1].split("      - name:", 1)[0]
        script = "\n".join(line[10:] for line in block.splitlines())
        fake = '''
python() {
  case "$2" in
    recipients)
      [ "$MODE" != recipients_fail ] || return 17
      [ "$MODE" != empty ] || return 0
      printf '%s\\n' receiver-a receiver-b ;;
    events)
      [ "$MODE" != events_fail ] || return 18
      [ "$MODE" != empty_events ] || return 0
      printf '%s\\n' weekly:2026-W33 alert:2026-08-13 ;;
    reserve) printf 'RESERVE %s %s\\n' "$4" "$8" ;;
    notify) printf 'SEND %s %s\\n' "$4" "$8" ;;
    *) return 99 ;;
  esac
}
git() { return 0; }
'''
        env = dict(os.environ, MODE=mode, GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="1")
        return subprocess.run([bash, "--noprofile", "--norc", "-c", fake + script],
                              env=env, capture_output=True, text=True, timeout=15)

    def test_workflow_list_query_failure_aborts_without_sends(self):
        for mode in ("recipients_fail", "events_fail"):
            with self.subTest(mode=mode):
                result = self.run_notification_shell(mode)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("SEND", result.stdout)

    def test_workflow_empty_and_multiple_lists(self):
        for mode in ("empty", "empty_events", "multiple"):
            with self.subTest(mode=mode):
                result = self.run_notification_shell(mode)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                sends = [line for line in result.stdout.splitlines() if line.startswith("SEND ")]
                if mode == "multiple":
                    self.assertEqual(set(sends), {
                        f"SEND {alias} {event}" for alias in ("receiver-a", "receiver-b")
                        for event in ("weekly:2026-W33", "alert:2026-08-13")})
                    self.assertEqual(len(sends), 4)
                else:
                    self.assertEqual(sends, [])

    def test_template_upgrade_preserves_pending_recipient_records_and_gates_send(self):
        config, aliases = delivery_config(3)
        event_id = "weekly:2026-W33"
        for deployed in (False, True):
            with self.subTest(deployed=deployed), tempfile.TemporaryDirectory() as tmp:
                path, site = Path(tmp, "state.json"), Path(tmp, "site")
                state = public_runner.prepare(public_store.empty_state(), fixture(), True)
                if deployed:
                    state = deploy_fixture(state)
                records = {aliases[0]: {"status": "accepted", "attempt": "accepted-1"},
                           aliases[1]: {"status": "unknown", "attempt": "unknown-1"}}
                state["pending"][event_id]["recipients"] = copy.deepcopy(records)
                public_store.save(path, state)
                original = public_brief.html_page
                with patch("public_brief.html_page", side_effect=lambda *a, **k: original(*a, **k) + "<!-- upgrade -->"):
                    state = public_runner.prepare(public_store.load(path), fixture(), True)
                    public_store.save(path, state)
                    self.assertEqual(state["pending"][event_id]["recipients"], records)
                    with self.assertRaisesRegex(ValueError, "deployed"):
                        public_runner.reserve_run(aliases[2], "fresh", event_id, path, config)
                    public_brief.render_site(site, state["weeks"], state["alerts"])
                # Deployment consumes saved hashes even if the process now uses another template.
                public_runner.mark_deployed(path, output=site, deployment_id="upgrade",
                                            published_at="2026-08-14T19:35:00+08:00")
                public_runner.reserve_run(aliases[2], "fresh", event_id, path, config)
                with self.assertRaises(ValueError):
                    public_runner.notify_run(aliases[1], "unknown-1", event_id, path, config,
                                             sender=lambda *a: self.fail("unknown was resent"))
                public_runner.notify_run(aliases[2], "fresh", event_id, path, config, sender=lambda *a: None)
                self.assertIn(event_id, public_store.load(path)["pending"])

    def test_schema_1_and_2_migration_preserves_history_without_rendering(self):
        config, (alias,) = delivery_config()
        state = deploy_fixture(public_runner.prepare(public_store.empty_state(), fixture(), True))
        state = public_runner.prepare(state, fixture("2026-08-13"))
        state["pending"]["weekly:2026-W33"]["recipients"][alias] = {"status": "unknown", "attempt": "old"}
        state["delivered"]["weekly:2026-W32"] = "2026-08-08T19:35:00+08:00"
        original_observation = copy.deepcopy(state["observations"][0])
        for schema in (1, 2):
            with self.subTest(schema=schema), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "state.json")
                old = copy.deepcopy(state)
                old["schema"] = schema
                del old["prepared_pages"]
                if schema == 1:
                    del old["deployed_pages"]
                    del old["pending"]["weekly:2026-W33"]["page_version"]
                    for key in ("published_at", "deployment_id", "published_asof"):
                        old["observations"][0].pop(key)
                path.write_text(json.dumps(old), encoding="utf-8")
                with patch("public_brief.page_versions", side_effect=AssertionError("load rendered HTML")):
                    migrated = public_store.load(path)
                    public_store.save(path, migrated)
                    self.assertEqual(public_store.load(path), migrated)
                self.assertEqual(migrated["schema"], 3)
                self.assertEqual(migrated["weeks"], old["weeks"])
                self.assertEqual(migrated["delivered"], old["delivered"])
                self.assertEqual(migrated["pending"]["weekly:2026-W33"]["recipients"],
                                 old["pending"]["weekly:2026-W33"]["recipients"])
                if schema == 1:
                    observation = migrated["observations"][0]
                    self.assertEqual(observation["legacy_returns"]["path"], original_observation["path"])
                    self.assertNotIn("reference_close", observation)
                    self.assertNotIn("published_at", observation)
                    self.assertEqual(migrated["legacy_deployed"], old["deployed"])
                    with self.assertRaisesRegex(ValueError, "deployed"):
                        public_runner.require_deployed(migrated, "weekly:2026-W33", migrated["pending"]["weekly:2026-W33"])
                    prepared = public_runner.prepare(migrated, fixture("2026-08-13"), True)
                    published = deploy_fixture(prepared, "2026-08-14")
                    self.assertEqual(published["observations"][0]["legacy_returns"], observation["legacy_returns"])
                    self.assertEqual(published["observations"][0]["published_at"], "2026-08-14T19:35:00+08:00")
                else:
                    self.assertEqual(migrated["deployed_pages"], old["deployed_pages"])
                    self.assertEqual(migrated["observations"][0], original_observation)
                    self.assertEqual(migrated["prepared_pages"], state["prepared_pages"])

    def test_corrupt_and_unknown_schema_never_reset(self):
        state = deploy_fixture(public_runner.prepare(public_store.empty_state(), fixture(), True))
        corruptions = [lambda s: s.update(schema=99), lambda s: s.update(schema=True),
                       lambda s: s.update(observations=[None]), lambda s: s.update(pending={"x": None}),
                       lambda s: s["week_asof"].update({"2026-W33": "2026-08-99"}),
                       lambda s: s["deployed_pages"]["weekly:2026-W33"].update(page_version="bad"),
                       lambda s: s["pending"]["weekly:2026-W33"].update(recipients={"real-name": {}}),
                       lambda s: s["snapshots"][0].update(asof="2026-08-14")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "state.json")
            for mutate in corruptions:
                bad = copy.deepcopy(state)
                mutate(bad)
                payload = json.dumps(bad)
                path.write_text(payload, encoding="utf-8")
                with self.assertRaises(ValueError):
                    public_store.load(path)
                self.assertEqual(path.read_text(encoding="utf-8"), payload)
            path.write_text("{broken json", encoding="utf-8")
            with self.assertRaises(ValueError):
                public_store.load(path)

    def test_tampered_manifest_and_wrong_prepared_version_block_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, site = Path(tmp, "state.json"), Path(tmp, "site")
            state = public_runner.prepare(public_store.empty_state(), fixture(), True)
            public_store.save(path, state)
            for damage in ("content", "page_mapping", "prepared", "path"):
                with self.subTest(damage=damage):
                    public_brief.render_site(site, state["weeks"], state["alerts"])
                    manifest_path = site / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if damage == "content":
                        (site / "weeks/2026-W33.html").write_text("tampered", encoding="utf-8")
                    elif damage == "page_mapping":
                        manifest["pages"]["weekly:2026-W33"] = "0" * 64
                        manifest["site_id"] = hashlib.sha256(json.dumps(
                            {"files": manifest["files"], "pages": manifest["pages"]},
                            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    elif damage == "path":
                        manifest["files"]["../outside.html"] = "0" * 64
                        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    else:
                        original = public_brief.html_page
                        with patch("public_brief.html_page", side_effect=lambda *a, **k: original(*a, **k) + "changed"):
                            public_brief.render_site(site, state["weeks"], state["alerts"])
                    with self.assertRaises(ValueError):
                        public_runner.mark_deployed(path, output=site, deployment_id="fake")
                    self.assertEqual(public_store.load(path)["deployed_pages"], {})

    def test_persistence_failure_before_send_sends_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "state.json")
            public_store.save(path, deploy_fixture(public_runner.prepare(public_store.empty_state(), fixture(), True)))
            config, (alias,) = delivery_config()
            event_id = "weekly:2026-W33"
            public_runner.reserve_run(alias, "one", event_id, path, config)
            def fail(*args):
                raise RuntimeError("ledger unavailable")
            with self.assertRaisesRegex(RuntimeError, "ledger unavailable"):
                public_runner.notify_run(alias, "one", event_id, path, config,
                                         sender=lambda *a: self.fail("sent without ledger"), persist=fail)
            self.assertEqual(public_store.load(path)["pending"][event_id]["recipients"][alias]["status"], "reserved")

    def test_final_persistence_failure_and_crash_leave_unknown(self):
        for crash in (False, True):
            with self.subTest(crash=crash), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "state.json")
                public_store.save(path, deploy_fixture(public_runner.prepare(public_store.empty_state(), fixture(), True)))
                config, (alias,) = delivery_config()
                event_id = "weekly:2026-W33"
                public_runner.reserve_run(alias, "one", event_id, path, config)
                sent, writes = [], []
                def persist(p, s):
                    writes.append(copy.deepcopy(s))
                    if len(writes) == 1:
                        public_store.save(p, s)
                        if crash:
                            raise KeyboardInterrupt("simulated crash after consumption")
                    else:
                        raise RuntimeError("final result not saved")
                with self.assertRaises(KeyboardInterrupt if crash else RuntimeError):
                    public_runner.notify_run(alias, "one", event_id, path, config,
                                             sender=lambda *a: sent.append(a), persist=persist)
                self.assertEqual(len(sent), 0 if crash else 1)
                self.assertEqual(public_store.load(path)["pending"][event_id]["recipients"][alias]["status"], "unknown")
                with self.assertRaises(ValueError):
                    public_runner.notify_run(alias, "one", event_id, path, config,
                                             sender=lambda *a: self.fail("consumed attempt was resent"))

    def test_git_ledger_push_precedes_sender_and_accepted_is_pushed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "state.json"
            public_store.save(path, deploy_fixture(public_runner.prepare(public_store.empty_state(), fixture(), True)))
            config, (alias,) = delivery_config()
            event_id = "weekly:2026-W33"
            public_runner.reserve_run(alias, "one", event_id, path, config)
            operations, remote = [], []
            def git(command, **kwargs):
                operations.append(command[1])
                if command[1] == "push":
                    remote.append(public_store.load(path))
                return subprocess.CompletedProcess(command, 1 if command[1] == "diff" else 0)
            def sender(*args):
                self.assertEqual(operations[-1], "push")
                self.assertEqual(remote[-1]["pending"][event_id]["recipients"][alias]["status"], "unknown")
                operations.append("sender")
            with patch("public_runner.ROOT", root):
                public_runner.notify_run(alias, "one", event_id, path, config, sender=sender,
                                         persist=lambda p, s: public_runner.persist_ledger(p, s, run=git))
            self.assertEqual(operations, ["add", "diff", "commit", "push", "sender", "add", "diff", "commit", "push"])
            self.assertIn(event_id, remote[-1]["delivered"])

    def test_git_push_failure_blocks_send_or_leaves_remote_unknown(self):
        for failing_push in (1, 2):
            with self.subTest(failing_push=failing_push), tempfile.TemporaryDirectory() as tmp:
                root, path = Path(tmp), Path(tmp, "state.json")
                public_store.save(path, deploy_fixture(public_runner.prepare(public_store.empty_state(), fixture(), True)))
                config, (alias,) = delivery_config()
                event_id = "weekly:2026-W33"
                public_runner.reserve_run(alias, "one", event_id, path, config)
                pushes, remote, sent = [], [], []
                def git(command, **kwargs):
                    if command[1] == "push":
                        pushes.append(1)
                        if len(pushes) == failing_push:
                            return subprocess.CompletedProcess(command, 17)
                        remote.append(public_store.load(path))
                    return subprocess.CompletedProcess(command, 1 if command[1] == "diff" else 0)
                with patch("public_runner.ROOT", root), self.assertRaisesRegex(RuntimeError, "ledger Git operation failed: push"):
                    public_runner.notify_run(alias, "one", event_id, path, config,
                                             sender=lambda *a: sent.append(a),
                                             persist=lambda p, s: public_runner.persist_ledger(p, s, run=git))
                self.assertEqual(len(sent), failing_push - 1)
                if remote:
                    self.assertEqual(remote[-1]["pending"][event_id]["recipients"][alias]["status"], "unknown")
                    public_store.save(path, remote[-1])
                with self.assertRaises(ValueError):
                    public_runner.notify_run(alias, "one", event_id, path, config,
                                             sender=lambda *a: self.fail("resent after Git failure"))

    def test_cli_uses_remote_ledger_callback(self):
        for phase, function in (("notify", "notify_run"), ("reserve", "reserve_run"), ("reconcile", "reconcile_run")):
            with self.subTest(phase=phase), patch("sys.argv", ["public_runner.py", phase]), patch.object(public_runner, function) as call:
                public_runner.main()
                self.assertIs(call.call_args.kwargs["persist"], public_runner.persist_ledger)

    def test_multiple_recipients_and_manual_reconciliation(self):
        for resolution in ("accepted", "retry"):
            with self.subTest(resolution=resolution), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "state.json")
                public_store.save(path, deploy_fixture(public_runner.prepare(public_store.empty_state(), fixture("2026-08-14"), True),
                                                      "2026-08-14"))
                config, (first, second) = delivery_config(2)
                event_id = "weekly:2026-W33"
                sent = []
                public_runner.reserve_run(first, "a", event_id, path, config)
                public_runner.notify_run(first, "a", event_id, path, config, sender=lambda *a: sent.append(a))
                self.assertEqual(public_runner.eligible_events(first, path, config), [])
                public_runner.reserve_run(second, "b", event_id, path, config)
                reserved = public_store.load(path)
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    public_health.assert_current(reserved, datetime(2026, 8, 15, 22, tzinfo=public_runner.TZ), "2026-08-14")
                def uncertain(*args):
                    sent.append(args)
                    raise TimeoutError()
                with self.assertRaises(RuntimeError):
                    public_runner.notify_run(second, "b", event_id, path, config, sender=uncertain)
                unresolved = public_store.load(path)
                self.assertNotIn(event_id, unresolved["delivered"])
                with self.assertRaisesRegex(ValueError, "unknown"):
                    public_health.assert_current(unresolved, datetime(2026, 8, 15, 22, tzinfo=public_runner.TZ), "2026-08-14")
                public_runner.reconcile_run(second, event_id, resolution, path, config)
                if resolution == "retry":
                    with self.assertRaisesRegex(ValueError, "new attempt"):
                        public_runner.reserve_run(second, "b", event_id, path, config)
                    public_runner.reserve_run(second, "c", event_id, path, config)
                    public_runner.notify_run(second, "c", event_id, path, config, sender=lambda *a: sent.append(a))
                self.assertEqual(len(sent), 2 if resolution == "accepted" else 3)
                self.assertIn(event_id, public_store.load(path)["delivered"])
                public_health.assert_current(public_store.load(path), datetime(2026, 8, 15, 22, tzinfo=public_runner.TZ), "2026-08-14")
                self.assertNotIn("synthetic", path.read_text(encoding="utf-8"))

    def test_notification_byte_limit_is_checked_before_consumption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "state.json")
            state = deploy_fixture(public_runner.prepare(public_store.empty_state(), fixture(), True))
            config, (alias,) = delivery_config()
            event_id = "weekly:2026-W33"
            state["pending"][event_id]["body"] = "字" * 1200
            public_store.save(path, state)
            with self.assertRaisesRegex(ValueError, "byte limit"):
                public_runner.reserve_run(alias, "one", event_id, path, config)
            state["pending"][event_id]["recipients"][alias] = {"status": "reserved", "attempt": "one"}
            public_store.save(path, state)
            with self.assertRaisesRegex(ValueError, "byte limit"):
                public_runner.notify_run(alias, "one", event_id, path, config,
                                         sender=lambda *a: self.fail("oversized content sent"))

    def test_weekly_deadline_boundaries_and_iso_year(self):
        cases = [("2026-08-15T19:34:59+08:00", "2026-W32"),
                 ("2026-08-15T19:35:00+08:00", "2026-W33"),
                 ("2026-08-16T22:17:00+08:00", "2026-W33"),
                 ("2026-08-17T22:17:00+08:00", "2026-W33"),
                 ("2026-10-05T22:17:00+08:00", "2026-W40"),
                 ("2027-01-04T22:17:00+08:00", "2026-W53"),
                 ("2026-08-15T11:35:00+00:00", "2026-W33")]
        for timestamp, week in cases:
            with self.subTest(timestamp=timestamp):
                self.assertEqual(public_health.due_week(datetime.fromisoformat(timestamp)), week)
        with self.assertRaises(ValueError):
            public_health.due_week(datetime(2026, 8, 15, 22))

    def test_heartbeat_requires_due_week_even_if_later_week_exists(self):
        now = datetime(2026, 8, 17, 22, 17, tzinfo=public_runner.TZ)
        state = public_runner.prepare(public_store.empty_state(), fixture("2026-08-17"), True)
        state = deploy_fixture(state, "2026-08-17")
        state["pending"].clear()
        with self.assertRaisesRegex(ValueError, "2026-W33"):
            public_health.assert_current(state, now, "2026-08-17")
        empty = public_store.empty_state()
        empty["snapshots"] = [fixture("2026-08-17")]
        with self.assertRaisesRegex(ValueError, "missing weekly"):
            public_health.assert_current(empty, now, "2026-08-17")

    def test_heartbeat_holiday_year_boundary_and_grace(self):
        cases = [("2026-08-07", "2026-08-07", "2026-08-15T19:34:59+08:00"),
                 ("2026-09-30", "2026-10-03", "2026-10-05T22:17:00+08:00"),
                 ("2026-12-31", "2027-01-02", "2027-01-04T22:17:00+08:00")]
        for asof, edition_day, timestamp in cases:
            with self.subTest(timestamp=timestamp):
                research = dict(fixture(asof), date=edition_day)
                state = deploy_fixture(public_runner.prepare(public_store.empty_state(), research, True), edition_day)
                state["pending"].clear()
                expected = asof
                if timestamp == "2026-08-15T19:34:59+08:00":
                    state = public_runner.prepare(state, fixture("2026-08-14"))
                    expected = "2026-08-14"
                public_health.assert_current(state, datetime.fromisoformat(timestamp), expected)
                state["prepared_pages"]["weekly:" + max(state["weeks"])] = "0" * 64
                with self.assertRaisesRegex(ValueError, "not deployed"):
                    public_health.assert_current(state, datetime.fromisoformat(timestamp), expected)

    def test_news_string_dates_window_sort_and_quality(self):
        rows = pd.DataFrame({"标题": ["制造业统计信息更新公告"] * 5,
                             "发布日期": ["2026-08-12", "2026-08-11", "2026-07-01", "broken", None],
                             "发布时间": ["11:30:00", "12:00:00", "12:00:00", "09:30:00", "bad"]})
        snapshot = public_market.collect(date(2026, 8, 12), NewsFetcher(rows))
        self.assertEqual([item["published"] for item in snapshot["news"]],
                         ["2026-08-12T11:30:00+08:00", "2026-08-11T12:00:00+08:00"])
        self.assertTrue(any("解析失败2/5" in message for message in snapshot["quality"]))

    def test_news_missing_fields_invalid_times_and_empty_are_distinct(self):
        valid = {"标题": ["制造业统计信息更新公告"], "发布日期": [date(2026, 8, 12)], "发布时间": [time(9, 30)]}
        frames = [(pd.DataFrame({k: v for k, v in valid.items() if k != "发布日期"}), "字段缺失"),
                  (pd.DataFrame(dict(valid, 发布时间=["invalid"])), "解析失败1/1"),
                  (pd.DataFrame(), "本次无新闻")]
        for frame, message in frames:
            with self.subTest(message=message):
                snapshot = public_market.collect(date(2026, 8, 12), NewsFetcher(frame))
                self.assertEqual(snapshot.get("news", []), [])
                self.assertTrue(any(message in item for item in snapshot["quality"]), snapshot["quality"])

    def test_temporary_ordinary_day_and_saturday_prepare_without_network(self):
        fetcher = NewsFetcher(pd.DataFrame(), day="2026-08-14")
        with tempfile.TemporaryDirectory() as tmp, patch("public_ai.explain", return_value=None):
            path, site = Path(tmp, "state.json"), Path(tmp, "site")
            baseline = deploy_fixture(public_runner.prepare(public_store.empty_state(), fixture("2026-08-07"), True),
                                      "2026-08-07")
            baseline["pending"].clear()
            public_store.save(path, baseline)
            daily = public_runner.prepare_run(path, site, date(2026, 8, 14), weekly_due=False,
                                             collector=public_market.collect, fetcher=fetcher)
            self.assertNotIn("2026-W33", daily["weeks"])
            self.assertEqual(daily["snapshots"][-1]["asof"], "2026-08-14")
            saturday = public_runner.prepare_run(path, site, date(2026, 8, 15), weekly_due=True,
                                                 collector=lambda **k: self.fail("Saturday should reuse the verified snapshot"),
                                                 fetcher=fetcher)
            self.assertEqual(len(saturday["weeks"]), 2)
            self.assertEqual(saturday["week_asof"]["2026-W33"], "2026-08-14")
            self.assertIn("weekly:2026-W33", saturday["pending"])
            self.assertEqual(public_brief.validate_manifest(site)["pages"], saturday["prepared_pages"])
            with patch.object(fetcher, "trade_calendar", side_effect=TimeoutError("offline failure")):
                with self.assertRaisesRegex(ValueError, "calendar"):
                    public_runner.prepare_run(path, site, date(2026, 8, 15), weekly_due=True,
                                              collector=lambda **k: self.fail("calendar failure must block reuse"), fetcher=fetcher)
            self.assertEqual(public_store.load(path), saturday)


class PublicTests(unittest.TestCase):
    def test_daily_watchdog_requires_current_snapshot_and_weekly(self):
        now = datetime(2026, 8, 15, 22, 17, tzinfo=public_runner.TZ)
        state = public_store.load(Path("nonexistent_public_state.json"))
        with self.assertRaisesRegex(ValueError, "missing market snapshot"):
            public_health.assert_current(state, now, "2026-08-14")
        state["snapshots"].append(fixture("2026-08-14"))
        with self.assertRaisesRegex(ValueError, "missing weekly"):
            public_health.assert_current(state, now, "2026-08-14")
        state = public_runner.prepare(state, fixture("2026-08-14"), True)
        with self.assertRaisesRegex(ValueError, "not deployed"):
            public_health.assert_current(state, now, "2026-08-14")
        state = deploy_fixture(state, "2026-08-14")
        event = state["pending"].pop("weekly:2026-W33")
        public_health.assert_current(state, now, "2026-08-14")
        state["pending"]["weekly:2026-W33"] = event
        with self.assertRaisesRegex(ValueError, "incomplete"):
            public_health.assert_current(state, now, "2026-08-14")
        state["pending"]["weekly:2026-W33"]["recipients"] = {
            "receiver-0000000000000000": {"status": "unknown", "attempt": "run"}}
        with self.assertRaisesRegex(ValueError, "unknown"):
            public_health.assert_current(state, now, "2026-08-14")

    def test_confirmed_opportunity_notifies_on_second_day(self):
        first = fixture("2026-08-11", "等待确认")
        second = fixture("2026-08-12", "优先研究")
        third = fixture("2026-08-13", "优先研究")
        self.assertEqual(public_runner._candidate_events(second, first), [])
        events = public_runner._candidate_events(third, second, first)
        self.assertEqual(len(events), 1)
        self.assertEqual(public_runner._candidate_events(fixture("2026-08-14"), third, second), [])

    def test_reference_price_is_next_observation(self):
        start = public_runner.prepare(public_store.load(Path("nonexistent_public_state.json")),
                                      fixture("2026-08-12"), weekly_due=True)
        start = deploy_fixture(start)
        self.assertNotIn("reference_close", start["observations"][0])
        next_day = fixture("2026-08-13")
        next_day["sectors"]["电子"]["data"]["close"] = 149
        after = public_runner.prepare(start, next_day)
        self.assertEqual(after["observations"][0]["reference_close"], 149)
        self.assertEqual(len(after["observations"][0]["path"]), 1)
        following = fixture("2026-08-14")
        following["sectors"]["电子"]["data"]["close"] = 146
        tracked = public_runner.prepare(after, following)
        self.assertEqual(len(tracked["observations"][0]["path"]), 2)
        self.assertIn("已过1个交易日", public_brief.weekly(following, observations=tracked["observations"]))

    def test_price_series_date_window_and_staleness(self):
        frame = pd.DataFrame({"date": pd.bdate_range(end="2026-08-12", periods=70),
                              "close": range(100, 170)})
        result = public_market.series_metrics(frame, date(2026, 8, 12))
        self.assertEqual(result["date"], "2026-08-12")
        self.assertGreater(result["returns"]["20"], 0)
        with self.assertRaisesRegex(ValueError, "stale"):
            public_market.series_metrics(frame, date(2026, 8, 20))

    def test_expected_session_weekend_and_exchange_holiday(self):
        class Ak:
            tool_trade_date_hist_sina = staticmethod(lambda: None)
        class Fetcher:
            def _ak(self, fn, **kwargs):
                return pd.DataFrame({"trade_date": ["2026-09-24", "2026-09-28"]})
        self.assertEqual(public_market.expected_session(date(2026, 9, 27), Fetcher(), Ak()),
                         "2026-09-24")
        self.assertEqual(public_market.expected_session(date(2026, 9, 28), Fetcher(), Ak()),
                         "2026-09-28")
        class BrokenFetcher:
            def trade_calendar(self):
                raise TimeoutError("calendar unavailable")
        with self.assertRaisesRegex(ValueError, "calendar"):
            public_market.expected_session(date(2026, 9, 28), BrokenFetcher())
        class FakeAk:
            @staticmethod
            def tool_trade_date_hist_sina():
                return pd.DataFrame({"trade_date": ["2026-09-28"]})
        self.assertEqual(public_sources.DataFetcher(ak=FakeAk()).trade_calendar().iloc[0, 0],
                         "2026-09-28")

    def test_public_collect_uses_fake_fetcher_only(self):
        day = date(2026, 8, 12)
        dates = pd.bdate_range(end=day.isoformat(), periods=70)
        prices = pd.DataFrame({"date": dates, "close": range(100, 170), "成交额": range(1000, 1070)})
        names = [f"行业{i:02d}" for i in range(20)]

        class FakeFetcher:
            def trade_calendar(self):
                return pd.DataFrame({"trade_date": [day.isoformat()]})

            def index_daily(self, symbol, days=180):
                return prices

            def industry_universe(self):
                return pd.DataFrame({"指数代码": names, "指数名称": names})

            def industry_history(self, code):
                return pd.DataFrame({"日期": dates, "收盘": range(120, 190)})

            def industry_analysis(self, asof):
                return pd.DataFrame({"指数名称": names, "发布日期": [day.isoformat()] * 20,
                                     "市盈率": [18] * 20, "市净率": [1.3] * 20})

            def bond_rates(self):
                return pd.DataFrame({"date": dates, "y2": [1.5] * 70, "y10": [2.1] * 70})

            def shibor(self):
                return pd.DataFrame({"报告日": [day.isoformat()], "利率": [1.8]})

            def gold_daily(self):
                return prices[["date", "close"]]

            def cpi(self):
                return 0.2, "2026-07"

            def ppi(self):
                return -1.8, "2026-07"

            def cls_news(self):
                return pd.DataFrame({"标题": ["制造业统计信息更新公告"], "内容": ["内容"],
                                     "发布时间": ["2026-08-12T09:30:00"]})

        snapshot = public_market.collect(day, fetcher=FakeFetcher())
        self.assertEqual(snapshot["asof"], day.isoformat())
        self.assertEqual(len(snapshot["sectors"]), 20)
        self.assertEqual(snapshot["macro"]["CPI"]["period"], "2026-07")

    def test_observation_starts_only_after_deployment(self):
        state = public_runner.prepare(public_store.load(Path("nonexistent_public_state.json")),
                                      fixture(), weekly_due=True)
        next_day = fixture("2026-08-13")
        before_deploy = public_runner.prepare(state, next_day)
        self.assertNotIn("reference_close", before_deploy["observations"][0])
        deployed = deploy_fixture(state)
        after_deploy = public_runner.prepare(deployed, next_day)
        self.assertEqual(after_deploy["observations"][0]["reference_close"], 150)

    def test_observation_keeps_reference_plus_60_sessions(self):
        state = deploy_fixture(public_runner.prepare(public_store.load(Path("nonexistent_public_state.json")),
                                                       fixture(), weekly_due=True))
        for stamp in pd.bdate_range(start="2026-08-13", periods=61):
            day = stamp.date().isoformat()
            state = public_runner.prepare(state, fixture(day), weekly_due=False)
        path = state["observations"][0]["path"]
        self.assertEqual(len(path), 61)
        self.assertIn("60个交易日后", public_brief.weekly(fixture(day), observations=state["observations"]))

    def test_delivery_is_rejected_before_pages_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "state.json")
            state = public_runner.prepare(public_store.load(path), fixture(), weekly_due=True)
            public_store.save(path, state)
            config = json.dumps({"recipients": [{"id": "test", "channel": "serverchan",
                                                   "key": "synthetic"}]})
            alias = next(iter(public_runner.recipients_from_config(json.loads(config))))
            with self.assertRaisesRegex(ValueError, "deployed"):
                public_runner.reserve_run(alias, "attempt", "weekly:2026-W33", path, config)

    def test_insufficient_industry_data_does_not_trigger(self):
        snapshot = {"date": "2026-08-12", "asof": "2026-08-12",
                    "assets": fixture()["assets"], "sectors": {"电子": fixture()["sectors"]["电子"]["data"]},
                    "quality": []}
        self.assertEqual(public_market.research(snapshot)["sectors"]["电子"]["state"], "优先研究")
        snapshot["sectors"]["电子"].pop("pe")
        self.assertEqual(public_market.research(snapshot)["sectors"]["电子"]["state"], "资料不足")

    def test_asset_readout_is_distinct_from_fund_returns(self):
        snapshot = {"date": "2026-08-12", "asof": "2026-08-12", "assets": {
            "沪深300": fixture()["assets"]["沪深300"],
            "中证全债": {"date": "2026-08-12", "close": 200,
                         "returns": {"5": 0.1, "20": 0.5, "60": 2.0}, "drawdown60": -1}},
            "sectors": {}, "quality": [], "bond_y10": {"date": "2026-08-12", "yield_pct": 1.8}}
        report = public_market.research(snapshot)
        lines = public_brief.asset_readout(report)
        self.assertTrue(any("久期风险" in line and "不代表基金收益" in line for line in lines))

    def test_news_is_context_not_a_trade_signal(self):
        r = fixture()
        r["news"] = [{"source": "财联社", "published": "2026-08-12T09:30:00",
                      "title": "制造业统计信息更新公告"}]
        digest = public_brief.weekly(r)
        self.assertIn("同期新闻线索（仅记录，并非涨跌原因）", digest)
        self.assertIn("制造业统计信息更新公告", digest)

    def test_state_transitions_and_weekly_idempotence(self):
        state = public_store.load(Path("nonexistent_public_state.json"))
        first = public_runner.prepare(state, fixture(), weekly_due=True)
        self.assertEqual(len(first["weeks"]), 1)
        self.assertEqual(first["week_asof"]["2026-W33"], "2026-08-12")
        self.assertEqual(len(first["observations"]), 1)
        self.assertEqual(len(first["pending"]), 1)
        self.assertEqual(public_runner.prepare(first, fixture(), weekly_due=True), first)
        tomorrow = fixture("2026-08-13", "暂缓追涨")
        next_state = public_runner.prepare(first, tomorrow)
        self.assertIn("alert:2026-08-13", next_state["pending"])
        self.assertTrue(next_state["pending"]["alert:2026-08-13"]["url"].endswith(
            "/alerts/2026-08-13.html"))
        self.assertEqual(public_runner.prepare(next_state, tomorrow), next_state)
        missing = fixture("2026-08-14", "资料不足")
        self.assertEqual(len(public_market.changes(missing, tomorrow)), 0)

    def test_alert_cooldown_and_published_week_comparison(self):
        state = public_store.load(Path("nonexistent_public_state.json"))
        state = public_runner.prepare(state, fixture("2026-08-12", "优先研究"), weekly_due=True)
        state = public_runner.prepare(state, fixture("2026-08-13", "暂缓追涨"))
        self.assertEqual(len(state["alerts"]), 1)
        state = public_runner.prepare(state, fixture("2026-08-14", "优先研究"))
        state = public_runner.prepare(state, fixture("2026-08-17", "优先研究"))
        self.assertEqual(len(state["alerts"]), 1)
        state = public_runner.prepare(state, fixture("2026-08-19", "等待确认"))
        state = public_runner.prepare(state, fixture("2026-08-20", "等待确认"), weekly_due=True)
        self.assertEqual(state["week_asof"]["2026-W34"], "2026-08-20")
        self.assertIn("2026-08-12", state["weeks"]["2026-W34"])

    def test_weekly_content_and_static_archive(self):
        content = public_brief.weekly(fixture())
        self.assertIn("尚无可比较", content)
        self.assertIn("电子", content)
        with tempfile.TemporaryDirectory() as tmp:
            public_brief.render_site(tmp, {"2026-W33": content},
                                     [{"asof": "2026-08-12", "body": "行业状态变化"}])
            Path(tmp, "stale.html").write_text("old", encoding="utf-8")
            public_brief.render_site(tmp, {"2026-W33": content}, [])
            page = Path(tmp, "index.html").read_text(encoding="utf-8")
            self.assertIn('href="weeks/2026-W33.html"', page)
            self.assertTrue(Path(tmp, "latest.html").is_file())
            self.assertFalse(Path(tmp, "alerts", "2026-08-12.html").exists())
            self.assertFalse(Path(tmp, "stale.html").exists())
            self.assertTrue(Path(tmp, "manifest.json").is_file())
            self.assertNotIn("Evan_Lei", page)

    def test_store_rejects_corruption_without_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "state.json")
            state = public_store.load(path)
            public_store.save(path, state)
            self.assertEqual(public_store.load(path), state)
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                public_store.load(path)

    def test_delivery_success_deduplicates_and_failure_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "state.json")
            state = deploy_fixture(public_runner.prepare(public_store.load(path), fixture(), weekly_due=True))
            public_store.save(path, state)
            sent = []
            config = json.dumps({"recipients": [{"id": "test", "channel": "serverchan", "key": "synthetic"}]})
            alias = next(iter(public_runner.recipients_from_config(json.loads(config))))
            self.assertTrue(alias.startswith("receiver-"))
            self.assertEqual(public_runner.eligible_events(alias, path, config), ["weekly:2026-W33"])
            public_runner.reserve_run(alias, "run-1", "weekly:2026-W33", path, config)
            public_runner.notify_run(alias, "run-1", "weekly:2026-W33", path, config,
                                     sender=lambda *a: sent.append(a))
            self.assertEqual(public_runner.eligible_events(alias, path, config), [])
            self.assertEqual(len(sent), 1)
            self.assertIn("weekly:2026-W33", public_store.load(path)["delivered"])

            state = public_store.load(path)
            state = deploy_fixture(public_runner.prepare(state, fixture("2026-08-13", "暂缓追涨")),
                                   "2026-08-13")
            public_store.save(path, state)
            def fails(*args):
                raise TimeoutError("provider uncertain")
            public_runner.reserve_run(alias, "run-2", "alert:2026-08-13", path, config)
            with self.assertRaisesRegex(RuntimeError, "unknown"):
                public_runner.notify_run(alias, "run-2", "alert:2026-08-13", path, config, sender=fails)
            self.assertEqual(public_store.load(path)["pending"]["alert:2026-08-13"]["recipients"][alias]["status"], "unknown")
            self.assertEqual(public_runner.eligible_events(alias, path, config), [])
            public_runner.reconcile_run(alias, "alert:2026-08-13", "retry", path, config)
            self.assertEqual(public_runner.eligible_events(alias, path, config), ["alert:2026-08-13"])
            self.assertEqual(len(sent), 1)

    def test_no_legacy_client_config_needed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp, "state.json")
            # A week without a verified snapshot must fail, not publish a blank brief.
            with self.assertRaises((KeyError, ValueError)):
                public_runner.prepare_run(state_path, Path(tmp, "site"), date(2026, 8, 15),
                                          weekly_due=True, collector=lambda **kwargs: {})
            # No legacy clients or portfolio configuration is involved.
            public_store.save(state_path, public_store.load(state_path))
            with self.assertRaises(ValueError):
                public_runner.recipients_from_config({"clients": {}})

    def test_legacy_delivery_secret_is_normalized_without_public_names(self):
        config = {"push": {"channel": "serverchan", "serverchan_key": "synthetic"},
                  "clients": {"Private Name": {"push": {"channel": "serverchan",
                                                            "serverchan_key": "synthetic-2"},
                                                 "holdings": [{"cost": 123}]}}}
        recipients = public_runner.recipients_from_config(config)
        self.assertEqual(len(recipients), 2)
        self.assertTrue(all(key.startswith("receiver-") for key in recipients))
        self.assertFalse(any("Private" in key for key in recipients))


if __name__ == "__main__":
    unittest.main()
