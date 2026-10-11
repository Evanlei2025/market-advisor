"""Data sources used by the public market brief.

This module deliberately contains no client configuration or portfolio logic.
AkShare calls run through a bounded daemon thread because some AkShare
endpoints do not expose a request timeout of their own.
"""
from __future__ import annotations

import re

import pandas as pd
import requests

import aktime


def _pick_column(frame, *candidates):
    if frame is None:
        return None
    return next((name for name in candidates if name in frame.columns), None)


class DataFetcher:
    """Fetch only the market data required by the public service."""

    def __init__(self, ak=None, session=None):
        if ak is None:
            import akshare as ak
        self.ak = ak
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "market-advisor-public/1.0"})

    def _ak(self, fn, *args, timeout=90, **kwargs):
        return aktime.call_with_timeout(fn, timeout, *args, **kwargs)

    def trade_calendar(self):
        return self._ak(self.ak.tool_trade_date_hist_sina, timeout=20)

    def index_daily(self, tx_symbol, days=320):
        try:
            response = self.session.get(
                "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                params={"param": f"{tx_symbol},day,,,{days},qfq"}, timeout=20)
            response.raise_for_status()
            payload = response.json()
            data = payload["data"][tx_symbol]
            key = "qfqday" if "qfqday" in data else "day"
            frame = pd.DataFrame(data[key]).iloc[:, :6]
            frame.columns = ["date", "open", "close", "high", "low", "volume"]
            for column in frame.columns[1:]:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
            frame["pct"] = frame["close"].pct_change() * 100
            return frame.reset_index(drop=True)
        except Exception:
            raw = self._ak(self.ak.stock_zh_index_daily, symbol=tx_symbol, timeout=35)
            if raw is None or raw.empty:
                return None
            frame = raw.tail(days).reset_index(drop=True)
            columns = ["date", "open", "close", "high", "low", "volume"]
            frame = frame[columns].copy()
            for column in columns[1:]:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
            frame["pct"] = frame["close"].pct_change() * 100
            return frame

    def industry_universe(self):
        return self._ak(self.ak.index_realtime_sw, symbol="一级行业", timeout=35)

    def industry_history(self, code):
        return self._ak(self.ak.index_hist_sw, symbol=code.split(".")[0], period="day", timeout=22)

    def industry_analysis(self, day):
        return self._ak(self.ak.index_analysis_daily_sw, symbol="一级行业",
                        start_date=day, end_date=day, timeout=30)

    def bond_rates(self):
        frame = self._ak(self.ak.bond_zh_us_rate, timeout=35)
        date_col = _pick_column(frame, "日期")
        y2_col = _pick_column(frame, "中国国债收益率2年")
        y10_col = _pick_column(frame, "中国国债收益率10年")
        if not all((date_col, y2_col, y10_col)):
            return None
        frame = frame[[date_col, y2_col, y10_col]].dropna().copy()
        frame.columns = ["date", "y2", "y10"]
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        return frame.dropna(subset=["date"])

    def shibor(self):
        frame = self._ak(self.ak.rate_interbank, indicator="3月", timeout=25)
        if frame is None or not {"报告日", "利率"}.issubset(frame.columns):
            return None
        return frame.sort_values("报告日").dropna(subset=["报告日", "利率"])

    def gold_daily(self, days=320):
        frame = self._ak(self.ak.spot_hist_sge, symbol="Au99.99", timeout=25)
        if frame is None or frame.empty:
            return None
        if not {"date", "close"}.issubset(frame.columns):
            return None
        return frame[["date", "close"]].tail(days).reset_index(drop=True)

    def _macro_yoy(self, function_name):
        try:
            frame = self._ak(getattr(self.ak, function_name), timeout=30)
            date_col = _pick_column(frame, "月份", "月")
            value_col = next((column for column in frame.columns if "同比" in str(column)), None)
            if not date_col or not value_col:
                return None
            values = frame[[date_col, value_col]].copy().dropna()
            parts = values[date_col].astype(str).str.extract(r"(\d{4})[年\-/]?(\d{1,2})")
            values["_period"] = (pd.to_numeric(parts[0], errors="coerce") * 100
                                  + pd.to_numeric(parts[1], errors="coerce"))
            values = values.dropna(subset=["_period"]).sort_values("_period")
            if values.empty:
                return None
            value = float(values[value_col].iloc[-1])
            if not pd.notna(value) or abs(value) > 15:
                return None
            match = re.search(r"(\d{4})[年\-/]?(\d{1,2})", str(values[date_col].iloc[-1]))
            period = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}" if match else ""
            return value, period
        except Exception:
            return None

    def cpi(self):
        return self._macro_yoy("macro_china_cpi")

    def ppi(self):
        return self._macro_yoy("macro_china_ppi")

    def cls_news(self):
        frame = self._ak(self.ak.stock_info_global_cls, timeout=30)
        if frame is None:
            return None
        if frame.empty:
            return pd.DataFrame(columns=["标题", "内容", "发布时间"])
        if not {"标题", "发布日期", "发布时间"}.issubset(frame.columns):
            raise ValueError("CLS news publication columns missing")
        news = frame.copy()
        timestamps = []
        for day, clock in zip(news["发布日期"], news["发布时间"]):
            try:
                # AkShare splits its Shanghai timestamp into date and time objects.
                stamp = pd.Timestamp(str(day) + "T" + str(clock))
                stamp = (stamp.tz_localize("Asia/Shanghai") if stamp.tzinfo is None
                         else stamp.tz_convert("Asia/Shanghai"))
            except (ValueError, TypeError):
                stamp = pd.NaT
            timestamps.append(stamp)
        news["发布时间"] = timestamps
        return news
