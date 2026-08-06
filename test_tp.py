# -*- coding: utf-8 -*-
"""take_profit_signal 触发重构 + 去重 单元测试（动态档位基准，场景全覆盖）"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import pandas as pd
import rules
import state_store

CFG = {"rules": {"take_profit": {
    "base": {"equity": 0.18, "mixed": 0.12, "bond": 0.08},
    "vol_ref": {"equity": 0.2, "mixed": 0.08, "bond": 0.02},
    "clamp": {"lo": 0.06, "hi": 0.3},
    "cap": {"start_pctile": 0.7, "max_reduce": 0.27},
    "hold_factor": {"per_year": 0.15, "max_years": 3.0},
    "hold_transition": 0.02, "dd_factor": 0.35,
    "dd_clamp": {"lo": 0.04, "hi": 0.12},
    "fee_margin": 0.01, "min_hold_days": 7, "tier_gap": [0.05, 0.1],
    "shadow_mode": True,
}}, "min_order_amount": 100}

CFG_NO_GAP = {"rules": {"take_profit": {k: v for k, v in CFG["rules"]["take_profit"].items()
                                        if k != "tier_gap"}}, "min_order_amount": 100}


def make_nav(levels):
    return pd.DataFrame({"date": pd.date_range("2025-01-01", periods=len(levels)),
                         "nav": levels})


NAV_FLAT = make_nav([1.0] * 60)          # 波动≈0 → T_eff 稳定
NAV_DD = make_nav([1.0] * 10 + [1.5] * 30 + [1.2] * 20)  # 回撤 25%
NAV_BIG_DD = make_nav([1.0] * 10 + [1.5] * 30 + [0.9] * 20)  # 峰值+50%后跌穿成本线（当前约-10%）


def base_pctx(nav=None):
    return {"nav_series": nav if nav is not None else NAV_FLAT, "fees": "短期赎回0.5%"}


def common(exposure=0.92):
    return {"code": "X", "exposure": exposure, "bench_pctile": 0.5,
            "hold_years": 1.0, "today": "2026-08-06", "fee_rate": 0.005,
            "lots": [{"buy_date": "2025-09-15", "shares": 100, "cost": 100}]}


def sig(ctx, nav=None, cfg=CFG):
    return rules.take_profit_signal(cfg, base_pctx(nav), ctx)


def run():
    passed = 0
    def check(name, cond, extra=""):
        nonlocal passed
        if cond:
            passed += 1
            print(f"PASS {name} {extra}")
        else:
            print(f"FAIL {name} {extra}")

    c0 = common()
    st = rules.compute_take_profit(CFG, base_pctx(), {**c0, "r_hold": 0.20})
    fee, margin = 0.005, 0.01
    lv1 = max(st["T_eff"], 0.06) + fee + margin
    lv2 = max(st["T_eff"] + 0.05, 0.06) + fee + margin
    lv3 = max(st["T_eff"] + 0.10, 0.06) + fee + margin
    print(f"基准: T_eff={st['T_eff']:.4f} lv1={lv1:.4f} lv2={lv2:.4f} lv3={lv3:.4f}")

    # A 单档穿越：昨 < lv1，今 ∈ [lv1, lv2) → tp_1of3
    a = dict(c0, r_hold=lv1 + 0.02, r_hold_prev=lv1 - 0.05)
    act, detail, rid, tr = sig(a)
    check("A 单档穿越→1of3", act == "tp_1of3" and rid == "TP-YIELD-1", f"act={act}")

    # B 跳档：昨 < lv1，今 ≥ lv3 → tp_rest（取最高档）
    b = dict(c0, r_hold=lv3 + 0.05, r_hold_prev=lv1 - 0.05)
    act, detail, rid, tr = sig(b)
    check("B 跳档→rest(最高档)", act == "tp_rest" and rid == "TP-YIELD-3", f"act={act}")

    # C 边界穿越（原 < 漏检场景）：昨 lv1−ε，今 lv1+ε → 触发
    c = dict(c0, r_hold=lv1 + 0.001, r_hold_prev=lv1 - 0.001)
    act, detail, rid, tr = sig(c)
    check("C 边界穿越触发", act == "tp_1of3", f"act={act}")

    # D 高位持续持平（修复重复）：昨/今均 ≥ lv3 且未穿越 → 无信号
    d = dict(c0, r_hold=lv3 + 0.10, r_hold_prev=lv3 + 0.10)
    act, detail, rid, tr = sig(d)
    check("D 高位持续无重复信号", act is None, f"act={act}")

    # E 昨日恰好等于 lv1 且今日持平 → 等值判穿越（交由 main 层 repeat 去重）
    e = dict(c0, r_hold=lv1, r_hold_prev=lv1)
    act, detail, rid, tr = sig(e)
    check("E 等值持平判穿越(去重层兜底)", act == "tp_1of3", f"act={act}")

    # F 年化保护：hold_years=3, r_hold=-0.99 → 不崩溃
    f = dict(c0, hold_years=3.0, r_hold=-0.99, r_hold_prev=-0.95)
    act, detail, rid, tr = sig(f)
    check("F 极端亏损不崩溃", act is None, f"act={act}")

    # G 首日 r_prev 缺失 → 今日达标即触发
    g = dict(c0, r_hold=lv1 + 0.02, r_hold_prev=None)
    act, detail, rid, tr = sig(g)
    check("G 首日缺失可触发", act == "tp_1of3", f"act={act}")

    # H exposure=NaN → T_eff 有效、判定正常
    h = dict(common(exposure=float("nan")), r_hold=lv1 + 0.02, r_hold_prev=lv1 - 0.05)
    act, detail, rid, tr = sig(h)
    check("H exposure=NaN 不静默禁用", act == "tp_1of3" and tr and tr["T_eff"] == tr["T_eff"],
          f"T_eff={tr and tr['T_eff']}")

    # I bench_pctile=NaN → T_cap 不 NaN
    i = dict(c0, bench_pctile=float("nan"), r_hold=lv1 + 0.02, r_hold_prev=lv1 - 0.05)
    act, detail, rid, tr = sig(i)
    check("I bench_pctile=NaN 不崩溃", act == "tp_1of3" and tr and tr["T_cap"] == tr["T_cap"],
          f"T_cap={tr and tr['T_cap']}")

    # J 回撤保护优先：峰值≥T_eff 且回撤≥阈值 → tp_dd
    j = dict(c0, r_hold=0.20, r_hold_prev=0.19)
    act, detail, rid, tr = sig(j, NAV_DD)
    check("J 回撤保护→tp_dd", act == "tp_dd" and rid == "TP-DD", f"act={act}")

    # K 纯债豁免估值门：exposure=0, bench=0.95 → T_cap 不压缩
    k = dict(common(exposure=0.0), bench_pctile=0.95, r_hold=lv1 + 0.02, r_hold_prev=lv1 - 0.05)
    act, detail, rid, tr = sig(k)
    check("K 纯债豁免估值门", tr is not None and tr["T_cap"] >= 0.28, f"T_cap={tr and tr['T_cap']}")

    # L 高估值惩罚：equity 高 exposure + bench 0.95 → T_cap 压缩显著
    l = dict(c0, bench_pctile=0.95, r_hold=lv1 + 0.02, r_hold_prev=lv1 - 0.05)
    act, detail, rid, tr = sig(l)
    check("L 高估值上限门压缩", tr is not None and tr["T_cap"] <= 0.15, f"T_cap={tr and tr['T_cap']}")

    # M 去重：recent_tp_actions（今天记录不算、5交易日窗口、按 code 独立）
    import datetime
    t0 = datetime.date.today()
    t_2d = (t0 - datetime.timedelta(days=2)).isoformat()   # ≈2交易日：窗口内
    t_10d = (t0 - datetime.timedelta(days=10)).isoformat()  # ≈8交易日：窗口外
    t_30d = (t0 - datetime.timedelta(days=30)).isoformat()  # 窗口外
    fake_traces = [
        {"code": "A", "action": "tp_dd", "signal_date": t_2d},
        {"code": "A", "action": "tp_1of3", "signal_date": t0.isoformat()},   # 今天：不算
        {"code": "B", "action": "tp_2of3", "signal_date": t_10d},            # 窗口外：不算
        {"code": "C", "action": "tp_rest", "signal_date": t_30d},            # 窗口外：不算
    ]
    got = state_store.recent_tp_actions(traces=fake_traces, days=5)
    check("M 去重窗口判定",
          got.get("A") == "tp_dd" and "B" not in got and "C" not in got,
          f"got={got}")

    # N 回撤保护放宽（5.5a）：峰值曾≥T_eff 且回撤≥阈值，但当前已转亏（r_hold<0）→ 仍触发 tp_dd
    n = dict(c0, r_hold=-0.10, r_hold_prev=-0.09)
    act, detail, rid, tr = sig(n, NAV_BIG_DD)
    st_n = rules.compute_take_profit(CFG, base_pctx(NAV_BIG_DD), {**n, "r_hold": -0.10})
    check("N 转亏后回撤保护仍触发",
          act == "tp_dd" and rid == "TP-DD" and st_n["peak_r_hold"] >= st_n["T_eff"]
          and st_n["dd_ret"] >= st_n["dd_thr"],
          f"peak={st_n and st_n['peak_r_hold']:.3f} dd={st_n and st_n['dd_ret']:.3f} act={act}")

    # O 高波动 tier_gap 动态化（5.5b）：σ_ann=0.35（无 config tier_gap）→ 档距 [0.10,0.20]，
    # lv1-lv2 间距 ≥ 0.07；若仍用固定档则此点位会错误触发次档
    st_o = rules.compute_take_profit(CFG_NO_GAP, base_pctx(), {**c0, "sigma_ann": 0.35, "r_hold": 0.30})
    lv1_o = max(st_o["T_eff"], 0.06) + fee + margin
    lv2_o = max(st_o["T_eff"] + 0.10, 0.06) + fee + margin
    lv2_fix = max(st_o["T_eff"] + 0.05, 0.06) + fee + margin
    r_now_o = lv2_o - 0.005
    o = dict(c0, sigma_ann=0.35, r_hold=r_now_o, r_hold_prev=lv1_o - 0.05)
    act, detail, rid, tr = sig(o, cfg=CFG_NO_GAP)
    check("O 高波动tier_gap动态增大",
          act == "tp_1of3" and (lv2_o - lv1_o) >= 0.07 and r_now_o >= lv2_fix,
          f"spacing={lv2_o - lv1_o:.4f} act={act}")

    print(f"\n通过 {passed}/15")
    return passed == 15


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
