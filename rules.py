# -*- coding: utf-8 -*-
"""确定性策略引擎：今日跟投指令的生成者。
输入必须是数据，输出必须是确定的仓位操作。AI 无权修改本模块的输出。
V2.2：市值口径算账、动态止盈（上限门+峰值回撤保护+短仓地板+费后口径）、
买入冻结（原风暴锁，仅保留最保守仓位档）、关注池买入（含零持仓）、冷却期。
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
    "MIN-MERGE": "最保守合并",
    "EP-CAP-10": "EP安全阀(权益≤10%)",
    "STORM-5": "市场预警-最保守仓位5%",
    "REB-EQ": "再平衡-买入权益",
    "REB-BOND": "再平衡-买入固收",
    "BUY-NEW": "关注池-买入",
}


def _trig(rid, text):
    return {"id": rid, "text": text}


# ---------------- 权益目标（阶梯 + EP + 动态区间） ----------------
def equity_target(cfg, ctx):
    """计算今日权益目标（最保守原则 + 股债性价比锁定）。
    返回 (target, triggers)；target 为区间基准值，由调用方套 target.band 形成区间。"""
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

    target = min(candidates) if candidates else base_val
    if candidates:
        triggers.append(_trig("MIN-MERGE", f"最保守原则：最终权益目标 = min(各规则值) = {target*100:.0f}%"))

    ep = ctx.get("ep_premium_pctile")
    if ep is not None:
        if ep < 0.10:
            target = min(target, 0.10)
            triggers.append(_trig("EP-CAP-10",
                                  f"股债性价比极端约束：分位 {ep*100:.0f}% < 10% → 已激活，权益目标强制 ≤ 10%"))
        else:
            triggers.append(_trig("EP-CAP-10",
                                  f"股债性价比极端约束：分位 {ep*100:.0f}% > 10% → 不触发权益仓位上限锁定（安全阀未激活）"))
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
    """σ_ref 按权益暴露度线性插值（0.02 ↔ 0.08 ↔ 0.20），消除档位跳变"""
    vr = cfg.get("rules", {}).get("take_profit", {}).get("vol_ref", {})
    v_eq, v_mix, v_bd = vr.get("equity", 0.20), vr.get("mixed", 0.08), vr.get("bond", 0.02)
    if exposure is None:
        return v_eq
    if exposure >= 0.8:
        return v_eq
    if exposure <= 0.2:
        return v_mix + (v_bd - v_mix) * (0.2 - exposure) / 0.2 if exposure < 0.2 else v_mix
    # 0.2~0.8 线性插值 mixed→equity
    return v_mix + (v_eq - v_mix) * (exposure - 0.2) / 0.6


def ewma_vol(nav_series, lam=0.94):
    """EWMA(λ=0.94) 日收益年化波动率。数据不足返回 None"""
    import pandas as pd
    try:
        if nav_series is None or len(nav_series) < 30:
            return None
        rets = nav_series["nav"].pct_change().dropna()
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
    tp = cfg.get("rules", {}).get("take_profit", {})
    nav_series = pctx.get("nav_series")
    if nav_series is None or len(nav_series) < 30:
        return None

    exposure = ctx.get("exposure")
    cls = _equity_class(exposure)
    sigma = ctx.get("sigma_ann") or ewma_vol(nav_series)
    if sigma is None:
        sigma = 0.20 if cls == "equity" else (0.08 if cls == "mixed" else 0.02)

    vref = _sigma_ref(cfg, exposure)
    F_vol = max(0.75, min(1.35, sigma / vref))

    hold_years = ctx.get("hold_years", 1.0)
    r_hold = ctx.get("r_hold", 0.0)
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
    T_cap = clamp_hi
    if v_bench is not None:
        w_bench = max(0.0, min(1.0, exposure or 0.0))
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
    tp = cfg.get("rules", {}).get("take_profit", {})
    st = compute_take_profit(cfg, pctx, ctx)
    if st is None:
        return None, "", None, None

    r_hold = ctx.get("r_hold", 0.0)
    r_prev = ctx.get("r_hold_prev", r_hold)
    hold_years = ctx.get("hold_years", 1.0)
    if hold_years <= 0:
        hold_years = 1.0
    fee = ctx.get("fee_rate", 0.005)
    margin = tp.get("fee_margin", 0.01)
    gap = tp.get("tier_gap", [0.05, 0.10])

    # 门槛：≤1 年按持有年数折算（带 6% 地板）；>1 年按年化
    if hold_years <= 1.0:
        def level_for(tev):
            return max(tev * hold_years, 0.06)
        r_now, r_yest = r_hold, r_prev
    else:
        def level_for(tev):
            return max(tev, 0.06)
        r_now = (1 + r_hold) ** (1.0 / hold_years) - 1
        r_yest = (1 + r_prev) ** (1.0 / hold_years) - 1 if r_prev > -1 else r_now

    lv1 = level_for(st["T_eff"]) + fee + margin
    lv2 = level_for(st["T_eff"] + gap[0]) + fee + margin
    lv3 = level_for(st["T_eff"] + gap[1]) + fee + margin

    # 回撤保护：峰值收益曾 ≥ T_eff 且收益自高点回撤 ≥ DD_thr 且当前盈利
    peak_r = st["peak_r_hold"]
    dd_hit = (peak_r is not None and st["r_hold"] > 0
              and peak_r >= st["T_eff"] and st["dd_ret"] >= st["dd_thr"])

    # 单向穿越检测（基于今日 vs 昨日）
    def crossed(level):
        return r_yest < level and r_now >= level

    action = None
    detail = ""
    rid = None
    if dd_hit:
        action, rid = "tp_dd", "TP-DD"
        detail = (f"回撤保护：持有收益 {r_hold*100:.1f}%（峰值 {peak_r*100:.1f}% ≥ 目标线 {st['T_eff']*100:.1f}%），"
                  f"自高点回撤 {st['dd_ret']*100:.1f}% ≥ {st['dd_thr']*100:.1f}% → 建议清仓")
    elif crossed(lv3) or (r_now >= lv3 and r_yest >= lv2):
        action, rid = "tp_rest", "TP-YIELD-3"
        detail = f"末档止盈：持有收益达 {r_now*100:.1f}% ≥ {lv3*100:.1f}%（目标线 {st['T_eff']*100:.1f}%+{gap[1]*100:.0f}%）→ 建议卖出剩余"
    elif crossed(lv2) or (r_now >= lv2 and r_yest >= lv1):
        action, rid = "tp_2of3", "TP-YIELD-2"
        detail = f"次档止盈：持有收益达 {r_now*100:.1f}% ≥ {lv2*100:.1f}%（目标线 {st['T_eff']*100:.1f}%+{gap[0]*100:.0f}%）→ 建议再卖 1/3"
    elif crossed(lv1):
        action, rid = "tp_1of3", "TP-YIELD-1"
        detail = f"首档止盈：持有收益达 {r_now*100:.1f}% ≥ {lv1*100:.1f}%（目标线 {st['T_eff']*100:.1f}%，含费后安全边际）→ 建议卖出 1/3"

    trace = None
    if action:
        trace = {
            "T_eff": round(st["T_eff"], 4), "T_pre": round(st["T_pre"], 4),
            "v_bench": ctx.get("bench_pctile"), "factors": {k: round(st[k], 4)
                                                            for k in ("F_vol", "F_hold", "F_sector")},
            "signal_date": str(ctx.get("today", "")), "execute_date": "",
            "fee_net_return": round(r_hold - fee, 4), "code": ctx.get("code", ""),
            "action": action, "shadow": bool(tp.get("shadow_mode", True)),
        }
    return action, detail, rid, trace


# ---------------- 订单簿（市值口径 + 关注池 + 余额约束 + 在途/冷却） ----------------
def add_trading_days(start_date, n):
    """日期 + n 个交易日（akshare 日历；失败按自然日）。"""
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
    return (start_date + timedelta(days=int(n * 1.5))).isoformat()


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
        if not act:
            continue
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
            cands = [p for p in products
                     if p.get("type") in ("equity", "gold") and remaining.get(p.get("code"), 0) >= 0
                     and p.get("status") in ("held", "observe")]
            # 优先已有持仓，其次观察池
            cands.sort(key=lambda p: (p.get("status") != "held", p.get("code", "")))
            if cands:
                code = cands[0]["code"]
                amt = round(min(eq_gap, max(0.0, cash_now - cash_target_amt)), 2)
                if amt >= min_amt:
                    orders.append({"side": "买入", "code": code, "name": names.get(code, code),
                                   "amount": amt, "shares": None,
                                   "reason": "再平衡：权益低于目标仓位" + ("（关注池建仓）" if remaining.get(code, 0) <= 0 else ""),
                                   "stop": False, "rule_id": "BUY-NEW" if remaining.get(code, 0) <= 0 else "REB-EQ"})
                    cash_now -= amt
                    remaining[code] = remaining.get(code, 0) + amt

        bd_gap = bd_target_amt - bond_mv
        if bd_gap > 0:
            cands = [p for p in products
                     if p.get("type") == "bond" and p.get("status") in ("held", "observe")]
            if not cands:
                cands = [p for p in products if p.get("type") == "bond"]
            if cands:
                cands.sort(key=lambda p: ctx.get("exposure", {}).get(p.get("fund_code", p.get("code", "")), 1.0))
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

    # 资金时间线（在途/挂起提示）
    pending_lines = []
    for pc in ctx.get("pending_cash", []) or []:
        sd = str(pc.get("settle_date", ""))[:10]
        pending_lines.append(f"- 在途资金：{pc.get('code')} 赎回 {pc.get('shares', 0)} 份，预计 {sd or '待确认'} 到账")

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


def portfolio_diagnostics(cfg, products, ctx):
    """组合诊断：年化波动率 / 250日最大回撤 / VaR95 / 实际权益暴露度"""
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
    return {"vol": annual_vol, "max_dd": max_dd, "var95": var95}


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
