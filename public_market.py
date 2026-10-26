"""Public market observations and deterministic research states (no client inputs)."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

TZ = ZoneInfo("Asia/Shanghai")
ASSETS = {
    "沪深300": "sh000300",
    "中证500": "sh000905",
    "上证红利": "sh000015",
    "创业板指": "sz399006",
    "中证全债": "sh000923",
}
BENCHMARK = "沪深300"


def _trade_dates(fetcher, ak=None):
    calendar = (fetcher.trade_calendar() if hasattr(fetcher, "trade_calendar")
                else fetcher._ak(ak.tool_trade_date_hist_sina, timeout=20))
    if calendar is None or "trade_date" not in calendar:
        raise ValueError("missing trade calendar")
    return pd.to_datetime(calendar["trade_date"], errors="coerce").dt.date.dropna()


def expected_session(today, fetcher, ak=None, calendar_dates=None):
    """Return the latest verified exchange session; never infer holidays."""
    try:
        dates = calendar_dates if calendar_dates is not None else _trade_dates(fetcher, ak)
        available = dates[(dates <= today) & (dates >= today - timedelta(days=20))]
        if not available.empty:
            return max(available).isoformat()
    except Exception as exc:
        raise ValueError("verified trading calendar unavailable") from exc
    raise ValueError("verified trading calendar has no recent session")


def session_age(observed, today):
    """Conservative weekday distance; holidays may be counted as extra days."""
    start = date.fromisoformat(str(observed)[:10])
    end = date.fromisoformat(str(today)[:10])
    if start > end:
        raise ValueError("future observation")
    return sum((start + timedelta(days=i)).weekday() < 5
               for i in range(1, (end - start).days + 1))


def _calendar_age(observed, today, calendar_dates):
    start = date.fromisoformat(str(observed)[:10])
    end = date.fromisoformat(str(today)[:10])
    if start > end:
        raise ValueError("future observation")
    return sum(start < stamp <= end for stamp in calendar_dates)


def series_metrics(frame, today, date_key="date", price_key="close", calendar_dates=None):
    if frame is None or date_key not in frame or price_key not in frame:
        raise ValueError("missing price columns")
    d = frame[[date_key, price_key]].copy()
    d[date_key] = pd.to_datetime(d[date_key], errors="coerce")
    d[price_key] = pd.to_numeric(d[price_key], errors="coerce")
    d = d.dropna().sort_values(date_key).drop_duplicates(date_key, keep="last")
    d = d[(d[date_key].dt.date <= today) & (d[price_key] > 0)]
    if len(d) < 61:
        raise ValueError("fewer than 61 valid sessions")
    prices = d[price_key]
    observed = d[date_key].iloc[-1].date().isoformat()
    if calendar_dates is None:
        age = session_age(observed, today)
    else:
        age = _calendar_age(observed, today, calendar_dates)
    if age > 2:
        raise ValueError("stale price history: " + observed)
    last = float(prices.iloc[-1])
    returns = {str(n): round((last / float(prices.iloc[-1-n]) - 1) * 100, 2)
               for n in (5, 20, 60)}
    dd = float((prices.tail(61) / prices.tail(61).cummax() - 1).min() * 100)
    result = {"date": observed, "close": round(last, 4), "returns": returns,
              "drawdown60": round(dd, 2)}
    if "成交额" in frame:
        amount = frame[[date_key, "成交额"]].copy()
        amount[date_key] = pd.to_datetime(amount[date_key], errors="coerce")
        amount["成交额"] = pd.to_numeric(amount["成交额"], errors="coerce")
        amount = amount.dropna().sort_values(date_key).drop_duplicates(date_key, keep="last")
        amount = amount[amount[date_key].dt.date <= today]
        if (len(amount) >= 25 and amount[date_key].iloc[-1].date().isoformat() == observed
                and amount["成交额"].tail(25).min() > 0):
            result["participation"] = round(
                float(amount["成交额"].tail(5).mean() / amount["成交额"].iloc[-25:-5].mean()), 2)
    return result


def _valid_number(value):
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def collect(today=None, fetcher=None, ak=None, generated_at=None):
    """Collect a snapshot from a shared classification; no holdings or model calls."""
    generated_at = generated_at or datetime.now(TZ)
    if not isinstance(generated_at, datetime) or generated_at.tzinfo is None:
        raise ValueError("timezone-aware generation timestamp required")
    generated_at = generated_at.astimezone(TZ)
    today = today or generated_at.date()
    if fetcher is None:
        from public_sources import DataFetcher
        fetcher = DataFetcher()
    try:
        calendar_dates = _trade_dates(fetcher, ak)
    except Exception as exc:
        raise ValueError("verified trading calendar unavailable") from exc
    expected = expected_session(today, fetcher, ak, calendar_dates)
    market_day = date.fromisoformat(expected)
    snapshot = {"schema": 1, "date": generated_at.date().isoformat(),
                "generated_at": generated_at.isoformat(), "assets": {}, "sectors": {},
                "quality": [], "asof": None}
    for name, symbol in ASSETS.items():
        try:
            snapshot["assets"][name] = series_metrics(fetcher.index_daily(symbol, days=180), market_day,
                                                       calendar_dates=calendar_dates)
        except Exception as exc:
            snapshot["quality"].append(f"{name}行情不可用：{type(exc).__name__}")
    if BENCHMARK not in snapshot["assets"]:
        raise ValueError("沪深300基准行情缺失，不能生成行业判断")
    if len(snapshot["assets"]) < 2:
        raise ValueError("宽基指数覆盖不足，不能发布完整市场简报")
    snapshot["asof"] = snapshot["assets"][BENCHMARK]["date"]
    if snapshot["asof"] != expected:
        raise ValueError(f"行情截至{snapshot['asof']}，应为最近交易日{expected}；不发布旧数据")
    for name, item in list(snapshot["assets"].items()):
        if item["date"] != snapshot["asof"]:
            del snapshot["assets"][name]
            snapshot["quality"].append(f"{name}与沪深300行情日期不一致，已排除")
    if len(snapshot["assets"]) < 3:
        raise ValueError("对齐后的宽基/债券指数覆盖不足")

    # Index membership is discovered from the same SW level-1 classification.
    try:
        universe = (fetcher.industry_universe() if hasattr(fetcher, "industry_universe")
                    else fetcher._ak(ak.index_realtime_sw, symbol="一级行业", timeout=35))
        if universe is None or not {"指数代码", "指数名称"}.issubset(universe.columns):
            raise ValueError("missing SW industry universe")
        universe = universe[["指数代码", "指数名称"]].dropna().drop_duplicates("指数代码")
        if len(universe) < 20 or len(universe) > 45:
            raise ValueError("unexpected SW level-1 universe size")
    except Exception as exc:
        snapshot["quality"].append(f"申万行业目录不可用：{type(exc).__name__}")
        universe = pd.DataFrame(columns=["指数代码", "指数名称"])

    # An unavailable sector never inherits another index's valuation or price.
    for row in universe.itertuples(index=False, name=None):
        code, name = str(row[0]), str(row[1])
        try:
            hist = (fetcher.industry_history(code) if hasattr(fetcher, "industry_history")
                    else fetcher._ak(ak.index_hist_sw, symbol=code.split(".")[0], period="day", timeout=22))
            item = series_metrics(hist, market_day, "日期", "收盘", calendar_dates)
            if item["date"] != snapshot["asof"]:
                raise ValueError("sector and benchmark observation dates differ")
            item["code"] = code
            snapshot["sectors"][name] = item
        except Exception:
            snapshot["quality"].append(f"{name}行业行情不可用")
    if len(snapshot["sectors"]) < 20:
        raise ValueError("申万一级行业行情覆盖不足，不能发布行业简报")

    try:
        # Daily analysis shares the SW classification and supplies PE/PB for that date.
        day = snapshot["asof"].replace("-", "")
        table = (fetcher.industry_analysis(day) if hasattr(fetcher, "industry_analysis")
                 else fetcher._ak(ak.index_analysis_daily_sw, symbol="一级行业",
                                  start_date=day, end_date=day, timeout=30))
        for _, row in table.iterrows():
            name = str(row.get("指数名称", ""))
            item = snapshot["sectors"].get(name)
            if item is None or str(row.get("发布日期", ""))[:10] != snapshot["asof"]:
                continue
            for key, column in (("pe", "市盈率"), ("pb", "市净率")):
                val = _valid_number(row.get(column))
                if val is not None and val > 0:
                    item[key] = round(val, 2)
    except Exception:
        snapshot["quality"].append("申万行业估值不可用；不依据估值发出机会判断")

    try:
        bonds = fetcher.bond_rates()
        if bonds is not None and not bonds.empty:
            eligible = bonds.copy()
            eligible["date"] = pd.to_datetime(eligible["date"], errors="coerce")
            eligible = eligible.dropna(subset=["date"])
            eligible = eligible[eligible["date"].dt.date <= market_day]
            if eligible.empty:
                raise ValueError("no bond yield on or before market date")
            b = eligible.sort_values("date").iloc[-1]
            bd = pd.to_datetime(b["date"]).date().isoformat()
            val = _valid_number(b["y10"])
            if val is not None and _calendar_age(bd, market_day, calendar_dates) <= 2:
                snapshot["bond_y10"] = {"date": bd, "yield_pct": round(val, 3)}
            else:
                snapshot["quality"].append("十年国债收益率日期滞后")
        else:
            snapshot["quality"].append("十年国债收益率不可用")
    except Exception:
        snapshot["quality"].append("十年国债收益率不可用")
    try:
        cash = (fetcher.shibor() if hasattr(fetcher, "shibor")
                else fetcher._ak(ak.rate_interbank, indicator="3月", timeout=25))
        cash = cash.copy()
        cash["_date"] = pd.to_datetime(cash["报告日"], errors="coerce")
        cash["利率"] = pd.to_numeric(cash["利率"], errors="coerce")
        cash = cash.dropna(subset=["_date", "利率"])
        cash = cash[cash["_date"].dt.date <= market_day].sort_values("_date")
        if cash.empty:
            raise ValueError("no Shibor on or before market date")
        row = cash.iloc[-1]
        cd = row["_date"].date().isoformat()
        rate = _valid_number(row["利率"])
        if rate is not None and _calendar_age(cd, market_day, calendar_dates) <= 2:
            snapshot["cash_shibor"] = {"date": cd, "rate_pct": round(rate, 3)}
        else:
            snapshot["quality"].append("3月期Shibor日期滞后")
    except Exception:
        snapshot["quality"].append("3月期Shibor不可用；现金参考利率暂缺")
    try:
        gold = (fetcher.gold_daily() if hasattr(fetcher, "gold_daily")
                else fetcher._ak(ak.spot_hist_sge, symbol="Au99.99", timeout=25))
        snapshot["gold"] = series_metrics(gold, market_day, calendar_dates=calendar_dates)
        snapshot["gold"]["proxy"] = "Au99.99现货"
    except Exception:
        snapshot["quality"].append("Au99.99现货不可用；不以期货冒充")
    for name, method in (("CPI", "cpi"), ("PPI", "ppi")):
        try:
            result = getattr(fetcher, method)()
            if result and result[1]:
                period = str(result[1])
                release = pd.Timestamp(result[2]) if len(result) > 2 and result[2] else pd.NaT
                if pd.notna(release):
                    release = (release.tz_localize(TZ) if release.tzinfo is None
                               else release.tz_convert(TZ))
                year, month = map(int, period.split("-"))
                age = (market_day.year - year) * 12 + market_day.month - month
                release_text = str(result[2]) if len(result) > 2 and result[2] else ""
                has_clock = bool(re.search(r"\d{1,2}:\d{2}", release_text))
                release_known_by_run = (pd.notna(release) and release <= generated_at
                                        and (release.date() < generated_at.date() or has_clock))
                if (0 <= age <= 3 and math.isfinite(float(result[0])) and release_known_by_run):
                    snapshot.setdefault("macro", {})[name] = {"period": period,
                        "yoy_pct": round(float(result[0]), 2), "published_at": release.isoformat()}
                else:
                    snapshot["quality"].append(f"{name}统计期或发布日期不可验证，已排除")
            else:
                snapshot["quality"].append(f"{name}统计期不可用")
        except Exception:
            snapshot["quality"].append(f"{name}统计期不可用")
    try:
        stories = fetcher.cls_news()
        if stories is None:
            snapshot["quality"].append("新闻来源未返回数据；不推断涨跌原因")
        elif stories.empty:
            snapshot["news"] = []
            snapshot["quality"].append("新闻来源本次无新闻")
        elif not {"标题", "发布时间"}.issubset(stories.columns):
            snapshot["quality"].append("新闻发布时间或标题字段缺失，已排除")
        else:
            candidates = []
            invalid_times = 0
            for _, story in stories.iterrows():
                value = story["发布时间"]
                # A clock without a source date must never acquire today's date.
                if not isinstance(value, (datetime, pd.Timestamp, str)):
                    invalid_times += 1
                    continue
                if isinstance(value, str) and not value[:4].isdigit():
                    invalid_times += 1
                    continue
                try:
                    published = pd.Timestamp(value)
                    if pd.isna(published):
                        raise ValueError("missing publication time")
                    published = (published.tz_localize(TZ) if published.tzinfo is None
                                 else published.tz_convert(TZ))
                except (ValueError, TypeError):
                    invalid_times += 1
                    continue
                if not (market_day - timedelta(days=7) <= published.date() <= generated_at.date()
                        and published <= generated_at):
                    continue
                title = str(story["标题"]).strip().replace("\n", " ")
                if len(title) < 8 or len(title) > 100:
                    continue
                candidates.append({"published": published.isoformat(), "title": title, "source": "财联社"})
            snapshot["news"] = sorted(candidates, key=lambda item: item["published"], reverse=True)[:3]
            if invalid_times:
                snapshot["quality"].append(f"新闻发布时间解析失败{invalid_times}/{len(stories)}条，已排除")
    except ValueError:
        snapshot["quality"].append("新闻来源日期/时间字段缺失或格式无效，已排除")
    except Exception:
        snapshot["quality"].append("新闻接口不可用；不推断涨跌原因")
    return snapshot


def research(snapshot):
    benchmark = snapshot["assets"][BENCHMARK]
    sectors = {}
    for name, item in snapshot["sectors"].items():
        rel = round(item["returns"]["20"] - benchmark["returns"]["20"], 2)
        # Heuristic research gate, not a backtested trading signal.
        participation = item.get("participation")
        evidence = ["近20日相对沪深300领先" if rel >= 2 else "近20日未明显跑赢沪深300",
                    "近5日平均成交额高于此前20日" if participation and participation >= 1.15
                    else "成交参与未确认"]
        if item["returns"]["5"] > 12 or item["drawdown60"] < -15:
            state = "暂缓追涨"
        elif ((item.get("pe") is None and item.get("pb") is None)
              or participation is None):
            state = "资料不足"
        elif (rel >= 2 and item["returns"]["60"] > 0 and
              participation is not None and participation >= 1.15 and item["returns"]["5"] <= 8):
            state = "优先研究"
        else:
            state = "等待确认"
        sectors[name] = {"state": state, "relative20_pp": rel, "evidence": evidence,
                         "risk": "指数近60日最大回撤 %.1f%%" % item["drawdown60"],
                         "invalidate": "相对沪深300近20日优势收窄到2个百分点以下",
                         "data": item}
    asset_views = {}
    for name, item in snapshot["assets"].items():
        if name == "中证全债":
            bond = snapshot.get("bond_y10")
            label = "价格走强，可研究久期风险" if item["returns"]["20"] > 0 else "价格偏弱，留意利率变化"
            reason = f"中证全债指数近20日{item['returns']['20']:+.1f}%"
            if bond:
                reason += f"；十年国债收益率{bond['yield_pct']:.2f}%（{bond['date']}）"
            risk = "利率上升可能压低债券价格；指数价格不代表基金收益"
        else:
            label = ("保持观察，勿仅凭短期上涨追入" if item["returns"]["5"] > 8
                     else "趋势待确认" if item["returns"]["20"] <= 0
                     else "可研究，仍需核对估值和回撤")
            reason = f"近5日{item['returns']['5']:+.1f}%、近20日{item['returns']['20']:+.1f}%"
            risk = f"近60个交易日历史最大回撤{item['drawdown60']:.1f}%"
        asset_views[name] = {"state": label, "reason": reason, "risk": risk, "asof": item["date"]}
    gold = snapshot.get("gold")
    if gold:
        asset_views["黄金"] = {
            "state": "偏强，警惕追高" if gold["returns"]["5"] > 6 else
                     "价格走强，仍需观察波动" if gold["returns"]["20"] > 0 else "趋势未确认",
            "reason": f"Au99.99现货近5日{gold['returns']['5']:+.1f}%、近20日{gold['returns']['20']:+.1f}%",
            "risk": "现货价格不含买卖价差，也不是黄金基金收益", "asof": gold["date"]}
    cash = snapshot.get("cash_shibor")
    if cash:
        asset_views["现金类参考"] = {"state": "流动性观察", "reason":
                    f"3个月Shibor {cash['rate_pct']:.2f}%",
                    "risk": "同业拆借利率不等于存款或货币基金收益", "asof": cash["date"]}
    return {"date": snapshot["date"], "asof": snapshot["asof"],
            "sectors": sectors, "assets": snapshot["assets"],
            "asset_views": asset_views,
            "bond_y10": snapshot.get("bond_y10"), "gold": snapshot.get("gold"),
            "cash_shibor": snapshot.get("cash_shibor"),
            "macro": snapshot.get("macro", {}),
            "news": snapshot.get("news", []),
            "quality": snapshot["quality"]}
