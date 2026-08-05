# -*- coding: utf-8 -*-
"""每日投顾报告生成器
数据采集 -> 规则决策 -> 本地报告 + 微信推送
"""
import argparse
import json
import os
import sys
import time
import traceback
from datetime import date, datetime

import pandas as pd
import requests

import llm

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
REPORT_DIR = os.path.join(BASE_DIR, "reports")
LOG_PATH = os.path.join(BASE_DIR, "logs", "run.log")

PE_HIST_YEARS = 10
FETCH_RETRY = 3
FETCH_RETRY_WAIT = 2.0
PUSH_RETRY = 3


def log(msg):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def load_config():
    """配置来源优先级：环境变量 ADVISOR_CONFIG_JSON > 本地 config.json"""
    env_cfg = os.environ.get("ADVISOR_CONFIG_JSON")
    if env_cfg:
        try:
            return json.loads(env_cfg)
        except Exception as e:
            log(f"[WARN] ADVISOR_CONFIG_JSON 解析失败({e})，回退本地文件")
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    log("[WARN] 未找到配置（环境变量或 config.json），使用默认目标仓位")
    return {
        "push": {"channel": "none", "wecom_webhook": "", "serverchan_key": ""},
        "target": {"equity": 0.45, "bond": 0.45, "gold": 0.1},
        "rebalance_band": 0.05,
        "holdings": [],
    }


class DataFetcher:
    """行情用腾讯源(稳定)，估值用乐咕，债券/黄金用官方源，失败不影响整体"""

    def __init__(self):
        import akshare as ak
        self.ak = ak
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

    def index_daily(self, tx_symbol, days=320):
        r = self.session.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{tx_symbol},day,,,{days},qfq"}, timeout=20)
        j = r.json()
        d = j["data"][tx_symbol]
        key = "qfqday" if "qfqday" in d else "day"
        rows = d[key]
        df = pd.DataFrame(rows).iloc[:, :6]
        df.columns = ["date", "open", "close", "high", "low", "volume"]
        for c in df.columns[1:]:
            df[c] = pd.to_numeric(df[c])
        df["pct"] = df["close"].pct_change() * 100
        return df.reset_index(drop=True)

    def realtime_quotes(self, tx_codes):
        url = "https://qt.gtimg.cn/q=" + ",".join(tx_codes)
        r = self.session.get(url, timeout=20)
        r.encoding = "gbk"
        out = {}
        for line in r.text.strip().split(";"):
            if "=" not in line:
                continue
            f = line.split("=")[1].strip('"').split("~")
            if len(f) > 32:
                out[f[2]] = {"name": f[1], "close": float(f[3]), "pct": float(f[32])}
        return out

    def pe_history(self, symbol):
        df = self.ak.stock_index_pe_lg(symbol=symbol)
        df = df[["日期", "滚动市盈率"]]
        df.columns = ["date", "pe"]
        df["date"] = pd.to_datetime(df["date"])
        cutoff = pd.Timestamp(f"{datetime.now().year - PE_HIST_YEARS}-01-01")
        return df[df["date"] >= cutoff].dropna()

    def bond_rates(self):
        df = self.ak.bond_zh_us_rate()
        df = df[["日期", "中国国债收益率2年", "中国国债收益率10年"]].dropna()
        df.columns = ["date", "y2", "y10"]
        df["date"] = pd.to_datetime(df["date"])
        cutoff = pd.Timestamp(f"{datetime.now().year - PE_HIST_YEARS}-01-01")
        return df[df["date"] >= cutoff]

    def gold_daily(self, days=320):
        df = self.ak.spot_hist_sge(symbol="Au99.99")
        df = df[["date", "close"]]
        return df.tail(days).reset_index(drop=True)

    def cls_news(self):
        df = self.ak.stock_info_global_cls()
        return df[["标题", "内容", "发布时间"]].dropna()

    def lpr(self):
        df = self.ak.macro_china_lpr()
        return df.iloc[-1]

    def usd_cny(self):
        df = self.ak.fx_spot_quote()
        row = df[df["货币对"] == "USD/CNY"].iloc[0]
        return float(row["买报价"])

    def fund_daily(self, fund_code):
        df = self.ak.fund_open_fund_info_em(symbol=fund_code, indicator="单位净值走势")
        df = df[["净值日期", "单位净值", "日增长率"]].tail(3)
        return df.reset_index(drop=True)

    def fund_nav_history(self, fund_code):
        df = self.ak.fund_open_fund_info_em(symbol=fund_code, indicator="单位净值走势")
        df = df[["净值日期", "单位净值", "日增长率"]]
        df.columns = ["date", "nav", "growth"]
        return df.dropna().reset_index(drop=True)

    def fund_profile(self, fund_code):
        df = self.ak.fund_individual_basic_info_xq(symbol=fund_code)
        return dict(zip(df["item"], df["value"]))

    def fund_fees(self, fund_code):
        df = self.ak.fund_individual_detail_info_xq(symbol=fund_code)
        return df.to_dict("records")

    def fund_achievement(self, fund_code):
        df = self.ak.fund_individual_achievement_xq(symbol=fund_code)
        return df.to_dict("records")

    def market_pe(self):
        df = self.ak.stock_market_pe_lg(symbol="上证")
        return df

    def us_index(self):
        df = self.ak.index_us_stock_sina(symbol=".INX").tail(2)
        df = df[["date", "close"]].reset_index(drop=True)
        df["pct"] = df["close"].pct_change() * 100
        return df

    def margin_sse(self):
        start = (datetime.now() - pd.Timedelta(days=10)).strftime("%Y%m%d")
        df = self.ak.stock_margin_sse(start_date=start, end_date=datetime.now().strftime("%Y%m%d"))
        return df.head(3)


