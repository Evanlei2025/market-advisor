# -*- coding: utf-8 -*-
# rules.py core function unit tests (phase 2 gap-fill).
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


def ids_of(triggers):
    return {t.get('id') for t in triggers}


def approx(a, b, tol=0.01):
    return abs(a - b) <= tol


def nav_df(vals):
    return pd.DataFrame({'nav': vals},
                        index=pd.date_range('2025-01-01', periods=len(vals), freq='B'))


def nav_trend(n=60):
    return nav_df([1.002 ** i * (1 + 0.0003 * ((i * 7) % 5 - 2)) for i in range(n)])


def nav_flat(n=60):
    return nav_df([1.0 + 0.0002 * ((i * 3) % 5 - 2) for i in range(n)])


def ret_series(n, amp, phase=0.0, start='2025-01-01'):
    return pd.Series([amp * math.sin(2 * math.pi * (i + phase) / 20.0) for i in range(n)],
                     index=pd.date_range(start, periods=n, freq='B'))


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
    # G3.1 pctx=None -> None
    check('G3 pctx=None -> None', rules.score_candidate(None, {}) is None)
    # G3.2/G3.3 全维度有值: 高夏普 > 平缓，且评分 0-100
    a = {'nav_series': nav_trend(), 'max_dd': -0.05, 'mgmt_fee': 1.2,
         'short_redeem_fee': 0.5, 'scale': 50, 'ranking': '近1年 12/500'}
    b = {'nav_series': nav_flat(), 'max_dd': -0.05, 'mgmt_fee': 1.2,
         'short_redeem_fee': 0.5, 'scale': 50, 'ranking': '近1年 12/500'}
    sa, sb = rules.score_candidate(a, {}), rules.score_candidate(b, {})
    check('G3 全维度评分0-100', sa is not None and 0 < sa <= 100 and sb is not None,
          'sa=%s sb=%s' % (sa, sb))
    check('G3 高夏普分更高', sb < sa, 'sa=%s sb=%s' % (sa, sb))
    # G3.4 部分维度缺失 -> 不抛异常并按剩余权重重归一（max_dd+scale 均满分 -> 100.0）
    s = rules.score_candidate({'max_dd': -0.1, 'scale': 50}, {})
    check('G3 部分维度重归一', s == 100.0, 's=%s' % s)
    # G3.5 全部维度缺失 -> None
    check('G3 全缺失 -> None', rules.score_candidate({'fees': '无'}, {}) is None
          and rules.score_candidate({}, {}) is None)
    # G3.6 最大回撤方向: 回撤深(0.4) 分低 vs 浅(-0.05) 分高
    s1 = rules.score_candidate({'max_dd': 0.4}, {})
    s2 = rules.score_candidate({'max_dd': -0.05}, {})
    check('G3 深度回撤分低', s1 == 60.0 and s2 == 100.0 and s1 < s2, 's1=%s s2=%s' % (s1, s2))
    # G3.7 实现注记: mdd=-0.5 与 -0.05 都被 1-mdd 剪裁到 1.0 -> 同分
    s3 = rules.score_candidate({'max_dd': -0.5}, {})
    check('G3 回撤剪裁同分', s3 == 100.0 and s3 == s2, 's3=%s' % s3)
    # G3.8 费率: 高费率分低（低者优）
    sf_hi = rules.score_candidate({'mgmt_fee': 2.5}, {})
    sf_lo = rules.score_candidate({'mgmt_fee': 1.2}, {})
    check('G3 高费率分低', approx(sf_hi, 16.67) and approx(sf_lo, 60.0) and sf_hi < sf_lo,
          'hi=%s lo=%s' % (sf_hi, sf_lo))
    # G3.9 费率<=1 按小数（0.6 = 60%）-> 0 分
    s = rules.score_candidate({'mgmt_fee': 0.6}, {})
    check('G3 费率0.6按60%计0分', s == 0.0, 's=%s' % s)
    # G3.10 fees 字符串解析（管理费+短期赎回）
    s = rules.score_candidate({'fees': '管理费1.2%短期赎回0.5%'}, {})
    check('G3 fees字符串解析', approx(s, 43.33), 's=%s' % s)
    # G3.11 ranking 字符串解析
    s = rules.score_candidate({'ranking': '近1年 12/500'}, {})
    check('G3 ranking字符串', approx(s, 97.6), 's=%s' % s)
    # G3.12 ranking 数值
    s = rules.score_candidate({'ranking': 0.9}, {})
    check('G3 ranking数值', approx(s, 10.0), 's=%s' % s)
    # G3.13 scale 分段插值: 1000亿 -> 0.4857 -> 48.57
    s = rules.score_candidate({'scale': 1000}, {})
    check('G3 scale分段插值', approx(s, 48.57), 's=%s' % s)


