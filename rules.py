# -*- coding: utf-8 -*-
"""确定性策略引擎：今日跟投指令的生成者。
输入必须是数据，输出必须是确定的仓位操作。AI 无权修改本模块的输出。
"""
import math

# ---- 规则 ID 体系（架构师 v1.0：AdvisorGatekeeper 白名单同源） ----
RULE_IDS = {
    "SL-EQ-18": "止损-权益型18%",
    "SL-CB-10": "止损-含转债10%",
    "SL-PB-3": "止损-纯债3%",
    "SL-FB-25": "止损-兜底25%",
    "TP-25": "止盈-1年25%+20日动量转负",
    "LAD-CSI300-80": "沪深300阶梯上限30%",
    "LAD-CSI300-90": "沪深300阶梯上限20%",
    "LAD-CSI300-95": "沪深300阶梯上限5%",
    "LAD-CSI500-75": "中证500阶梯上限5%",
    "MIN-MERGE": "最保守合并",
    "EP-CAP-10": "EP安全阀(权益≤10%)",
    "STORM-5": "风暴锁-最保守仓位5%",
    "STORM-STOP": "风暴锁-无条件清仓",
    "REB-EQ": "再平衡-买入权益",
    "REB-BOND": "再平衡-买入固收",
}


def _trig(rid, text):
    """结构化触发器：{id, text}，供触发链追溯与 Gatekeeper ID 白名单"""
    return {"id": rid, "text": text}


def equity_target(cfg, ctx):
    """计算今日权益目标仓位（最保守原则 + 股债性价比锁定）
    返回 (target, triggers[list of {id, text}])
    ctx: csi300_pe_pctile, csi300_mom20, csi500_pe_pctile, csi500_mom20, ep_premium_pctile
    """
    ladder = cfg.get("rules", {}).get("equity_ladder", [])
    base = cfg.get("target", {}).get("equity", 0.40)
    triggers = []
    candidates = []

    p300 = ctx.get("csi300_pe_pctile")
    if p300 is not None:
        m300 = ctx.get("csi300_mom20") or 0.0
        level = None
        rid = None
        if p300 >= 0.95:
            level = 0.05
            rid = "LAD-CSI300-95"
        elif p300 >= 0.90:
            level = 0.20
            rid = "LAD-CSI300-90"
        elif p300 >= 0.80 and m300 < 0:
            level = 0.30
            rid = "LAD-CSI300-80"
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

    target = min(candidates) if candidates else base
    if candidates:
        triggers.append(_trig("MIN-MERGE",
                              f"最保守原则：最终权益目标 = min(各规则值) = {target*100:.0f}%"))

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


def storm_lock(eq_target, triggers, products_status):
    """风暴安全锁前置判定（架构师 v1.0：仅两条，EP 不纳入）。
    - STORM-5: equity_target == 5% 且由阶梯信号（CSI300≥95% 或 CSI500≥75%+动量<-5% 或 min 合并）导致；
      EP 压制到 10% 的情况天然不会等于 5%，明确排除。
    - STORM-STOP: 任一产品触发无条件清仓（硬止损）。
    返回 (active: bool, reasons: list[str])
    """
    reasons = []
    ids = {t.get("id") for t in triggers}
    if eq_target == 0.05 and (ids & {"LAD-CSI300-95", "LAD-CSI500-75"}):
        reasons.append("STORM-5")
    if any(ps.get("action") == "sell_all" for ps in products_status.values()):
        reasons.append("STORM-STOP")
    return bool(reasons), reasons


def stop_loss_level(cfg, equity_exposure):
    """按实际权益暴露度分级止损线（距250日高点回撤阈值）"""
    sl = cfg.get("rules", {}).get("stop_loss", {})
    if equity_exposure is None:
        return sl.get("fallback", 0.25)
    if equity_exposure >= 0.80:
        return sl.get("equity", 0.18)
    if equity_exposure >= 0.05:
        return sl.get("convertible", 0.10)
    return sl.get("pure_bond", 0.03)


def stop_loss_rule_id(cfg, equity_exposure):
    """止损命中的规则 ID（SL-EQ-18 / SL-CB-10 / SL-PB-3 / SL-FB-25）"""
    if equity_exposure is None:
        return "SL-FB-25"
    if equity_exposure >= 0.80:
        return "SL-EQ-18"
    if equity_exposure >= 0.05:
        return "SL-CB-10"
    return "SL-PB-3"