def pct_rank(series, value):
    """value 在 series 历史中的分位（0~1）"""
    s = series.dropna().astype(float)
    if s.empty:
        return None
    return float((s <= value).mean())


def stance_label(score):
    if score >= 1.0:
        return "增配"
    if score >= 0.3:
        return "略增"
    if score > -0.3:
        return "持有"
    if score > -1.0:
        return "略减"
    return "减配"


def sma(series, n):
    return series.rolling(n).mean()


def fetch_section(fn, name):
    for attempt in range(1, FETCH_RETRY + 1):
        try:
            return fn()
        except Exception as e:
            if attempt < FETCH_RETRY:
                log(f"[WARN] {name} 第{attempt}次失败({type(e).__name__})，重试中...")
                time.sleep(FETCH_RETRY_WAIT)
            else:
                log(f"[WARN] {name} 获取失败: {type(e).__name__}: {str(e)[:100]}")
    return None


def _fee_num(v):
    try:
        return float(str(v).replace("%", "").strip())
    except Exception:
        return None


def analyze_product(fetcher, p):
    """单个理财产品分析：返回 (report_lines, ctx_dict)。失败不阻塞。"""
    code = p.get("code", "?")
    name = p.get("name") or ""
    ptype = p.get("type", "other")
    fcode = p.get("fund_code", "")
    ctx = {"code": code, "name": name, "type": ptype,
           "platform": p.get("platform", ""), "notes": p.get("notes", "")}
    if not fcode:
        return ([f"- {code} {name}: 银行理财/券商产品，自动数据不可用（需人工维护）"],
                {**ctx, "unavailable": True})

    nav = fetch_section(lambda: fetcher.fund_nav_history(fcode), f"净值{fcode}")
    achievement = fetch_section(lambda: fetcher.fund_achievement(fcode), f"业绩{fcode}") or []
    profile = fetch_section(lambda: fetcher.fund_profile(fcode), f"资料{fcode}") or {}

    # 货币基金：净值走势接口不支持，用业绩表兜底
    if nav is None or nav.empty or len(nav) < 10:
        if ptype == "money" and achievement:
            prof_map = {str(k).strip(): str(v).strip() for k, v in profile.items()}
            fname = name or prof_map.get("基金名称") or f"货币基金"
            ytd = next((r.get("本产品区间收益", "") for r in achievement if str(r.get("周期", "")) == "今年以来"), "")
            rank = next((r.get("周期收益同类排名", "") for r in achievement if str(r.get("周期", "")) == "今年以来"), "")
            scale = prof_map.get("基金规模", "")
            try:
                ytd_fmt = f"{float(ytd):.2f}%"
            except Exception:
                ytd_fmt = f"{ytd}%"
            ctx.update({
                "unavailable": False, "name": fname,
                "nav_latest": f"现金管理类",
                "returns": f"今年以来 {ytd_fmt}，历史年度正收益稳定",
                "max_dd": "极低（货基类）",
                "ranking": f"今年以来 {rank}" if rank else "无",
                "scale": scale, "inception": prof_map.get("成立时间", ""),
                "manager": prof_map.get("基金经理", ""),
                "fees": "无（货币基金通常免申赎费）",
                "signal": "持有（现金管理类产品，收益平稳）",
            })
            return ([f"- **{code} {fname}**（money类，现金管理）",
                     f"  今年以来收益 {ytd_fmt}；同类排名 {rank}；规模 {scale}",
                     f"  规则信号: 持有（现金管理类产品，收益平稳）"], ctx)
        return ([f"- {code} {name}: 净值数据不可用"], {**ctx, "unavailable": True})

    n = nav["nav"]
    def rb(days):
        return (n.iloc[-1] / n.iloc[-1 - days] - 1) if len(nav) > days else None
    r1w, r1m, r3m, r6m, r1y = rb(5), rb(21), rb(63), rb(126), rb(250)
    max_dd = float((n / n.cummax() - 1).min())
    ma60 = float(n.rolling(60).mean().iloc[-1])
    trend_up = float(n.iloc[-1]) > ma60
    latest = nav.iloc[-1]
    growth = float(latest["growth"]) if pd_isna(latest["growth"]) == False else 0.0
    nav_str = f"{float(latest['nav']):.4f}（{growth:+.2f}%）"

    fees = fetch_section(lambda: fetcher.fund_fees(fcode), f"费率{fcode}") or []

    prof_map = {str(k).strip(): str(v).strip() for k, v in profile.items()}
    scale = prof_map.get("基金规模", "")
    inception = prof_map.get("成立时间", "")
    manager = prof_map.get("基金经理", "")
    fname = name or prof_map.get("基金名称", code)

    buy_min = sell_min = mgmt = ""
    for row in fees:
        t = str(row.get("费用类型", ""))
        cond = str(row.get("条件或名称", ""))
        fee_raw = str(row.get("费用", ""))
        try:
            fee_val = float(fee_raw.replace("%", "").strip())
        except Exception:
            continue
        # 买入/卖出均取首档（起购/最短持有档）；固定金额档（如 1000 元）跳过
        if t == "买入规则" and not buy_min and fee_val < 10:
            buy_min = f"{fee_val:g}%"
        elif t == "卖出规则" and not sell_min and fee_val < 10:
            sell_min = f"{fee_val:g}%"
        elif t == "其他费用" and "管理费" in cond and not mgmt:
            mgmt = f"{fee_val:g}%"
    fees_str = "，".join(x for x in [f"申购{buy_min}", f"短期赎回{sell_min}", f"管理{mgmt}"] if x)

    # 同类排名（取今年以来/近1年行）
    ranking = ""
    for row in achievement:
        period = str(row.get("周期", ""))
        if period in ("今年以来", "近1年"):
            rk = row.get("周期收益同类排名", "")
            if rk == rk and rk:
                ranking = f"{period} {rk}"
                break

    def pct(x, suffix="%"):
        return "—" if x is None else f"{x*100:+.1f}{suffix}"

    # 规则信号（保守）
    type_hint = {"gold": "黄金类，波动较大，建议分批", "equity": "权益类，波动较大，建议分批", "bond": ""}
    if ptype == "money":
        signal = "持有"
        reason = "现金管理类产品，收益平稳"
        if r1y is not None and r1y < 0.012:
            reason = "收益偏低，可对比同类现金产品"
    else:
        if max_dd < -0.03 and not trend_up:
            signal, reason = "观望", "近一年回撤超3%且趋势向下"
        elif not trend_up and (r3m is not None and r3m < 0):
            signal, reason = "关注", "短期趋势走弱"
        else:
            signal, reason = "持有", "趋势平稳"
        if ptype in type_hint and type_hint[ptype]:
            reason = reason + "；" + type_hint[ptype] if reason else type_hint[ptype]
        if ptype == "bond":
            mgmt_val = _fee_num(mgmt)
            if mgmt_val is not None and mgmt_val > 0.4:
                reason += "；管理费偏高需长期持有摊薄成本"

    line1 = f"- **{code} {fname}**（{ptype}类）"
    line2 = (f"  净值 {nav_str}；近1周{pct(r1w)} 近1月{pct(r1m)} 近3月{pct(r3m)} "
             f"近6月{pct(r6m)} 近1年{pct(r1y)}；近1年最大回撤 {max_dd*100:.1f}%")
    line3 = "  " + "；".join(x for x in [f"同类排名 {ranking}" if ranking else "",
                                          f"规模 {scale}" if scale else "",
                                          f"成立 {inception}" if inception else "",
                                          f"经理 {manager}" if manager else "",
                                          f"费用 {fees_str}" if fees_str else "",
                                          f"平台 {p.get('platform','')}" if p.get("platform") else ""] if x)
    line4 = f"  规则信号: {signal}（{reason}）"

    ctx.update({
        "unavailable": False, "name": fname,
        "nav_latest": nav_str,
        "returns": f"近1周{pct(r1w)} 近1月{pct(r1m)} 近3月{pct(r3m)} 近6月{pct(r6m)} 近1年{pct(r1y)}",
        "max_dd": f"{max_dd*100:.1f}%",
        "ranking": ranking or "无",
        "scale": scale, "inception": inception, "manager": manager,
        "fees": fees_str or "无", "signal": f"{signal}（{reason}）",
    })
    return ([line1, line2, line3, line4], ctx)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push-off", action="store_true", help="不推送，仅生成报告")
    args = ap.parse_args()

    cfg = load_config()

    # 非交易日直接退出（节假日不推送）
    if not is_trading_day():
        log("今天非交易日，跳过报告")
        return

    fetcher = DataFetcher()

    today = date.today().isoformat()
    lines = []
    L = lines.append
    ctx = {"news": []}

    # ---------- 1. 指数速览 ----------
    indexes = [
        ("沪深300", "sh000300"), ("中证500", "sh000905"),
        ("中证红利", "sh000922"), ("创业板指", "sz399006"),
    ]
    pe_indexes = ["沪深300", "中证500", "上证红利"]
    equity_stances = []
    idx_ctx = {}
    L(f"## 市场速览")
    for name, tx in indexes:
        df = fetch_section(lambda t=tx: fetcher.index_daily(t), name)
        if df is None or df.empty:
            L(f"- {name}: 数据获取失败")
            continue
        last = df.iloc[-1]
        close = last["close"]
        pct = last["pct"] if last["pct"] == last["pct"] else 0.0
        emoji = "🔴" if pct >= 0 else "🟢"
        L(f"- {name}: {close:.2f} {emoji}{pct:+.2f}%")
        idx_ctx[name] = f"{close:.2f}（{pct:+.2f}%）"
        ma120 = sma(df["close"], 120).iloc[-1]
        # 估值分位 + 趋势（仅对乐咕支持的指数）
        if name not in pe_indexes:
            continue
        pe = fetch_section(lambda s=name: fetcher.pe_history(s), f"{name}PE")
        if pe is not None and not pe.empty:
            last_pe = pe.iloc[-1]["pe"]
            r = pct_rank(pe["pe"], last_pe)
            if r is not None:
                score = 1.0 if r < 0.3 else (0.5 if r < 0.5 else (0.0 if r <= 0.7 else (-0.5 if r <= 0.85 else -1.0)))
                if last["close"] > ma120:
                    score += 0.5
                else:
                    score -= 0.5
                m20 = last["close"] / df["close"].iloc[-21] - 1 if len(df) > 21 else 0.0
                if m20 > 0.03:
                    score += 0.5
                elif m20 < -0.03:
                    score -= 0.5
                equity_stances.append(score)

    # 隔夜外盘（新浪源）
    us = fetch_section(fetcher.us_index, "标普500")
    if us is not None and len(us) >= 2:
        us_pct = us.iloc[-1]["pct"]
        L(f"- 隔夜标普500: {us.iloc[-1]['close']:.1f} {'🔴' if us_pct >= 0 else '🟢'}{us_pct:+.2f}%")
        idx_ctx["隔夜标普500"] = f"{us.iloc[-1]['close']:.1f}（{us_pct:+.2f}%）"
    ctx["indexes"] = idx_ctx

    # ---------- 2. 估值温度 ----------
    L(f"\n## 估值温度 (近{PE_HIST_YEARS}年分位)")
    val_lines = []
    for name in pe_indexes:
        pe = fetch_section(lambda s=name: fetcher.pe_history(s), f"{name}PE")
        if pe is None or pe.empty:
            val_lines.append(f"- {name}: 无数据")
            continue
        last_pe = pe.iloc[-1]["pe"]
        r = pct_rank(pe["pe"], last_pe)
        if r is None or pd_isna(last_pe):
            val_lines.append(f"- {name}: 无数据")
            continue
        tag = "低估" if r < 0.3 else ("合理" if r <= 0.7 else "偏高")
        val_lines.append(f"- {name}: PE {last_pe:.2f}，分位 {r*100:.0f}%（{tag}）")
    # 全市场 PE（乐咕，上证）
    mpe = fetch_section(fetcher.market_pe, "全市场PE")
    if mpe is not None and not mpe.empty:
        last_row = mpe.iloc[-1]
        val_lines.append(f"- 全市场(上证): PE {last_row['平均市盈率']:.2f}")
    L("\n".join(val_lines))
    ctx["valuation"] = dict((l.split(":")[0].strip(), l.split(":", 1)[1].strip())
                            for l in val_lines if ":" in l)

    # 债券
    bond_ctx = {}
    L(f"\n## 债市与利率")
    bond = fetch_section(fetcher.bond_rates, "国债收益率")
    bond_sig = None
    if bond is not None and not bond.empty:
        y10 = bond.iloc[-1]["y10"]
        y2 = bond.iloc[-1]["y2"]
        r = pct_rank(bond["y10"], y10)
        spread = y10 - y2
        L(f"- 中国10年期国债: {y10:.2f}%，分位 {r*100:.0f}%")
        bond_ctx["10年期国债"] = f"{y10:.2f}%，分位 {r*100:.0f}%"
        L(f"- 期限利差(10Y-2Y): {spread:+.2f}%{'（倒挂，警惕）' if spread < 0 else ''}")
        bond_ctx["期限利差(10Y-2Y)"] = f"{spread:+.2f}%{'（倒挂）' if spread < 0 else ''}"
        if r < 0.3:
            bond_sig = -1.0
            L(f"- 债券研判: 利率处于低位，债价偏贵 → 债券略减")
        elif r > 0.7:
            bond_sig = 1.0
            L(f"- 债券研判: 利率处于高位，债价便宜 → 债券可增")
        else:
            bond_sig = 0.0
            L(f"- 债券研判: 中性")
    else:
        L(f"- 无数据")

    # LPR 政策利率（央行数据）
    lpr_row = fetch_section(fetcher.lpr, "LPR")
    if lpr_row is not None:
        L(f"- LPR: 1年期 {lpr_row['LPR1Y']:.2f}%，5年期 {lpr_row['LPR5Y']:.2f}%（{str(lpr_row['TRADE_DATE'])[:10]}）")
        bond_ctx["LPR"] = f"1Y {lpr_row['LPR1Y']:.2f}%，5Y {lpr_row['LPR5Y']:.2f}%"
    ctx["bond"] = bond_ctx
    ctx["signal_bond"] = stance_label(bond_sig) if bond_sig is not None else "无数据"

    # ---------- 4. 黄金 ----------
    gold_ctx = {}
    L(f"\n## 黄金")
    gold = fetch_section(fetcher.gold_daily, "上海金")
    gold_sig = None
    if gold is not None and not gold.empty:
        last = gold.iloc[-1]["close"]
        ma120 = sma(gold["close"], 120).iloc[-1]
        m20 = last / gold["close"].iloc[-21] - 1 if len(gold) > 21 else 0.0
        d1 = last / gold["close"].iloc[-2] - 1 if len(gold) > 2 else 0.0
        trend = "多头" if last > ma120 else "空头"
        L(f"- Au99.99: ¥{last:.2f}，当日 {d1:+.2%}，20日动量 {m20:+.1%}，趋势{trend}")
        gold_ctx["Au99.99"] = f"¥{last:.2f}，当日 {d1:+.2%}，20日动量 {m20:+.1%}，趋势{trend}"
        gold_sig = 0.5 if last > ma120 else -0.5
        L(f"- 黄金研判: {'趋势向上，可持有' if trend == '多头' else '趋势向下，暂不加仓'}")
    else:
        L(f"- 无数据")
    ctx["gold"] = gold_ctx
    ctx["signal_gold"] = stance_label(gold_sig) if gold_sig is not None else "无数据"

    # ---------- 4.5 宏观与资金面 ----------
    macro_ctx = {}
    L(f"\n## 宏观与资金面")
    fx = fetch_section(fetcher.usd_cny, "汇率")
    if fx is not None:
        L(f"- 美元/人民币: {fx:.4f}")
        macro_ctx["美元/人民币"] = f"{fx:.4f}"
    else:
        L(f"- 汇率: 无数据")
    margin = fetch_section(fetcher.margin_sse, "融资融券")
    if margin is not None and len(margin) >= 1:
        cur = float(margin.iloc[0]["融资融券余额"])
        L(f"- 两融余额(沪市): {cur/1e8:,.0f}亿", )
        if len(margin) >= 2:
            prev = float(margin.iloc[1]["融资融券余额"])
            chg = (cur - prev) / 1e8
            L(f"  （{chg:+.0f}亿 vs 上一交易日，杠杆情绪{'升温' if chg > 0 else '降温'}）")
            macro_ctx["两融余额(沪市)"] = f"{cur/1e8:,.0f}亿（{chg:+.0f}亿 vs 上日，{'升温' if chg > 0 else '降温'}）"
        else:
            macro_ctx["两融余额(沪市)"] = f"{cur/1e8:,.0f}亿"
    else:
        L(f"- 两融余额: 无数据")
    ctx["macro"] = macro_ctx

    # ---------- 4.6 市场要闻（财联社电报） ----------
    news = fetch_section(fetcher.cls_news, "财联社电报")
    news_ctx = []
    if news is not None and not news.empty:
        L(f"\n## 市场要闻（财联社）")
        for _, row in news.head(6).iterrows():
            title = str(row["标题"]).strip()
            if not title or pd_isna(title):
                title = str(row["内容"])[:60]
            title = title.replace("【", "").replace("】", " ")
            if len(title) > 62:
                title = title[:62] + "…"
            L(f"- {title}")
        for _, row in news.head(20).iterrows():
            title = str(row["标题"]).strip()
            content = str(row["内容"]).strip()
            if not title or pd_isna(title):
                title = content[:60]
            news_ctx.append({"title": title, "content": content})
    ctx["news"] = news_ctx

    # ---------- 4.7 理财产品分析 ----------
    products = cfg.get("products", [])
    product_ctxs = []
    if products:
        L(f"\n## 理财产品跟踪")
        for p in products:
            plines, pctx = analyze_product(fetcher, p)
            for ln in plines:
                L(ln)
            product_ctxs.append(pctx)
    ctx["products"] = product_ctxs

    # ---------- 5. 组合检查 ----------
    L(f"\n## 组合检查")
    holdings = cfg["holdings"]
    target = cfg["target"]
    band = cfg["rebalance_band"]
    total = sum(h["amount"] for h in holdings)
    if total <= 0:
        L("- 请先在 config.json 填写持仓金额")
        total = 1
    actual = {}
    tx_codes = []
    for h in holdings:
        code = h.get("code")
        if code:
            tx_codes.append("sh" + code if code.startswith("5") else "sz" + code)
    spot_map = {}
    if tx_codes:
        rt = fetch_section(lambda: fetcher.realtime_quotes(tx_codes), "持仓行情")
        if rt:
            for h in holdings:
                code = h.get("code")
                if code:
                    key = "sh" + code if code.startswith("5") else "sz" + code
                    if key in rt:
                        spot_map[code] = rt[key]["pct"]
    for h in holdings:
        t = h["type"]
        actual[t] = actual.get(t, 0) + h["amount"]
    classes = [("equity", "权益"), ("bond", "债券"), ("gold", "黄金"), ("cash", "现金")]
    L(f"- 总资产: ¥{total:,.0f}")
    for t, label in classes:
        if t in actual:
            L(f"- {label}: ¥{actual[t]:,.0f}（{actual[t]/total*100:.0f}%）")
    # 现金并入债券视为固收
    def class_share(t):
        amt = actual.get(t, 0)
        if t == "bond":
            amt += actual.get("cash", 0)
        return amt / total

    actions = []
    rebal_lines = []
    for t, label in classes:
        if t not in target:
            continue
        share = class_share(t)
        diff = share - target[t]
        if abs(diff) >= band:
            amt = int(round(abs(diff) * total / 100) * 100)
            if diff > 0:
                actions.append((t, "减", amt))
                rebal_lines.append(f"- {label}超配 {diff*100:.0f}pp → 卖出/转出约 ¥{amt:,.0f} → 资金转入低配类别")
            else:
                actions.append((t, "增", amt))
                rebal_lines.append(f"- {label}低配 {abs(diff)*100:.0f}pp → 买入/转入约 ¥{amt:,.0f}，建议分2-3周执行")
        else:
            rebal_lines.append(f"- {label} {diff*100:+.1f}pp，在阈值内 → 无需操作")
    L("\n".join(rebal_lines))

    # 持仓今日估算（场内ETF用实时，场外基金用昨日净值）
    mv_lines = []
    for h in holdings:
        if h.get("code") in spot_map:
            mv_lines.append(f"- {h['name']}: 今日{spot_map[h['code']]:+.2f}%")
        elif h.get("fund_code"):
            fh = fetch_section(lambda c=h["fund_code"]: fetcher.fund_daily(c), f"净值{h['fund_code']}")
            if fh is not None and not fh.empty:
                last_row = fh.iloc[-1]
                g = float(last_row["日增长率"]) if pd_isna(last_row["日增长率"]) == False else 0.0
                mv_lines.append(f"- {h['name']}: 最新净值 {last_row['单位净值']:.4f}（{last_row['净值日期']}，{g:+.2f}%）")
    if mv_lines:
        L(f"\n持仓最新表现")
        L("\n".join(mv_lines))

    # ---------- 6. 总体建议 ----------
    L(f"\n## 今日操作建议")
    eq = None
    if equity_stances:
        avg = sum(equity_stances) / len(equity_stances)
        eq = stance_label(avg)
        L(f"- 权益: {eq}（{avg:+.1f}）")
        if eq in ("增配", "略增") and actions and any(a[0] == "equity" and a[1] == "增" for a in actions):
            L(f"- 权益低配+估值合理 → 按上面再平衡金额分批买入")
        if eq in ("减配", "略减") and actions and any(a[0] == "equity" and a[1] == "减" for a in actions):
            L(f"- 权益超配+信号转弱 → 优先执行再平衡卖出")
    if bond_sig is not None:
        L(f"- 债券: {stance_label(bond_sig)}")
    if gold_sig is not None:
        L(f"- 黄金: {stance_label(gold_sig)}（卫星仓位，维持{cfg['target'].get('gold', 0.1)*100:.0f}%以内）")
    if not actions and (not equity_stances or abs(sum(equity_stances)/len(equity_stances)) < 0.3):
        L(f"- 组合在目标区间内，市场信号中性 → 今天不需要任何操作")

    ctx["signal_equity"] = eq or "无数据"
    ctx["rebalance"] = "\n".join(rebal_lines) if rebal_lines else "无"
    ctx["holdings_perf"] = "\n".join(mv_lines) if mv_lines else "无"

    L(f"\n---")
    L(f"*本报告由本地程序按固定规则自动生成，仅供参考，不构成投资建议。*")
    L(f"*买卖请手动执行；每次交易后请更新 config.json 中对应持仓金额。*")

    report = "\n".join(lines)
    title = f"每日投顾报告 {today}"

    # ---------- 6.5 AI 解读层（LLM 不可用时自动降级为纯静态报告） ----------
    insights, usage_info = llm.generate_insights(ctx)
    if insights:
        report = llm.insert_insights(report, insights)
        log(f"AI 解读已生成（{usage_info}）")
    else:
        log(f"AI 解读跳过: {usage_info}")

    # ---------- 7. 输出 ----------
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, f"report_{today}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n" + report)
    log(f"报告已生成: {path}")

    if not args.push_off:
        channel = cfg.get("push", {}).get("channel", "none")
        if channel == "wecom":
            push_wecom(cfg["push"]["wecom_webhook"], title, report)
        elif channel == "serverchan":
            push_serverchan(cfg["push"]["serverchan_key"], title, report)
        else:
            log("push.channel = none，未推送（本地报告已保存）")


