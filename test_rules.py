# -*- coding: utf-8 -*-
# rules.py core function unit tests (V3: score_candidate 重构 P0/P1/P2 + build_order_book 硬门槛/换手抑制/rf_annual).
# Cover: equity_target / ep_threshold / score_candidate / portfolio_diagnostics / build_order_book.
# Style like test_tp.py: zero external deps, pure asserts, standalone runnable
# (python test_rules.py; non-zero exit code means failure).
# Zero network: build_order_book internally calls add_trading_days which imports akshare
# and fetches the trade calendar online; this test replaces rules.add_trading_days with a
# deterministic fake at runtime (rules.py itself is NOT modified).
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import math
import pandas as pd
import rules


# ---- zero-network: replace add_trading_days (real impl would call akshare online) ----
def _fake_add_trading_days(start_date, n):
    return '2026-08-10' if n == 1 else '2026-08-12'

rules.add_trading_days = _fake_add_trading_days

CFG = {'target': {'equity': {'base': 0.40, 'band': 0.05}, 'cash': {'base': 0.10}},
       'rules': {'take_profit': {'shadow_mode': True, 'min_hold_days': 7},
                 'min_order_amount': 100}}
CFG_NS = {'target': {'equity': {'base': 0.40, 'band': 0.05}, 'cash': {'base': 0.10}},
          'rules': {'take_profit': {'shadow_mode': False, 'min_hold_days': 7},
                    'min_order_amount': 100}}
TODAY = '2026-08-07'


def ids_of(triggers):
    return {t.get('id') for t in triggers}


def approx(a, b, tol=0.01):
    return abs(a - b) <= tol


def nav_df(vals):
    return pd.DataFrame({'nav': vals},
                        index=pd.date_range('2025-01-01', periods=len(vals), freq='B'))


def nav_df2(vals, start='2025-01-01'):
    return pd.DataFrame({'date': pd.date_range(start, periods=len(vals), freq='B'),
                         'nav': vals})


def nav_trend(n=60):
    return nav_df([1.002 ** i * (1 + 0.0003 * ((i * 7) % 5 - 2)) for i in range(n)])


def nav_flat(n=60):
    return nav_df([1.0 + 0.0002 * ((i * 3) % 5 - 2) for i in range(n)])


def nav_mu_vol(mu, vol, n=120, start='2025-01-01'):
    # 确定性日收益：年化 mu / 年化 vol（噪声模式固定 → 纯确定性）
    rs = []
    for i in range(n):
        noise = (((i * 7) % 5 - 2) / 2.0)
        rs.append(mu / 250.0 + noise * vol / math.sqrt(250.0))
    nav = [1.0]
    for r in rs:
        nav.append(nav[-1] * (1.0 + r))
    return nav_df2(nav[1:], start)


def nav_smooth(n=120):
    # 慢正弦日收益 → 一阶自相关 ≈ cos(2π/40) ≈ 0.988 > 0.9（平滑检测用例）
    rs = [0.001 * math.sin(2 * math.pi * i / 40.0) for i in range(n)]
    nav = [1.0]
    for r in rs:
        nav.append(nav[-1] * (1 + r))
    return nav_df2(nav[1:])


def nav_tiny_vol(n=80):
    # 波动趋零（年化 σ << 1%）→ σ 下限 0.01 用例
    return nav_df2([1.0 + ((i * 7) % 5 - 2) * 1e-6 for i in range(n)])


def ret_series(n, amp, phase=0.0, start='2025-01-01'):
    return pd.Series([amp * math.sin(2 * math.pi * (i + phase) / 20.0) for i in range(n)],
                     index=pd.date_range(start, periods=n, freq='B'))


def full_pctx(nav=None, **kw):
    p = {'type': 'equity', 'name': 'TestEquity',
         'nav_series': nav if nav is not None else nav_trend(),
         'max_dd': '-17.9%', 'scale': '43.53亿',
         'purchase_status': '开放申购', 'purchase_meta': {'daily_limit': 50000},
         'fees': '申购0.15%，短期赎回0.5%，管理0.5%'}
    p.update(kw)
    return p


def bok_ctx(**kw):
    ctx = {'equity_target': 0.4,
           'mvs': {'E': 1000, 'B': 0},
           'shares': {'E': 1000, 'B': 0},
           'product_name': {'E': 'TestEquity', 'B': 'TestBond'},
           'cash_mv': 50000, 'settled_cash': 0, 'pending_cash': [],
           'lots': {'E': [], 'B': []},
           'nav': {'E': 1.0, 'B': 1.0},
           'total': 100000,
           'tp_ctx': {},
           'product_ctx': {},
           'exposure': {'E': 0.92, 'B': 0.05}}
    ctx.update(kw)
    return ctx


def prods_equity():
    return [{'code': 'E', 'type': 'equity', 'status': 'held', 'name': 'TestEquity'}]


def prods_eq_bond():
    return [{'code': 'E', 'type': 'equity', 'status': 'held', 'name': 'TestEquity'},
            {'code': 'B', 'type': 'bond', 'status': 'held', 'name': 'TestBond'}]


def obs(code, **kw):
    return {'code': code, 'type': 'equity', 'status': 'observe', 'name': code, **kw}


