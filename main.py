# -*- coding: utf-8 -*-
"""每日投顾报告生成器
数据采集 -> 规则决策 -> 本地报告 + 微信推送
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

import llm
import rules
import style

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

    def pe_history_full(self, symbol):
        """全历史 PE（用于口径敏感性提示）"""
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
        """备选源：新浪中债 10 年期国债收益率（英为财情失败时切换）"""
        df = self.ak.bond_gb_zh_sina(symbol="中国10年期国债")
        df = df[["date", "close"]].dropna()
        df["date"] = pd.to_datetime(df["date"])
        df = df.rename(columns={"close": "y10"})
        df["y2"] = float("nan")
        cutoff = pd.Timestamp(f"{datetime.now().year - PE_HIST_YEARS}-01-01")
        return df[df["date"] >= cutoff].reset_index(drop=True)

    def fund_stock_position(self, fund_code):
        """股票总仓位（天天基金官方季度资产配置，f_assetAllocation）"""
        try:
            r = self.session.get(f"https://fund.eastmoney.com/pingzhongdata/{fund_code}.js", timeout=20)
            m = re.search(r"var f_assetAllocation = (\[.*?\]);", r.text)
            if not m:
                return 0.0
            arr = json.loads(m.group(1))
            if not arr:
                return 0.0
            return float(arr[-1][1]) / 100.0
        except Exception:
            return 0.0

    def fund_bond_convertible(self, fund_code):
        """转债仓位占比（季度持仓，债券名称含'转债'）"""
        try:
            df = self.ak.fund_portfolio_bond_hold_em(symbol=fund_code, date=str(datetime.now().year))
            if df is None or df.empty or "占净值比例" not in df.columns:
                return 0.0
            mask = df["债券名称"].astype(str).str.contains("转债", na=False)
            return float(df.loc[mask, "占净值比例"].sum()) / 100.0
        except Exception:
            return 0.0

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


def fetch_once(fn, name):
    """单次尝试（用于主备双源的主源，失败立即切备选）"""
    try:
        return fn()
    except Exception as e:
        log(f"[WARN] {name} 失败({type(e).__name__})，切换备选源")
        return None


def _fee_num(v):
    try:
        return float(str(v).replace("%", "").strip())
    except Exception:
        return None


TYPE_LABELS = {"bond": "债券型", "money": "现金管理", "equity": "权益型", "gold": "黄金型", "other": "理财"}


def analyze_product(fetcher, p, cfg_ref):
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
            return ([f"\n**{code} {fname}**（现金管理）",
                     f"- 今年以来收益 {ytd_fmt}，同类排名 {rank}" + (f"，规模 {scale}" if scale else ""),
                     f"- 规则信号：持有（现金管理类产品，收益平稳）"], ctx)
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

    # ---- 穿透风险分类：权益暴露度 = 股票仓位 + 转债仓位×50% ----
    equity_exposure = None
    if ptype in ("bond", "equity", "gold"):
        try:
            stock_pos = fetch_section(lambda: fetcher.fund_stock_position(fcode), f"股票仓{fcode}")
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

    stop_line = rules.stop_loss_level(cfg_ref, equity_exposure)
    nav_series = nav.reset_index(drop=True)
    action, action_reason = rules.product_action(
        cfg_ref, p,
        {"nav_series": nav_series, "equity_exposure": equity_exposure,
         "nav_latest": float(n.iloc[-1])})

    dd_from_high = float(n.iloc[-1] / n.max() - 1)

    # 规则信号展示
    action_label = {"sell_all": "止损清仓", "sell_half": "止盈减半", "hold": "持有"}.get(action, "持有")
    if equity_exposure is not None:
        exp_tag = "权益型" if equity_exposure >= 0.8 else ("混合偏债" if equity_exposure >= 0.2 else ("含转债" if equity_exposure >= 0.05 else "纯债"))
        exp_str = f"权益暴露 {equity_exposure*100:.1f}%（{exp_tag}），止损线 {stop_line*100:.0f}%"
    else:
        exp_str = f"止损线 {stop_line*100:.0f}%"

    # 动态类型标签（反映真实风险敞口）
    if equity_exposure is not None:
        type_label = "权益型" if equity_exposure >= 0.8 else ("混合偏债型" if equity_exposure >= 0.2 else ("含转债" if equity_exposure >= 0.05 else "债券型"))
    else:
        type_label = TYPE_LABELS.get(ptype, ptype)

    signal = action_label
    reason = action_reason

    line1 = f"\n**{code} {fname}**（{type_label}）"
    line2 = f"- 净值 {nav_str}"
    line3 = f"- 区间收益："
    line4 = f"近1周 {pct(r1w)}"
    line5 = f"近1月 {pct(r1m)}"
    line6 = f"近3月 {pct(r3m)}"
    line7 = f"近6月 {pct(r6m)}"
    line8 = f"近1年 {pct(r1y)}"
    line9 = f"- 近1年最大回撤 {max_dd*100:.1f}%，距250日高点 {dd_from_high*100:.1f}%"
    line10 = "- " + "，".join(x for x in [f"同类排名 {ranking}" if ranking else "",
                                           f"规模 {scale}" if scale else "",
                                           f"成立 {inception}" if inception else "",
                                           f"经理 {manager}" if manager else ""] if x)
    line11 = "- " + "，".join(x for x in [f"费用 {fees_str}" if fees_str else "",
                                           f"平台 {p.get('platform','')}" if p.get("platform") else "",
                                           f"备注 {p.get('notes','')}" if p.get("notes") else ""] if x)
    line12 = f"- {exp_str}"
    line13 = f"- 规则指令：{signal}（{reason}）"

    ctx.update({
        "unavailable": False, "name": fname,
        "nav_latest": nav_str,
        "returns": f"近1周{pct(r1w)} 近1月{pct(r1m)} 近3月{pct(r3m)} 近6月{pct(r6m)} 近1年{pct(r1y)}",
        "max_dd": f"{max_dd*100:.1f}%",
        "ranking": ranking or "无",
        "scale": scale, "inception": inception, "manager": manager,
        "fees": fees_str or "无", "signal": f"{signal}（{reason}）",
        "equity_exposure": equity_exposure,
        "stop_line": stop_line,
        "action": action,
        "action_reason": action_reason,
        "nav_series": nav_series,
        "dd_from_high": dd_from_high,
    })
    # 区间收益细分行之间用空行分隔（markdown 普通行需空行才可靠换行）
    return ([line1, line2, line3, line4, "", line5, "", line6, "", line7, "", line8,
             line9, line10, line11, line12, line13], ctx)


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
    equity_detail = []
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
                pe_tag = "偏高" if r > 0.7 else ("中性" if r >= 0.3 else "低估")
                trend_tag = "站上" if last["close"] > ma120 else "跌破"
                equity_detail.append(
                    f"{name}：PE 分位 {r*100:.0f}%（{pe_tag}），{trend_tag}120日均线，20日动量 {m20:+.1%}，单项分 {score:+.1f}")
                # 策略引擎输入
                if name == "沪深300":
                    ctx["csi300_pe_pctile"] = float(r)
                    ctx["csi300_mom20"] = float(m20)
                elif name == "中证500":
                    ctx["csi500_pe_pctile"] = float(r)
                    ctx["csi500_mom20"] = float(m20)

    # 隔夜外盘（新浪源）
    us = fetch_section(fetcher.us_index, "标普500")
    if us is not None and len(us) >= 2:
        us_pct = us.iloc[-1]["pct"]
        L(f"- 隔夜标普500: {us.iloc[-1]['close']:.1f} {'🔴' if us_pct >= 0 else '🟢'}{us_pct:+.2f}%")
        idx_ctx["隔夜标普500"] = f"{us.iloc[-1]['close']:.1f}（{us_pct:+.2f}%）"
    ctx["indexes"] = idx_ctx

    # ---------- 2. 估值温度 ----------
    L("")
    L(f"## 估值温度 (近{PE_HIST_YEARS}年分位)")
    val_lines = []
    pe_full_cache = {}
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
        # 全历史口径敏感性（专家要求）
        pe_full = fetch_section(lambda s=name: fetcher.pe_history_full(s), f"{name}PE全史")
        sens = ""
        if pe_full is not None and not pe_full.empty:
            r_full = pct_rank(pe_full["pe"], last_pe)
            if r_full is not None:
                pe_full_cache[name] = pe_full
                sens = f"（全历史分位 {r_full*100:.0f}%）"
        val_lines.append(f"- {name}: PE {last_pe:.2f}，近{PE_HIST_YEARS}年分位 {r*100:.0f}%（{tag}）{sens}")
    # 全市场 PE（乐咕，上证）
    mpe = fetch_section(fetcher.market_pe, "全市场PE")
    if mpe is not None and not mpe.empty:
        last_row = mpe.iloc[-1]
        val_lines.append(f"- 全市场(上证): PE {last_row['平均市盈率']:.2f}")
    L("\n".join(val_lines))
    ctx["valuation"] = dict((l.split(":")[0].strip(), l.split(":", 1)[1].strip())
                            for l in val_lines if ":" in l)

    # 股债性价比：E/P(沪深300) − 10Y 国债，近5年分位
    ep_ctx = {}
    pe_full = pe_full_cache.get("沪深300")
    bond_for_ep = fetch_once(fetcher.bond_rates, "国债收益率(EP)")
    bond_ep_src = "英为"
    if bond_for_ep is None or bond_for_ep.empty:
        bond_for_ep = fetch_section(fetcher.bond_rates_cn, "中债收益率(EP)")
        bond_ep_src = "新浪中债"
    if pe_full is not None and not pe_full.empty and bond_for_ep is not None and not bond_for_ep.empty:
        try:
            ep_years = cfg.get("rules", {}).get("ep_premium_years", 5)
            ep_cutoff = pd.Timestamp(f"{datetime.now().year - ep_years}-01-01")
            pe_ep = pe_full[pe_full["date"] >= ep_cutoff]
            b_ep = bond_for_ep[bond_for_ep["date"] >= ep_cutoff]
            merged = pd.merge(pe_ep, b_ep[["date", "y10"]], on="date", how="inner").dropna()
            if len(merged) > 60:
                merged["ep"] = 1.0 / merged["pe"] - merged["y10"] / 100.0
                cur_ep = float(merged["ep"].iloc[-1])
                cur_y10 = float(merged["y10"].iloc[-1])
                ep_r = pct_rank(merged["ep"], cur_ep)
                ep_ctx = {"ep": cur_ep, "y10": cur_y10, "pctile": ep_r,
                          "label": f"盈利收益率 1/PE = {1.0/float(pe_full['pe'].iloc[-1])*100:.2f}% − 10年国债 {cur_y10:.2f}% = {cur_ep*100:+.2f}%，{ep_years}年分位 {ep_r*100:.0f}%"}
                ctx["ep_premium_pctile"] = float(ep_r)
        except Exception as e:
            log(f"[WARN] 股债性价比计算失败: {e}")
    ctx["ep"] = ep_ctx

    # 债券（双源：英为财情单次尝试 → 新浪中债备选）
    bond_ctx = {}
    L("")
    L(f"## 债市与利率")
    bond = fetch_once(fetcher.bond_rates, "国债收益率(英为)")
    bond_src = "英为财情"
    if bond is None or bond.empty:
        bond = fetch_section(fetcher.bond_rates_cn, "中债收益率")
        bond_src = "新浪中债"
    bond_sig = None
    if bond is not None and not bond.empty:
        y10 = bond.iloc[-1]["y10"]
        y2 = bond.iloc[-1]["y2"]
        r = pct_rank(bond["y10"], y10)
        spread = y10 - y2
        L(f"- 中国10年期国债: {y10:.2f}%，分位 {r*100:.0f}%（{bond_src}）")
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
    L("")
    L(f"## 黄金")
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
    L("")
    L(f"## 宏观与资金面")
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
        L("")
        L(f"## 市场要闻（财联社）")
        for _, row in news.head(6).iterrows():
            title = str(row["标题"]).strip()
            if not title or pd_isna(title):
                title = str(row["内容"])[:60]
            title = title.replace("【", "").replace("】", " ")
            if len(title) > 30:
                title = title[:30] + "…"
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
    returns_map = {}
    weights_map = {}
    if products:
        L("")
        L(f"## 理财产品跟踪")
        for p in products:
            plines, pctx = analyze_product(fetcher, p, cfg)
            for ln in plines:
                L(ln)
            product_ctxs.append(pctx)
            nav_s = pctx.get("nav_series")
            if nav_s is not None and len(nav_s) > 30:
                returns_map[p["code"]] = nav_s["nav"].pct_change().dropna()
    ctx["products"] = product_ctxs

    # ---------- 5. 组合检查 ----------
    L("")
    L(f"## 组合检查")
    holdings = cfg["holdings"]
    target = cfg["target"]
    total = sum(h["amount"] for h in holdings)
    actual = {}
    for h in holdings:
        t = h["type"]
        actual[t] = actual.get(t, 0) + h["amount"]
    classes = [("equity", "权益"), ("bond", "债券"), ("gold", "黄金"), ("cash", "现金")]
    rebal_lines = []
    mv_lines = []
    actions = []
    order_lines = []
    diag_lines = []
    orders = []
    summary_lines = []
    target_alloc = {}
    rule_triggers = []

    # ---- 今日跟投指令（规则引擎，确定性） ----
    L("")
    L(f"## 今日跟投指令")
    if total <= 0:
        L(f"- 标准组合持仓金额未填写，暂无法生成指令")
        L(f"- 请填写 holdings 金额后，此处将输出可执行的买卖清单")
    else:
        L(f"- 标准跟随组合（总资产 ¥{total:,.0f}，跟投份数 {cfg.get('portfolio', {}).get('follow_units', 1)}）")
        # 产品 ctx 索引
        pctx_map = {p.get("code", ""): p for p in product_ctxs}
        # 组合权重（用于诊断）
        for h in holdings:
            key = h.get("fund_code") or h.get("code")
            if key and key in returns_map and total > 0:
                weights_map[key] = h["amount"] / total
        # 权益目标仓位（最保守原则 + 股债性价比）
        eq_target, rule_triggers = rules.equity_target(cfg, ctx)
        # 持仓金额映射（按 fund_code）
        holdings_amt = {}
        nav_map = {}
        name_map = {}
        for h in holdings:
            key = h.get("fund_code") or h.get("code")
            if key:
                holdings_amt[key] = h["amount"]
                name_map[key] = h["name"]
        for p in products:
            fcode = p.get("fund_code", "")
            if fcode and fcode in pctx_map:
                ns = pctx_map[fcode].get("nav_series")
                if ns is not None and not ns.empty:
                    nav_map[fcode] = float(ns["nav"].iloc[-1])
        orders, target_alloc, summary_lines = rules.build_order_book(
            cfg, products,
            {"equity_target": eq_target, "holdings_amount": holdings_amt, "nav": nav_map,
             "product_name": name_map,
             "equity_exposure": {fcode: pctx_map[fcode].get("equity_exposure") for fcode in pctx_map},
             "product_ctx": {fcode: pctx_map[fcode] for fcode in pctx_map},
             "total": total})
        if orders:
            for o in orders:
                sh = f"，赎回 {o['shares']:,.2f} 份（估）" if o["side"] == "卖出" and o.get("shares") else ""
                L(f"- {o['side']}：{o['code']} {o['name']}，{o['amount']:,.0f} 元{sh}")
                L(f"  原因：{o['reason']}")
        else:
            L(f"- 无操作，维持当前持仓（今日无任何规则触发）")
        L("")
        L(f"- 调仓后目标仓位：权益 {target_alloc.get('equity', 0)*100:.0f}% / 固收 {target_alloc.get('bond', 0)*100:.0f}% / 现金 {target_alloc.get('cash', 0)*100:.0f}%")
        for s in summary_lines:
            L(f"{s}")
        L(f"- 执行窗口：今日 15:00 前；份额为估值（基于 T-1 净值），实际以基金公司确认份额为准")
        ob = []
        for o in orders:
            ob.append(f"{o['side']} {o['code']} {o['name']} {o['amount']:,.0f}元（{o['reason']}）")
        ctx["order_book"] = "\n".join(ob) if ob else "无操作，维持当前持仓"
        ctx["target_alloc"] = f"权益 {target_alloc.get('equity',0)*100:.0f}% / 固收 {target_alloc.get('bond',0)*100:.0f}% / 现金 {target_alloc.get('cash',0)*100:.0f}%"

        # ---- 组合诊断 ----
        diag = rules.portfolio_diagnostics(
            cfg, products,
            {"returns": returns_map, "weights": weights_map})
        if diag:
            L("")
            L(f"## 组合诊断")
            L(f"- 年化波动率 {diag['vol']*100:.1f}%")
            L(f"- 250日最大回撤 {diag['max_dd']*100:.1f}%")
            L(f"- 日度 VaR95 {diag['var95']*100:.2f}%（历史分位法）")
            # 实际权益暴露度（转债按 50% 折算）
            total_exp = 0.0
            for p in products:
                fcode = p.get("fund_code", "")
                if fcode in weights_map and fcode in pctx_map:
                    exp = pctx_map[fcode].get("equity_exposure") or 0.0
                    total_exp += weights_map[fcode] * exp
            L(f"- 经转债折算后，组合实际权益暴露度 {total_exp*100:.1f}%")
            ctx["diagnostics"] = (f"波动率 {diag['vol']*100:.1f}%，最大回撤 {diag['max_dd']*100:.1f}%，"
                                  f"VaR95 {diag['var95']*100:.2f}%，实际权益暴露 {total_exp*100:.1f}%")

        # ---- 决策依据（规则触发明细） ----
        L("")
        L(f"## 决策依据（今日触发的规则）")
        ep = ctx.get("ep", {})
        if ep:
            L(f"- 股债性价比：{ep.get('label', '')}")
        if rule_triggers:
            for tr in rule_triggers:
                L(f"- {tr}")
        else:
            L(f"- 无阶梯规则触发，权益维持目标 {target.get('equity', 0.4)*100:.0f}%")
        L(f"- 止损/止盈状态见理财产品跟踪的规则指令行")
        ctx["decision_basis"] = "\n".join((["股债性价比：" + ep.get("label", "")] if ep else []) + rule_triggers)

    # 组合检查明细（原再平衡逻辑保留为仓位展示）
    if total > 0:
        L("")
        L(f"## 组合检查")
        L(f"- 总资产: ¥{total:,.0f}")
        for t, label in classes:
            if t in actual:
                L(f"- {label}: ¥{actual[t]:,.0f}（{actual[t]/total*100:.0f}%）")

    # ---------- 6. 操作建议（AI 解读指令区，规则已在上方输出） ----------
    L("")
    L(f"## 今日操作建议（解读）")
    eq = None
    if equity_stances:
        avg = sum(equity_stances) / len(equity_stances)
        eq = stance_label(avg)
        L(f"- 权益：{eq}（综合分 {avg:+.1f}）")
        for d in equity_detail:
            L(f"  - {d}")
    if bond_sig is not None:
        bs = stance_label(bond_sig)
        L(f"- 债券：{bs}")
        if bond is not None and not bond.empty:
            L(f"  - 10年国债收益率 {y10:.2f}%，十年分位 {r*100:.0f}%"
              + ("（收益率极低，债价偏贵）" if r < 0.3 else ("（收益率较高，债价便宜）" if r > 0.7 else "（收益率中性）")))
            L(f"  - 期限利差 {spread:+.2f}%" + ("（倒挂，警惕）" if spread < 0 else ""))
    if gold_sig is not None:
        L(f"- 黄金：{stance_label(gold_sig)}（卫星仓，维持 {cfg['target'].get('gold', 0.1)*100:.0f}% 以内）")
        if gold is not None and not gold.empty:
            L(f"  - {trend}趋势，20日动量 {m20:+.1%}" + (" → 暂不加仓" if trend == "空头" else " → 可持有"))
    if not orders and (not equity_stances or abs(sum(equity_stances)/len(equity_stances)) < 0.3):
        L(f"- 今日无规则触发 → 唯一操作就是不操作")

    ctx["signal_equity"] = eq or "无数据"
    ctx["rebalance"] = "\n".join(rebal_lines) if rebal_lines else "无"
    ctx["holdings_perf"] = "\n".join(mv_lines) if mv_lines else "无"

    L("")
    L(f"---")
    L(f"*本报告由本地程序按固定规则自动生成，仅供参考，不构成投资建议。*")
    L(f"*买卖请手动执行；指令基于 T-1 日净值估算，实际成交偏差通常在 ±0.3% 以内，以 App 确认值为准。*")

    report = "\n".join(lines)
    title = f"每日投顾报告 {today}"

    # ---------- 6.5 AI 解读层（LLM 不可用时自动降级为纯静态报告） ----------
    insights, usage_info = llm.generate_insights(ctx)
    if insights:
        pnames = {p.get("code", ""): p.get("name", "") for p in products}
        report = llm.insert_insights(report, insights, pnames)
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
    # 银行风样式（HTML）仅用于 Server酱渠道
    styled = style.style_report(md)
    last_err = None
    for attempt in range(1, PUSH_RETRY + 1):
        try:
            r = requests.post(f"https://sctapi.ftqq.com/{key}.send",
                              data={"title": title, "desp": styled}, timeout=15)
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
