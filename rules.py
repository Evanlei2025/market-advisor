# -*- coding: utf-8 -*-
"""确定性策略引擎：今日跟投指令的生成者。
输入必须是数据，输出必须是确定的仓位操作。AI 无权修改本模块的输出。
V2.3：市值口径算账、动态止盈（上限门+峰值回撤保护+短仓地板+费后口径+单向穿越取最高档）、
买入冻结（市场预警，仅最保守仓位档）、关注池买入（含零持仓）、冷却期、在途资金。
5.x：创业板估值信号(LAD-CYB-90)、EP安全阀动态阈值(5.4)、止盈边界完善(5.5)、
组合基准对比(5.1)、买入候选评分(5.2)、配置静态校验(5.6)。
V3：买入候选评分重构（P0 止血：rf 链/σ下限/平滑检测/完整度门槛；P1 结构重构：
同类分组 + 组内百分位 + 新权重 边际贡献20/TCO18/风格中性超额17/尾部风险15/稳定性15/可交易性15；
P2 贝叶斯收缩）、复权列契约 ret_col（main.py 自动接管）、候选硬门槛
（成立<18月/暂停申购|限大额/规模<2亿 于 cands 过滤处剔除）、换手抑制（held +5 分缓冲）。
"""
import math

# ---- 规则 ID 体系（AdvisorGatekeeper 白名单同源；对外人话解释见 narrative） ----
RULE_IDS = {
    "TP-YIELD-1": "止盈-首档卖1/3",
    "TP-YIELD-2": "止盈-次档再1/3",
    "TP-YIELD-3": "止盈-末档卖剩余",
    "TP-DD": "止盈-回撤保护清仓",
    "LAD-CSI300-80": "沪深300阶梯上限30%",
    "LAD-CSI300-90": "沪深300阶梯上限20%",
    "LAD-CSI300-95": "沪深300阶梯上限5%",
    "LAD-CSI500-75": "中证500阶梯上限5%",
    "LAD-CYB-90": "创业板指阶梯上限5%",
    "MIN-MERGE": "最保守合并",
    "EP-CAP-10": "EP安全阀(权益≤动态阈值)",
    "STORM-5": "市场预警-最保守仓位5%",
    "REB-EQ": "再平衡-买入权益",
    "REB-BOND": "再平衡-买入固收",
    "BUY-NEW": "关注池-买入",
}


def _trig(rid, text):
    return {"id": rid, "text": text}


# ---------------- 权益目标（阶梯 + EP + 动态区间） ----------------
def ep_threshold(ctx):
    """EP 安全阀动态阈值（5.4）：10Y 国债收益率十年分位越低 → 股债性价比越极端时越保守。
    y10_pctile < 0.2 → 0.15；0.2-0.8 → 0.10；> 0.8 → 0.05；缺失/NaN/非法 → 默认 0.10。"""
    ctx = ctx if isinstance(ctx, dict) else {}
    y10 = ctx.get("y10_pctile")
    try:
        y10 = float(y10)
    except Exception:
        return 0.10
    if y10 != y10:      # NaN 防护
        return 0.10
    if y10 < 0.2:
        return 0.15
    if y10 > 0.8:
        return 0.05
    return 0.10


