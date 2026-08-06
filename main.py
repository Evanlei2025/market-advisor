# -*- coding: utf-8 -*-
"""每日投顾报告生成器（V2.2）
数据采集 -> 规则决策 -> 完整版报告 + 精简版推送 + HTML 网页
"""
import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import date, datetime

import pandas as pd
import requests

import html_render
import llm
import narrative
import news_alert
import rules
import state_store
import style

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
REPORT_DIR = os.path.join(BASE_DIR, "reports")
LOG_PATH = os.path.join(BASE_DIR, "logs", "run.log")
PAGE_URL = "https://Evanlei2025.github.io/market-advisor/latest.html"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

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
    log("[WARN] 未找到配置，使用默认")
    return {"target": {"equity": {"base": 0.4, "band": 0.05}, "cash": {"base": 0.1, "band": 0.05}},
            "holdings": [], "products": []}


class DataFetcher:
    """行情用腾讯源(稳定)，估值用乐咕，债券/黄金用官方源，宏观扩展指标失败不阻塞"""

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

    def pe_history_full(self, symbol):
        df = self.ak.stock_index_pe_lg(symbol=symbol)
        df = df[["日期", "滚动市盈率"]]
        df.columns = ["date", "pe"]
        df["date"] = pd.to_datetime(df["date"])
        return df.dropna()

    def bond_rates(self):
        df = self.ak.bond_zh_us_rate()
        df = df[["日期", "中国国债收益率2年", "中国国债收益率10年"]].dropna()
        df.columns = ["date", "y2", "y10"]
        df["date"] = pd.to_datetime(df["date"])
        cutoff = pd.Timestamp(f"{datetime.now().year - PE_HIST_YEARS}-01-01")
        return df[df["date"] >= cutoff]

    def bond_rates_cn(self):
        df = self.ak.bond_gb_zh_sina(symbol="中国10年期国债")
        df = df[["date", "close"]].dropna()
        df["date"] = pd.to_datetime(df["date"])
        df = df.rename(columns={"close": "y10"})
        df["y2"] = float("nan")
        cutoff = pd.Timestamp(f"{datetime.now().year - PE_HIST_YEARS}-01-01")
        return df[df["date"] >= cutoff].reset_index(drop=True)

    def fund_stock_position(self, fund_code):
        try:
            r = self.session.get(f"https://fund.eastmoney.com/pingzhongdata/{fund_code}.js", timeout=20)
            if "var Data_assetAllocation = " not in r.text:
                return 0.0, ""
            part = r.text.split("var Data_assetAllocation = ", 1)[1]
            seg = part.split("};", 1)[0] + "}"
            d = json.loads(seg)
            cats = d.get("categories", [])
            for s in d.get("series", []):
                if s.get("name") == "股票占净比" and s.get("data"):
                    return float(s["data"][-1]) / 100.0, str(cats[-1])[:10] if cats else ""
            return 0.0, ""
        except Exception:
            return 0.0, ""

    def fund_bond_convertible(self, fund_code):
        try:
            df = self.ak.fund_portfolio_bond_hold_em(symbol=fund_code, date=str(datetime.now().year))
            if df is None or df.empty or "占净值比例" not in df.columns:
                return 0.0
            mask = df["债券名称"].astype(str).str.contains("转债", na=False)
            return float(df.loc[mask, "占净值比例"].sum()) / 100.0
        except Exception:
            return 0.0

    def fund_industry_hhi(self, fund_code):
        """行业集中度 HHI：基金行业配置（证监会大类）前三大权重平方和（归一化）。
        返回 0~1；数据缺失/异常返回 None。"""
        try:
            df = self.ak.fund_portfolio_industry_allocation_em(
                symbol=fund_code, date=str(datetime.now().year))
            if df is None or df.empty or "占净值比例" not in df.columns:
                return None
            w = df["占净值比例"].astype(float).dropna()
            w = w[w > 0]
            if w.empty:
                return None
            total = w.sum()
            if total <= 0:
                return None
            w = (w / total).sort_values(ascending=False)
            top3 = w.head(3)
            return float((top3 ** 2).sum())
        except Exception:
            return None

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

    # ---- 宏观扩展指标（V2.2 扩容；全部失败不阻塞） ----
    def us_bond_10y(self):
        """美债10年收益率（英为中美国债收益率表）"""
        df = self.ak.bond_zh_us_rate()
        col = "美国国债收益率10年"
        if col not in df.columns:
            return None
        v = df[col].dropna()
        if v.empty:
            return None
        return float(v.iloc[-1])

    def crude_oil(self):
        """上期能源原油主力（SC0）"""
        df = self.ak.futures_main_sina(symbol="SC0")
        if df is None or len(df) < 2:
            return None
        cur = float(df["收盘价"].iloc[-1])
        prev = float(df["收盘价"].iloc[-2])
        return cur, (cur / prev - 1)

    def copper(self):
        """沪铜主力（CU0，国际铜价定价锚）"""
        df = self.ak.futures_main_sina(symbol="CU0")
        if df is None or len(df) < 2:
            return None
        cur = float(df["收盘价"].iloc[-1])
        prev = float(df["收盘价"].iloc[-2])
        return cur, (cur / prev - 1)

    def cpi(self):
        df = self.ak.macro_china_cpi()
        col = [c for c in df.columns if "同比" in str(c)]
        if not col:
            return None
        v = df[col[0]].dropna()
        return float(v.iloc[-1]) if not v.empty else None

    def ppi(self):
        df = self.ak.macro_china_ppi()
        col = [c for c in df.columns if "同比" in str(c)]
        if not col:
            return None
        v = df[col[0]].dropna()
        return float(v.iloc[-1]) if not v.empty else None

    def pmi(self):
        df = self.ak.macro_china_pmi()
        col = [c for c in df.columns if "制造业" in str(c)]
        if not col:
            return None
        v = df[col[0]].dropna()
        return float(v.iloc[-1]) if not v.empty else None

    def shrzgm(self):
        df = self.ak.macro_china_shrzgm()
        col = [c for c in df.columns if "社会融资规模" in str(c) and "累计" not in str(c)]
        if not col:
            return None
        v = df[col[0]].dropna()
        return float(v.iloc[-1]) if not v.empty else None


def pct_rank(series, value):
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


def safe_format(v, fmt="{:+.2f}", suffix="%"):
    try:
        if v is None or v != v:
            return "暂缺"
        return fmt.format(v) + suffix
    except (TypeError, ValueError):
        return "暂缺"