def pd_isna(v):
    try:
        return v != v
    except Exception:
        return False


TRADE_CAL_CACHE = None


def is_trading_day():
    """非交易日（周末/法定节假日）跳过推送；交易日历获取失败时不阻塞"""
    global TRADE_CAL_CACHE
    if TRADE_CAL_CACHE is None:
        try:
            import akshare as ak
            df = ak.tool_trade_date_hist_sina()
            TRADE_CAL_CACHE = set(df["trade_date"].astype(str))
        except Exception as e:
            log(f"[WARN] 交易日历获取失败({type(e).__name__}: {str(e)[:60]})，不阻塞运行")
            return True
    return date.today().isoformat() in TRADE_CAL_CACHE


def post_with_retry(url, payload):
    last_err = None
    for attempt in range(1, PUSH_RETRY + 1):
        try:
            r = requests.post(url, json=payload, timeout=15)
            return r
        except Exception as e:
            last_err = e
            if attempt < PUSH_RETRY:
                log(f"[WARN] 推送第{attempt}次失败({type(e).__name__})，重试中...")
                time.sleep(3)
    log(f"[ERROR] 推送重试{PUSH_RETRY}次均失败: {last_err}")
    return None


def push_wecom(webhook, title, md):
    if not webhook:
        log("[WARN] 未配置企业微信 webhook，跳过推送")
        return
    content = f"## {title}\n" + md
    if len(content.encode("utf-8")) > 3800:
        content = content[:2500] + "\n...(正文过长，详见本地 reports 目录)"
    r = post_with_retry(webhook, {"msgtype": "markdown", "markdown": {"content": content}})
    if r is not None:
        j = r.json()
        log(f"企业微信推送: {j.get('errcode')} {j.get('errmsg')}")


