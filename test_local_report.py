"""Offline behavioral tests for one-shot local report generation."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
import os
import time
import unittest
from unittest.mock import patch

import pandas as pd

import local_report
import public_market


ASOF = "2026-10-23"


def snapshot(asof=ASOF, quality=None):
    assets = {}
    for index, name in enumerate(("沪深300", "中证500", "中证全债")):
        assets[name] = {"date": asof, "close": 1000 + index,
                        "returns": {"5": index + 1.0, "20": index + 2.0, "60": index + 3.0},
                        "drawdown60": -4.0}
    sectors = {}
    for index in range(20):
        sectors[f"行业{index:02d}"] = {
            "date": asof, "close": 100 + index,
            "returns": {"5": 2.0 + index / 10, "20": 5.0 + index / 10, "60": 8.0},
            "drawdown60": -4.0, "participation": 1.3, "pe": 18.0, "pb": 1.2,
            "code": f"{index:06d}",
        }
    return {"schema": 1, "date": asof, "asof": asof, "assets": assets, "sectors": sectors,
            "quality": list(quality or []), "bond_y10": None, "cash_shibor": None,
            "gold": None, "macro": {}, "news": []}


def fake_collector(generated_at):
    asof = local_report.market_date_ceiling(generated_at)
    while asof.weekday() >= 5:
        asof -= timedelta(days=1)
    return snapshot(asof.isoformat())


def missing_baseline_collector(_generated_at):
    value = snapshot()
    del value["assets"]["沪深300"]
    return value


def optional_missing_collector(_generated_at):
    return snapshot(quality=["十年国债收益率不可用", "现金参考利率暂缺"])


def slow_collector(_generated_at):
    time.sleep(10)
    return snapshot()


class MarketFetcher:
    def __init__(self, include_future_aux=False):
        self.days = pd.bdate_range(end=ASOF, periods=70)
        self.include_future_aux = include_future_aux
        self.names = [f"行业{index:02d}" for index in range(20)]

    def trade_calendar(self):
        return pd.DataFrame({"trade_date": self.days})

    def index_daily(self, _symbol, days=180):
        dates = self.days[-min(days, len(self.days)):]
        return pd.DataFrame({"date": dates, "close": range(100, 100 + len(dates))})

    def industry_universe(self):
        return pd.DataFrame({"指数代码": [f"{i:06d}.SI" for i in range(20)], "指数名称": self.names})

    def industry_history(self, code):
        return pd.DataFrame({"日期": self.days, "收盘": range(100, 170), "成交额": [100.0] * 70})

    def industry_analysis(self, day):
        return pd.DataFrame({"指数名称": self.names, "发布日期": [f"{day[:4]}-{day[4:6]}-{day[6:]}" for _ in self.names],
                             "市盈率": [18.0] * 20, "市净率": [1.2] * 20})

    def bond_rates(self):
        dates = list(self.days)
        yields = [2.1] * len(dates)
        if self.include_future_aux:
            dates.append(pd.Timestamp("2026-10-26"))
            yields.append(9.9)
        return pd.DataFrame({"date": dates, "y2": [1.5] * len(dates), "y10": yields})

    def shibor(self):
        dates = [day.date().isoformat() for day in self.days]
        rates = [1.8] * len(dates)
        if self.include_future_aux:
            dates.append("2026-10-26")
            rates.append(9.9)
        return pd.DataFrame({"报告日": dates, "利率": rates})

    def gold_daily(self):
        return pd.DataFrame({"date": self.days, "close": range(100, 170)})

    def cpi(self):
        return 0.9, "2026-09", "2026-10-15T09:30:00+08:00"

    def ppi(self):
        return 5.0, "2026-09", "2026-10-15T09:30:00+08:00"

    def cls_news(self):
        return pd.DataFrame({"标题": ["制造业运行相关的最新市场新闻", "不应包含的未来日期新闻"],
                             "发布时间": ["2026-10-23T09:30:00+08:00", "2026-10-27T09:30:00+08:00"]})


class LocalReportTests(unittest.TestCase):
    def at(self, value):
        return datetime.fromisoformat(value + "+08:00")

    def test_market_cutoff_uses_last_completed_calendar_day_boundary(self):
        self.assertEqual(local_report.market_date_ceiling(self.at("2026-10-26T19:34:59")).isoformat(),
                         "2026-10-25")
        self.assertEqual(local_report.market_date_ceiling(self.at("2026-10-26T19:35:00")).isoformat(),
                         "2026-10-26")
        self.assertEqual(local_report.market_date_ceiling(self.at("2026-10-25T12:00:00")).isoformat(),
                         "2026-10-24")

    def test_real_calendar_and_prices_align_to_last_completed_session(self):
        generated = self.at("2026-10-26T19:30:00")
        ceiling = local_report.market_date_ceiling(generated)
        result = public_market.collect(today=ceiling, fetcher=MarketFetcher(), generated_at=generated)
        self.assertEqual(result["asof"], ASOF)
        self.assertEqual(result["date"], "2026-10-26")
        self.assertEqual(result["assets"]["沪深300"]["date"], ASOF)
        self.assertEqual(len(result["sectors"]), 20)
        self.assertIn("published_at", result["macro"]["CPI"])

    def test_holiday_uses_calendar_instead_of_weekday_guess(self):
        class HolidayCalendar(MarketFetcher):
            def trade_calendar(self):
                return super().trade_calendar().query("trade_date <= '2026-10-22'")

        generated = self.at("2026-10-26T19:30:00")
        result = public_market.collect(today=local_report.market_date_ceiling(generated),
                                       fetcher=HolidayCalendar(), generated_at=generated)
        self.assertEqual(result["asof"], "2026-10-22")

    def test_future_auxiliary_data_is_excluded_and_future_news_is_filtered(self):
        generated = self.at("2026-10-26T19:30:00")
        result = public_market.collect(today=local_report.market_date_ceiling(generated),
                                       fetcher=MarketFetcher(include_future_aux=True), generated_at=generated)
        self.assertEqual(result["bond_y10"]["date"], ASOF)
        self.assertEqual(result["bond_y10"]["yield_pct"], 2.1)
        self.assertEqual(result["cash_shibor"]["date"], ASOF)
        self.assertEqual(result["cash_shibor"]["rate_pct"], 1.8)
        self.assertEqual([item["title"] for item in result["news"]], ["制造业运行相关的最新市场新闻"])

    def test_macro_without_verifiable_release_time_is_reported_as_gap(self):
        class Fetcher(MarketFetcher):
            def cpi(self):
                return 0.9, "2026-09"

        generated = self.at("2026-10-26T20:00:00")
        result = public_market.collect(today=generated.date(), fetcher=Fetcher(), generated_at=generated)
        self.assertNotIn("CPI", result.get("macro", {}))
        self.assertTrue(any("CPI" in item and "发布日期" in item for item in result["quality"]))

    def test_calendar_and_required_coverage_fail_closed(self):
        class NoCalendar(MarketFetcher):
            def trade_calendar(self):
                raise TimeoutError("calendar timeout")

        generated = self.at("2026-10-26T20:00:00")
        with self.assertRaisesRegex(ValueError, "trading calendar unavailable"):
            public_market.collect(today=generated.date(), fetcher=NoCalendar(), generated_at=generated)

        class ShortHistory(MarketFetcher):
            def index_daily(self, symbol, days=180):
                return super().index_daily(symbol, days).tail(60)

        with self.assertRaisesRegex(ValueError, "沪深300基准行情缺失"):
            public_market.collect(today=generated.date(), fetcher=ShortHistory(), generated_at=generated)

        class TooFewSectors(MarketFetcher):
            def industry_universe(self):
                return super().industry_universe().head(19)

        with self.assertRaisesRegex(ValueError, "申万一级行业行情覆盖不足"):
            public_market.collect(today=generated.date(), fetcher=TooFewSectors(), generated_at=generated)

    def test_first_run_writes_all_artifacts_offline_and_includes_generation_metadata(self):
        with self.subTest("fresh directory"), patch("webbrowser.open") as browser:
            with tempfile_directory() as reports:
                result = local_report.generate(reports, self.at("2026-10-26T18:30:00"),
                                               collector=fake_collector)
                self.assertTrue(result["html"].is_file())
                self.assertTrue(result["markdown"].is_file())
                self.assertTrue(result["snapshot"].is_file())
                self.assertTrue(result["latest"].is_file())
                data = json.loads(result["snapshot"].read_text(encoding="utf-8"))
                self.assertEqual(data["generated_at"], "2026-10-26T18:30:00+08:00")
                self.assertEqual(data["asof"], "2026-10-23")
                page = result["html"].read_text(encoding="utf-8")
                self.assertIn("2026-W43", page)
                self.assertIn("不代表整周收盘", page)
                self.assertNotRegex(page, r"https?://")
                self.assertIn("# 市场简报", result["markdown"].read_text(encoding="utf-8"))
                browser.assert_not_called()

    def test_long_idle_and_repeated_runs_need_no_previous_state_and_never_overwrite(self):
        with tempfile_directory() as tmp:
            reports = tmp / "reports"
            legacy = tmp / "public_data" / "state.json"
            legacy.parent.mkdir()
            legacy.write_text('{"pending":{"old":"unknown"}}', encoding="utf-8")
            before = legacy.read_bytes()
            first = local_report.generate(reports, self.at("2026-10-26T20:00:00"), collector=fake_collector)
            first_html = first["html"].read_bytes()
            idle = local_report.generate(reports, self.at("2026-11-16T20:00:00"), collector=fake_collector)
            repeated = local_report.generate(reports, self.at("2026-11-16T20:00:00"), collector=fake_collector)
            self.assertNotEqual(first["folder"], idle["folder"])
            self.assertNotEqual(idle["folder"], repeated["folder"])
            self.assertEqual(first["html"].read_bytes(), first_html)
            self.assertEqual(legacy.read_bytes(), before)
            self.assertEqual(len(list(reports.glob("20*"))), 3)

    def test_optional_gaps_appear_in_report_without_blocking_generation(self):
        with tempfile_directory() as reports:
            result = local_report.generate(reports, self.at("2026-10-26T20:00:00"),
                                           collector=optional_missing_collector)
            markdown = result["markdown"].read_text(encoding="utf-8")
            self.assertIn("十年国债收益率不可用", markdown)
            self.assertIn("现金参考利率暂缺", markdown)

    def test_key_data_failure_and_timeout_preserve_previous_latest(self):
        with tempfile_directory() as reports:
            first = local_report.generate(reports, self.at("2026-10-26T20:00:00"), collector=fake_collector)
            latest_before = first["latest"].read_bytes()
            with self.assertRaisesRegex(RuntimeError, "沪深300基准行情缺失"):
                local_report.generate(reports, self.at("2026-10-26T20:01:00"),
                                      collector=missing_baseline_collector)
            started = time.monotonic()
            with self.assertRaisesRegex(TimeoutError, "worker stopped"):
                local_report.generate(reports, self.at("2026-10-26T20:02:00"), timeout_seconds=2,
                                      collector=slow_collector)
            self.assertLess(time.monotonic() - started, 7)
            self.assertEqual(first["latest"].read_bytes(), latest_before)
            self.assertFalse(list(reports.glob(".report-staging-*")))

    def test_output_lock_rejects_a_second_run_without_touching_latest(self):
        with tempfile_directory() as reports:
            first = local_report.generate(reports, self.at("2026-10-26T20:00:00"), collector=fake_collector)
            before = first["latest"].read_bytes()
            with local_report.output_lock(reports):
                with self.assertRaisesRegex(RuntimeError, "already using"):
                    local_report.generate(reports, self.at("2026-10-26T20:01:00"), collector=fake_collector)
            self.assertEqual(first["latest"].read_bytes(), before)

    def test_retired_secret_environment_does_not_change_local_report(self):
        with tempfile_directory() as reports:
            secret_names = ("ADVISOR_CONFIG", "ADVISOR_DELIVERY_JSON", "SC_KEY",
                            "PUBLIC_HEARTBEAT_URL", "DEEPSEEK_API_KEY", "GITHUB_TOKEN")
            with patch.dict(os.environ, {name: "retired-value" for name in secret_names}):
                with patch("webbrowser.open") as browser:
                    result = local_report.generate(reports, self.at("2026-10-26T18:30:00"),
                                                   collector=fake_collector)
            self.assertEqual(result["snapshot"].is_file(), True)
            self.assertNotIn("retired-value", result["snapshot"].read_text(encoding="utf-8"))
            browser.assert_not_called()


class _TempDirectory:
    def __enter__(self):
        import tempfile
        self._temporary = tempfile.TemporaryDirectory()
        return Path(self._temporary.name)

    def __exit__(self, *_exc):
        self._temporary.cleanup()


def tempfile_directory():
    return _TempDirectory()


if __name__ == "__main__":
    unittest.main()