def fetch_once(fn, name):
    try:
        return fn()
    except Exception as e:
        log(f"[WARN] {name} 失败({type(e).__name__})，切换备选源")
        return None


def pd_isna(v):
    try:
        return v != v
    except Exception:
        return False


TYPE_LABELS = {"bond": "债券型", "money": "现金管理", "equity": "权益型", "gold": "黄金型", "other": "理财"}

# 指标注释（指标温度表：每项一行 + 反映什么趋势）
MACRO_NOTES = {
    "美债10Y": "全球利率锚，走高压制股票估值",
    "WTI原油": "反映全球需求与通胀预期",
    "SC原油": "反映全球需求与通胀预期（国内主力合约）",
    "伦敦铜": "经济景气风向标（铜博士）",
    "沪铜": "经济景气风向标（铜博士，国内主力）",
    "CPI": "物价水平，影响货币政策松紧",
    "PPI": "工业品价格，反映企业利润与库存周期",
    "PMI": "制造业景气，50 为荣枯线",
    "社融": "实体经济融资需求，宽信用的领先指标",
}


def analyze_product(fetcher, p, cfg_ref, bench_pctile_map):
    """单个产品分析：返回 (report_lines, ctx_dict)。失败不阻塞。"""
    code = p.get("code", "?")
    name = p.get("name") or ""
    ptype = p.get("type", "other")
    fcode = p.get("fund_code", "")
    ctx = {"code": code, "name": name, "type": ptype,
           "platform": p.get("platform", ""), "notes": p.get("notes", ""),
           "status": p.get("status", "held")}
    if not fcode:
        return ([f"- {code} {name}: 银行理财/券商产品，自动数据不可用（需人工维护）"],
                {**ctx, "unavailable": True})

    nav = fetch_section(lambda: fetcher.fund_nav_history(fcode), f"净值{fcode}")
    achievement = fetch_section(lambda: fetcher.fund_achievement(fcode), f"业绩{fcode}") or []
    profile = fetch_section(lambda: fetcher.fund_profile(fcode), f"资料{fcode}") or {}

    if nav is None or nav.empty or len(nav) < 30:
        return ([f"- {code} {name}: 净值数据不可用"], {**ctx, "unavailable": True})

    n = nav["nav"]
    def rb(days):
        return (n.iloc[-1] / n.iloc[-1 - days] - 1) if len(nav) > days else None
    r1w, r1m, r3m, r6m, r1y = rb(5), rb(21), rb(63), rb(126), rb(250)
    max_dd = float((n / n.cummax() - 1).min())
    latest = nav.iloc[-1]
    nav_str = f"{float(latest['nav']):.4f}（{safe_format(float(latest['growth']) if not pd_isna(latest['growth']) else None, '{:+.2f}')}，净值日期 {str(latest['date'])[:10]}）"

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
        if t == "买入规则" and not buy_min and fee_val < 10:
            buy_min = f"{fee_val:g}%"
        elif t == "卖出规则" and not sell_min and fee_val < 10:
            sell_min = f"{fee_val:g}%"
        elif t == "其他费用" and "管理费" in cond and not mgmt:
            mgmt = f"{fee_val:g}%"
    fees_str = "，".join(x for x in [f"申购{buy_min}", f"短期赎回{sell_min}", f"管理{mgmt}"] if x)

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

    # 穿透风险分类：权益暴露度 = 股票仓位 + 转债仓位×50%
    equity_exposure = None
    position_date = ""
    if ptype in ("bond", "equity", "gold"):
        try:
            stock_pos, position_date = fetch_section(lambda: fetcher.fund_stock_position(fcode), f"股票仓{fcode}") or (0.0, "")
            conv_pos = fetch_section(lambda: fetcher.fund_bond_convertible(fcode), f"转债仓{fcode}")
            conv_ratio = float(cfg_ref["rules"].get("convertible_exposure_ratio", 0.5))
            if stock_pos is None:
                stock_pos = 0.0
            if conv_pos is None:
                conv_pos = 0.0
            equity_exposure = stock_pos + conv_pos * conv_ratio
            if ptype == "equity" and stock_pos < 0.01:
                equity_exposure = max(equity_exposure, 0.8)
        except Exception:
            equity_exposure = None

    # ---- 动态止盈线 V2.2（仅计算展示，触发由规则引擎判定） ----
    nav_series = nav.reset_index(drop=True)
    hhi = fetch_section(lambda: fetcher.fund_industry_hhi(fcode), f"行业HHI{fcode}")
    if equity_exposure is not None:
        exp_tag = "权益型" if equity_exposure >= 0.8 else ("混合偏债" if equity_exposure >= 0.2 else ("含转债" if equity_exposure >= 0.05 else "纯债"))
        exp_str = f"权益暴露 {equity_exposure*100:.1f}%（{exp_tag}）"
        type_label = "权益型" if equity_exposure >= 0.8 else ("混合偏债型" if equity_exposure >= 0.2 else ("含转债" if equity_exposure >= 0.05 else "债券型"))
    else:
        exp_str = "权益暴露 暂缺"
        type_label = TYPE_LABELS.get(ptype, ptype)

    line1 = f"\n**{code} {fname}**（{type_label}）"
    if p.get("status") == "observe":
        line1 += " ｜ **零持仓·仅观察**（持续跟踪，逢机提醒买入）"
    line2 = f"- 净值 {nav_str}"
    line3 = "- 区间收益："
    line4 = f"近1周 {pct(r1w)}"
    line5 = f"近1月 {pct(r1m)}"
    line6 = f"近3月 {pct(r3m)}"
    line7 = f"近6月 {pct(r6m)}"
    line8 = f"近1年 {pct(r1y)}"
    line9 = f"- 近1年最大回撤 {max_dd*100:.1f}%"
    line10 = "- " + "，".join(x for x in [f"同类排名 {ranking}" if ranking else "",
                                           f"规模 {scale}" if scale else "",
                                           f"成立 {inception}" if inception else "",
                                           f"经理 {manager}" if manager else ""] if x)
    line11 = "- " + "，".join(x for x in [f"费用 {fees_str}" if fees_str else "",
                                           f"平台 {p.get('platform','')}" if p.get("platform") else "",
                                           f"备注 {p.get('notes','')}" if p.get("notes") else ""] if x)
    line12 = f"- {exp_str}"
    line13 = "- 规则信号：持有观察（本系统无止损，仅动态止盈）"
    if position_date:
        line13 += f"\n- 仓位数据基于最近季报（{position_date}），可能滞后1-3个月"

    ctx.update({
        "unavailable": False, "name": fname,
        "nav_latest": nav_str,
        "returns": f"近1周{pct(r1w)} 近1月{pct(r1m)} 近3月{pct(r3m)} 近6月{pct(r6m)} 近1年{pct(r1y)}",
        "max_dd": f"{max_dd*100:.1f}%",
        "ranking": ranking or "无",
        "scale": scale, "inception": inception, "manager": manager,
        "fees": fees_str or "无",
        "equity_exposure": equity_exposure,
        "bench_pctile": bench_pctile_map.get(code),
        "r3m": r3m,
        "hhi": hhi,
        "nav_series": nav_series,
        "action": "hold",
    })
    return ([line1, line2, line3, line4, "", line5, "", line6, "", line7, "", line8,
             line9, line10, line11, line12, line13], ctx)


