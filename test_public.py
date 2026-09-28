"""Offline contract tests for the public research and publication path."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

import public_brief
import public_health
import public_market
import public_runner
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


class PublicTests(unittest.TestCase):
    def test_daily_watchdog_requires_current_snapshot_and_weekly(self):
        state = public_store.load(Path("nonexistent_public_state.json"))
        with self.assertRaisesRegex(ValueError, "missing market snapshot"):
            public_health.assert_current(state, date(2026, 8, 15), "2026-08-14")
        state["snapshots"].append(fixture("2026-08-14"))
        with self.assertRaisesRegex(ValueError, "missing weekly"):
            public_health.assert_current(state, date(2026, 8, 15), "2026-08-14")
        state["weeks"]["2026-W33"] = "public text"
        with self.assertRaisesRegex(ValueError, "not deployed"):
            public_health.assert_current(state, date(2026, 8, 15), "2026-08-14")
        state["week_asof"]["2026-W33"] = "2026-08-14"
        state["deployed"]["2026-W33"] = "2026-08-14"
        public_health.assert_current(state, date(2026, 8, 15), "2026-08-14")

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
            page = Path(tmp, "index.html").read_text(encoding="utf-8")
            self.assertIn('href="weeks/2026-W33.html"', page)
            self.assertTrue(Path(tmp, "latest.html").is_file())
            self.assertTrue(Path(tmp, "alerts", "2026-08-12.html").is_file())
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
            state = public_store.load(path)
            state["pending"]["weekly:2026-W33"] = {"body": "周报", "url": "https://example.com",
                                                     "recipients": {}, "kind": "weekly"}
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
            state["pending"]["alert:2026-08-12"] = {"body": "alert", "url": "https://example.com",
                                                    "recipients": {}, "kind": "alert"}
            public_store.save(path, state)
            def fails(*args):
                raise TimeoutError("provider uncertain")
            public_runner.reserve_run(alias, "run-2", "alert:2026-08-12", path, config)
            with self.assertRaisesRegex(RuntimeError, "unknown"):
                public_runner.notify_run(alias, "run-2", "alert:2026-08-12", path, config, sender=fails)
            self.assertEqual(public_store.load(path)["pending"]["alert:2026-08-12"]["recipients"][alias]["status"], "unknown")
            self.assertEqual(public_runner.eligible_events(alias, path, config), [])
            public_runner.reconcile_run(alias, "alert:2026-08-12", "retry", path, config)
            self.assertEqual(public_runner.eligible_events(alias, path, config), ["alert:2026-08-12"])
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