def push_serverchan(key, title, md):
    if not key:
        log("[WARN] 未配置 Server酱 key，跳过推送")
        return
    last_err = None
    for attempt in range(1, PUSH_RETRY + 1):
        try:
            r = requests.post(f"https://sctapi.ftqq.com/{key}.send",
                              data={"title": title, "desp": md}, timeout=15)
            j = r.json()
            log(f"Server酱推送: code={j.get('code')}")
            return
        except Exception as e:
            last_err = e
            if attempt < PUSH_RETRY:
                time.sleep(3)
    log(f"[ERROR] Server酱推送失败: {last_err}")


def push_alert(cfg, message):
    """主流程异常时的告警推送"""
    push = cfg.get("push", {})
    try:
        if push.get("channel") == "wecom":
            webhook = push.get("wecom_webhook", "")
            if webhook:
                post_with_retry(webhook, {"msgtype": "text", "text": {"content": message}})
        elif push.get("channel") == "serverchan":
            key = push.get("serverchan_key", "")
            if key:
                requests.post(f"https://sctapi.ftqq.com/{key}.send",
                              data={"title": "投顾报告异常", "desp": message}, timeout=15)
    except Exception as e:
        log(f"[ERROR] 告警推送失败: {e}")


if __name__ == "__main__":
    cfg = None
    try:
        cfg = load_config()
        main()
    except Exception:
        log("[ERROR] 运行异常:\n" + traceback.format_exc())
        if cfg is not None:
            push_alert(cfg, f"投顾报告生成失败，请检查。\n{traceback.format_exc()[-500:]}")
        sys.exit(1)