def test_equity_target(check):
    # G1.1 p300=0.95 -> target 0.05 + LAD-CSI300-95
    t, tr = rules.equity_target(CFG, {'csi300_pe_pctile': 0.95})
    check('G1 300分位95% -> 目标5%', t == 0.05 and 'LAD-CSI300-95' in ids_of(tr)
          and 'MIN-MERGE' in ids_of(tr), 't=%s' % t)
    # G1.2 p300=0.90 -> target 0.20 + LAD-CSI300-90
    t, tr = rules.equity_target(CFG, {'csi300_pe_pctile': 0.90})
    check('G1 300分位90% -> 目标20%', t == 0.20 and 'LAD-CSI300-90' in ids_of(tr), 't=%s' % t)
    # G1.3 p300=0.80 动量>0 -> 不触发，返回 base 0.40
    t, tr = rules.equity_target(CFG, {'csi300_pe_pctile': 0.80, 'csi300_mom20': 0.03})
    check('G1 300分位80%动量>0不触发', t == 0.40 and tr == [], 't=%s' % t)
    # G1.4 p300=0.80 动量<0 -> target 0.30 + LAD-CSI300-80
    t, tr = rules.equity_target(CFG, {'csi300_pe_pctile': 0.80, 'csi300_mom20': -0.01})
    check('G1 300分位80%动量<0 -> 30%', t == 0.30 and 'LAD-CSI300-80' in ids_of(tr), 't=%s' % t)
    # G1.5 p300=0.85 (区间内) 动量<0 -> 命中 >=0.80 档 0.30
    t, tr = rules.equity_target(CFG, {'csi300_pe_pctile': 0.85, 'csi300_mom20': -0.02})
    check('G1 300分位85%动量<0 -> 30%', t == 0.30 and 'LAD-CSI300-80' in ids_of(tr), 't=%s' % t)
    # G1.6 中证500: p500>=0.75 且 m500<-5% -> target 0.05 + LAD-CSI500-75
    t, tr = rules.equity_target(CFG, {'csi500_pe_pctile': 0.75, 'csi500_mom20': -0.06})
    check('G1 500分位75%动量-6% -> 5%', t == 0.05 and 'LAD-CSI500-75' in ids_of(tr), 't=%s' % t)
    # G1.7 中证500 边界: m500=-0.05 须严格小于 -> 不触发
    t, tr = rules.equity_target(CFG, {'csi500_pe_pctile': 0.75, 'csi500_mom20': -0.05})
    check('G1 500动量恰-5%不触发', t == 0.40 and tr == [], 't=%s' % t)
    # G1.8 创业板: pcyb>=0.90 -> target 0.05 + LAD-CYB-90
    t, tr = rules.equity_target(CFG, {'csi_cyb_pe_pctile': 0.90})
    check('G1 创业板分位90% -> 5%', t == 0.05 and 'LAD-CYB-90' in ids_of(tr), 't=%s' % t)
    # G1.9 多信号同时触发 -> MIN-MERGE 取 min
    t, tr = rules.equity_target(CFG, {'csi300_pe_pctile': 0.90, 'csi500_pe_pctile': 0.80,
                                      'csi500_mom20': -0.10})
    check('G1 多信号合并取min', t == 0.05 and 'LAD-CSI300-90' in ids_of(tr)
          and 'LAD-CSI500-75' in ids_of(tr) and 'MIN-MERGE' in ids_of(tr), 't=%s' % t)
    # G1.10 EP 激活: ep分位 < 动态阈值 -> 目标锁定为阈值
    t, tr = rules.equity_target(CFG, {'ep_premium_pctile': 0.05, 'y10_pctile': 0.5})
    check('G1 EP激活锁10%', t == 0.10 and any('EP-CAP-10' == x.get('id') and '已激活' in x.get('text', '')
                                              for x in tr), 't=%s' % t)
    # G1.11 EP 未激活 -> 保持 base，仍输出 EP-CAP-10 记录
    t, tr = rules.equity_target(CFG, {'ep_premium_pctile': 0.5, 'y10_pctile': 0.5})
    check('G1 EP未激活保持base', t == 0.40 and any('EP-CAP-10' == x.get('id') and '不触发' in x.get('text', '')
                                                   for x in tr), 't=%s' % t)
    # G1.12 EP 与阶梯交互: 阶梯5% 比 EP阈值10% 更保守 -> min 保持 5%
    t, tr = rules.equity_target(CFG, {'csi300_pe_pctile': 0.95, 'ep_premium_pctile': 0.05,
                                      'y10_pctile': 0.5})
    check('G1 阶梯5%与EP取min', t == 0.05, 't=%s' % t)
    # G1.13 NaN 输入 -> 比较均为 False，不触发不崩溃
    t, tr = rules.equity_target(CFG, {'csi300_pe_pctile': float('nan')})
    check('G1 NaN分位不触发', t == 0.40 and tr == [], 't=%s' % t)
    # G1.14 空 ctx -> base_val
    t, tr = rules.equity_target(CFG, {})
    check('G1 空ctx返回base', t == 0.40 and tr == [], 't=%s' % t)
    # G1.15 客户子配置 base 覆盖 -> base_val=0.3
    t, tr = rules.equity_target({'target': {'equity': {'base': 0.3}}}, {})
    check('G1 子配置base覆盖', t == 0.30, 't=%s' % t)


def test_ep_threshold(check):
    # G2 动态阈值三段分位 + 边界 + NaN/缺失/非法
    cases = [((0.1,), 0.15), ((0.5,), 0.10), ((0.9,), 0.05),
             ((0.2,), 0.10), ((0.8,), 0.10), ((float('nan'),), 0.10),
             ((None,), 0.10), (('abc',), 0.10), ((), 0.10)]
    for i, (args, want) in enumerate(cases):
        ctx = {'y10_pctile': args[0]} if args else {}
        got = rules.ep_threshold(ctx)
        check('G2 ep_threshold场景%d -> %.2f' % (i + 1, want), got == want, 'got=%s' % got)