def equity_target(cfg, ctx):
    """计算今日权益目标（最保守原则 + 股债性价比锁定 + 创业板估值信号）。
    返回 (target, triggers)；target 为区间基准值，由调用方套 target.band 形成区间。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    ctx = ctx if isinstance(ctx, dict) else {}
    base = cfg.get("target", {}).get("equity", {})
    base_val = base.get("base", 0.40) if isinstance(base, dict) else base
    triggers = []
    candidates = []

    p300 = ctx.get("csi300_pe_pctile")
    if p300 is not None:
        m300 = ctx.get("csi300_mom20") or 0.0
        level = None
        rid = None
        if p300 >= 0.95:
            level, rid = 0.05, "LAD-CSI300-95"
        elif p300 >= 0.90:
            level, rid = 0.20, "LAD-CSI300-90"
        elif p300 >= 0.80 and m300 < 0:
            level, rid = 0.30, "LAD-CSI300-80"
        if level is not None:
            candidates.append(level)
            triggers.append(_trig(rid, f"沪深300 PE分位 {p300*100:.0f}%（动量{m300:+.1%}）→ 权益上限 {level*100:.0f}%"))

    p500 = ctx.get("csi500_pe_pctile")
    if p500 is not None:
        m500 = ctx.get("csi500_mom20") or 0.0
        if p500 >= 0.75 and m500 < -0.05:
            candidates.append(0.05)
            triggers.append(_trig("LAD-CSI500-75",
                                  f"中证500 PE分位 {p500*100:.0f}% 且 20日动量 {m500:+.1%} < -5% → 权益上限 5%"))

    # 5.3 创业板指估值信号：PE 十年分位 ≥ 90% → 权益上限 5%（独立于 STORM-5，不参与买入冻结）
    pcyb = ctx.get("csi_cyb_pe_pctile")
    if pcyb is not None and pcyb >= 0.90:
        candidates.append(0.05)
        triggers.append(_trig("LAD-CYB-90",
                              f"创业板指 PE分位 {pcyb*100:.0f}% ≥ 90% → 权益上限 5%"))

    target = min(candidates) if candidates else base_val
    if candidates:
        triggers.append(_trig("MIN-MERGE", f"最保守原则：最终权益目标 = min(各规则值) = {target*100:.0f}%"))

    # 5.4 EP 安全阀动态阈值：按 10Y 国债分位取 0.15/0.10/0.05（规则 ID 不变，仅阈值与文案动态）
    ep = ctx.get("ep_premium_pctile")
    if ep is not None:
        ep_thr = ep_threshold(ctx)
        if ep < ep_thr:
            target = min(target, ep_thr)
            triggers.append(_trig("EP-CAP-10",
                                  f"股债性价比极端约束：分位 {ep*100:.0f}% < {ep_thr*100:.0f}% → 已激活，权益目标强制 ≤ {ep_thr*100:.0f}%"))
        else:
            triggers.append(_trig("EP-CAP-10",
                                  f"股债性价比极端约束：分位 {ep*100:.0f}% ≥ {ep_thr*100:.0f}% → 不触发权益仓位上限锁定（安全阀未激活）"))
    return target, triggers


def target_band(cfg, center=None):
    """权益目标浮动带（±band pp），供 AI 微调。以规则目标（center）为中心；
    无 center 时以配置 base 为中心。返回 (lo, hi)"""
    t = cfg.get("target", {}).get("equity", {})
    base = t.get("base", 0.40) if isinstance(t, dict) else 0.40
    band = t.get("band", 0.05) if isinstance(t, dict) else 0.05
    c = center if center is not None else base
    return max(0.0, c - band), min(1.0, c + band)


def clamp_equity_target(cfg, value, center=None):
    """AI 建议的权益目标必须落在浮动带内（Gatekeeper 同源校验）"""
    lo, hi = target_band(cfg, center)
    try:
        v = float(value)
    except Exception:
        v = None
    if v is None or v != v:
        return None
    if v < lo or v > hi:
        return None
    return v


def storm_lock(eq_target, triggers):
    """市场预警（买入冻结）前置判定（V2.2：仅最保守仓位档）。
    触发：权益目标 == 5% 且由阶梯信号（CSI300≥95% 或 CSI500≥75%+动量）导致。
    返回 (active: bool, reasons: list[str])；EP 压制到 10% 天然不会等于 5%，明确排除。
    """
    reasons = []
    ids = {t.get("id") for t in triggers}
    if eq_target == 0.05 and (ids & {"LAD-CSI300-95", "LAD-CSI500-75"}):
        reasons.append("STORM-5")
    return bool(reasons), reasons


# ---------------- 动态止盈算法 V2.2 ----------------
def _equity_class(exposure):
    if exposure is None:
        return "equity"
    if exposure >= 0.8:
        return "equity"
    if exposure >= 0.2:
        return "mixed"
    return "bond"


def _sigma_ref(cfg, exposure):
    """σ_ref 按权益暴露度线性插值（0.02 ↔ 0.08 ↔ 0.20），消除档位跳变。
    数据缺失/NaN → 权益中心 0.20（保守：波动中心越高 → 目标线越低）"""
    if exposure is None or exposure != exposure:
        return cfg.get("rules", {}).get("take_profit", {}).get("vol_ref", {}).get("equity", 0.20)
    exposure = max(0.0, min(1.0, exposure))
    vr = cfg.get("rules", {}).get("take_profit", {}).get("vol_ref", {})
    v_eq, v_mix, v_bd = vr.get("equity", 0.20), vr.get("mixed", 0.08), vr.get("bond", 0.02)
    if exposure >= 0.8:
        return v_eq
    if exposure <= 0.2:
        return v_mix + (v_bd - v_mix) * (0.2 - exposure) / 0.2 if exposure < 0.2 else v_mix
    # 0.2~0.8 线性插值 mixed→equity
    return v_mix + (v_eq - v_mix) * (exposure - 0.2) / 0.6


def ret_col(df):
    """复权收益列（A7 契约）：df 含 nav_adj 列 → 用它（分红再投资口径），否则回退 nav。
    main.py 的 _ret_col_local 会自动接管本函数；Series 输入原样返回；异常 → None。"""
    try:
        if df is None:
            return None
        if hasattr(df, "columns"):
            if "nav_adj" in df.columns:
                return df["nav_adj"]
            return df["nav"]
        return df
    except Exception:
        return None


def ewma_vol(nav_series, lam=0.94):
    """EWMA(λ=0.94) 日收益年化波动率（复权口径 ret_col；nav 无 nav_adj 列时行为不变）。
    数据不足返回 None。公式本身不许动——F_vol 因子消费本函数，口径变化影响 T_eff 数值属预期"""
    import pandas as pd
    try:
        if nav_series is None or len(nav_series) < 30:
            return None
        rets = ret_col(nav_series).pct_change().dropna()
        if len(rets) < 20:
            return None
        var = rets.iloc[0] ** 2
        for r in rets.iloc[1:]:
            var = lam * var + (1 - lam) * r * r
        return float(math.sqrt(var) * math.sqrt(250))
    except Exception:
        return None


def _fee_rate_of(cfg, pctx, code):
    """该产品当前赎回费率（首档），解析失败用保守 0.5%"""
    try:
        fees = (pctx.get("fees") or "")
        import re
        m = re.search(r"短期赎回([\d.]+)%", fees)
        if m:
            return float(m.group(1)) / 100.0
    except Exception:
        pass
    return 0.005


def compute_take_profit(cfg, pctx, ctx):
    """计算动态止盈目标线 T_eff（V2.2 全因子）。
    ctx: {"hold_years", "r_hold", "sigma_ann", "bench_pctile", "exposure"}
    返回 dict {class, sigma_ann, F_vol, F_hold, F_sector, T_base, T_pre, T_cap, T_eff,
               peak_r_hold, dd_ret, dd_thr} 或 None（数据不足）
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    ctx = ctx if isinstance(ctx, dict) else {}
    if not isinstance(pctx, dict):
        return None
    tp = (cfg.get("rules") or {}).get("take_profit") or {}
    nav_series = pctx.get("nav_series")
    if nav_series is None or len(nav_series) < 30:
        return None

    exposure = ctx.get("exposure")
    if exposure is None or exposure != exposure:  # NaN 防护
        exposure = None
    cls = _equity_class(exposure)
    sigma = ctx.get("sigma_ann") or ewma_vol(nav_series)
    if sigma is None or sigma != sigma:
        sigma = 0.20 if cls == "equity" else (0.08 if cls == "mixed" else 0.02)

    vref = _sigma_ref(cfg, exposure)
    F_vol = max(0.75, min(1.35, sigma / vref))

    hold_years = ctx.get("hold_years", 1.0)
    if hold_years is None or hold_years != hold_years or hold_years <= 0:
        hold_years = 1.0
    r_hold = ctx.get("r_hold", 0.0)
    if r_hold is None or r_hold != r_hold:
        r_hold = 0.0
    ht = tp.get("hold_factor", {})
    per_year = ht.get("per_year", 0.15)
    max_years = ht.get("max_years", 3.0)
    trans = tp.get("hold_transition", 0.02)
    if r_hold <= -trans:
        F_hold = 1.0
    elif r_hold >= trans:
        F_hold = 1.0 + per_year * min(hold_years, max_years)
    else:
        F_hold = 1.0 + per_year * min(hold_years, max_years) * (r_hold + trans) / (2 * trans)

    F_sector = 1.0
    try:
        excess = ctx.get("excess_3m")   # 基金近3月收益 − 基准指数近3月收益（领先指标）
        hhi = ctx.get("hhi")            # 行业集中度（0~1，前三大行业权重平方和）
        if excess is not None and excess != excess:  # NaN
            excess = None
        if excess is not None:
            # 持仓越集中（HHI 高）→ 超额收益越由行业景气驱动 → 景气权重越大
            w_sector = 0.5 + 0.5 * (hhi if hhi is not None and hhi == hhi else 0.5)
            boom = max(-0.5, min(0.5, excess))        # 超额收益限幅 ±50%
            F_sector = max(0.9, min(1.15, 1.0 + 0.2 * w_sector * (boom / 0.5)))
    except Exception:
        F_sector = 1.0

    base = tp.get("base", {})
    T_base = base.get(cls, 0.18 if cls == "equity" else (0.12 if cls == "mixed" else 0.08))

    clamp_lo = tp.get("clamp", {}).get("lo", 0.06)
    clamp_hi = tp.get("clamp", {}).get("hi", 0.30)
    T_pre = max(clamp_lo, min(clamp_hi, T_base * F_vol * F_hold * F_sector))

    # 上限门（一票否决）：v_bench ≥ 70% 起线性压缩；纯债（exposure→0）豁免
    cap = tp.get("cap", {})
    v_bench = ctx.get("bench_pctile")
    if v_bench is not None and v_bench != v_bench:  # NaN 防护
        v_bench = None
    T_cap = clamp_hi
    if v_bench is not None:
        w_bench = max(0.0, min(1.0, exposure if exposure is not None else 0.0))
        start = cap.get("start_pctile", 0.7)
        max_reduce = cap.get("max_reduce", 0.27)
        if v_bench > start:
            T_cap = clamp_hi - max_reduce * (v_bench - start) / (1.0 - start) * w_bench
    T_eff = max(clamp_lo, min(T_pre, T_cap))

    # 峰值收益与收益回撤（回撤保护前置条件）
    # r_hold(t) = shares×nav(t)/cost − 1，份额固定时：峰值收益 = (1+r_hold)×(peak_nav/cur_nav) − 1
    peak_r = ctx.get("r_hold")
    dd_ret = 0.0
    try:
        cur_nav = float(nav_series["nav"].iloc[-1])
        peak_nav = float(nav_series["nav"].max())
        if cur_nav > 0 and peak_nav > 0:
            peak_r = (1 + ctx.get("r_hold", 0.0)) * (peak_nav / cur_nav) - 1
            dd_ret = peak_nav / cur_nav - 1
    except Exception:
        pass
    dd_thr = max(tp.get("dd_clamp", {}).get("lo", 0.04),
                 min(tp.get("dd_clamp", {}).get("hi", 0.12),
                     tp.get("dd_factor", 0.35) * sigma))
    return {
        "class": cls, "sigma_ann": sigma, "F_vol": F_vol, "F_hold": F_hold,
        "F_sector": F_sector, "T_base": T_base, "T_pre": T_pre, "T_cap": T_cap,
        "T_eff": T_eff, "peak_r_hold": peak_r, "dd_ret": dd_ret, "dd_thr": dd_thr,
        "r_hold": r_hold,
    }


def _lot_adjusted(cfg, lots, today):
    """剔除持有 < min_hold_days 的笔；返回 (可卖 lots, 豁免笔份额)"""
    tp = cfg.get("rules", {}).get("take_profit", {})
    min_days = int(tp.get("min_hold_days", 7))
    from datetime import datetime
    saleable, exempt = [], []
    for lot in lots or []:
        bd = str(lot.get("buy_date", ""))[:10]
        try:
            days = (datetime.strptime(str(today), "%Y-%m-%d").date()
                    - datetime.strptime(bd, "%Y-%m-%d").date()).days
        except Exception:
            days = 9999
        if days < min_days:
            exempt.append(lot)
        else:
            saleable.append(lot)
    return saleable, exempt


def min_hold_days(cfg):
    return int(cfg.get("rules", {}).get("take_profit", {}).get("min_hold_days", 7))