def split_blocks(report):
    """按 ## 板块切分 markdown，返回 {标题: 内容}（标题不含##）"""
    blocks = {}
    cur = None
    for ln in report.splitlines():
        if ln.startswith("## "):
            cur = ln[3:].strip()
            blocks[cur] = []
        elif ln.startswith("# "):
            cur = None
        elif cur is not None:
            blocks[cur].append(ln)
    return {k: "\n".join(v) for k, v in blocks.items()}


def build_compact(blocks, page_url, date_str):
    """精简版：客户必看板块（其余细节见网页）"""
    want = ["今日一句话", "今日指令摘要", "指标温度表", "理财产品跟踪",
            "今日跟投指令", "行业与产品关注", "风险观察", "术语速查"]
    out = [f"# 每日投顾报告（精简版）{date_str}", ""]
    for w in want:
        if w in blocks and blocks[w].strip():
            out.append(f"## {w}")
            out.append(blocks[w].strip())
            out.append("")
    out.append("---")
    out.append("")
    out.append(f"📄 完整版报告（含全部数据明细）: {page_url}")
    out.append("*本报告由本地程序按固定规则自动生成，仅供参考，不构成投资建议。*")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push-off", action="store_true", help="不推送，仅生成报告")
    args = ap.parse_args()

    cfg = load_config()

    if not is_trading_day():
        log("今天非交易日，跳过报告")
        return

    fetcher = DataFetcher()
    today = date.today().isoformat()
    lines = []
    L = lines.append
    ctx = {"news": []}

    # ---------- 1. 指数速览（完整版明细 + 策略输入） ----------
    indexes = [
        ("沪深300", "sh000300"), ("中证500", "sh000905"),
        ("中证红利", "sh000922"), ("创业板指", "sz399006"),
    ]
    pe_indexes = ["沪深300", "中证500", "上证红利"]
    L(f"## 市场速览")
    idx_ctx = {}
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
        if name not in pe_indexes:
            continue
        pe = fetch_section(lambda s=name: fetcher.pe_history(s), f"{name}PE")
        if pe is not None and not pe.empty:
            last_pe = pe.iloc[-1]["pe"]
            r = pct_rank(pe["pe"], last_pe)
            if r is not None:
                m20 = last["close"] / df["close"].iloc[-21] - 1 if len(df) > 21 else 0.0
                if name == "沪深300":
                    ctx["csi300_pe_pctile"] = float(r)
                    ctx["csi300_mom20"] = float(m20)
                    ctx["csi300_pe_full"] = pe
                    ctx.setdefault("bench_ret_63", {})["csi300"] = (
                        float(last["close"] / df["close"].iloc[-63] - 1) if len(df) > 63 else None)
                elif name == "中证500":
                    ctx["csi500_pe_pctile"] = float(r)
                    ctx["csi500_mom20"] = float(m20)
                    ctx["csi500_pe_full"] = pe
                    ctx.setdefault("bench_ret_63", {})["csi500"] = (
                        float(last["close"] / df["close"].iloc[-63] - 1) if len(df) > 63 else None)
                pe_tag = "偏高" if r > 0.7 else ("中性" if r >= 0.3 else "低估")
                L(f"  - PE 分位 {r*100:.0f}%（{pe_tag}），20日动量 {m20:+.1%}")
    us = fetch_section(fetcher.us_index, "标普500")
    if us is not None and len(us) >= 2:
        us_pct = us.iloc[-1]["pct"]
        L(f"- 隔夜标普500: {us.iloc[-1]['close']:.1f} {'🔴' if us_pct >= 0 else '🟢'}{us_pct:+.2f}%")
        idx_ctx["隔夜标普500"] = f"{us.iloc[-1]['close']:.1f}（{us_pct:+.2f}%）"
    ctx["indexes"] = idx_ctx

    # ---------- 2. 估值温度 + 股债性价比 ----------
    L("")
    L(f"## 估值温度 (近{PE_HIST_YEARS}年分位)")
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
        pe_full = fetch_section(lambda s=name: fetcher.pe_history_full(s), f"{name}PE全史")
        sens = ""
        if pe_full is not None and not pe_full.empty:
            r_full = pct_rank(pe_full["pe"], last_pe)
            if r_full is not None:
                sens = f"（全历史分位 {r_full*100:.0f}%）"
        val_lines.append(f"- {name}: PE {last_pe:.2f}，近{PE_HIST_YEARS}年分位 {r*100:.0f}%（{tag}）{sens}")
    mpe = fetch_section(fetcher.market_pe, "全市场PE")
    if mpe is not None and not mpe.empty:
        val_lines.append(f"- 全市场(上证): PE {mpe.iloc[-1]['平均市盈率']:.2f}")
    L("\n".join(val_lines))
    ctx["valuation"] = dict((l.split(":")[0].strip(), l.split(":", 1)[1].strip())
                            for l in val_lines if ":" in l)

    # 股债性价比 EP
    ep_ctx = {}
    pe_full300 = ctx.get("csi300_pe_full")
    bond_for_ep = fetch_once(fetcher.bond_rates, "国债收益率(EP)")
    if bond_for_ep is None or bond_for_ep.empty:
        bond_for_ep = fetch_section(fetcher.bond_rates_cn, "中债收益率(EP)")
    if pe_full300 is not None and bond_for_ep is not None and not bond_for_ep.empty:
        try:
            ep_years = cfg.get("rules", {}).get("ep_premium_years", 5)
            ep_cutoff = pd.Timestamp(f"{datetime.now().year - ep_years}-01-01")
            pe_ep = pe_full300[pe_full300["date"] >= ep_cutoff]
            b_ep = bond_for_ep[bond_for_ep["date"] >= ep_cutoff]
            merged = pd.merge(pe_ep, b_ep[["date", "y10"]], on="date", how="inner").dropna()
            if len(merged) > 60:
                merged["ep"] = 1.0 / merged["pe"] - merged["y10"] / 100.0
                cur_ep = float(merged["ep"].iloc[-1])
                cur_y10 = float(merged["y10"].iloc[-1])
                ep_r = pct_rank(merged["ep"], cur_ep)
                ep_ctx = {"ep": cur_ep, "y10": cur_y10, "pctile": ep_r,
                          "label": f"盈利收益率 1/PE = {1.0/float(pe_full300['pe'].iloc[-1])*100:.2f}% − 10年国债 {cur_y10:.2f}% = {cur_ep*100:+.2f}%，{ep_years}年分位 {ep_r*100:.0f}%"}
                ctx["ep_premium_pctile"] = float(ep_r)
        except Exception as e:
            log(f"[WARN] 股债性价比计算失败: {e}")
    ctx["ep"] = ep_ctx

    # ---------- 3. 债市与利率 + 宏观扩展指标 ----------
    L("")
    L(f"## 债市与利率")
    bond = fetch_once(fetcher.bond_rates, "国债收益率(英为)")
    bond_src = "英为财情"
    if bond is None or bond.empty:
        bond = fetch_section(fetcher.bond_rates_cn, "中债收益率")
        bond_src = "新浪中债"
    bond_ctx = {}
    bond_sig = None
    if bond is not None and not bond.empty:
        y10 = bond.iloc[-1]["y10"]
        y2 = bond.iloc[-1]["y2"]
        r = pct_rank(bond["y10"], y10)
        spread = y10 - y2
        L(f"- 中国10年期国债: {y10:.2f}%，分位 {r*100:.0f}%（{bond_src}）")
        L(f"- 期限利差(10Y-2Y): {safe_format(spread, '{:+.2f}')}{'（倒挂，警惕）' if spread == spread and spread < 0 else ''}")
        bond_ctx["10年期国债"] = f"{y10:.2f}%，分位 {r*100:.0f}%"
        if r < 0.3:
            bond_sig = -1.0
            L(f"- 债券研判: 利率处低位、债价偏贵；战略方向以「今日跟投指令」为准")
        elif r > 0.7:
            bond_sig = 1.0
            L(f"- 债券研判: 利率处高位、债价便宜；战略方向以「今日跟投指令」为准")
        else:
            bond_sig = 0.0
            L(f"- 债券研判: 中性")
    lpr_row = fetch_section(fetcher.lpr, "LPR")
    if lpr_row is not None:
        L(f"- LPR: 1年期 {lpr_row['LPR1Y']:.2f}%，5年期 {lpr_row['LPR5Y']:.2f}%（{str(lpr_row['TRADE_DATE'])[:10]}）")
        bond_ctx["LPR"] = f"1Y {lpr_row['LPR1Y']:.2f}%，5Y {lpr_row['LPR5Y']:.2f}%"
    ctx["bond"] = bond_ctx
    ctx["signal_bond"] = stance_label(bond_sig) if bond_sig is not None else "无数据"

    # 宏观扩展（V2.2 指标扩容）
    L("")
    L(f"## 宏观与资金面")
    macro_ctx = {}
    fx = fetch_section(fetcher.usd_cny, "汇率")
    if fx is not None:
        L(f"- 美元/人民币: {fx:.4f}")
        macro_ctx["美元/人民币"] = f"{fx:.4f}"
    margin = fetch_section(fetcher.margin_sse, "融资融券")
    if margin is not None and len(margin) >= 1:
        cur_m = float(margin.iloc[0]["融资融券余额"])
        L(f"- 两融余额(沪市): {cur_m/1e8:,.0f}亿")
        macro_ctx["两融余额(沪市)"] = f"{cur_m/1e8:,.0f}亿"
    ext = []
    us10 = fetch_section(fetcher.us_bond_10y, "美债10Y")
    if us10 is not None:
        ext.append(("美债10Y", f"{us10:.2f}%"))
        macro_ctx["美债10Y"] = f"{us10:.2f}%"
    oil = fetch_section(fetcher.crude_oil, "SC原油")
    if oil is not None:
        ext.append(("SC原油", f"{oil[0]:,.0f}（{safe_format(oil[1], '{:+.2f}')}）"))
        macro_ctx["SC原油"] = f"{oil[0]:,.0f}"
    copper = fetch_section(fetcher.copper, "沪铜")
    if copper is not None:
        ext.append(("沪铜", f"{copper[0]:,.0f}（{safe_format(copper[1], '{:+.2f}')}）"))
        macro_ctx["沪铜"] = f"{copper[0]:,.0f}"
    cpi_v = fetch_section(fetcher.cpi, "CPI")
    if cpi_v is not None:
        ext.append(("CPI", f"{cpi_v:+.1f}%"))
        macro_ctx["CPI"] = f"{cpi_v:+.1f}%"
    ppi_v = fetch_section(fetcher.ppi, "PPI")
    if ppi_v is not None:
        ext.append(("PPI", f"{ppi_v:+.1f}%"))
        macro_ctx["PPI"] = f"{ppi_v:+.1f}%"
    pmi_v = fetch_section(fetcher.pmi, "PMI")
    if pmi_v is not None:
        ext.append(("PMI", f"{pmi_v:.1f}"))
        macro_ctx["PMI"] = f"{pmi_v:.1f}"
    shrz = fetch_section(fetcher.shrzgm, "社融")
    if shrz is not None:
        ext.append(("社融", f"{shrz/1e8:,.0f}亿"))
        macro_ctx["社融"] = f"{shrz/1e8:,.0f}亿"
    for nm, v in ext:
        L(f"- {nm}: {v} ｜ {MACRO_NOTES.get(nm, '')}")
    ctx["macro"] = macro_ctx

    # ---------- 4. 黄金 ----------
    L("")
    L(f"## 黄金")
    gold_ctx = {}
    gold = fetch_section(fetcher.gold_daily, "上海金")
    gold_sig = None
    if gold is not None and not gold.empty:
        last = gold.iloc[-1]["close"]
        ma120 = sma(gold["close"], 120).iloc[-1]
        m20 = last / gold["close"].iloc[-21] - 1 if len(gold) > 21 else 0.0
        d1 = last / gold["close"].iloc[-2] - 1 if len(gold) > 2 else 0.0
        trend = "多头" if last > ma120 else "空头"
        L(f"- Au99.99: ¥{last:.2f}，当日 {safe_format(d1, '{:+.2f}')}，20日动量 {safe_format(m20, '{:+.1f}')}，趋势{trend}")
        gold_ctx["Au99.99"] = f"¥{last:.2f}，趋势{trend}"
        gold_sig = 0.5 if last > ma120 else -0.5
        L(f"- 黄金研判: {'趋势向上，可持有' if trend == '多头' else '趋势向下，暂不加仓'}")
    else:
        L(f"- 无数据")
    ctx["gold"] = gold_ctx
    ctx["signal_gold"] = stance_label(gold_sig) if gold_sig is not None else "无数据"

    # ---------- 指标温度表（客户看板：浓缩一行+注释） ----------
    temp_lines = []
    if ctx.get("csi300_pe_pctile") is not None:
        v = ctx["csi300_pe_pctile"]
        tag = "偏高" if v > 0.7 else ("中性" if v >= 0.3 else "低估")
        temp_lines.append(f"- 沪深300估值：分位 {v*100:.0f}%（{tag}）｜人话：{'比过去十年多数时间都贵，买股票容易买贵' if v > 0.7 else ('估值适中' if v >= 0.3 else '比过去多数时间便宜')}")
    if ctx.get("csi500_pe_pctile") is not None:
        v = ctx["csi500_pe_pctile"]
        tag = "偏高" if v > 0.7 else ("中性" if v >= 0.3 else "低估")
        temp_lines.append(f"- 中证500估值：分位 {v*100:.0f}%（{tag}）")
    if ep_ctx:
        temp_lines.append(f"- 股债性价比：分位 {ep_ctx['pctile']*100:.0f}%｜人话：{('买股票多赚的差价已经很薄，性价比不高' if ep_ctx['pctile'] < 0.3 else '股票相对债券的性价比正常')}")
    if bond is not None and not bond.empty and not pd_isna(y10):
        temp_lines.append(f"- 10年国债：{y10:.2f}%（分位 {r*100:.0f}%）｜人话：{'利率很低，债价偏贵' if r < 0.3 else ('利率较高，债价便宜' if r > 0.7 else '利率中性')}")
    if gold is not None and not gold.empty:
        temp_lines.append(f"- 黄金：{trend}趋势｜人话：{'趋势向上，可持有' if trend == '多头' else '趋势向下，暂不加仓'}")
    for nm, v in macro_ctx.items():
        if nm in ("美元/人民币", "两融余额(沪市)"):
            continue
        note = MACRO_NOTES.get(nm, "")
        temp_lines.append(f"- {nm}：{v}" + (f"｜{note}" if note else ""))

    # ---------- 5. 新闻采集（仅输入哨兵） ----------
    news = fetch_section(fetcher.cls_news, "财联社电报")
    news_ctx = []
    if news is not None and not news.empty:
        for _, row in news.head(60).iterrows():
            title = str(row["标题"]).strip()
            content = str(row["内容"]).strip()
            if not title or pd_isna(title):
                title = content[:60]
            news_ctx.append({"title": title, "content": content})
    ctx["news"] = news_ctx

    # ---------- 6. 理财产品分析（关注池） ----------
    products = cfg.get("products", [])
    product_ctxs = []
    returns_map = {}
    weights_map = {}
    bench_pctile_map = {}
    if ctx.get("csi300_pe_pctile") is not None:
        bench_pctile_map["csi300"] = ctx["csi300_pe_pctile"]
    if ctx.get("csi500_pe_pctile") is not None:
        bench_pctile_map["csi500"] = ctx["csi500_pe_pctile"]
    per_code_bench = {p.get("code", ""): bench_pctile_map.get(p.get("bench_index", ""))
                      for p in products}
    if products:
        L("")
        L(f"## 理财产品跟踪")
        for p in products:
            plines, pctx = analyze_product(fetcher, p, cfg, per_code_bench)
            for ln in plines:
                L(ln)
            product_ctxs.append(pctx)
            nav_s = pctx.get("nav_series")
            if nav_s is not None and len(nav_s) > 30:
                returns_map[p["code"]] = nav_s["nav"].pct_change().dropna()
    ctx["products"] = product_ctxs

    # ---------- 7. 组合与跟投指令 ----------
    holdings = cfg["holdings"]
    total = sum(float(h.get("amount", 0)) for h in holdings if h.get("type") == "cash")
    mv_map, mv_total = rules.market_value_weights(
        holdings,
        {pc.get("code", ""): pc.get("nav_series") for pc in product_ctxs},
        {pc.get("code", ""): (float(pc.get("nav_series")["nav"].iloc[-1])
                              if pc.get("nav_series") is not None and len(pc["nav_series"]) else None)
         for pc in product_ctxs})
    total_mv = mv_total
    for key, mv in mv_map.items():
        if key in returns_map and total_mv > 0:
            weights_map[key] = mv / total_mv

    L("")
    L(f"## 今日跟投指令")
    orders = []
    summary_lines = []
    target_alloc = {}
    rule_triggers = []
    cro = None
    alerts = None
    tp_actions = {}
    storm_active = False
    ep_lock = False
    cooldown_active = False

    if total_mv <= 0:
        L(f"- 持仓市值为 0，暂无法生成指令")
    else:
        L(f"- 客户组合（总市值 ¥{total_mv:,.0f}，跟投份数 {cfg.get('portfolio', {}).get('follow_units', 1)}）")
        eq_target, rule_triggers = rules.equity_target(cfg, ctx)
        storm_active, storm_reasons = rules.storm_lock(eq_target, rule_triggers)
        ep_lock = (not storm_active) and (ctx.get("ep_premium_pctile") is not None) and (ctx["ep_premium_pctile"] < 0.10)
        cooldown_active = state_store.in_cooldown(cfg)

        pctx_map = {p.get("code", ""): p for p in product_ctxs}
        nav_map = {}
        for pc in product_ctxs:
            ns = pc.get("nav_series")
            if ns is not None and len(ns) > 0:
                nav_map[pc["code"]] = float(ns["nav"].iloc[-1])

        hm = rules.hold_metrics(holdings, {pc.get("code"): pc.get("nav_series") for pc in product_ctxs},
                                nav_map)
        shares_map = {}
        costs_map = {}
        lots_map = {}
        for h in holdings:
            key = h.get("fund_code") or h.get("code")
            if not key or h.get("type") == "cash":
                continue
            shares_map[key] = float(h.get("shares") or 0)
            costs_map[key] = float(h.get("cost") or 0)
            lots_map[key] = h.get("lots") or [{"buy_date": h.get("buy_date", ""),
                                               "shares": h.get("shares", 0), "cost": h.get("cost", 0)}]

        # ---- 动态止盈 V2.2：信号判定（影子模式只记录） ----
        tp_ctx_map = {}
        product_sells = []
        for p in products:
            code = p.get("code", "")
            pc = pctx_map.get(code)
            if pc is None or pc.get("nav_series") is None or len(pc.get("nav_series")) == 0:
                continue
            hmk = hm.get(code)
            if not hmk:
                continue
            sig_ctx = {
                "code": code, "exposure": pc.get("equity_exposure"),
                "bench_pctile": pc.get("bench_pctile"),
                "r_hold": hmk.get("r_hold", 0.0), "r_hold_prev": hmk.get("r_hold_prev", 0.0),
                "hold_years": hmk.get("hold_years", 1.0),
                "lots": lots_map.get(code, []),
                "today": today,
                "fee_rate": 0.005,
                "hhi": pc.get("hhi"),
            }
            bench_ret = (ctx.get("bench_ret_63", {}) or {}).get(p.get("bench_index", ""))
            pc_r3m = pc.get("r3m")
            if pc_r3m is not None and bench_ret is not None:
                sig_ctx["excess_3m"] = pc_r3m - bench_ret
            fee_str = pc.get("fees", "")
            m = re.search(r"短期赎回([\d.]+)%", fee_str)
            if m:
                sig_ctx["fee_rate"] = float(m.group(1)) / 100.0
            act, detail, rid, trace = rules.take_profit_signal(cfg, pc, sig_ctx)
            if act:
                tp_ctx_map[code] = {"action": act, "detail": detail, "rid": rid, "trace": trace}
                state_store.record_trace(trace) if trace else None

        # ---- 订单簿（市值口径 + 关注池 + 余额约束 + 冷却/在途） ----
        cash_mv = sum(float(h.get("amount", 0)) for h in holdings if h.get("type") == "cash")
        orders, target_alloc, summary_lines, tp_actions = rules.build_order_book(
            cfg, products,
            {"equity_target": eq_target,
             "mvs": mv_map, "shares": shares_map, "costs": costs_map,
             "lots": lots_map,
             "cash_mv": cash_mv,
             "settled_cash": state_store.settled_cash(cfg),
             "pending_cash": state_store.pending_cash(cfg),
             "bench_pctile": {c: pctx_map[c].get("bench_pctile") for c in pctx_map},
             "exposure": {c: pctx_map[c].get("equity_exposure") for c in pctx_map},
             "product_name": {pc["code"]: pc.get("name", pc["code"]) for pc in product_ctxs},
             "tp_ctx": tp_ctx_map,
             "nav": nav_map,
             "total": total_mv},
            storm_active=storm_active, storm_reasons=storm_reasons, ep_lock=ep_lock,
            cooldown_active=cooldown_active, today=today)

        # ---- CRO 叙事 ----
        bond_codes = {p.get("code", "") for p in products if p.get("type") == "bond"}
        cro = narrative.CRO(narrative.CROInput(
            orders=orders, equity_target=eq_target, storm_active=storm_active,
            storm_reasons=storm_reasons, ep_lock=ep_lock,
            ep_pctile=ctx.get("ep_premium_pctile"),
            triggers=rule_triggers, product_sells=product_sells,
            tp_signals=tp_actions, cooldown_active=cooldown_active,
            bond_codes=bond_codes))
        storm_line = cro.get_storm_status_line()
        if storm_line:
            L(f"> {storm_line}")
        ep_line = cro.get_ep_status_line()
        if ep_line:
            L(f"> {ep_line}")

        # 影子止盈信号（仅记录）
        if tp_actions:
            L("")
            L("**止盈信号（影子模式·仅记录不执行）**")
            for code, a in tp_actions.items():
                L(f"- {code}：建议落袋约 {a['amount']:,.0f} 元")
                L(f"  原因：{a['reason']}")
            L("*止盈目标线算法处于 6 个月观察期，信号仅供跟踪与评估，暂不生成执行指令。*")

        if orders:
            holdings_buy = {h.get("fund_code") or h.get("code"): h.get("buy_date", "")
                            for h in holdings if h.get("type") != "cash"}
            for o in orders:
                fee_note = ""
                if o["side"] == "卖出":
                    fee_note = redemption_note(o["code"], holdings_buy.get(o["code"], ""),
                                               pctx_map.get(o["code"], {}).get("fees", ""))
                L(f"- {o['side']}：{o['code']} {o['name']}，{o['amount']:,.0f} 元{fee_note}")
                L(f"  原因：{o['reason']}")
                if o.get("settle_date"):
                    L(f"  资金时间线：今日提交 → 确认 {o.get('confirm_date','?')} → 预计到账 {o.get('settle_date','?')}")
        else:
            L(f"- 无操作，维持当前持仓（今日无任何执行指令）")
        L("")
        L(f"- 调仓后目标仓位：权益 {target_alloc.get('equity', 0)*100:.0f}% / 固收 {target_alloc.get('bond', 0)*100:.0f}% / 现金 {target_alloc.get('cash', 0)*100:.0f}%")
        for s in summary_lines:
            L(f"{s}")
        pending = state_store.pending_cash(cfg)
        if pending:
            L("")
            L("**在途资金提醒**")
            for pc_ in pending:
                L(f"- {pc_.get('code')} 赎回 {pc_.get('shares', 0)} 份，预计 {str(pc_.get('settle_date',''))[:10] or '待确认'} 到账（到账前不可用）")
        cro_narr = cro.get_narrative()
        if cro_narr:
            L("")
            L(f"> {cro_narr}")

        # ---- 新闻哨兵 ----
        try:
            entity_table = news_alert.build_entity_table(fetcher, products, cfg)
            alerts = news_alert.process_news(news_ctx, entity_table, orders)
        except Exception as e:
            log(f"[WARN] 新闻哨兵失败: {str(e)[:100]}")
            alerts = None
        if alerts:
            L("")
            L(f"## ⚠ 异常事件预警")
            for a in alerts:
                lvl = "一级警报" if a["level"] == 1 else f"二级警报（{a.get('note', '与今日纪律同向，印证决策方向')}）"
                L(f"**{lvl}**")
                L(f"- {a['content']}")
                L(f"  - 原始新闻（用于审计核实）：")
                orig = a.get("original", "")
                for i in range(0, len(orig), 80):
                    L(f"    {orig[i:i+80]}")
            L(f"*此为异常预警，不改变既定纪律指令。*")
        L("")
        L(f"- 执行窗口：今日 15:00 前提交；赎回确认 T+1，资金到账见各订单时间线；买入确认 T+1")
        ob = []
        for o in orders:
            ob.append(f"{o['side']} {o['code']} {o['name']} {o['amount']:,.0f}元（{o['reason']}）")
        if tp_actions:
            for code, a in tp_actions.items():
                ob.append(f"止盈信号 {code} {a['amount']:,.0f}元（影子）")
        ctx["order_book"] = "\n".join(ob) if ob else "无操作，维持当前持仓"
        ctx["target_alloc"] = f"权益 {target_alloc.get('equity',0)*100:.0f}% / 固收 {target_alloc.get('bond',0)*100:.0f}% / 现金 {target_alloc.get('cash',0)*100:.0f}%"
        bond_state = "今日按纪律买入债券（组合风险管理驱动，非看多债市）" if any(
            o.get("side") == "买入" and o.get("code") in bond_codes for o in orders) else (
            "今日卖出债券（减配固收）" if any(
                o.get("side") == "卖出" and o.get("code") in bond_codes for o in orders) else "今日无债券买卖指令")
        ctx["bond_order_state"] = bond_state
        ctx["rule_ids"] = sorted(set([t.get("id") for t in rule_triggers if t.get("id")] +
                                     [o.get("rule_id") for o in orders if o.get("rule_id")] +
                                     [v.get("rid") for v in tp_ctx_map.values() if v.get("rid")]))
        ctx["cro_headline"] = cro.get_headline()
        ctx["cro_narrative"] = cro_narr or ""
        ctx["storm_status_line"] = storm_line
        ctx["ep_status_line"] = ep_line
        ctx["storm_active"] = storm_active

        # ---- 组合诊断 + 前瞻模拟 ----
        diag = rules.portfolio_diagnostics(
            cfg, products, {"returns": returns_map, "weights": weights_map})
        if diag:
            L("")
            L(f"## 组合诊断")
            L(f"- 组合市值 ¥{total_mv:,.0f}（份额×最新净值）")
            L(f"- 年化波动率 {diag['vol']*100:.1f}%｜人话：一年里收益上蹿下跳的平均幅度")
            L(f"- 250日最大回撤 {diag['max_dd']*100:.1f}%｜人话：历史上从最高点最多跌过这么多")
            L(f"- 日度 VaR95 {diag['var95']*100:.2f}%（历史分位法）｜人话：按历史规律，一天最坏大概率不会亏超过这个数")
            total_exp = 0.0
            for p in products:
                fcode = p.get("code", "")
                if fcode in weights_map and fcode in pctx_map:
                    exp = pctx_map[fcode].get("equity_exposure") or 0.0
                    total_exp += weights_map[fcode] * exp
            L(f"- 组合实际权益暴露度 {total_exp*100:.1f}%｜人话：你的钱里有 {total_exp*100:.0f}% 实质押在股票上")
            if orders:
                after_map = dict(mv_map)
                after_cash = cash_mv
                for o in orders:
                    if o["side"] == "卖出":
                        after_map[o["code"]] = after_map.get(o["code"], 0) - o["amount"]
                        after_cash += o["amount"]
                    elif o["side"] == "买入":
                        after_map[o["code"]] = after_map.get(o["code"], 0) + o["amount"]
                        after_cash -= o["amount"]
                after_map["cash"] = after_cash
                sim = rules.portfolio_simulator(returns_map, after_map, total_mv)
                if sim:
                    L(f"- 调仓后前瞻预览（历史数据模拟）：年化波动率 {sim['vol']*100:.1f}%（vs 当前 {diag['vol']*100:.1f}%），"
                      f"250日最大回撤 {sim['max_dd']*100:.1f}%（vs 当前 {diag['max_dd']*100:.1f}%）")
            ctx["diagnostics"] = (f"波动率 {diag['vol']*100:.1f}%，最大回撤 {diag['max_dd']*100:.1f}%，"
                                  f"VaR95 {diag['var95']*100:.2f}%，实际权益暴露 {total_exp*100:.1f}%")

        # ---- 决策依据（规则 ID + 人话原理） ----
        L("")
        L(f"## 决策依据（今日触发的规则）")
        ep = ctx.get("ep", {})
        if ep:
            L(f"- 股债性价比：{ep.get('label', '')}")
        plain = cro.plain_basis(rule_triggers, orders)
        if plain:
            for ln in plain:
                L(f"- {ln}")
        elif rule_triggers:
            for tr in rule_triggers:
                L(f"- [{tr.get('id')}] {tr.get('text')}")
        else:
            L(f"- 无阶梯规则触发，权益维持目标区间")
        if storm_active:
            src_ids = [t["id"] for t in rule_triggers
                       if t.get("id") in {"LAD-CSI300-95", "LAD-CSI500-75", "MIN-MERGE"}]
            src_txt = " + ".join(f"[{i}]" for i in src_ids) if src_ids else "权益规则信号"
            L(f"- [{'/'.join(storm_reasons)}] 触发源：{src_txt} → 买入冻结")
        if tp_ctx_map:
            L(f"- 止盈信号（影子模式）：见上方「今日跟投指令」止盈信号区")
        db = []
        if ep:
            db.append("股债性价比：" + ep.get("label", ""))
        db += [f"[{t.get('id')}] {t.get('text')}" for t in rule_triggers]
        db += [f"[{v.get('rid')}] {v.get('detail')}" for v in tp_ctx_map.values()]
        ctx["decision_basis"] = "\n".join(db)

        # ---- 指标温度表（客户看板，独立板块） ----
        L("")
        L(f"## 指标温度表")
        L("\n".join(temp_lines) if temp_lines else "- 暂缺")

        L("")
        L(f"> {cro.get_separator()}")

    # ---------- 8. 执行回执区（云端状态人工维护提示） ----------
    L("")
    L("## 执行回执")
    L("- 执行后请告知：已执行 / 部分 / 未执行（用于记录下次冷却期与在途资金）")

    # ---------- 9. 术语速查 ----------
    L("")
    L("## 术语速查")
    for k, v in narrative.TERM_GLOSSARY.items():
        L(f"- **{k}**：{v}")

    L("")
    L(f"---")
    L(f"*本报告由本地程序按固定规则自动生成，仅供参考，不构成投资建议。*")
    L(f"*买卖请手动执行；指令基于 T-1 日净值估算，实际成交偏差通常在 ±0.3% 以内，以 App 确认值为准。*")

    report_full = "\n".join(lines)
    title = f"每日投顾报告 {today}"

    # ---------- 10. AI 解读层 ----------
    ctx["equity_band"] = "、".join(f"{x*100:.0f}%" for x in rules.target_band(cfg, eq_target)) + f"（以规则目标 {eq_target*100:.0f}% 为中心）"
    ctx["equity_target"] = f"{eq_target*100:.0f}%（规则引擎基准）" if total_mv > 0 else "未计算"
    kb_parts = []
    for p in products:
        kb = state_store.kb_product(p.get("code", ""))
        if kb:
            kb_parts.append(f"[{p.get('code')}] {kb}")
    ctx["kb_summary"] = "\n".join(kb_parts) if kb_parts else "暂无档案"
    if tp_actions:
        ctx["tp_signal_text"] = "；".join(f"{k} 建议落袋 {v['amount']:,.0f} 元（影子模式）" for k, v in tp_actions.items())
    else:
        ctx["tp_signal_text"] = "今日无止盈信号"

    insights, usage_info = llm.generate_insights(ctx)
    advice_value = None
    if insights:
        pnames = {p.get("code", ""): p.get("name", "") for p in products}
        report_full = llm.insert_insights(report_full, insights, pnames, ctx)
        log(f"AI 解读已生成（{usage_info}）")
        # 推荐日志：当日推荐追加（近一月次数统计的数据源，云端 commit 回写）
        rec_entries = []
        for r in insights.get("recommendations") or []:
            nm = str(r.get("industry") or r.get("product") or "").strip()
            rsn = str(r.get("reason") or "").strip()
            if nm and rsn:
                rec_entries.append({"name": nm, "type": "industry" if r.get("industry") else "product",
                                    "reason": rsn})
        state_store.kb_append_recommendations(rec_entries)
        adv = insights.get("equity_target_advice") or {}
        raw = adv.get("value")
        if raw is not None:
            try:
                raw = float(raw)
                if raw > 1:
                    raw = raw / 100.0
                if storm_active:
                    advice_value = None
                    log(f"市场预警期间 AI 目标微调已禁用（纪律优先），沿用规则基准 {eq_target*100:.0f}%")
                else:
                    advice_value = rules.clamp_equity_target(cfg, raw, eq_target)
            except Exception:
                advice_value = None
        if advice_value is not None:
            log(f"AI 权益目标建议 {raw*100:.0f}% → 校验通过（规则目标 ± 浮动带）→ 参考生效 {advice_value*100:.0f}%")
            report_full += (f"\n## AI 目标研判\n- AI 建议权益目标 {advice_value*100:.0f}%"
                            f"（规则目标 {eq_target*100:.0f}% ± 浮动带内，作为参考研判）\n"
                            f"- 理由：{str(adv.get('reason', ''))[:80]}\n"
                            f"*AI 研判仅供理解参考，实际指令以规则引擎为准。*")
        else:
            log(f"AI 权益目标建议未通过区间校验，沿用规则基准 {eq_target*100:.0f}%")
    else:
        log(f"AI 解读跳过: {usage_info}")
        if total_mv > 0:
            fallback = cro.get_narrative() if cro else "今日无规则触发，维持当前持仓。"
            report_full += f"\n## 今日指令解读\n> {fallback}"

    # ---------- 11. 今日一句话 + 指令摘要置顶 ----------
    if total_mv > 0 and cro is not None:
        top = [f"## 今日一句话", f"> {cro.get_headline()}", ""]
        top.append(cro.get_summary())
        report_full = "\n".join(top) + "\n\n" + report_full

    # ---------- 12. 输出：完整版 md + HTML + 精简版推送 ----------
    os.makedirs(REPORT_DIR, exist_ok=True)
    md_path = os.path.join(REPORT_DIR, f"report_{today}.md")
    html_path = os.path.join(REPORT_DIR, f"report_{today}.html")
    latest_html = os.path.join(REPORT_DIR, "latest.html")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n" + report_full)
    page = html_render.render(report_full, title)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(page)
    with open(latest_html, "w", encoding="utf-8") as f:
        f.write(page)
    log(f"完整版报告已生成: {md_path} / {html_path}")

    if not args.push_off:
        channel = cfg.get("push", {}).get("channel", "none")
        blocks = split_blocks(report_full)
        compact = build_compact(blocks, PAGE_URL, today)
        if channel == "wecom":
            push_wecom(cfg["push"]["wecom_webhook"], title, compact)
        elif channel == "serverchan":
            push_serverchan(cfg["push"]["serverchan_key"], title, compact)
        else:
            log("push.channel = none，未推送（本地报告已保存）")