def test_score_candidate(check):
    # ================= P0 止血 =================
    # G3.1 pctx=None -> None；全维度缺失（完整度 0 < 0.8）-> None
    check('G3 pctx=None -> None', rules.score_candidate(None, {}) is None)
    check('G3 全缺失 -> None', rules.score_candidate({}, {}) is None
          and rules.score_candidate({'fees': '无'}, {}) is None)
    # G3.2 rf 生效：同 pctx，rf 越高夏普越低 → 总分越低（nav 需非平滑、σ 未封顶）
    pa = full_pctx(nav=nav_mu_vol(0.30, 0.20))
    s_rf0 = rules.score_candidate(pa, CFG, rf_annual=0.0)
    s_rf5 = rules.score_candidate(pa, CFG, rf_annual=0.05)
    check('G3 rf生效', s_rf0 is not None and 0 < s_rf5 < s_rf0 <= 100,
          'rf0=%.2f rf5=%.2f' % (s_rf0, s_rf5))
    # G3.3 max_dd 解析：-17.9%→0.821；-0.05→0.95；0.4→0.6（abs 防御正数）
    check('G3 max_dd解析', approx(rules._tail_risk({'max_dd': '-17.9%'}), 0.821)
          and approx(rules._tail_risk({'max_dd': -0.05}), 0.95)
          and approx(rules._tail_risk({'max_dd': 0.4}), 0.6))
    # G3.4 σ 下限：波动趋零（年化 σ<1%）→ 不爆表，alpha 子分 0-1，总分 0-100
    pt = full_pctx(nav=nav_tiny_vol())
    a_floor = rules._excess_alpha(pt, CFG, 0.0)
    s_floor = rules.score_candidate(pt, CFG)
    check('G3 σ下限不爆表', a_floor is not None and 0.0 <= a_floor <= 1.0
          and s_floor is not None and 0 <= s_floor <= 100, 'a=%s s=%s' % (a_floor, s_floor))
    # G3.5 平滑检测：慢正弦日收益自相关≈0.988>0.9 → alpha 中性 0.5（不进正常池）
    ps = {'nav_series': nav_smooth(), 'type': 'equity', 'name': 'X'}
    check('G3 平滑检测', rules._excess_alpha(ps, CFG, 0.0) == 0.5)
    # G3.6 成立<18个月：评分不拦截（硬门槛在 build_order_book 的 cands 过滤处，G5 覆盖）
    py = full_pctx(inception='2026-03-01')
    sy = rules.score_candidate(py, CFG)
    check('G3 成立<18月不进评分拦截', sy is not None and 0 <= sy <= 100, 's=%s' % sy)
    # ================= P1 结构重构 =================
    # G3.7 权重表存在且 =100
    w = rules.candidate_weights()
    check('G3 权重表=100', sum(w.values()) == 100
          and w == {'marginal': 20, 'tco': 18, 'alpha': 17,
                    'tail': 15, 'stability': 15, 'tradability': 15}, 'w=%s' % w)
    # G3.8 pool 机制：同组百分位生效（差距放大）；无池 → winsorized 原始值
    A = full_pctx(nav=nav_mu_vol(0.30, 0.20))
    B = full_pctx(nav=nav_mu_vol(0.05, 0.20))
    d0 = rules.score_candidate(A, CFG) - rules.score_candidate(B, CFG)
    rules._set_candidate_pool([A, B])
    d1 = rules.score_candidate(A, CFG) - rules.score_candidate(B, CFG)
    rules._clear_candidate_pool()
    check('G3 pool放大同组差距', d1 > d0 > 0, 'd0=%.2f d1=%.2f' % (d0, d1))
    # G3.9 组内单样本 → 全维中性 0.5 → 基准分 50（完整度 100 无惩罚）
    rules._set_candidate_pool([A])
    s_single = rules.score_candidate(A, CFG)
    rules._clear_candidate_pool()
    check('G3 池单样本中性50', approx(s_single, 50.0), 's=%s' % s_single)
    # G3.10 TCO：高费用 → 低子分；持有年数可配（cfg.rules.candidate_holding_years）
    t_hi = rules._tco_score({'fees': '申购0.15%，短期赎回0.5%，管理0.5%'}, CFG)
    t_lo = rules._tco_score({'fees': '申购0.05%，短期赎回0%，管理0.15%'}, CFG)
    t_y2 = rules._tco_score({'fees': '管理0.5%'}, {})
    t_y5 = rules._tco_score({'fees': '管理0.5%'}, {'rules': {'candidate_holding_years': 5}})
    check('G3 TCO高费低分', t_hi < t_lo and approx(t_y2, 0.5) and t_y5 < t_y2,
          'hi=%s lo=%s y2=%s y5=%s' % (t_hi, t_lo, t_y2, t_y5))
    # G3.11 边际贡献：与组合其他产品高相关 → 低贡献（top10 同 → 更低）
    c1 = nav_mu_vol(0.10, 0.20)
    cand = {'nav_series': c1, 'top10': [{'stock_name': 'A'}, {'stock_name': 'B'}]}
    o_same = {'nav_series': c1, 'top10': [{'stock_name': 'A'}, {'stock_name': 'B'}]}
    o_anti = {'nav_series': nav_df2([2.0 - x for x in c1['nav']]),
              'top10': [{'stock_name': 'C'}, {'stock_name': 'D'}]}
    rules._set_candidate_pool([], others=[o_same])
    m_same = rules._marginal_contribution(cand)
    rules._set_candidate_pool([], others=[o_anti])
    m_diff = rules._marginal_contribution(cand)
    rules._clear_candidate_pool()
    check('G3 边际贡献相关性与top10', m_same < m_diff and approx(m_same, 0.0) and approx(m_diff, 1.0),
          'same=%s diff=%s' % (m_same, m_diff))
    # G3.12 惩罚项：尾部风险<0.3 → ×0.6；可交易<0.3 → ×0.6；完整度折扣 0.9+0.1×完整度
    check('G3 惩罚项与折扣', approx(rules._synthesize(50.0, 0.2, 0.8, 1.0), 30.0)
          and approx(rules._synthesize(50.0, 0.5, 0.2, 1.0), 30.0)
          and approx(rules._synthesize(50.0, 0.5, 0.8, 1.0), 50.0)
          and approx(rules._synthesize(50.0, 0.5, 0.5, 0.8), 49.0))
    # G3.13 完整度门槛：可用权重 <80% → None（仅 max_dd 一个维度 = 15/100）
    check('G3 完整度门槛', rules.score_candidate({'max_dd': '-0.1'}, CFG) is None)
    # G3.14 缺失填充：组内中位数（_group_median 直接验证）
    gm = rules._group_median('tco', [{'fees': '管理1%'}, {'fees': '管理0.5%'}], CFG, 0.0)
    check('G3 缺失组中位数填充', approx(gm, 0.25), 'gm=%s' % gm)
    # ================= P2 贝叶斯收缩 =================
    # G3.15 收缩：λ=τ²/(τ²+SE²)；own 极端离群 + SE 巨大 → 收缩后百分位 < 未收缩
    # own=0.30 极端离群且 SE 巨大（年数 0.05）→ 收缩到中位数附近，落至 0.08 成员之下 → 百分位下降
    raw = [0.05, 0.06, 0.08, 0.30]
    unshr = rules._rank01(raw, 0.30)
    pct = rules._shrink_pct(3, raw, [0.5, 0.5, 0.1, 3.0], [10.0, 10.0, 10.0, 0.05])
    check('G3 收缩拉低离群', pct < unshr and 0.5 <= pct < unshr, 'unshr=%.3f shr=%.3f' % (unshr, pct))
    # G3.16 组内全同（τ=0）→ 收缩无信息 → 0.5
    check('G3 全同组中性', rules._shrink_pct(0, [0.1, 0.1], [0.5, 0.5], [1.0, 1.0]) == 0.5)
    # ================= 迭代六：评分层数据源增强（holder/scale_hist/leverage/turnover/partner） =================
    # G3.17 稳定性四因子（任期5/规模4/持有人3/换手3）：
    #   - holder inst_ratio=0.95（机构定制）→ 子分显著低于 inst_ratio=0.5（均衡结构）
    sh_h = rules._stability({'holder': {'inst_ratio': 0.95}})
    sh_m = rules._stability({'holder': {'inst_ratio': 0.5}})
    check('G3 稳定性机构定制折价', sh_h < sh_m - 0.05 and 0.0 <= sh_h < 1.0,
          'inst95=%.3f inst50=%.3f' % (sh_h, sh_m))
    #   - turnover.value=5.0（高换手）→ 低于 value=0.5（低换手）
    st_hi = rules._stability({'turnover': {'value': 5.0}})
    st_lo = rules._stability({'turnover': {'value': 0.5}})
    check('G3 稳定性高换手折价', st_hi < st_lo, 'to5=%.3f to0.5=%.3f' % (st_hi, st_lo))
    #   - scale_hist 最新期 net_assets 生效（优先于 scale 字段）；change_rate 翻倍/骤降 → 规模子分 ×0.7
    s_field = rules._stability({'scale': '43.53亿'})
    s_sh = rules._stability({'scale': '1亿', 'scale_hist': [{'date': '2025-12-31',
                             'net_assets': 43.53, 'change_rate': 0.1}]})
    s_dbl = rules._stability({'scale_hist': [{'date': '2025-12-31', 'net_assets': 43.53,
                             'change_rate': 2.0}]})
    s_crash = rules._stability({'scale_hist': [{'date': '2025-12-31', 'net_assets': 43.53,
                                'change_rate': -0.5}]})
    check('G3 规模变动监测', approx(s_sh, s_field) and approx(s_dbl, s_crash)
          and s_dbl < s_sh - 0.05, 'f=%.3f sh=%.3f dbl=%.3f crash=%.3f'
          % (s_field, s_sh, s_dbl, s_crash))
    #   - 全缺失 → 中性（不崩、0-1）
    s_none = rules._stability({})
    check('G3 稳定性全缺失中性', s_none is not None and 0.0 <= s_none <= 1.0 and approx(s_none, 0.73),
          'none=%s' % s_none)
    # G3.18 债基杠杆修正：leverage=1.5 → 尾部子分 ×0.7；1.15/1.2 → ×0.85；≤1.1 → 不修正
    t_dd = rules._tail_risk({'max_dd': '-0.05'})
    check('G3 尾部杠杆修正',
          approx(rules._tail_risk({'max_dd': '-0.05', 'leverage': 1.5}), t_dd * 0.7)
          and approx(rules._tail_risk({'max_dd': '-0.05', 'leverage': 1.15}), t_dd * 0.85)
          and approx(rules._tail_risk({'max_dd': '-0.05', 'leverage': 1.2}), t_dd * 0.85)
          and approx(rules._tail_risk({'max_dd': '-0.05', 'leverage': 1.1}), t_dd)
          and approx(rules._tail_risk({'max_dd': '-0.05', 'leverage': 0.9}), t_dd),
          'dd=%.3f' % t_dd)
    # G3.19 TCO A/C 份额择优（partner 结构化费率，低者生效；持有年数可配）
    pf = {'mgmt': 0.012, 'trustee': 0.002, 'sales': 0.004, 'purchase': 0.0, 'short_redeem': 0.015}
    partner = {'code': '016858', 'fees': pf}
    # partner_total(2年) = (0.012+0.002+0.004)*2 + 0 + 0.015 = 0.051 > 本产品 0.015 → 本产品低者生效
    t_own = rules._tco_score({'fees': '管理0.5%，短期赎回0.5%'}, {'rules': {'candidate_holding_years': 2}})
    t_both = rules._tco_score({'fees': '管理0.5%，短期赎回0.5%', 'partner': partner},
                              {'rules': {'candidate_holding_years': 2}})
    check('G3 TCO partner低者生效own档', approx(t_both, t_own) and approx(t_both, 0.25),
          'own=%s both=%s' % (t_own, t_both))
    # 本产品无费率但 partner 存在 → 用 partner 口径（None → 数值）
    t_pr = rules._tco_score({'partner': partner}, {'rules': {'candidate_holding_years': 2}})
    check('G3 TCO partner独立可用', t_pr == 0.0
          and rules._tco_score({}, {'rules': {'candidate_holding_years': 2}}) is None, 'pr=%s' % t_pr)
    # partner 更优档（管理0.2%/托管0.05%/销售0.05%/短期赎回0.1%）：partner_total(2年)=0.007 < 本产品 0.015
    pf_c = {'mgmt': 0.002, 'trustee': 0.0005, 'sales': 0.0005, 'purchase': 0.0, 'short_redeem': 0.001}
    t_c2 = rules._tco_score({'fees': '管理0.5%，短期赎回0.5%', 'partner': {'code': 'X', 'fees': pf_c}},
                            {'rules': {'candidate_holding_years': 2}})
    t_c5 = rules._tco_score({'fees': '管理0.5%，短期赎回0.5%', 'partner': {'code': 'X', 'fees': pf_c}},
                            {'rules': {'candidate_holding_years': 5}})
    check('G3 TCO partner择优与持有年数', approx(t_c2, 0.65) and t_c2 > t_own and approx(t_c5, 0.2),
          'c2=%s c5=%s' % (t_c2, t_c5))