def take_profit_signal(cfg, pctx, ctx):
    """止盈触发判定（V2.2）：返回 (action, detail, rule_id, trace) 或 (None, ...)（无信号）
    ctx: {"r_hold", "r_hold_prev", "hold_years", "sigma_ann", "bench_pctile",
          "exposure", "lots", "today", "fee_rate"}
    action: "tp_1of3"/"tp_2of3"/"tp_rest"/"tp_dd"/None
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    ctx = ctx if isinstance(ctx, dict) else {}
    tp = (cfg.get("rules") or {}).get("take_profit") or {}
    st = compute_take_profit(cfg, pctx, ctx)
    if st is None:
        return None, "", None, None

    r_hold = ctx.get("r_hold", 0.0)
    if r_hold is None or r_hold != r_hold:
        r_hold = 0.0
    # 昨日收益缺失视为远低于任何档位（首日建仓即达标也可触发）
    r_prev = ctx.get("r_hold_prev")
    if r_prev is None or r_prev != r_prev:
        r_prev = -1.0
    hold_years = ctx.get("hold_years", 1.0)
    if hold_years is None or hold_years != hold_years or hold_years <= 0:
        hold_years = 1.0
    fee = ctx.get("fee_rate", 0.005)
    if fee is None or fee != fee:
        fee = 0.005
    margin = tp.get("fee_margin", 0.01)
    # 止盈档间距（5.5b）：config 显式 tier_gap 优先（向后兼容）；否则按年化波动率动态分档
    gap_cfg = tp.get("tier_gap")
    if isinstance(gap_cfg, list) and len(gap_cfg) >= 2:
        gap = list(gap_cfg)
    else:
        s_sigma = st.get("sigma_ann")
        if s_sigma is None or s_sigma != s_sigma:
            gap = [0.05, 0.10]
        elif s_sigma < 0.15:
            gap = [0.05, 0.10]
        elif s_sigma < 0.30:
            gap = [0.07, 0.14]
        else:
            gap = [0.10, 0.20]

    # 门槛：≤1 年按持有年数折算（带 6% 地板）；>1 年按年化
    if hold_years <= 1.0:
        def level_for(tev):
            return max(tev * hold_years, 0.06)
        r_now, r_yest = r_hold, r_prev
    else:
        def level_for(tev):
            return max(tev, 0.06)
        base_now = max(r_hold, -0.99)   # 年化保护：r_hold ≤ −1 时负底数分数幂崩溃
        r_now = (1 + base_now) ** (1.0 / hold_years) - 1
        base_prev = max(r_prev, -0.99)
        r_yest = (1 + base_prev) ** (1.0 / hold_years) - 1

    lv1 = level_for(st["T_eff"]) + fee + margin
    lv2 = level_for(st["T_eff"] + gap[0]) + fee + margin
    lv3 = level_for(st["T_eff"] + gap[1]) + fee + margin

    # 回撤保护（5.5a）：峰值收益曾 ≥ T_eff 且自高点回撤 ≥ DD_thr 即触发；
    # 不再要求当前仍盈利——从大幅盈利直接跌穿成本线的场景同样进入保护
    peak_r = st["peak_r_hold"]
    dd_hit = (peak_r is not None and peak_r >= st["T_eff"] and st["dd_ret"] >= st["dd_thr"])

    # 单向穿越检测（基于今日 vs 昨日；等值也视为穿越，消除漏检）
    # 三档各自独立穿越判定，取穿越的最高档 → 跳档自然取最高档，且无持续性重复触发
    def crossed(level):
        return r_yest <= level and r_now >= level

    action = None
    detail = ""
    rid = None
    if dd_hit:
        action, rid = "tp_dd", "TP-DD"
        if r_hold <= 0:
            detail = (f"回撤保护：当前虽已转亏/微利（持有收益 {r_hold*100:.1f}%），"
                      f"但峰值曾达 {peak_r*100:.1f}% ≥ 目标线 {st['T_eff']*100:.1f}%，"
                      f"自高点回撤 {st['dd_ret']*100:.1f}% ≥ {st['dd_thr']*100:.1f}% → 建议清仓")
        else:
            detail = (f"回撤保护：持有收益 {r_hold*100:.1f}%（峰值 {peak_r*100:.1f}% ≥ 目标线 {st['T_eff']*100:.1f}%），"
                      f"自高点回撤 {st['dd_ret']*100:.1f}% ≥ {st['dd_thr']*100:.1f}% → 建议清仓")
    else:
        hit = []
        if crossed(lv1):
            hit.append(("tp_1of3", "TP-YIELD-1",
                        f"首档止盈：持有收益达 {r_now*100:.1f}% ≥ {lv1*100:.1f}%（目标线 {st['T_eff']*100:.1f}%，含费后安全边际）→ 建议卖出 1/3"))
        if crossed(lv2):
            hit.append(("tp_2of3", "TP-YIELD-2",
                        f"次档止盈：持有收益达 {r_now*100:.1f}% ≥ {lv2*100:.1f}%（目标线 {st['T_eff']*100:.1f}%+{gap[0]*100:.0f}%）→ 建议再卖 1/3"))
        if crossed(lv3):
            hit.append(("tp_rest", "TP-YIELD-3",
                        f"末档止盈：持有收益达 {r_now*100:.1f}% ≥ {lv3*100:.1f}%（目标线 {st['T_eff']*100:.1f}%+{gap[1]*100:.0f}%）→ 建议卖出剩余"))
        if hit:
            action, rid, detail = hit[-1]

    trace = None
    if action:
        trace = {
            "T_eff": round(st["T_eff"], 4), "T_pre": round(st["T_pre"], 4),
            "T_cap": round(st["T_cap"], 4),
            "v_bench": ctx.get("bench_pctile"), "factors": {k: round(st[k], 4)
                                                            for k in ("F_vol", "F_hold", "F_sector")},
            "signal_date": str(ctx.get("today", "")), "execute_date": "",
            "fee_net_return": round(r_hold - fee, 4), "code": ctx.get("code", ""),
            "action": action, "shadow": bool(tp.get("shadow_mode", True)),
        }
    return action, detail, rid, trace


# ---------------- 订单簿（市值口径 + 关注池 + 余额约束 + 在途/冷却） ----------------
def add_trading_days(start_date, n):
    """日期 + n 个交易日（akshare 日历；失败按自然日近似）。"""
    from datetime import timedelta
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        cal = sorted(df["trade_date"].astype(str))
        if str(start_date) not in cal:
            import bisect
            idx = bisect.bisect_left(cal, str(start_date))
        else:
            idx = cal.index(str(start_date))
        idx += n
        if 0 <= idx < len(cal):
            return cal[idx]
    except Exception:
        pass
    try:
        base = date.fromisoformat(str(start_date)[:10])
        return (base + timedelta(days=int(n * 1.5))).isoformat()
    except Exception:
        return str(start_date)


def settlement_of(cfg, product):
    """产品结算参数：per_product > per_platform > default"""
    st = cfg.get("settlement", {})
    code = product.get("code", "")
    spec = (st.get("per_product", {}) or {}).get(code)
    if not spec:
        spec = (st.get("per_platform", {}) or {}).get(product.get("platform", ""))
    if not spec:
        spec = st.get("default", {})
    return spec


def _piecewise(x, pts):
    """分段线性插值（单调节点升序列表）。"""
    x = float(x)
    if x <= pts[0][0]:
        return float(pts[0][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return float(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
    return float(pts[-1][1])


# ---------------- 买入候选评分 V3（P0 止血 + P1 结构重构 + P2 贝叶斯收缩） ----------------
_CAND_DIMS = [
    ('marginal', 20, '边际贡献'),
    ('tco', 18, 'TCO'),
    ('alpha', 17, '风格中性超额'),
    ('tail', 15, '尾部风险'),
    ('stability', 15, '运作稳定性'),
    ('tradability', 15, '可交易性'),
]
_THEME_WORDS = ('半导体', '医药', '医疗', '消费', '红利', '煤炭', '军工', '新能源',
                '港股', '白酒', '科技', '银行', '证券', '地产', '汽车', '有色',
                '化工', '农业', '传媒', '通信', '电子', '光伏', '碳中和', '环保',
                '食品', '保险', '计算机', '芯片', '科创', '中概', '高端装备', '智能')
_candidate_pool = []      # 本轮候选 pctx 列表（build_order_book 过滤后设置；用后清理）
_candidate_others = []    # 组合内其他产品 pctx 列表（边际贡献用）
_candidate_monitor = []   # B2 评分监控条目；emit 后清空，仅观测不参与打分


def candidate_weights():
    """V3 权重表（=100）：marginal20 / tco18 / alpha17 / tail15 / stability15 / tradability15"""
    return {d: w for d, w, _ in _CAND_DIMS}


def _set_candidate_pool(pool, others=None):
    """设置本轮候选池（build_order_book 在 cands 排序前调用；score_candidate 从中取同组样本。）
    pool: list[pctx]（已过滤候选）；others: list[pctx]（组合其他产品，边际贡献用）。
    模块级全局（单线程主流程；调用方须在结束后调用 _clear_candidate_pool）。"""
    global _candidate_pool, _candidate_others, _candidate_monitor
    _candidate_pool = [p for p in (pool or []) if isinstance(p, dict)]
    _candidate_others = [p for p in (others or []) if isinstance(p, dict)]
    _candidate_monitor = []   # 新评分会话：清空上一轮监控条目
def _clear_candidate_pool():
    global _candidate_pool, _candidate_others, _candidate_monitor
    _candidate_pool = []
    _candidate_others = []
    _candidate_monitor = []


def _emit_candidate_monitor(pool_n):
    """B2 评分监控埋点：pool 内各维度 饱和率（子分=0 或 =1 占比）/缺失率（raw None 占比）
    + 全部候选分；一条结构化行。某维度饱和率 >30% → 追加 ⚠️SAT 告警（维度失效）。
    由 build_order_book 在 cands 评分循环（sort）结束后调用一次；仅观测不参与打分。"""
    try:
        entries = _candidate_monitor
        if not entries:
            return
        dims = [d for d, _, _ in _CAND_DIMS]
        n = float(len(entries))
        sat = {d: sum(1 for e in entries
                      if e['subs'].get(d) is not None and e['subs'][d] in (0.0, 1.0)) / n for d in dims}
        miss = {d: sum(1 for e in entries if e['raw'].get(d) is None) / n for d in dims}
        groups = {}
        for e in entries:
            g = e['group']
            groups[g] = groups.get(g, 0) + 1
        gs = '\'%s\'' % next(iter(groups)) if len(groups) == 1 else str(groups)
        flags = ','.join(d for d in dims if sat[d] > 0.30)
        scores = [round(e['score'], 2) for e in entries]
        print('[candidate_monitor] pool=%d group=%s sat={%s} missing={%s} scores=%s%s'
              % (pool_n, gs,
                 ','.join('%s:%.2f' % (d, sat[d]) for d in dims),
                 ','.join('%s:%.2f' % (d, miss[d]) for d in dims),
                 scores, (' ' + '\u26a0\ufe0fSAT:' + flags) if flags else ''))
    except Exception:
        pass
    finally:
        _candidate_monitor.clear()

def _peer_group(pctx):
    """同类分组键：f"组合：大类"。大类 = type（equity/mixed/bond/gold/other）；
    风格 = 名称关键词推导（指数/ETF/LOF→指数；量化/多因子→量化；主题词→主题；否则主动）。
    bench_index 作为分组信息来源预留（现由名称关键词承载）。"""
    if not isinstance(pctx, dict):
        return 'other:主动'
    big = str(pctx.get('type') or 'other').strip().lower() or 'other'
    name = str(pctx.get('name') or '')
    if any(k in name for k in ('指数', 'ETF', 'LOF')):
        style = '指数'
    elif any(k in name for k in ('量化', '多因子')):
        style = '量化'
    elif any(k in name for k in _THEME_WORDS):
        style = '主题'
    else:
        style = '主动'
    return '%s:%s' % (big, style)


def _parse_mdd(v):
    """max_dd 解析：兼容 '-17.9%'（剥 %/100）与数值；NaN → None"""
    try:
        if v is None:
            return None
        if isinstance(v, str):
            f = float(v.strip().replace('%', '')) / 100.0
        else:
            f = float(v)
        return f if f == f else None
    except Exception:
        return None


def _parse_scale(v):
    """规模解析（亿元）：'43.53亿'→43.53；'1.2万'→ 0.00012（万→/10000）；纯数值→原值；非法→None"""
    try:
        if v is None:
            return None
        s = str(v).strip().replace(',', '').replace('元', '').replace('人民币', '')
        if not s:
            return None
        if '万' in s:
            f = float(s.replace('万', '')) / 10000.0
        else:
            f = float(s.replace('亿', ''))
        return f if f == f else None
    except Exception:
        return None


def _top10_names(top10):
    """top10 股票名列表：兼容 [{'stock_name':..,'weight':..}] 与 [字符串]"""
    out = []
    try:
        for item in (top10 or []):
            if isinstance(item, dict):
                n = str(item.get('stock_name') or item.get('name') or item.get('code') or '').strip()
            else:
                n = str(item).strip()
            if n:
                out.append(n)
    except Exception:
        return []
    return out


def _rets(pctx, dated=False):
    """复权日收益序列（ret_col 口径）；数据不足/异常 → None。dated=True 时索引改为日期（相关性对齐）"""
    try:
        if not isinstance(pctx, dict):
            return None
        df = pctx.get('nav_series')
        if df is None:
            return None
        rc = ret_col(df)
        if rc is None or len(rc) < 2:
            return None
        import pandas as pd
        s = rc.pct_change()
        if dated and hasattr(df, 'columns') and 'date' in df.columns:
            s = s.copy()
            s.index = pd.to_datetime(df['date'])
        s = s.dropna()
        return s if len(s) >= 2 else None
    except Exception:
        return None


def _ret_last_n(rets, n):
    """最近 n 日累计收益（复权口径）"""
    r = rets.tail(min(int(n), len(rets)))
    return float((1.0 + r).prod() - 1.0)


def _rank01(vals, own):
    """组内百分位：（低于数 + 0.5×等值数）/ 样本数；空 → 0.5"""
    n = len(vals)
    if n == 0:
        return 0.5
    below = sum(1 for v in vals if v < own)
    equal = sum(1 for v in vals if v == own)
    return (below + 0.5 * equal) / n


def _rank_in_group(v, vals):
    """winsorize（1%/99% 分位夹取）后组内百分位；组内单样本/空 → 0.5"""
    if v is None or v != v:
        return 0.5
    vals = [x for x in vals if x is not None and x == x]
    if not vals or len(vals) < 2:
        return 0.5
    import pandas as pd
    s = pd.Series(vals)
    q01 = float(s.quantile(0.01))
    q99 = float(s.quantile(0.99))
    clipped = [max(q01, min(q99, x)) for x in vals]
    own = max(q01, min(q99, v))
    return _rank01(clipped, own)


def _shrink_pct(own_idx, rets, srs, years):
    """P2 贝叶斯收缩：组内 250 日收益 → 收缩后组内百分位（0~1）。
    SE_i = sqrt((1+SR_i²/2)/年数_i)；τ = 组内横截面标准差；λ_i = τ²/(τ²+SE_i²)；
    收缩后_i = 组内中位数 + λ_i×(本值_i−中位数)。组内样本 <2 或 τ=0 → 0.5。"""
    n = len(rets)
    if n < 2:
        return 0.5
    xs = [float(x) for x in rets]
    if any(x != x for x in xs):
        return 0.5
    sx = sorted(xs)
    m = sx[n // 2] if n % 2 else (sx[n // 2 - 1] + sx[n // 2]) / 2.0
    tau = math.sqrt(sum((x - m) ** 2 for x in xs) / n)
    if tau == 0:
        return 0.5
    shrunk = []
    for i, x in enumerate(xs):
        sr = srs[i] if i < len(srs) else 0.0
        if sr is None or sr != sr:
            sr = 0.0
        y = years[i] if i < len(years) else 1.0
        y = max(float(y), 1.0 / 250.0)
        se = math.sqrt((1.0 + sr * sr / 2.0) / y)
        lam = tau * tau / (tau * tau + se * se)
        shrunk.append(m + lam * (x - m))
    return _rank01(shrunk, shrunk[own_idx])


def _excess_alpha(pctx, cfg, rf):
    """风格中性超额：0.5×夏普（P0 口径：rf 链、σ下限 0.01）+ 0.5×组内 250 日收益
    贝叶斯收缩百分位（P2；池缺失/组内单样本 → 0.5 中性）。
    平滑检测：复权日收益一阶自相关 >0.9 → 判定平滑产品，子分用 0.5（不进正常池）。"""
    rets = _rets(pctx)
    if rets is None or len(rets) < 20:
        return None
    mu = float(rets.mean()) * 250.0
    vol = float(rets.std() * math.sqrt(250.0))
    if mu != mu or vol != vol:
        return 0.5
    vol = max(vol, 0.01)
    sr_ann = (mu - rf) / vol
    sharpe = max(0.0, min(1.0, sr_ann / 2.0))
    try:
        auto = float(rets.autocorr(lag=1))
        if auto == auto and auto > 0.9:
            return 0.5
    except Exception:
        pass
    own_in = len(rets) >= 30
    grp_ret, grp_sr, grp_yr = [], [], []
    if own_in:
        grp_ret.append(_ret_last_n(rets, 250))
        grp_sr.append(sr_ann)
        grp_yr.append(len(rets) / 250.0)
    if _candidate_pool:
        gk = _peer_group(pctx)
        seen = {id(pctx)}
        for q in _candidate_pool:
            if q is pctx or id(q) in seen or not isinstance(q, dict):
                continue
            seen.add(id(q))
            if _peer_group(q) != gk:
                continue
            qr = _rets(q)
            if qr is None or len(qr) < 30:
                continue
            qmu = float(qr.mean()) * 250.0
            qvol = float(qr.std() * math.sqrt(250.0))
            if qvol != qvol or qvol <= 0:
                qvol = 0.01
            qvol = max(qvol, 0.01)
            grp_ret.append(_ret_last_n(qr, 250))
            grp_sr.append((qmu - rf) / qvol)
            grp_yr.append(len(qr) / 250.0)
    pct = 0.5
    if own_in and len(grp_ret) >= 2:
        pct = _shrink_pct(0, grp_ret, grp_sr, grp_yr)
    return 0.5 * sharpe + 0.5 * pct


def _tail_risk(pctx):
    """尾部风险：MaxDD 子分（1-abs，复权口径，nav_series 优先）+
    CVaR95 子分（-5% 分位，/0.03 归一）+ 下行波动子分（负收益日标准差，同归一），三部分平均。
    nav_series 不足 30 日时 MaxDD 回退 pctx.max_dd（'-17.9%'/数值兼容）。"""
    parts = []
    rets = _rets(pctx)
    if rets is not None and len(rets) >= 30:
        nav = (1.0 + rets).cumprod()
        mdd = float((nav / nav.cummax() - 1.0).min())
        parts.append(max(0.0, min(1.0, 1.0 - abs(mdd))))
        q05 = float(rets.quantile(0.05))
        if q05 == q05:
            cvar = max(0.0, -q05)
            parts.append(max(0.0, min(1.0, 1.0 - min(1.0, cvar / 0.03))))
        neg = rets[rets < 0.0]
        if len(neg) >= 2:
            dvol = float(neg.std())
            if dvol == dvol:
                parts.append(max(0.0, min(1.0, 1.0 - min(1.0, dvol / 0.03))))
    else:
        mdd = _parse_mdd(pctx.get('max_dd'))
        if mdd is not None:
            parts.append(max(0.0, min(1.0, 1.0 - abs(mdd))))
    if not parts:
        return None
    return sum(parts) / len(parts)

_TCO_PATTERNS = (
    ('管理费', r'管理费([\d.]+)%'),
    ('管理', r'管理([\d.]+)%'),
    ('托管费', r'托管费([\d.]+)%'),
    ('托管', r'托管([\d.]+)%'),
    ('销售服务费', r'销售服务费([\d.]+)%'),
    ('销售服务', r'销售服务([\d.]+)%'),
    ('申购', r'申购([\d.]+)%'),
    ('短期赎回', r'短期赎回([\d.]+)%'),
)


def _tco_score(pctx, cfg):
    """TCO（总持有成本）：(管理+托管+销售服务)×持有年数 + 申购费 + 持有期赎回费。
    量纲契约：接口边界写死小数口径——解析出的百分数一律 /100 转小数（失败项跳过）；
    满分基准 2%：score = 1 - min(1, 合计/0.02)，clamp 0-1。
    持有年数默认 2（cfg.rules.candidate_holding_years 可配）。A/C 份额择优不自动做（config 人工选）。"""
    import re
    ongoing, one_time = [], []
    fees_txt = str(pctx.get('fees') or '')
    if fees_txt and fees_txt != '无':
        for key, pat in _TCO_PATTERNS:
            m = re.search(pat, fees_txt)
            if m:
                try:
                    v = float(m.group(1)) / 100.0
                    if v == v:
                        (ongoing if key in ('管理费', '管理', '托管费', '托管',
                                           '销售服务费', '销售服务') else one_time).append(v)
                except Exception:
                    pass
    if not ongoing and not one_time:
        for kk in ('mgmt_fee', 'manage_fee', 'custody_fee', 'sales_service_fee'):
            v = pctx.get(kk)
            if v is not None:
                try:
                    v = float(v)
                    if v == v:
                        ongoing.append(v / 100.0 if v > 1.0 else v)
                except Exception:
                    pass
        for kk in ('purchase_fee', 'short_redeem_fee', 'redeem_fee'):
            v = pctx.get(kk)
            if v is not None:
                try:
                    v = float(v)
                    if v == v:
                        one_time.append(v / 100.0 if v > 1.0 else v)
                except Exception:
                    pass
    if not ongoing and not one_time:
        return None
    years = 2.0
    try:
        y = float((cfg.get('rules') or {}).get('candidate_holding_years', 2.0))
        if y == y and y > 0:
            years = y
    except Exception:
        pass
    total = sum(ongoing) * years + sum(one_time)
    return max(0.0, min(1.0, 1.0 - min(1.0, total / 0.02)))


def _marginal_contribution(pctx):
    """边际贡献：0.5×(1−与组合其他产品复权收益相关性均值) + 0.5×(1−top10 重合率)。
    其他产品 = _candidate_others（build_order_book 传入组合内 held 产品 pctx）；
    低相关=高贡献；重合率 = 重合股票数 / 本产品 top10 数。
    数据缺失的部分用 0.5 中性；自身无序列且无 top10 → None（维度缺失）。"""
    own_ret = _rets(pctx, dated=True)
    if own_ret is not None and len(own_ret) < 20:
        own_ret = None
    corrs = []
    for o in _candidate_others:
        if o is pctx or not isinstance(o, dict):
            continue
        osr = _rets(o, dated=True)
        if osr is None or len(osr) < 20:
            continue
        if own_ret is None:
            continue
        common = own_ret.index.intersection(osr.index)
        if len(common) < 20:
            continue
        a, b = own_ret.loc[common], osr.loc[common]
        if float(a.std()) == 0.0 or float(b.std()) == 0.0:
            continue
        c = float(a.corr(b))
        if c == c:
            corrs.append(c)
    half1 = 1.0 - (sum(corrs) / len(corrs)) if corrs else 0.5
    half1 = max(0.0, min(1.0, half1))
    cand_names = _top10_names(pctx.get('top10'))
    other_names = set()
    for o in _candidate_others:
        if o is pctx or not isinstance(o, dict):
            continue
        other_names.update(_top10_names(o.get('top10')))
    if not cand_names:
        half2 = 0.5
    elif not other_names:
        half2 = 0.5
    else:
        half2 = 1.0 - len(set(cand_names) & other_names) / float(len(cand_names))
    if own_ret is None and not cand_names:
        return None
    return 0.5 * half1 + 0.5 * half2


def _stability(pctx):
    """运作稳定性（15 分维度）：经理任期三态 10 分 + 规模适配 5 分。
    任期：state_store.manager_tenure_days(code, config_since=manager_since) ≥12月 → 1.0；
    <12月（已知短任期）→ 0.85；未知 → 0.95。
    规模：指数/ETF/LOF 类越大越好（<5亿 0.4；5-50亿 0.6→1.0 线性；>50亿 1.0）；
    主动类容量曲线（50-300 亿满分区，同 _piecewise）；规模缺失 → 0.5 中性。
    注：规模数据为季报口径（滞后 1-3 个月），仅注释说明不参与打分。"""
    code = str(pctx.get('code') or '').strip()
    mgr_since = pctx.get('manager_since')
    days = None
    try:
        import state_store
        days = state_store.manager_tenure_days(code, config_since=mgr_since)
    except Exception:
        days = None
    avail = (days is not None) or bool(code) or bool(mgr_since)
    tenure = 1.0 if (days is not None and days >= 365) else (0.85 if days is not None else 0.95)
    scale = _parse_scale(pctx.get('scale'))
    if scale is not None:
        avail = True
    if scale is None:
        scale_s = 0.5
    else:
        name = str(pctx.get('name') or '')
        if any(k in name for k in ('指数', 'ETF', 'LOF')):
            scale_s = _piecewise(scale, [(0.0, 0.4), (5.0, 0.6), (50.0, 1.0)])
        else:
            scale_s = _piecewise(scale, [(0.0, 0.0), (1.0, 0.2), (5.0, 0.8),
                                         (50.0, 1.0), (300.0, 1.0), (600.0, 0.6), (2000.0, 0.2)])
    if not avail:
        return None
    return (10.0 * tenure + 5.0 * scale_s) / 15.0


def _tradability(pctx):
    """可交易性：申购状态 + 日限额 + 最短持有期（费率表），三部分平均。
    申购状态：开放=1.0 / 限大额=0.6 / 暂停申购=0.0（已被硬过滤剔除，此处兜底）/ 未知=0.7；
    日限额（purchase_meta.daily_limit）：None/0 → 0.7；0-1万 → 0.3→1.0 线性；≥1万 → 1.0；
    最短持有：fees 含「短期赎回」且费率>0 → 0.8；否则 → 1.0。确认时效不深挖（实现简化）。"""
    import re
    parts = []
    avail = False
    status = pctx.get('purchase_status')
    if status is not None:
        avail = True
    parts.append({'开放申购': 1.0, '限大额': 0.6, '暂停申购': 0.0}.get(status, 0.7))
    pm = pctx.get('purchase_meta') or {}
    if not isinstance(pm, dict):
        pm = {}
    dl = None
    if pm.get('daily_limit') is not None:
        try:
            dl = float(pm.get('daily_limit'))
            if dl != dl:
                dl = None
        except Exception:
            dl = None
    if dl is not None:
        avail = True
    if dl is None or dl <= 0:
        dl_s = 0.7
    elif dl >= 10000:
        dl_s = 1.0
    else:
        dl_s = 0.3 + 0.7 * dl / 10000.0
    parts.append(dl_s)
    fees_txt = str(pctx.get('fees') or '')
    m = re.search(r'短期赎回([\d.]+)%', fees_txt)
    if m:
        avail = True
        try:
            short = float(m.group(1))
            parts.append(0.8 if short > 0 else 1.0)
        except Exception:
            parts.append(1.0)
    else:
        parts.append(1.0)
    if not avail:
        return None
    return sum(parts) / len(parts)


def _synthesize(weighted, tail_s, trad_s, completeness):
    """合成：基础加权分（0-100）× 惩罚项 × 完整度折扣。
    惩罚（及格线，仅尾部/可交易）：子分 <0.3 → ×0.6；完整度折扣 = 0.9 + 0.1×完整度。"""
    s = float(weighted)
    if tail_s is not None and tail_s == tail_s and tail_s < 0.3:
        s *= 0.6
    if trad_s is not None and trad_s == trad_s and trad_s < 0.3:
        s *= 0.6
    s *= 0.9 + 0.1 * float(completeness)
    return s


def _dim_raw(dim, pctx, cfg, rf):
    try:
        if dim == 'marginal':
            return _marginal_contribution(pctx)
        if dim == 'tco':
            return _tco_score(pctx, cfg)
        if dim == 'alpha':
            return _excess_alpha(pctx, cfg, rf)
        if dim == 'tail':
            return _tail_risk(pctx)
        if dim == 'stability':
            return _stability(pctx)
        if dim == 'tradability':
            return _tradability(pctx)
    except Exception:
        return None
    return None


def _group_median(dim, group, cfg, rf):
    vals = [_dim_raw(dim, q, cfg, rf) for q in group]
    vals = [x for x in vals if x is not None and x == x]
    if not vals:
        return 0.5
    sx = sorted(vals)
    n = len(sx)
    return sx[n // 2] if n % 2 else (sx[n // 2 - 1] + sx[n // 2]) / 2.0


def score_candidate(pctx, cfg, rf_annual=None):
    """买入候选评分 V3（P0 止血 + P1 结构重构 + P2 贝叶斯收缩）。
    维度：边际贡献20 / TCO18 / 风格中性超额17 / 尾部风险15 / 运作稳定性15 / 可交易性15。
    - 子分 0~1：winsorize（1%/99%）后组内百分位（_candidate_pool 同组；无池 → 原始值；
      组内单样本 → 0.5）；缺失子分 → 组内中位数填充（无池/组空 → 0.5）；
    - 完整度门槛：可用维度权重 /100 < 0.8 → 返回 None（出局，不参与排序）；
    - 合成：100×Σ(w·s)/Σw（权重恒 100，防御性归一）× 惩罚项（尾部/可交易 <0.3 → ×0.6）
      × 完整度折扣（0.9+0.1×完整度）；
    - 纯确定性：同一输入恒得同一分；AI 无权修改本函数输出。
    签名契约：score_candidate(pctx, cfg, rf_annual=None)（build_order_book 传 ctx.rf_annual）。"""
    if pctx is None or not isinstance(pctx, dict):
        return None
    cfg = cfg if isinstance(cfg, dict) else {}
    rf = rf_annual if rf_annual is not None else (cfg.get('rules') or {}).get('risk_free_rate', 0.015)
    try:
        rf = float(rf)
        if rf != rf:
            rf = 0.0
    except Exception:
        rf = 0.0
    own = {}
    avail_w = 0
    for dim, w, _ in _CAND_DIMS:
        v = _dim_raw(dim, pctx, cfg, rf)
        own[dim] = v
        if v is not None:
            avail_w += w
    completeness = avail_w / 100.0
    if completeness < 0.8:
        return None
    gk = _peer_group(pctx)
    group = [pctx]
    if _candidate_pool:
        seen = {id(pctx)}
        for q in _candidate_pool:
            if q is pctx or id(q) in seen or not isinstance(q, dict):
                continue
            seen.add(id(q))
            if _peer_group(q) == gk:
                group.append(q)
    rank_mode = bool(_candidate_pool)
    subs = {}
    for dim, w, _ in _CAND_DIMS:
        v = own[dim]
        if v is None:
            if rank_mode and len(group) >= 2:
                subs[dim] = _group_median(dim, group, cfg, rf)
            else:
                subs[dim] = 0.5
            continue
        if v != v:
            subs[dim] = 0.5
            continue
        if rank_mode:
            if len(group) >= 2:
                vals = [_dim_raw(dim, q, cfg, rf) for q in group]
                subs[dim] = _rank_in_group(v, vals)
            else:
                subs[dim] = 0.5          # 组内单样本 → 中性 0.5
        else:
            subs[dim] = v
    total_w = sum(w for _, w, _ in _CAND_DIMS)
    weighted = sum(w * subs[dim] for dim, w, _ in _CAND_DIMS)
    base = 100.0 * weighted / total_w if total_w > 0 else 0.0
    score = _synthesize(base, subs.get('tail'), subs.get('tradability'), completeness)
    # ---- B2 埋点：收集条目（pool 评分循环结束由 _emit_candidate_monitor 统一输出一行） ----
    try:
        _candidate_monitor.append({
            'code': str(pctx.get('code') or '?'),
            'group': gk,
            'subs': {d: subs.get(d) for d, _, _ in _CAND_DIMS},
            'raw': {d: own.get(d) for d, _, _ in _CAND_DIMS},
            'score': score,
        })
    except Exception:
        pass
    return score


def _gate_reason(p, pc, today):
    """候选硬门槛（V3，cands 列表过滤处执行，非评分返回）：
    成立 <18 个月 / 申购状态 暂停申购|限大额 / 规模 <2 亿 → 返回原因字符串；
    非法/缺失 → None（fail-open：缺失不过度拦截）。p=产品 dict，pc=pctx。"""
    from datetime import datetime
    inc = pc.get('inception')
    if inc:
        try:
            d0 = datetime.strptime(str(inc)[:10], '%Y-%m-%d').date()
            d_t = datetime.strptime(str(today)[:10], '%Y-%m-%d').date()
            if (d_t - d0).days < 548:
                return '成立<18个月(%s)' % str(inc)[:10]
        except Exception:
            pass
    ps = pc.get('purchase_status')
    if ps in ('暂停申购', '限大额'):
        return '申购受限(%s)' % ps
    sc = _parse_scale(pc.get('scale'))
    if sc is not None and sc < 2.0:
        return '规模<2亿(%s)' % str(pc.get('scale'))
    return None


def build_order_book(cfg, products, ctx, storm_active=False, storm_reasons=None,
                     ep_lock=False, cooldown_active=False, today=None):
    """生成今日跟投指令（V2.2）。
    ctx: {"equity_target", "mvs": {code:市值}, "shares": {code}, "costs": {code},
          "lots": {code: [lots]}, "cash_mv", "settled_cash", "pending_cash": [...],
          "bench_pctile": {code}, "fee_rate": {code}, "exposure": {code},
          "r_hold": {code}, "r_hold_prev": {code}, "hold_years": {code},
          "product_ctx": {code}, "total"}
    返回 (orders[], target_alloc, summary_lines)
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    ctx = ctx if isinstance(ctx, dict) else {}
    products = products if isinstance(products, list) else []
    from datetime import date
    today = today or date.today().isoformat()
    rules = cfg.get("rules", {})
    target_equity = ctx.get("equity_target", 0.4)
    target_cash = cfg.get("target", {}).get("cash", {})
    t_cash = target_cash.get("base", 0.10) if isinstance(target_cash, dict) else 0.10
    target_bond = max(0.0, 1.0 - target_equity - t_cash)

    mvs = ctx.get("mvs", {})
    shares = ctx.get("shares", {})
    names = ctx.get("product_name", {})
    total = ctx.get("total", 0) or 1
    min_amt = float(rules.get("min_order_amount", 100))

    orders = []
    cash_now = float(ctx.get("cash_mv", 0)) + float(ctx.get("settled_cash", 0))
    remaining = dict(mvs)
    tp_ctx_map = ctx.get("tp_ctx", {})   # {code: {action, detail, rid, trace}}
    shadow = bool(rules.get("take_profit", {}).get("shadow_mode", True))

    # ---- 第一轮：止盈（撤/卖出）。影子模式下只记录信号，不生成指令 ----
    tp_actions = {}
    for p in products:
        code = p.get("code", "")
        tpc = (tp_ctx_map or {}).get(code) or {}
        act = tpc.get("action")
        if not act or tpc.get("repeat"):
            continue  # 同档位近5交易日已触发（去重），不重复生成指令/展示
        amt = remaining.get(code, 0)
        if amt <= 0:
            continue
        lots = ctx.get("lots", {}).get(code, [])
        saleable, exempt = _lot_adjusted(cfg, lots, today)
        if not saleable:
            continue
        # 逐笔豁免（P1-3）：卖出金额只按可卖笔（持有≥min_hold_days）计算，<7天笔不参与
        saleable_shares = sum(float(l.get("shares", 0)) for l in saleable)
        exempt_shares = sum(float(l.get("shares", 0)) for l in exempt)
        nav_price = float(ctx.get("nav", {}).get(code, 1)) or 1
        sellable_mv = saleable_shares * nav_price
        if act == "tp_dd":
            frac = 1.0
        elif act == "tp_rest":
            frac = 1.0
        elif act == "tp_2of3":
            frac = 2.0 / 3.0
        else:
            frac = 1.0 / 3.0
        sell_amt = round(min(amt, sellable_mv * frac), 2)
        if sell_amt < min_amt:
            continue
        exempt_note = ""
        if exempt_shares > 0:
            exempt_note = f"（其中 {exempt_shares:,.2f} 份持有不足 {min_hold_days(cfg)} 天已豁免，未计入卖出）"
        if shadow:
            tp_actions[code] = {"side": "信号", "amount": sell_amt,
                                "reason": tpc.get("detail", "") + exempt_note,
                                "rule_id": tpc.get("rid"),
                                "trace": tpc.get("trace")}
            continue
        spec = settlement_of(cfg, p)
        confirm = add_trading_days(today, int(spec.get("redeem_confirm_days", 1)))
        settle = add_trading_days(confirm, int(spec.get("redeem_cash_days", 2)))
        orders.append({
            "side": "卖出", "code": code, "name": names.get(code, code),
            "amount": sell_amt,
            "shares": round(sell_amt / nav_price, 2),
            "reason": tpc.get("detail", "") + exempt_note, "stop": True, "rule_id": tpc.get("rid"),
            "confirm_date": confirm, "settle_date": settle,
        })
        cash_now += sell_amt
        remaining[code] = amt - sell_amt

    # ---- 第二轮：目标仓位与关注池买入 ----
    equity_mv = sum(v for k, v in remaining.items()
                    if k in {p.get("code") for p in products if p.get("type") in ("equity", "gold")})
    bond_mv = sum(v for k, v in remaining.items()
                  if k in {p.get("code") for p in products if p.get("type") == "bond"})

    eq_target_amt = total * target_equity
    bd_target_amt = total * target_bond
    cash_target_amt = total * t_cash

    # 买入冻结（市场预警）：跳过一切买入，现金冻结（只卖不买）
    buy_frozen = bool(storm_active) or cooldown_active
    if not buy_frozen:
        # 权益缺口：目标 − 当前（含在途赎回按到账后计，买入以当前可用现金为上限）
        eq_gap = eq_target_amt - equity_mv
        if eq_gap > 0 and not ep_lock:
            pctx_map = ctx.get('product_ctx') or {}
            cands = [p for p in products
                     if p.get('type') in ('equity', 'gold') and remaining.get(p.get('code'), 0) >= 0
                     and p.get('status') in ('held', 'observe')]
            # ---- 硬门槛（V3，P0）：成立<18个月 / 暂停申购|限大额 / 规模<2亿 → 出局（cands 过滤处，非评分返回） ----
            kept = []
            for p in cands:
                why = _gate_reason(p, pctx_map.get(p.get('code', '')) or {}, today)
                if why:
                    try:
                        print('[rules] 候选剔除 %s(%s): %s' % (p.get('code', ''), p.get('name', ''), why))
                    except Exception:
                        pass
                    continue
                kept.append(p)
            cands = kept
            # 买入候选排序（V3）：score_candidate 降序；评分缺失（数据全缺）排最后；
            # 换手抑制：已持有同类（同 type）产品排序键 +5 分缓冲——候选须领先已持 >5 分才换手；
            # 同分/缺失组内保持原兜底顺序（已持有优先、代码序）
            def _buy_key(p):
                sc = None
                if pctx_map:
                    sc = score_candidate(pctx_map.get(p.get('code', '')) or {}, cfg,
                                         rf_annual=ctx.get('rf_annual'))
                st_rk = 0 if p.get('status') == 'held' else 1
                if sc is None:
                    return (1, 0, st_rk, p.get('code', ''))
                eff = sc + (5.0 if p.get('status') == 'held' else 0.0)
                return (0, -eff, st_rk, p.get('code', ''))
            if cands:
                # 候选池（组内百分位上下文）+ 组合其他产品（边际贡献上下文）；用后清理
                _set_candidate_pool(
                    [pctx_map.get(p.get('code', '')) or {} for p in cands],
                    [pc for pc in (ctx.get('product_ctx') or {}).values()
                     if isinstance(pc, dict) and pc.get('status') == 'held'])
                try:
                    cands.sort(key=_buy_key)
                    _emit_candidate_monitor(len(cands))  # B2：评分循环结束输出一条监控行
                    if cands:
                        code = cands[0]['code']
                        amt = round(min(eq_gap, max(0.0, cash_now - cash_target_amt)), 2)
                        if amt >= min_amt:
                            orders.append({'side': '买入', 'code': code, 'name': names.get(code, code),
                                           'amount': amt, 'shares': None,
                                           'reason': '再平衡：权益低于目标仓位' + ('（关注池建仓）' if remaining.get(code, 0) <= 0 else ''),
                                           'stop': False, 'rule_id': 'BUY-NEW' if remaining.get(code, 0) <= 0 else 'REB-EQ'})
                            cash_now -= amt
                            remaining[code] = remaining.get(code, 0) + amt
                finally:
                    _clear_candidate_pool()

        bd_gap = bd_target_amt - bond_mv
        if bd_gap > 0:
            cands = [p for p in products
                     if p.get("type") == "bond" and p.get("status") in ("held", "observe")]
            if not cands:
                cands = [p for p in products if p.get("type") == "bond"]
            if cands:
                cands.sort(key=lambda p: ctx.get("exposure", {}).get(p.get("code", ""), 1.0))
                code = cands[0]["code"]
                avail = max(0.0, cash_now - cash_target_amt)
                amt = round(min(bd_gap, avail), 2)
                if amt >= min_amt:
                    orders.append({"side": "买入", "code": code, "name": names.get(code, code),
                                   "amount": amt, "shares": None,
                                   "reason": "再平衡：固收低于目标仓位（选权益暴露最低产品）",
                                   "stop": False, "rule_id": "BUY-NEW" if remaining.get(code, 0) <= 0 else "REB-BOND"})
                    cash_now -= amt
                    remaining[code] = remaining.get(code, 0) + amt

    summary_lines = []
    for p in products:
        code = p.get("code", "")
        if remaining.get(code, 0) <= 0 and ctx.get("mvs", {}).get(code, 0) > 0:
            summary_lines.append(f"- {names.get(code, code)}：已清仓")
    if tp_actions and shadow:
        for code, a in tp_actions.items():
            summary_lines.append(f"- 止盈信号（影子模式，仅记录不执行）：{names.get(code, code)} {a['amount']:,.0f} 元（{a['reason']}）")
    if storm_active:
        summary_lines.append(
            f"- 市场预警：估值与动量同时达警戒（{'/'.join(storm_reasons or ['STORM-5'])}）→ 买入冻结，持有现金。"
            f"解锁条件：下一无风险交易日自动判定")
    elif ep_lock:
        summary_lines.append(
            f"- 战略防御：[EP-CAP-10] 权益上限锁定 10%，暂停权益买入；固收再平衡照常执行")
    elif not buy_frozen and not orders and not tp_actions:
        summary_lines.append("- 今日无规则触发，维持当前持仓")
    elif cooldown_active and not orders and not tp_actions:
        summary_lines.append("- 处于操作冷却期，今日不生成新买入指令")

    target_alloc = {"equity": target_equity, "bond": target_bond, "cash": t_cash}
    return orders, target_alloc, summary_lines, tp_actions