def redemption_note(code, buy_date, fees_str):
    """赎回费纪律：持有天数 > 7 天免惩罚赎回费（等比例台账口径）"""
    fee = ""
    m = re.search(r"短期赎回([\d.]+)%", fees_str)
    if m:
        fee = f"{float(m.group(1)):g}%"
    if buy_date:
        try:
            days = (date.today() - datetime.strptime(str(buy_date)[:10], "%Y-%m-%d").date()).days
        except Exception:
            days = None
        if days is not None:
            if days > 7:
                return f"（持有约 {days} 天＞7 天，无惩罚赎回费）"
            return f"（持有约 {days} 天≤7 天，将收短期赎回费 {fee or '?'}，请确认后执行）"
    return f"（持有天数未录入；若持有≤7天将收短期赎回费 {fee or '?'}，请以 App 确认）"


TRADE_CAL_CACHE = None


def is_trading_day():
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


def push_serverchan(sendkey, title, content):
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    r = post_with_retry(url, {"title": title[:100], "desp": content})
    if r is not None:
        try:
            j = r.json()
            log(f"Server酱推送: {j.get('code')} {j.get('message', '')}")
        except Exception:
            log(f"Server酱推送: HTTP {r.status_code}")


def push_wecom(webhook, title, content):
    payload = {"msgtype": "markdown",
               "markdown": {"content": f"### {title}\n{content}"}}
    r = post_with_retry(webhook, payload)
    if r is not None:
        log(f"企微推送: HTTP {r.status_code}")


if __name__ == "__main__":
    main()