def product_action(cfg, prod, ctx):
    """单产品规则：返回 (action, detail, rule_id)
    action: "sell_all" / "sell_half" / "hold"
    ctx: {"nav_series": DataFrame(date,nav), "equity_exposure": 0.5}
    """
    nav = ctx.get("nav_series")
    if nav is None or len(nav) < 30:
        return "hold", "数据不足，暂不触发规则", None
    high = float(nav["nav"].max())
    cur = float(nav["nav"].iloc[-1])
    dd = cur / high - 1
    stop_line = stop_loss_level(cfg, ctx.get("equity_exposure"))
    if dd < -stop_line:
        rid = stop_loss_rule_id(cfg, ctx.get("equity_exposure"))
        return "sell_all", f"触发止损线：净值距250日高点回撤 {dd*100:.1f}% > {stop_line*100:.0f}% → 无条件清仓", rid
    r1y = cur / float(nav["nav"].iloc[-251]) - 1 if len(nav) > 251 else None
    mom20 = cur / float(nav["nav"].iloc[-21]) - 1 if len(nav) > 21 else 0.0
    if r1y is not None and r1y > cfg.get("rules", {}).get("take_profit_1y", 0.25) and mom20 < 0:
        return "sell_half", f"触发止盈线：近1年收益 {r1y*100:.1f}% > 25% 且 20日动量转负 → 减半锁定收益", "TP-25"
    return "hold", f"规则未触发（距高点回撤 {dd*100:.1f}%，止损线 {stop_line*100:.0f}%）", None