# ---------------- 组合诊断（市值口径） ----------------
def estimate_shares(nav_series, amount, buy_date):
    """份额估算：amount / 建仓日净值（buy_date 当日或之前最近净值）。数据不足返回 None。"""
    import pandas as pd
    if nav_series is None or len(nav_series) == 0 or not buy_date or not amount:
        return None
    try:
        bd = pd.Timestamp(str(buy_date)[:10])
        df = nav_series.copy()
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            row = df[df["date"] <= bd]
            if not row.empty:
                return amount / float(row["nav"].iloc[-1])
    except Exception:
        return None
    return None


def market_value_weights(holdings, nav_series_map, nav_latest_map):
    """市值权重：优先 shares×最新净值 → 份额估算 → 成本回退。现金按面值。
    返回 ({key: 市值}, total_mv)"""
    mvs = {}
    for h in holdings:
        key = h.get("fund_code") or h.get("code") or h.get("name", "")
        if h.get("type") == "cash" or not key:
            mvs[key] = float(h.get("amount", 0))
            continue
        shares = float(h.get("shares") or 0)
        latest = nav_latest_map.get(key)
        if shares > 0 and latest:
            mvs[key] = shares * latest
            continue
        est = estimate_shares(nav_series_map.get(key), float(h.get("amount", 0)), h.get("buy_date"))
        if est and latest:
            mvs[key] = est * latest
        else:
            mvs[key] = float(h.get("amount", 0))
    total = sum(mvs.values())
    return mvs, total