def test_portfolio_diagnostics(check):
    # G4.1 正常: 2产品120天 + 满对齐基准 -> 全部字段有值
    returns = {'BOND': ret_series(120, 0.0005), 'EQ': ret_series(120, 0.02)}
    weights = {'BOND': 0.6, 'EQ': 0.4}
    bench_full = {'returns': returns, 'weights': weights,
                  'bench_returns': {'CSI300': ret_series(120, 0.015)},
                  'bench_weights': {'CSI300': 1.0}}
    out = rules.portfolio_diagnostics(CFG, bench_full)
    check('G4 正常组合键完整', out is not None and set(out.keys()) ==
          {'vol', 'max_dd', 'var95', 'excess_ann', 'alpha', 'beta', 'ir'}, 'out=%s' % out)
    check('G4 vol/max_dd/var95合理', out is not None and 0.01 < out['vol'] < 0.35
          and out['max_dd'] < 0 and out['var95'] < 0,
          'vol=%s dd=%s var95=%s' % (out and out['vol'], out and out['max_dd'], out and out['var95']))
    check('G4 基准对齐足四字段非None', out is not None and out['excess_ann'] is not None
          and out['alpha'] is not None and out['beta'] is not None and out['beta'] > 0
          and out['ir'] is not None,
          'ex=%s a=%s b=%s ir=%s' % (out and out['excess_ann'], out and out['alpha'],
                                     out and out['beta'], out and out['ir']))
    # G4.2 样本不足（<30天）-> None
    r29 = {'A': ret_series(29, 0.02)}
    check('G4 样本<30 -> None',
          rules.portfolio_diagnostics(CFG, {'returns': r29, 'weights': {'A': 1.0}}) is None)
    # G4.3 基准对齐不足（<60天）-> 四字段 None，vol 等仍正常
    bench_short = {'returns': returns, 'weights': weights,
                   'bench_returns': {'CSI300': ret_series(50, 0.015, start='2025-06-01')},
                   'bench_weights': {'CSI300': 1.0}}
    out = rules.portfolio_diagnostics(CFG, bench_short)
    check('G4 基准对齐<60四字段None', out is not None and out['vol'] is not None
          and out['excess_ann'] is None and out['alpha'] is None
          and out['beta'] is None and out['ir'] is None, 'vol=%s' % (out and out['vol']))
    # G4.4 纯债低波动 vs 全权益高波动
    b = rules.portfolio_diagnostics(CFG,
                                    {'returns': {'B': ret_series(120, 0.0005)}, 'weights': {'B': 1.0}})
    e = rules.portfolio_diagnostics(CFG,
                                    {'returns': {'E': ret_series(120, 0.02)}, 'weights': {'E': 1.0}})
    check('G4 纯债波动显著低', b is not None and e is not None
          and b['vol'] < e['vol'] / 3 and b['max_dd'] > e['max_dd'] and b['max_dd'] < 0,
          'vol_b=%s vol_e=%s dd_b=%s dd_e=%s' % (b and b['vol'], e and e['vol'],
                                                 b and b['max_dd'], e and e['max_dd']))
    # G4.5 空 returns -> None
    check('G4 空returns -> None',
          rules.portfolio_diagnostics(CFG, {'returns': {}, 'weights': {}}) is None)
    # G4.6 weights 与 returns 无交集 -> None
    check('G4 权重无交集 -> None',
          rules.portfolio_diagnostics(CFG, {'returns': returns, 'weights': {'X': 1.0}}) is None)