def test_portfolio_diagnostics(check):
    # G4.1 正常: 2产品120天 + 满对齐基准 -> 全部字段有值
    returns = {'BOND': ret_series(120, 0.0005), 'EQ': ret_series(120, 0.02)}
    weights = {'BOND': 0.6, 'EQ': 0.4}
    bench_full = {'returns': returns, 'weights': weights,
                  'bench_returns': {'CSI300': ret_series(120, 0.015)},
                  'bench_weights': {'CSI300': 1.0}}
    out = rules.portfolio_diagnostics(CFG, [], bench_full)
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
          rules.portfolio_diagnostics(CFG, [], {'returns': r29, 'weights': {'A': 1.0}}) is None)
    # G4.3 基准对齐不足（<60天）-> 四字段 None，vol 等仍正常
    bench_short = {'returns': returns, 'weights': weights,
                   'bench_returns': {'CSI300': ret_series(50, 0.015, start='2025-06-01')},
                   'bench_weights': {'CSI300': 1.0}}
    out = rules.portfolio_diagnostics(CFG, [], bench_short)
    check('G4 基准对齐<60四字段None', out is not None and out['vol'] is not None
          and out['excess_ann'] is None and out['alpha'] is None
          and out['beta'] is None and out['ir'] is None, 'vol=%s' % (out and out['vol']))
    # G4.4 纯债低波动 vs 全权益高波动
    b = rules.portfolio_diagnostics(CFG, [],
                                    {'returns': {'B': ret_series(120, 0.0005)}, 'weights': {'B': 1.0}})
    e = rules.portfolio_diagnostics(CFG, [],
                                    {'returns': {'E': ret_series(120, 0.02)}, 'weights': {'E': 1.0}})
    check('G4 纯债波动显著低', b is not None and e is not None
          and b['vol'] < e['vol'] / 3 and b['max_dd'] > e['max_dd'] and b['max_dd'] < 0,
          'vol_b=%s vol_e=%s dd_b=%s dd_e=%s' % (b and b['vol'], e and e['vol'],
                                                 b and b['max_dd'], e and e['max_dd']))
    # G4.5 空 returns -> None
    check('G4 空returns -> None',
          rules.portfolio_diagnostics(CFG, [], {'returns': {}, 'weights': {}}) is None)
    # G4.6 weights 与 returns 无交集 -> None
    check('G4 权重无交集 -> None',
          rules.portfolio_diagnostics(CFG, [], {'returns': returns, 'weights': {'X': 1.0}}) is None)


def test_build_order_book(check):
    today = '2026-08-07'
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
    # G5.11 零持仓关注池产品 -> BUY-NEW 建仓（实现：mvs=0 有产品即建仓）
    obs = [{'code': 'O', 'type': 'equity', 'status': 'observe', 'name': 'Observe'}]
    ctx = bok_ctx(mvs={'O': 0}, product_name={'O': 'Observe'}, nav={'O': 1.0}, cash_mv=50000,
                  exposure={'O': 0.9})
    orders, alloc, summary, tp_act = rules.build_order_book(CFG, obs, ctx, today=today)
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