def hold_metrics(holdings, nav_series_map, nav_latest_map, cost_map=None):
    """持有收益/加权持有年数（份额台账口径）。
    返回 ({key: {"r_hold","r_hold_prev","hold_years","cost","shares","mv"}}, total)
    """
    from datetime import date
    out = {}
    for h in holdings:
        key = h.get("fund_code") or h.get("code")
        if h.get("type") == "cash" or not key:
            continue
        lots = h.get("lots") or [{"buy_date": h.get("buy_date", ""), "shares": h.get("shares", 0), "cost": h.get("cost", 0)}]
        shares = float(h.get("shares") or sum(float(l.get("shares", 0)) for l in lots))
        cost = float(cost_map.get(key) if cost_map else 0) or float(h.get("cost") or sum(float(l.get("cost", 0)) for l in lots))
        latest = nav_latest_map.get(key)
        ns = nav_series_map.get(key)
        if not latest:
            out[key] = {"r_hold": 0.0, "r_hold_prev": 0.0, "hold_years": 1.0, "cost": cost, "shares": shares, "mv": cost}
            continue
        mv = shares * latest
        r_hold = mv / cost - 1 if cost > 0 else 0.0
        r_prev = r_hold
        try:
            if ns is not None and len(ns) > 2:
                prev_nav = float(ns["nav"].iloc[-2])
                r_prev = shares * prev_nav / cost - 1 if cost > 0 else 0.0
        except Exception:
            pass
        today = date.today().isoformat()
        wsum = 0.0
        wsh = 0.0
        for lot in lots:
            bd = str(lot.get("buy_date", ""))[:10]
            try:
                yrs = max(0.0, (date.fromisoformat(today) - date.fromisoformat(bd)).days / 365.0)
            except Exception:
                yrs = 0.0
            wsum += float(lot.get("shares", 0)) * yrs
            wsh += float(lot.get("shares", 0))
        hold_years = wsum / wsh if wsh > 0 else 1.0
        out[key] = {"r_hold": r_hold, "r_hold_prev": r_prev, "hold_years": max(hold_years, 1 / 365.0),
                    "cost": cost, "shares": shares, "mv": mv}
    return out