def build_order_book(cfg, products, ctx, storm_active=False, storm_reasons=None, ep_lock=False):
    """生成今日跟投指令。
    ctx: {"equity_target": 0.3, "holdings_amount": {code: 金额}, "nav": {code: 最新净值},
          "equity_exposure": {code: 0.5}, "product_name": {code: name},
          "hold_days": {code: 天数} or None, "total": 总金额}
    storm_active/ep_lock: StormLock 与 EP 战略防御状态（架构师 v1.0 分层防御）
    返回 (orders[], target_alloc, summary)
    """
    rules = cfg.get("rules", {})
    target_equity = ctx.get("equity_target", cfg.get("target", {}).get("equity", 0.4))
    target_cash = cfg.get("target", {}).get("cash", 0.10)
    target_bond = max(0.0, 1.0 - target_equity - target_cash)

    holdings = ctx.get("holdings_amount", {})
    nav_map = ctx.get("nav", {})
    names = ctx.get("product_name", {})
    exposures = ctx.get("equity_exposure", {})
    total = ctx.get("total", 0) or 1

    orders = []
    stopped_cash = 0.0  # 卖出产生的现金（风暴/止盈/超配）
    remaining = dict(holdings)  # 卖出后的剩余金额

    # 第一轮：止损/止盈（只撤不买，风暴锁不干预卖出）
    for p in products:
        code = p.get("code", "")
        amt = holdings.get(code, 0)
        if amt <= 0:
            continue
        action, reason, rid = product_action(cfg, p, ctx.get("product_ctx", {}).get(code, {}))
        if action == "sell_all":
            shares = amt / nav_map.get(code, 1)
            orders.append({"side": "卖出", "code": code, "name": names.get(code, code),
                           "amount": round(amt, 2), "shares": round(shares, 2),
                           "reason": reason, "stop": True, "rule_id": rid})
            stopped_cash += amt
            remaining[code] = 0
        elif action == "sell_half":
            half = amt / 2
            shares = half / nav_map.get(code, 1)
            orders.append({"side": "卖出", "code": code, "name": names.get(code, code),
                           "amount": round(half, 2), "shares": round(shares, 2),
                           "reason": reason, "stop": False, "rule_id": rid})
            stopped_cash += half
            remaining[code] = amt - half

    # 卖出后各类资产金额（现金 = 原始现金 + 卖出现金）
    cash_now = holdings.get("cash", 0) + stopped_cash
    equity_now = sum(remaining.get(p.get("code"), 0) for p in products
                     if p.get("type") in ("equity", "gold"))
    bond_now = sum(remaining.get(p.get("code"), 0) for p in products if p.get("type") == "bond")

    # 目标金额
    equity_target_amt = total * target_equity
    bond_target_amt = total * target_bond
    cash_target_amt = total * target_cash

    # 第二轮：再平衡
    # 风暴锁：跳过全部买入（权益+固收），现金冻结；卖出（撤）不受限
    # EP 防御：禁止"现金→权益"买入，固收再平衡照常
    equity_gap = equity_target_amt - equity_now
    if equity_gap > 0 and not storm_active and not ep_lock and stopped_cash <= 0:
        target_eq_products = [p for p in products if p.get("type") in ("equity", "gold") and remaining.get(p.get("code"), 0) >= 0]
        if target_eq_products:
            code = target_eq_products[0]["code"]
            amt = round(equity_gap, 2)
            if amt > 100:
                orders.append({"side": "买入", "code": code, "name": names.get(code, code),
                               "amount": amt, "shares": None, "reason": "再平衡：权益低于目标仓位",
                               "stop": False, "rule_id": "REB-EQ"})
    elif equity_gap < 0 and not storm_active:
        # 权益超配 → 卖出权益（撤，不触发风暴锁限制）；风暴日同样允许撤离风险资产
        eq_products = [p for p in products if p.get("type") in ("equity", "gold") and remaining.get(p.get("code"), 0) > 0]
        sell_amt = min(abs(equity_gap), sum(remaining.get(p["code"], 0) for p in eq_products))
        if eq_products and sell_amt > 100:
            code = eq_products[0]["code"]
            amt = round(sell_amt, 2)
            orders.append({"side": "卖出", "code": code, "name": names.get(code, code),
                           "amount": amt, "shares": round(amt / nav_map.get(code, 1), 2),
                           "reason": "再平衡：权益超配，转固收", "stop": False, "rule_id": "REB-BOND"})
            stopped_cash += sell_amt
            remaining[code] -= sell_amt

    # 债券再平衡：风暴锁激活时跳过（现金冻结）；买入选权益暴露最低的产品
    bond_gap = bond_target_amt - bond_now
    if bond_gap > 0 and not storm_active:
        bond_candidates = [p for p in products if p.get("type") == "bond" and remaining.get(p.get("code"), 0) > 0]
        if not bond_candidates:
            bond_candidates = [p for p in products if p.get("type") == "bond"]
        if bond_candidates:
            bond_candidates.sort(key=lambda p: exposures.get(p.get("fund_code", p.get("code", "")), 1.0))
            code = bond_candidates[0]["code"]
            amt = round(min(bond_gap, max(0, stopped_cash + cash_now - cash_target_amt) + max(0, bond_gap)), 2)
            if amt > 100:
                orders.append({"side": "买入", "code": code, "name": names.get(code, code),
                               "amount": amt, "shares": None,
                               "reason": "再平衡：固收低于目标仓位（选权益暴露最低产品）",
                               "stop": False, "rule_id": "REB-BOND"})

    # 现金目标：多余现金提示（买入货基由用户自行处理，不生成指令）
    summary_lines = []
    for p in products:
        code = p.get("code", "")
        if remaining.get(code, 0) <= 0 and holdings.get(code, 0) > 0:
            summary_lines.append(f"- {names.get(code, code)}：已清仓（{exposures.get(code, '?')*100:.0f}% 权益暴露）")

    # 铁律说明三态化（架构师/内容分析师定稿文案）
    if storm_active:
        if stopped_cash > 0:
            summary_lines.append(
                f"- 铁律执行：卖出产生现金 {stopped_cash:,.0f} 元 → 风暴锁已激活（{'/'.join(storm_reasons or ['STORM']) if storm_reasons else 'STORM'}），"
                f"全部现金冻结，暂停一切买入（含固收）。解锁前，现金仅可停留于货基或活期。解锁后自动恢复再平衡")
    elif ep_lock:
        if stopped_cash > 0:
            summary_lines.append(
                f"- 铁律执行：权益减仓产生现金 {stopped_cash:,.0f} 元 → 固收再平衡照常执行，权益买入已被 [EP-CAP-10] 禁止。现金将按规则配置入固收产品")
    elif stopped_cash > 0:
        summary_lines.append(
            f"- 铁律执行：卖出产生现金 {stopped_cash:,.0f} 元 → 按规则配置入目标资产。不立即补仓权益，等待下一次规则信号")

    target_alloc = {"equity": target_equity, "bond": target_bond, "cash": target_cash}
    return orders, target_alloc, summary_lines


def portfolio_diagnostics(cfg, products, ctx):
    """组合诊断：年化波动率 / 250日最大回撤 / VaR95 / 实际权益暴露度
    ctx: {"returns": {code: 日收益Series}} 或 None（数据不足返回 None）
    """
    ret_map = ctx.get("returns")
    if not ret_map:
        return None
    weights = ctx.get("weights", {})
    codes = [c for c in weights if c in ret_map]
    if not codes:
        return None
    # 对齐索引
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