def test_build_order_book(check):
    today = TODAY
    # G5.1 止盈非影子 -> 生成卖出订单（amount=市值1/3，确认/到账日确定）
    ctx = bok_ctx(mvs={'E': 60000}, shares={'E': 60000},
                  lots={'E': [{'buy_date': '2025-01-01', 'shares': 60000}]},
                  nav={'E': 1.0}, cash_mv=50000,
                  tp_ctx={'E': {'action': 'tp_1of3', 'detail': '首档止盈', 'rid': 'TP-YIELD-1'}})
    orders, alloc, summary, tp_act = rules.build_order_book(CFG_NS, prods_equity(), ctx, today=today)
    check('G5 止盈生成卖出单', len(orders) == 1 and orders[0]['side'] == '卖出'
          and orders[0]['code'] == 'E' and orders[0]['rule_id'] == 'TP-YIELD-1'
          and orders[0]['stop'] is True, str(orders))
    check('G5 卖出金额为市值1/3', len(orders) == 1 and approx(orders[0]['amount'], 20000.0)
          and approx(orders[0]['shares'], 20000.0), 'amt=%s' % orders[0]['amount'])
    check('G5 结算日确定', len(orders) == 1 and orders[0]['confirm_date'] == '2026-08-10'
          and orders[0]['settle_date'] == '2026-08-12', str(orders[0]))
    check('G5 非影子tp_actions为空', tp_act == {}, str(tp_act))
    # G5.2 影子模式 -> tp_actions 记录信号，orders 无卖出
    ctx = bok_ctx(mvs={'E': 60000}, shares={'E': 60000},
                  lots={'E': [{'buy_date': '2025-01-01', 'shares': 60000}]},
                  nav={'E': 1.0}, cash_mv=50000,
                  tp_ctx={'E': {'action': 'tp_1of3', 'detail': '首档止盈', 'rid': 'TP-YIELD-1'}})
    orders, alloc, summary, tp_act = rules.build_order_book(CFG, prods_equity(), ctx, today=today)
    check('G5 影子模式无卖出订单', orders == [], str(orders))
    check('G5 影子模式记录信号', 'E' in tp_act and tp_act['E']['side'] == '信号'
          and tp_act['E']['rule_id'] == 'TP-YIELD-1' and approx(tp_act['E']['amount'], 20000.0),
          str(tp_act))
    check('G5 影子摘要行', any('止盈信号' in x and '影子模式' in x for x in summary), str(summary))
    # G5.3 repeat 去重 -> 无动作
    ctx = bok_ctx(mvs={'E': 60000}, shares={'E': 60000},
                  lots={'E': [{'buy_date': '2025-01-01', 'shares': 60000}]},
                  nav={'E': 1.0}, cash_mv=50000,
                  tp_ctx={'E': {'action': 'tp_1of3', 'detail': 'x', 'rid': 'TP-YIELD-1',
                                'repeat': True}})
    orders, alloc, summary, tp_act = rules.build_order_book(CFG_NS, prods_equity(), ctx, today=today)
    check('G5 repeat去重无动作', orders == [] and tp_act == {}, str(orders))
    # G5.4 短仓豁免: 持有<7天笔不计入卖出，reason 含豁免说明
    ctx = bok_ctx(mvs={'E': 60000}, shares={'E': 60000},
                  lots={'E': [{'buy_date': '2025-01-01', 'shares': 54000},
                              {'buy_date': '2026-08-06', 'shares': 6000}]},
                  nav={'E': 1.0}, cash_mv=50000,
                  tp_ctx={'E': {'action': 'tp_1of3', 'detail': '首档止盈', 'rid': 'TP-YIELD-1'}})
    orders, alloc, summary, tp_act = rules.build_order_book(CFG_NS, prods_equity(), ctx, today=today)
    check('G5 豁免笔不计入卖出', len(orders) == 1 and approx(orders[0]['amount'], 18000.0)
          and '6,000.00' in orders[0]['reason'] and '豁免' in orders[0]['reason'],
          str(orders))
    # G5.5 卖出金额低于最小门槛 -> 无操作
    ctx = bok_ctx(equity_target=0.0, mvs={'E': 100}, shares={'E': 100},
                  lots={'E': [{'buy_date': '2025-01-01', 'shares': 100}]},
                  nav={'E': 1.0}, cash_mv=50000,
                  tp_ctx={'E': {'action': 'tp_1of3', 'detail': 'x', 'rid': 'TP-YIELD-1'}})
    orders, alloc, summary, tp_act = rules.build_order_book(CFG_NS, prods_equity(), ctx, today=today)
    check('G5 卖出低于最小额无操作', orders == [] and tp_act == {}, str(orders))
    # G5.6 权益缺口 -> 生成买入订单（REB-EQ）
    ctx = bok_ctx(mvs={'E': 1000}, cash_mv=50000)
    orders, alloc, summary, tp_act = rules.build_order_book(CFG, prods_equity(), ctx, today=today)
    check('G5 权益缺口买入', len(orders) == 1 and orders[0]['side'] == '买入'
          and orders[0]['code'] == 'E' and orders[0]['rule_id'] == 'REB-EQ'
          and approx(orders[0]['amount'], 39000.0) and orders[0]['stop'] is False, str(orders))
    # G5.7 storm_active -> 买入冻结（只卖不买）
    ctx = bok_ctx(mvs={'E': 1000}, cash_mv=50000)
    orders, alloc, summary, tp_act = rules.build_order_book(CFG, prods_equity(), ctx,
                                                            storm_active=True, today=today)
    check('G5 风暴冻结买入', orders == [], str(orders))
    check('G5 风暴摘要行', any('市场预警' in x and '买入冻结' in x for x in summary), str(summary))
    # G5.8 cooldown_active -> 冻结 + 冷却期摘要
    ctx = bok_ctx(mvs={'E': 1000}, cash_mv=50000)
    orders, alloc, summary, tp_act = rules.build_order_book(CFG, prods_equity(), ctx,
                                                            cooldown_active=True, today=today)
    check('G5 冷却期冻结买入', orders == [], str(orders))
    check('G5 冷却期摘要行', any('冷却期' in x for x in summary), str(summary))
    # G5.9 在途/余额约束: cash_mv=0 且无在途到账 -> 买入受限（金额0不生成）
    ctx = bok_ctx(mvs={'E': 1000}, cash_mv=0, settled_cash=0)
    orders, alloc, summary, tp_act = rules.build_order_book(CFG, prods_equity(), ctx, today=today)
    check('G5 现金不足不买入', orders == [] and any('无规则触发' in x for x in summary),
          str(summary))
    # G5.10 空组合（无产品）-> 无操作
    orders, alloc, summary, tp_act = rules.build_order_book(CFG, [],
                                                            bok_ctx(mvs={'E': 1000}, cash_mv=50000),
                                                            today=today)
    check('G5 空产品无操作', orders == [] and tp_act == {}, str(orders))
    # G5.11 零持仓关注池产品 -> BUY-NEW 建仓（pctx 缺失时硬门槛 fail-open）
    obs1 = [obs('O')]
    ctx = bok_ctx(mvs={'O': 0}, product_name={'O': 'Observe'}, nav={'O': 1.0}, cash_mv=50000,
                  exposure={'O': 0.9})
    orders, alloc, summary, tp_act = rules.build_order_book(CFG, obs1, ctx, today=today)
    check('G5 关注池建仓BUY-NEW', len(orders) == 1 and orders[0]['rule_id'] == 'BUY-NEW'
          and approx(orders[0]['amount'], 40000.0) and '关注池建仓' in orders[0]['reason'],
          str(orders))
    # G5.12 ep_lock -> 权益买入暂停，固收再平衡照常
    ctx = bok_ctx(mvs={'E': 1000, 'B': 0})
    orders, alloc, summary, tp_act = rules.build_order_book(CFG, prods_eq_bond(), ctx,
                                                            ep_lock=True, today=today)
    check('G5 ep_lock仅固收买入', len(orders) == 1 and orders[0]['code'] == 'B'
          and orders[0]['rule_id'] == 'BUY-NEW' and approx(orders[0]['amount'], 40000.0),
          str(orders))
    check('G5 ep_lock摘要行', any('战略防御' in x for x in summary), str(summary))
    # G5.13 权益+固收双缺口 -> 权益优先，固收用剩余现金
    ctx = bok_ctx(mvs={'E': 1000, 'B': 0})
    orders, alloc, summary, tp_act = rules.build_order_book(CFG, prods_eq_bond(), ctx, today=today)
    check('G5 双缺口权益优先', len(orders) == 2 and orders[0]['code'] == 'E'
          and orders[0]['rule_id'] == 'REB-EQ' and approx(orders[0]['amount'], 39000.0)
          and orders[1]['code'] == 'B' and approx(orders[1]['amount'], 1000.0), str(orders))
    # G5.14 缺口低于最小订单额 -> 无买入
    mini = {'target': {'equity': {'base': 0.40, 'band': 0.05}, 'cash': {'base': 0.10}},
            'rules': {'take_profit': {'shadow_mode': True, 'min_hold_days': 7},
                      'min_order_amount': 5000}}
    ctx = bok_ctx(total=10000, mvs={'E': 3900}, cash_mv=50000)
    orders, alloc, summary, tp_act = rules.build_order_book(mini, prods_equity(), ctx, today=today)
    check('G5 缺口低于最小额无买入', orders == [], str(orders))
    # G5.15 target_alloc 恒等
    orders, alloc, summary, tp_act = rules.build_order_book(CFG, [], bok_ctx(), today=today)
    check('G5 target_alloc', alloc == {'equity': 0.4, 'bond': 0.5, 'cash': 0.1}, str(alloc))
    # G5.16 硬门槛：暂停申购/限大额/成立<18月/规模<2亿 → 剔除；正常候选被买入
    prods_g = [obs('OK'), obs('P1'), obs('P2'), obs('P3'), obs('P4')]
    pm_g = {
        'OK': full_pctx(code='OK'),
        'P1': full_pctx(code='P1', purchase_status='暂停申购'),
        'P2': full_pctx(code='P2', purchase_status='限大额'),
        'P3': full_pctx(code='P3', inception='2026-03-01'),
        'P4': full_pctx(code='P4', scale='1.2亿'),
    }
    ctx_g = bok_ctx(mvs={c: 0 for c in pm_g}, product_name={c: c for c in pm_g},
                    nav={c: 1.0 for c in pm_g}, exposure={c: 0.9 for c in pm_g},
                    product_ctx=pm_g)
    orders, alloc, summary, tp_act = rules.build_order_book(CFG, prods_g, ctx_g, today=today)
    check('G5 硬门槛剔除', len(orders) == 1 and orders[0]['code'] == 'OK'
          and orders[0]['rule_id'] == 'BUY-NEW', str(orders))
    # G5.17 rf_annual 流入排序：买入选 == score_candidate(同 rf、同池) 的最高分
    rf_pctx = {
        'E1': full_pctx(code='E1', nav=nav_mu_vol(0.30, 0.50)),
        'G1': full_pctx(code='G1', nav=nav_mu_vol(0.10, 0.10)),
    }
    ctx_rf = bok_ctx(mvs={'E1': 0, 'G1': 0}, product_name={'E1': 'E1', 'G1': 'G1'},
                     nav={'E1': 1.0, 'G1': 1.0}, exposure={'E1': 0.9, 'G1': 0.9},
                     product_ctx=rf_pctx, rf_annual=0.10)
    orders, alloc, summary, tp_act = rules.build_order_book(CFG, [obs('E1'), obs('G1')],
                                                            ctx_rf, today=today)
    rules._set_candidate_pool([rf_pctx['E1'], rf_pctx['G1']])
    s_e1 = rules.score_candidate(rf_pctx['E1'], CFG, rf_annual=0.10)
    s_g1 = rules.score_candidate(rf_pctx['G1'], CFG, rf_annual=0.10)
    rules._clear_candidate_pool()
    want = 'E1' if s_e1 >= s_g1 else 'G1'
    check('G5 rf生效于排序', len(orders) == 1 and orders[0]['code'] == want,
          'winner=%s want=%s E1=%.2f G1=%.2f' % (orders and orders[0]['code'], want, s_e1, s_g1))
    # G5.18 换手抑制：观察候选仅领先 4.5 分（<5）→ 保持原持仓（held +5 缓冲）
    # 唯一差异维度：管理费（TCO）→ 组内 tco 排序差 = 0.25×18 = 4.5 < 5；
    # 短期赎回统一 0%（否则可交易性维度制造 11 分差距）；日期错开 → 边际相关性缺失 → 各 0.5 中性
    navh = nav_mu_vol(0.15, 0.25)
    navh_off = nav_mu_vol(0.15, 0.25, start='2024-01-01')
    pm_h = {
        'E': full_pctx(code='E', name='EqE', nav=navh, fees='管理0.3%，短期赎回0%，申购0%'),
        'O': full_pctx(code='O', name='EqO', nav=navh_off, fees='管理0.1%，短期赎回0%，申购0%'),
        'O2': full_pctx(code='O2', name='EqO2', nav=navh_off, fees='管理0.5%，短期赎回0%，申购0%'),
        'O3': full_pctx(code='O3', name='EqO3', nav=navh_off, fees='管理0.8%，短期赎回0%，申购0%'),
    }
    prods_h = [{'code': 'E', 'type': 'equity', 'status': 'held', 'name': 'EqE'},
               obs('O', name='EqO'), obs('O2', name='EqO2'), obs('O3', name='EqO3')]
    ctx_h = bok_ctx(mvs={c: 0 for c in pm_h}, product_name={c: c for c in pm_h},
                    nav={c: 1.0 for c in pm_h}, exposure={c: 0.9 for c in pm_h},
                    product_ctx=pm_h)
    orders, alloc, summary, tp_act = rules.build_order_book(CFG, prods_h, ctx_h, today=today)
    rules._set_candidate_pool([pm_h[c] for c in ('E', 'O', 'O2', 'O3')])
    s_held = rules.score_candidate(pm_h['E'], CFG)
    s_obs = rules.score_candidate(pm_h['O'], CFG)
    rules._clear_candidate_pool()
    check('G5 换手抑制held优先', len(orders) == 1 and orders[0]['code'] == 'E'
          and 0 < s_obs - s_held < 5.0,
          'winner=%s sE=%.2f sO=%.2f' % (orders and orders[0]['code'], s_held, s_obs))
    # G5.19 已持有产品被硬门槛剔除 -> 观察候选接替买入
    pm_h2 = {'E': full_pctx(code='E', purchase_status='暂停申购'),
             'O': full_pctx(code='O')}
    prods_h2 = [{'code': 'E', 'type': 'equity', 'status': 'held', 'name': 'EqE'},
                obs('O')]
    ctx_h2 = bok_ctx(mvs={'E': 0, 'O': 0}, product_name={'E': 'EqE', 'O': 'O'},
                     nav={'E': 1.0, 'O': 1.0}, exposure={'E': 0.9, 'O': 0.9},
                     product_ctx=pm_h2)
    orders, alloc, summary, tp_act = rules.build_order_book(CFG, prods_h2, ctx_h2, today=today)
    check('G5 已持受限换观察买入', len(orders) == 1 and orders[0]['code'] == 'O'
          and orders[0]['rule_id'] == 'BUY-NEW', str(orders))


def run():
    passed = 0
    total = 0

    def check(name, cond, extra=''):
        nonlocal passed, total
        total += 1
        if cond:
            passed += 1
            print('PASS ' + name + ' ' + extra)
        else:
            print('FAIL ' + name + ' ' + extra)

    print('== G1 equity_target ==')
    test_equity_target(check)
    print('== G2 ep_threshold ==')
    test_ep_threshold(check)
    print('== G3 score_candidate ==')
    test_score_candidate(check)
    print('== G4 portfolio_diagnostics ==')
    test_portfolio_diagnostics(check)
    print('== G5 build_order_book ==')
    test_build_order_book(check)
    print('')
    print('pass %d/%d' % (passed, total))
    return passed == total


if __name__ == '__main__':
    sys.exit(0 if run() else 1)