def portfolio_diagnostics(cfg, ctx):
    """组合诊断：年化波动率 / 250日最大回撤 / VaR95 / 实际权益暴露度。
    5.1：可选基准对比（bench_returns/bench_weights）→ excess_ann/alpha/beta/ir；
    基准缺失或对齐样本 <60 时这些字段为 None，不影响既有字段。"""
    ctx = ctx if isinstance(ctx, dict) else {}
    ret_map = ctx.get("returns")
    if not ret_map:
        return None
    weights = ctx.get("weights", {})
    codes = [c for c in weights if c in ret_map]
    if not codes:
        return None
    common = None
    for c in codes:
        s = ret_map[c]
        common = s.index if common is None else common.intersection(s.index)
    if common is None or len(common) < 30:
        return None
    import pandas as pd
    combo = pd.DataFrame({c: ret_map[c].loc[common] for c in codes})
    w = pd.Series({c: weights.get(c, 0) for c in codes})
    if w.sum() <= 0:
        return None
    w = w / w.sum()
    combo_ret = combo.dot(w)
    annual_vol = float(combo_ret.std() * math.sqrt(250))
    nav = (1 + combo_ret).cumprod()
    max_dd = float((nav / nav.cummax() - 1).min())
    var95 = float(combo_ret.quantile(0.05))
    out = {"vol": annual_vol, "max_dd": max_dd, "var95": var95,
           "excess_ann": None, "alpha": None, "beta": None, "ir": None}

    # ---- 5.1 基准对比：复合基准日收益（inner join 对齐、按权重求和） ----
    bench_map = ctx.get("bench_returns")
    bench_w = ctx.get("bench_weights")
    if isinstance(bench_map, dict) and isinstance(bench_w, dict):
        bnames = [b for b in bench_w if b in bench_map]
        if bnames:
            bcommon = None
            for b in bnames:
                bs = bench_map[b]
                if bs is None or len(bs) == 0:
                    bcommon = None
                    break
                bcommon = bs.index if bcommon is None else bcommon.intersection(bs.index)
            if bcommon is not None:
                bcommon = bcommon.intersection(common)
                if len(bcommon) >= 60:
                    bw = pd.Series({b: float(bench_w.get(b, 0) or 0) for b in bnames})
                    if bw.sum() > 0:
                        bw = bw / bw.sum()
                        bdf = pd.DataFrame({b: bench_map[b].loc[bcommon] for b in bnames})
                        bench_ret = bdf.dot(bw)
                        pr = combo_ret.loc[bcommon]
                        excess = pr - bench_ret
                        excess_ann = float(excess.mean() * 250)
                        te = float(excess.std() * math.sqrt(250))
                        if excess_ann == excess_ann:      # NaN 防护
                            out["excess_ann"] = excess_ann
                            out["alpha"] = excess_ann
                        varb = float(bench_ret.var())
                        if varb == varb and varb > 0:
                            covpb = float(((pr - pr.mean()) * (bench_ret - bench_ret.mean())).mean())
                            out["beta"] = covpb / varb
                        if te == te and te > 0 and out["excess_ann"] is not None:
                            out["ir"] = out["excess_ann"] / te
    return out


def portfolio_simulator(ret_map, weights_after, total):
    """调仓后前瞻模拟（现金 0 收益，不参与归一化）。"""
    if total <= 0:
        return None
    import pandas as pd
    codes = [c for c in weights_after if c in ret_map and weights_after.get(c, 0) > 0]
    if not codes:
        return None
    common = None
    for c in codes:
        s = ret_map[c]
        common = s.index if common is None else common.intersection(s.index)
    if common is None or len(common) < 30:
        return None
    combo = pd.DataFrame({c: ret_map[c].loc[common] for c in codes})
    w = pd.Series({c: weights_after[c] / total for c in codes})
    combo_ret = combo.dot(w)
    annual_vol = float(combo_ret.std() * math.sqrt(250))
    nav = (1 + combo_ret).cumprod()
    max_dd = float((nav / nav.cummax() - 1).min())
    var95 = float(combo_ret.quantile(0.05))
    return {"vol": annual_vol, "max_dd": max_dd, "var95": var95}


def validate_config(cfg):
    """静态结构校验（5.6）：返回问题字符串列表，空列表 = 无问题。不联网、不调用采集。"""
    probs = []
    if not isinstance(cfg, dict):
        return ["cfg 必须是 dict（当前类型 %s）" % type(cfg).__name__]
    for k in ("holdings", "products"):
        v = cfg.get(k)
        if v is not None and not isinstance(v, list):
            probs.append("cfg.%s 必须是 list" % k)
    t = cfg.get("target")
    if not isinstance(t, dict):
        probs.append("cfg.target 必须是 dict（且需包含 equity）")
    elif not isinstance(t.get("equity"), dict):
        probs.append("cfg.target.equity 缺失或不是 dict（必须存在）")
    rules_ = cfg.get("rules")
    if rules_ is not None and not isinstance(rules_, dict):
        probs.append("cfg.rules 必须是 dict")
    else:
        tp = (rules_ or {}).get("take_profit")
        if tp is not None:
            if not isinstance(tp, dict):
                probs.append("cfg.rules.take_profit 必须是 dict")
            else:
                vr = tp.get("vol_ref")
                if vr is not None and not isinstance(vr, dict):
                    probs.append("rules.take_profit.vol_ref 必须是 dict")
                tg = tp.get("tier_gap")
                if tg is not None and (not isinstance(tg, list) or len(tg) < 2
                                       or not all(isinstance(x, (int, float)) and x >= 0 for x in tg)):
                    probs.append("rules.take_profit.tier_gap 必须是 ≥2 个非负数的 list")
                for kk in ("fee_margin", "dd_factor", "min_hold_days"):
                    v = tp.get(kk)
                    if v is not None and not isinstance(v, (int, float)):
                        probs.append("rules.take_profit.%s 必须是数值" % kk)
    # shares/cost 数值合法（holdings/products 条目）
    for sec in ("holdings", "products"):
        for item in (cfg.get(sec) or []):
            if not isinstance(item, dict):
                probs.append("cfg.%s 元素必须是 dict" % sec)
                continue
            tag = str(item.get("code") or item.get("fund_code") or item.get("name") or "?")
            for kk in ("shares", "cost"):
                v = item.get(kk)
                if v is None:
                    continue
                if not isinstance(v, (int, float)) or v != v or v < 0:
                    probs.append("cfg.%s[%s].%s 不合法（须为非负数值）" % (sec, tag, kk))
    return probs