# -*- coding: utf-8 -*-
'''IC 回测框架（B1，离线，不每日跑）：候选池产品逐维度前瞻预测力检验。

验证 score_candidate V3.1 权重表的实证基础（用户深度评审：验证与上线）：
  1. 费率(TCO) IC 应最稳定为正
  2. 同类排名/规模 IC~0（历史数据不可得 -> 标注跳过）
  3. 夏普（风格中性化后）仅弱正

方法：
  - point-in-time：因子只用 t 及以前数据；规模/同类排名无历史序列 -> 不测并注明
  - 前瞻标签：t 起 6M/12M 风格中性超额 = 产品复权收益 - 基准同期收益
  - 逐截面 Spearman Rank IC（无 scipy：Series.rank() 后 pearson）
  - walk-forward：窗口起点每 step（默认 60）交易日一步；purge/embargo：
    相邻窗口间距 >= embargo（默认 20）交易日；另报无重叠标签（间距>=H）稳健 ICIR
  - 基准：equity->沪深300（bench_index=csi500 时用中证500）；bond->中证全债；
    mixed/other->50%沪深300+50%中证全债复合；gold->Au99.99（数据不足则跳过）

用法：
  python ic_backtest.py                        # config 全部产品，6M+12M，近5年
  python ic_backtest.py --codes 006195,014846  # 指定产品
  python ic_backtest.py --horizon 6 --years 3 --min-n 4 --no-write

输出：汇总表（按 ICIR 排序）+ 权重校准建议；默认写 reports/ic_report_<date>.md
（reports/ 在 .gitignore 内，不污染仓库）。缓存：reports/ic_cache/ 下净值/费率/基准。
'''
import argparse
import io
import json
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
CACHE_DIR = os.path.join(BASE_DIR, 'reports', 'ic_cache')
REPORT_DIR = os.path.join(BASE_DIR, 'reports')
MAX_STALE = 10        # 因子/标签端点允许的最大数据滞后（交易日）
LOOKBACK = 250        # 因子回看窗口（交易日）
MIN_WIN = 60          # 因子窗口最少行数
DEFAULT_RF = 0.015    # 夏普因子 rf 常数近似


def load_config():
    env_cfg = os.environ.get('ADVISOR_CONFIG_JSON')
    if env_cfg:
        try:
            return json.loads(env_cfg)
        except Exception:
            pass
    if os.path.exists(CONFIG_PATH):
        with io.open(CONFIG_PATH, encoding='utf-8') as f:
            return json.load(f)
    return {}


def default_codes(cfg):
    codes = []
    seen = set()
    clients = cfg.get('clients') if isinstance(cfg, dict) else None
    if isinstance(clients, dict):
        for cid, csec in clients.items():
            if not isinstance(csec, dict):
                continue
            for p in csec.get('products', []) or []:
                if not isinstance(p, dict):
                    continue
                c = str(p.get('fund_code') or p.get('code') or '').strip()
                if c and c not in seen:
                    seen.add(c)
                    codes.append(c)
    return codes


def product_meta(cfg, code):
    clients = cfg.get('clients') if isinstance(cfg, dict) else None
    if isinstance(clients, dict):
        for cid, csec in clients.items():
            if not isinstance(csec, dict):
                continue
            for p in csec.get('products', []) or []:
                if isinstance(p, dict) and str(p.get('fund_code') or p.get('code') or '').strip() == code:
                    return p
    return None


def _to_df(raw):
    c_date = None
    for c in ('date',):
        if c in raw.columns:
            c_date = c
            break
    c_nav = None
    for c in ('nav_adj',):
        if c in raw.columns:
            c_nav = c
            break
    if c_date is None or c_nav is None:
        return None
    out = raw[[c_date, c_nav]].copy()
    out.columns = ['date', 'nav_adj']
    out['date'] = pd.to_datetime(out['date'])
    out['nav_adj'] = pd.to_numeric(out['nav_adj'], errors='coerce')
    out = out.dropna(subset=['nav_adj']).sort_values('date').drop_duplicates('date')
    return out.reset_index(drop=True)


def _fresh_enough(df):
    if df is None or len(df) < MIN_WIN:
        return False
    try:
        last = pd.to_datetime(df['date'].iloc[-1]).date()
        return (date.today() - last).days <= 45
    except Exception:
        return False


def _fetch_retry(fn, tries=3, wait=1.0):
    import time
    for k in range(tries):
        try:
            return fn()
        except Exception:
            if k < tries - 1:
                time.sleep(wait)
    return None


def load_nav(fetcher, fcode, use_cache=True):
    cache = os.path.join(CACHE_DIR, fcode + '_nav.csv')
    if use_cache and os.path.exists(cache):
        try:
            df = pd.read_csv(cache)
            df['date'] = pd.to_datetime(df['date'])
            if _fresh_enough(df):
                return df
        except Exception:
            pass
    raw = _fetch_retry(lambda: fetcher.fund_nav_history(fcode))   # 复用 main.DataFetcher：含 nav_adj 复权重建列
    if raw is None or raw.empty:
        return None
    df = _to_df(raw)
    if df is None or len(df) < MIN_WIN:
        return None
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        df.to_csv(cache, index=False)
    except Exception:
        pass
    return df


def load_fees(fetcher, fcode, use_cache=True):
    cache = os.path.join(CACHE_DIR, fcode + '_fees.json')
    if use_cache and os.path.exists(cache):
        try:
            with io.open(cache, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    fees_str = ''
    try:
        rows = _fetch_retry(lambda: fetcher.fund_fees(fcode)) or []
        buy_min = sell_min = mgmt = ''
        for row in rows:
            t = str(row.get('费用类型', ''))
            cond = str(row.get('条件或名称', ''))
            fee_raw = str(row.get('费用', ''))
            try:
                fee_val = float(fee_raw.replace('%', '').strip())
            except Exception:
                continue
            if t == '买入规则' and not buy_min and fee_val < 10:
                buy_min = '%g%%' % fee_val
            elif t == '卖出规则' and not sell_min and fee_val < 10:
                sell_min = '%g%%' % fee_val
            elif t == '其他费用' and '管理费' in cond and not mgmt:
                mgmt = '%g%%' % fee_val
        fees_str = '，'.join(x for x in ('申购%s' % buy_min, '短期赎回%s' % sell_min, '管理%s' % mgmt) if x)
    except Exception:
        fees_str = ''
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with io.open(cache, 'w', encoding='utf-8') as f:
            json.dump(fees_str, f, ensure_ascii=False)
    except Exception:
        pass
    return fees_str


BENCH_CHAIN = {
    'sh000300': ['sh000300'],
    'sh000905': ['sh000905'],
    'sh000923': ['sh000923', 'sh000012'],   # 中证全债 → 备选上证国债
}


def load_bench(fetcher, symbol, use_cache=True):
    cache = os.path.join(CACHE_DIR, 'bench_' + symbol + '.csv')
    if use_cache and os.path.exists(cache):
        try:
            df = pd.read_csv(cache)
            df['date'] = pd.to_datetime(df['date'])
            if _fresh_enough(df):
                return df
        except Exception:
            pass
    best = None
    for sym in BENCH_CHAIN.get(symbol, [symbol]):
        try:
            raw = fetcher._ak(fetcher.ak.stock_zh_index_daily, symbol=sym)
            if raw is None or raw.empty:
                continue
            c_date = 'date' if 'date' in raw.columns else raw.columns[0]
            c_close = 'close' if 'close' in raw.columns else raw.columns[3]
            df = raw[[c_date, c_close]].copy()
            df.columns = ['date', 'close']
            df['date'] = pd.to_datetime(df['date'])
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df = df.dropna().sort_values('date')
            if len(df) < MIN_WIN:
                continue
            if best is None or df['date'].iloc[-1] > best['date'].iloc[-1]:
                best = df
        except Exception:
            continue
    df = best
    if df is None or not _fresh_enough(df):
        try:
            alt = fetcher.index_daily(symbol, days=2000)   # 腾讯指数日线备选
            if alt is not None and not alt.empty:
                df = alt[['date', 'close']].copy()
                df['date'] = pd.to_datetime(df['date'])
                df['close'] = pd.to_numeric(df['close'], errors='coerce')
                df = df.dropna().sort_values('date')
        except Exception:
            df = None
    if df is None or not _fresh_enough(df):
        return None
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        df.to_csv(cache, index=False)
    except Exception:
        pass
    return df.reset_index(drop=True)


def composite_bench(fetcher, use_cache=True):
    a = load_bench(fetcher, 'sh000300', use_cache)
    b = load_bench(fetcher, 'sh000923', use_cache)
    if a is None or b is None:
        return None
    m = pd.merge(a, b, on='date', suffixes=('_a', '_b')).dropna()
    if len(m) < MIN_WIN:
        return None
    m['r'] = 0.5 * m['close_a'].pct_change() + 0.5 * m['close_b'].pct_change()
    m = m.dropna(subset=['r'])
    m['close'] = (1.0 + m['r']).cumprod()
    return m[['date', 'close']].reset_index(drop=True)


def load_gold_bench(fetcher, use_cache=True):
    cache = os.path.join(CACHE_DIR, 'bench_gold.csv')
    if use_cache and os.path.exists(cache):
        try:
            df = pd.read_csv(cache)
            df['date'] = pd.to_datetime(df['date'])
            if len(df) >= MIN_WIN:
                return df
        except Exception:
            pass
    df = None
    try:
        raw = fetcher.gold_daily(days=2000)
        if raw is not None and not raw.empty:
            df = raw[['date', 'close']].copy()
            df['date'] = pd.to_datetime(df['date'])
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df = df.dropna().sort_values('date')
    except Exception:
        df = None
    if df is None or len(df) < MIN_WIN:
        return None
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        df.to_csv(cache, index=False)
    except Exception:
        pass
    return df.reset_index(drop=True)


def bench_for(ptype, bench_index, fetcher, use_cache=True):
    if ptype == 'equity':
        return load_bench(fetcher, 'sh000905' if bench_index == 'csi500' else 'sh000300', use_cache)
    if ptype == 'bond':
        return load_bench(fetcher, 'sh000923', use_cache)
    if ptype == 'gold':
        return load_gold_bench(fetcher, use_cache)
    return composite_bench(fetcher, use_cache)


def trailing_maxdd(nav, w=LOOKBACK, minp=MIN_WIN):
    # 向量化：尾部窗口 drawdown 用 sliding_window_view 一次性计算，
    # 起始不足一个完整窗口的前缀段用累计 min 复用同一 cummax/dd 序列。
    from numpy.lib.stride_tricks import sliding_window_view
    arr = np.asarray(nav.values, dtype=float)
    n = len(arr)
    vals = np.full(n, np.nan)
    if n == 0:
        return pd.Series(vals, index=nav.index)
    # 前缀段（窗口起点 < 0 → seg = arr[0:i+1]）的 drawdown 直接用全局 cummax
    cummax = np.maximum.accumulate(arr)
    dd_series = arr / cummax - 1.0
    prefix_min_dd = np.minimum.accumulate(dd_series)
    end_partial = min(w - 1, n)
    for i in range(minp - 1, end_partial):
        vals[i] = prefix_min_dd[i]
    # 完整窗口段：sliding_window_view + 沿轴 cummax → 与逐段循环数值等价
    if n >= w:
        windows = sliding_window_view(arr, w)            # shape (n-w+1, w)
        peaks = np.maximum.accumulate(windows, axis=1)
        dd = (windows / peaks - 1.0).min(axis=1)
        vals[w - 1:] = dd
    return pd.Series(vals, index=nav.index)


def _roll_prod(r, w=LOOKBACK, minp=MIN_WIN):
    # C 级 rolling sum of logs 替代 Python lambda，与 (1+x).prod()-1 数学等价
    log_cum = np.log1p(r).rolling(w, min_periods=minp).sum()
    out = np.exp(log_cum) - 1.0
    if not isinstance(out, pd.Series):
        out = pd.Series(out, index=r.index)
    return out


def product_factors(nav, bench_close, rf):
    idx = pd.DatetimeIndex(nav['date'])
    r = nav['nav_adj'].pct_change()
    r.index = idx
    win_ret = _roll_prod(r)
    mu_ann = r.rolling(LOOKBACK, min_periods=MIN_WIN).mean() * 250.0
    vol_ann = r.rolling(LOOKBACK, min_periods=MIN_WIN).std() * np.sqrt(250.0)
    sharpe = (mu_ann - rf) / vol_ann.where(vol_ann > 0.01, 0.01)
    tail = 1.0 - trailing_maxdd(nav['nav_adj']).abs()
    tail.index = idx
    alpha = pd.Series(np.nan, index=idx)
    if bench_close is not None and len(bench_close) >= 2:
        bc = bench_close.reindex(idx).ffill()
        br = bc.pct_change(fill_method=None)
        alpha = win_ret - _roll_prod(br)
    return {'win_ret': win_ret, 'sharpe': sharpe, 'tail': tail, 'alpha': alpha}


def rank01(vals, own):
    n = len(vals)
    if n == 0:
        return 0.5
    below = sum(1.0 for v in vals if v < own)
    equal = sum(1.0 for v in vals if v == own)
    return (below + 0.5 * equal) / n


def spearman(x, y):
    x = pd.Series(x)
    y = pd.Series(y)
    return float(x.rank().corr(y.rank(), method='pearson'))


def align(own_dates, own_vals, D):
    s = pd.Series(np.asarray(own_vals, dtype=float), index=own_dates)
    ff = s.reindex(D).ffill()
    posD = D.searchsorted(pd.DatetimeIndex(own_dates))
    pos_ff = pd.Series(posD, index=pd.DatetimeIndex(own_dates)).reindex(D).ffill()
    stale = pd.Series(np.arange(len(D)), index=D) - pos_ff
    fresh = pos_ff.notna() & (stale <= MAX_STALE)
    return ff, fresh


FACTORS = ('tco', 'tco_ongoing', 'tail', 'sharpe', 'alpha', 'alpha_sub')
H_DAYS = {6: 126, 12: 250}   # 前瞻月数 → 交易日


def cross_section(aligned, t, H, min_n):
    out = {}
    if t + H >= len(aligned[0]['nav']):
        return out
    cands = []
    for p in aligned:
        nav_t = p['nav'].iloc[t]
        nav_th = p['nav'].iloc[t + H]
        fresh_t = bool(p['fresh'].iloc[t])
        fresh_th = bool(p['fresh'].iloc[t + H])
        if nav_t is None or nav_t != nav_t or nav_th is None or nav_th != nav_th:
            continue
        if not (fresh_t and fresh_th):
            continue
        lab = None
        if p['bench'] is not None:
            b_t = p['bench'].iloc[t]
            b_th = p['bench'].iloc[t + H]
            b_fresh = p.get('bench_fresh')
            ok_bf = True
            if b_fresh is not None:
                ok_bf = bool(b_fresh.iloc[t]) and bool(b_fresh.iloc[t + H])
            if ok_bf and b_t is not None and b_t == b_t and b_th is not None and b_th == b_th:
                lab = (nav_th / nav_t - 1.0) - (b_th / b_t - 1.0)
        if lab is None:
            continue
        cands.append({'p': p, 'label': lab})
    if len(cands) < min_n:
        return out
    for fac in ('tco', 'tco_ongoing', 'tail', 'sharpe', 'alpha'):
        xs = []
        ys = []
        for c in cands:
            x = None
            if fac == 'tco':
                x = c['p']['tco']
            elif fac == 'tco_ongoing':
                x = c['p']['tco_ongoing']
            else:
                ff, fr = c['p']['fac'][fac]
                if bool(fr.iloc[t]):
                    x = ff.iloc[t]
            if x is None or x != x:
                continue
            xs.append(float(x))
            ys.append(float(c['label']))
        if len(xs) >= min_n and len(set(xs)) > 1:
            ic = spearman(xs, ys)
            if ic == ic:
                out[fac] = ic
    g_vals = {}
    for c in cands:
        ff, fr = c['p']['fac']['win_ret']
        if not bool(fr.iloc[t]):
            continue
        x = ff.iloc[t]
        if x is None or x != x:
            continue
        g_vals.setdefault(c['p']['group'], []).append(float(x))
    xs = []
    ys = []
    for c in cands:
        sf, sf_fr = c['p']['fac']['sharpe']
        wf, wf_fr = c['p']['fac']['win_ret']
        if not (bool(sf_fr.iloc[t]) and bool(wf_fr.iloc[t])):
            continue
        s = sf.iloc[t]
        w = wf.iloc[t]
        if s is None or s != s or w is None or w != w:
            continue
        gk = c['p']['group']
        pct = rank01(g_vals.get(gk, [w]), w) if len(g_vals.get(gk, [])) >= 2 else 0.5
        xs.append(0.5 * float(s) + 0.5 * pct)
        ys.append(float(c['label']))
    if len(xs) >= min_n and len(set(xs)) > 1:
        ic = spearman(xs, ys)
        if ic == ic:
            out['alpha_sub'] = ic
    return out


def walk_ic(aligned, D, H, step, embargo, min_n, years):
    rows = []
    ts = []
    last_t = -10 ** 9
    lo = max(0, len(D) - years * 250)
    cur = lo
    while cur + H + MAX_STALE < len(D):
        if cur - last_t >= embargo:
            ics = cross_section(aligned, cur, H, min_n)
            if ics:
                for fac, ic in ics.items():
                    rows.append((cur, fac, ic))
                last_t = cur
                ts.append(cur)
        cur += step
    return rows, ts


def aggregate(rows):
    out = {}
    for fac in FACTORS:
        ics = [ic for (t, f, ic) in rows if f == fac]
        if not ics:
            continue
        arr = pd.Series(ics)
        m = float(arr.mean())
        s = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
        out[fac] = {
            'n': len(ics),
            'mean': m,
            'std': s,
            'icir': m / s if s > 0 else 0.0,
            'pos': float((arr > 0).mean()),
        }
    return out


def bootstrap_icir_ci(ics, n_boot=2000, ci=0.95, seed=42):
    """ICIR 的 bootstrap 置信区间。
    ics: IC 值列表
    返回 {'lo': float, 'hi': float} 或 None（样本不足）
    """
    arr = np.array(ics, dtype=float)
    n = len(arr)
    if n < 5:
        return None
    rng = np.random.RandomState(seed)
    boot_icirs = []
    for _ in range(n_boot):
        sample = rng.choice(arr, size=n, replace=True)
        s = sample.std(ddof=1) if len(sample) > 1 else 0.0
        if s > 0:
            boot_icirs.append(sample.mean() / s)
    if len(boot_icirs) < 10:
        return None
    lo = float(np.percentile(boot_icirs, (1 - ci) / 2 * 100))
    hi = float(np.percentile(boot_icirs, (1 + ci) / 2 * 100))
    return {'lo': lo, 'hi': hi}


def ic_half_life(aligned, D, factor, horizons=None, min_n=4, years=5):
    """因子 IC 衰减半衰期。在多前瞻期计算 IC，拟合指数衰减。
    返回 {'half_life': float|None, 'r_squared': float}
    """
    if horizons is None:
        horizons = [5, 10, 21, 63, 126, 250]
    ic_means = []
    valid_h = []
    for h in horizons:
        if h > len(D):
            continue
        # walk_ic(aligned, D, H, step, embargo, min_n, years) → (rows, ts)
        rows_h, _ = walk_ic(aligned, D, h, max(h // 2, 10), max(h // 4, 5), min_n, years)
        if not rows_h:
            continue
        fac_rows = [(t, f, ic) for t, f, ic in rows_h if f == factor]
        if len(fac_rows) < min_n:
            continue
        ics = [ic for _, _, ic in fac_rows]
        ic_means.append(np.mean(ics))
        valid_h.append(h)
    if len(valid_h) < 3:
        return {'half_life': None, 'r_squared': 0.0}
    h_arr = np.array(valid_h, dtype=float)
    ic_arr = np.array(ic_means)
    y = np.abs(ic_arr)
    log_y = np.log(y + 1e-8)
    coeffs = np.polyfit(h_arr, log_y, 1)
    slope = coeffs[0]
    y_pred = np.polyval(coeffs, h_arr)
    ss_res = float(np.sum((log_y - y_pred) ** 2))
    ss_tot = float(np.sum((log_y - log_y.mean()) ** 2))
    r_sq = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    if slope >= 0:
        return {'half_life': None, 'r_squared': r_sq}
    tau = -1.0 / slope
    half_life = float(tau * np.log(2))
    return {'half_life': half_life, 'r_squared': r_sq}


def regime_split(D, bench_series, window=250):
    """按基准 250 日滚动收益分市场状态。
    返回 {date: 'bull'|'bear'|'sideways'} 字典
    """
    if bench_series is None or len(bench_series) < window:
        return {}
    roll_ret = bench_series.rolling(window).apply(lambda x: (1 + x).prod() - 1, raw=True)
    regimes = {}
    for d in D:
        if d in roll_ret.index:
            r = roll_ret.loc[d]
            if r == r:
                if r > 0.10:
                    regimes[d] = 'bull'
                elif r < -0.10:
                    regimes[d] = 'bear'
                else:
                    regimes[d] = 'sideways'
    return regimes


def conditional_ic(aligned, D, H, step, embargo, min_n, years, regimes):
    """分市场状态计算 IC/ICIR。返回 {regime: {factor: {n, mean, std, icir, pos}}}
    walk_ic 返回 (rows, ts)；rows 中 t 为整数索引，需用 D[t] 映射到日期再查 regimes。
    """
    rows, _ = walk_ic(aligned, D, H, step, embargo, min_n, years)
    result = {}
    for regime_name in ('bull', 'bear', 'sideways'):
        regime_rows = [(t, f, ic) for t, f, ic in rows if regimes.get(D[t]) == regime_name]
        if len(regime_rows) < min_n:
            result[regime_name] = {}
            continue
        agg = aggregate(regime_rows)
        result[regime_name] = agg
    return result


def portfolio_backtest(aligned, D, factor, H, min_n=4, top_pctile=0.67, years=5):
    """因子→组合回测：每截面按因子值排名选 top 分位，等权构建组合。
    aligned 为 list[dict]（与 cross_section 同结构）；标签用风格中性超额（产品复权收益 - 基准同期）。
    返回 {'cumret': float, 'sharpe': float, 'max_dd': float, 'ir': float|None, 'n_periods': int} 或 None
    """
    # step=H 使各截面间距 ≥ H，标签无重叠；embargo 仍施加
    rows, _ = walk_ic(aligned, D, H, H, max(H // 4, 5), min_n, years)
    port_rets = []
    bench_rets = []
    for t, fac, _ in rows:
        if fac != factor:
            continue
        xs = []  # (code, factor_value, fwd_excess_ret)
        for p in aligned:
            nav_ff = p['nav']
            fresh_nav = p['fresh']
            if t + H >= len(nav_ff):
                continue
            if not (bool(fresh_nav.iloc[t]) and bool(fresh_nav.iloc[t + H])):
                continue
            nav_t = nav_ff.iloc[t]
            nav_th = nav_ff.iloc[t + H]
            if nav_t is None or nav_t != nav_t or nav_th is None or nav_th != nav_th:
                continue
            # 因子值（与 cross_section 同款取数）
            x = None
            if factor in ('tco', 'tco_ongoing'):
                x = p.get(factor)
            elif factor in p['fac']:
                ff, fr = p['fac'][factor]
                if bool(fr.iloc[t]):
                    x = ff.iloc[t]
            if x is None or x != x:
                continue
            # 前瞻风格中性超额 = 产品收益 - 基准同期收益
            lab = None
            bench_ff = p.get('bench')
            b_fresh = p.get('bench_fresh')
            if bench_ff is not None:
                ok_bf = True
                if b_fresh is not None:
                    ok_bf = bool(b_fresh.iloc[t]) and bool(b_fresh.iloc[t + H])
                if ok_bf:
                    b_t = bench_ff.iloc[t]
                    b_th = bench_ff.iloc[t + H]
                    if b_t is not None and b_t == b_t and b_th is not None and b_th == b_th:
                        lab = (nav_th / nav_t - 1.0) - (b_th / b_t - 1.0)
            if lab is None:
                continue
            xs.append((p['code'], float(x), float(lab)))
        if len(xs) < min_n:
            continue
        xs.sort(key=lambda z: z[1], reverse=True)
        n_top = max(1, int(len(xs) * top_pctile))
        top = xs[:n_top]
        port_ret = np.mean([z[2] for z in top])
        bench_ret = np.mean([z[2] for z in xs])
        port_rets.append(port_ret)
        bench_rets.append(bench_ret)
    if len(port_rets) < min_n:
        return None
    pr = np.array(port_rets)
    br = np.array(bench_rets)
    cumret = float(np.prod(1 + pr) - 1)
    ann_ret = pr.mean() * (250.0 / H)
    ann_vol = pr.std(ddof=1) * np.sqrt(250.0 / H) if len(pr) > 1 else 0.0
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    nav = np.cumprod(1 + pr)
    max_dd = float(np.min(nav / np.maximum.accumulate(nav) - 1))
    excess = pr - br
    te = excess.std(ddof=1) * np.sqrt(250.0 / H) if len(excess) > 1 else 0.0
    ir = (excess.mean() * (250.0 / H)) / te if te > 0 else None
    return {'cumret': cumret, 'sharpe': float(sharpe), 'max_dd': max_dd,
            'ir': float(ir) if ir is not None else None, 'n_periods': len(port_rets)}


def deoverlap_icir(rows, H, min_n):
    acc = {}
    last = {}
    for (t, fac, ic) in sorted(rows, key=lambda z: (z[1], z[0])):
        if fac in last and t - last[fac] < H:
            continue
        last[fac] = t
        acc.setdefault(fac, []).append(ic)
    out = {}
    for fac, ics in acc.items():
        arr = pd.Series(ics)
        m = float(arr.mean())
        s = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
        out[fac] = m / s if s > 0 else 0.0
    return out


CURRENT_WEIGHTS = {'marginal': 20, 'tco': 18, 'alpha': 17, 'tail': 15,
                   'stability': 15, 'tradability': 15}
MEASURED_DIMS = {'tco': 'tco', 'alpha': 'alpha_sub', 'tail': 'tail'}
UNMEASURED_NOTE = 'marginal/stability/tradability 为组合上下文/管理信息，离线无历史序列，维持现权'


def suggest_weights(icir_map):
    sugg = dict(CURRENT_WEIGHTS)
    meas = {}
    for dim, fac in MEASURED_DIMS.items():
        if fac in icir_map and icir_map[fac] is not None:
            v = icir_map[fac]
            if isinstance(v, dict) and 'icir' in v:
                v = v['icir']
            meas[dim] = v
    if meas:
        base_sum = sum(CURRENT_WEIGHTS[d] for d in meas)
        w_sum = sum(max(0.0, v) for v in meas.values())
        if w_sum > 0:
            for d, v in meas.items():
                if v > 0:
                    sugg[d] = max(0.0, v) / w_sum * base_sum
    tot = sum(sugg.values())
    if tot > 0:
        sugg = {k: round(v / tot * 100.0, 1) for k, v in sugg.items()}
    return sugg


def verdict(icir):
    if icir is None:
        return '未测（数据限制）'
    if icir >= 0.5:
        return 'IC 稳定为正 → 支撑权重'
    if icir >= 0.2:
        return '弱正 → 维持低权重观察'
    if icir > -0.2:
        return '≈0 → 无预测力，权重应趋零'
    return '负向 → 建议降权/反向审视'


FAC_NAMES = {
    'tco': '费率TCO(生产口径)',
    'tco_ongoing': '费率TCO(仅管理费,诊断)',
    'tail': '尾部风险(250d MaxDD)',
    'sharpe': '夏普(复权, rf=0.015)',
    'alpha': '风格中性超额(250d)',
    'alpha_sub': '生产alpha子分模拟(夏普+组内收缩)',
}

FAC_LIMIT_NOTES = {
    'tco': '费率取当前档（历史费率可能调整，point-in-time 受限）；生产口径含申购+赎回费，2% 满分基准下多数产品饱和为 0',
    'tco_ongoing': '仅管理费×持有年数（满分基准 2%），去饱和诊断变体',
    'tail': '窗口=min(250, 可用行)，>=60 行',
    'sharpe': '窗口同上；平滑产品未剔除（离线无生产平滑检测上下文）',
    'alpha': '需基准；黄金/基准缺失产品排除',
    'alpha_sub': '组=type:style（名称关键词），点内分组，组内<2 用 0.5 中性',
}


def _fmt(x, nd=3):
    if x is None or x != x:
        return '—'
    return ('%.' + str(nd) + 'f') % x


def build_report(args, products, D, results, rows_by_h, ts_by_h, skipped, weights, combined_icir,
                 aligned=None, bench_series=None):
    lines = []
    lines.append('# IC 回测报告（score_candidate V3.1 权重验证）')
    lines.append('')
    lines.append('- 运行日期：' + date.today().isoformat())
    lines.append('- 产品（%d 个）：%s' % (len(products), '，'.join(sorted(products))))
    lines.append('- 前瞻：%s 个月；步长 %d 交易日；purge/embargo 窗口间距 ≥ %d 交易日；无重叠标签稳健版另报'
                 % (args.horizon, args.step, args.embargo))
    lines.append('- 统一日历：%s ~ %s（%d 交易日）；因子窗口 min(%d, 可用行)，>=%d 行；标签端点允许滞后 ≤ %d 交易日'
                 % (str(D[0])[:10], str(D[-1])[:10], len(D), LOOKBACK, MIN_WIN, MAX_STALE))
    lines.append('- 基准：equity→沪深300（bench_index=csi500→中证500）；bond→中证全债；mixed/other→50%沪深300+50%中证全债；gold→Au99.99')
    lines.append('- 跳过未测维度：规模（无历史规模序列，含规模重述风险）、同类排名（数据不可得）；'
                 + UNMEASURED_NOTE)
    if skipped:
        lines.append('- 数据限制跳过：' + '；'.join(skipped))
    lines.append('')
    for H, agg in results.items():
        lines.append('## 前瞻 %d 个月：逐截面 Spearman Rank IC（标签=风格中性超额）' % H)
        lines.append('')
        lines.append('| 因子 | 截面数 | IC均值 | IC标准差 | ICIR | IC>0占比 | 结论 |')
        lines.append('|---|---|---|---|---|---|---|')
        order = sorted(agg.keys(), key=lambda f: agg[f]['icir'], reverse=True)
        for fac in order:
            a = agg[fac]
            lines.append('| %s | %d | %s | %s | %s | %.0f%% | %s |'
                         % (FAC_NAMES.get(fac, fac), a['n'], _fmt(a['mean']), _fmt(a['std']),
                            _fmt(a['icir']), a['pos'] * 100.0, verdict(a['icir'])))
        do = deoverlap_icir(rows_by_h[H], H_DAYS[H], args.min_n)
        if do:
            lines.append('')
            lines.append('无重叠标签稳健版 ICIR（窗口间距≥%d 交易日，防标签重叠自相关）：' % H_DAYS[H]
                         + '；'.join('%s=%s' % (FAC_NAMES.get(f, f), _fmt(v)) for f, v in sorted(do.items(), key=lambda z: -z[1])))
        lines.append('')
    lines.append('## 权重校准建议（权重 ∝ ICIR，实证数值）')
    lines.append('')
    lines.append('| 维度 | 当前权重 | 6M建议 | 12M建议 | 综合建议 | 依据 |')
    lines.append('|---|---|---|---|---|---|')
    rows = []
    for dim in ('tco', 'alpha', 'tail', 'marginal', 'stability', 'tradability'):
        cur = CURRENT_WEIGHTS[dim]
        s6 = weights[6].get(dim, '')
        s12 = weights[12].get(dim, '')
        sc = weights[99].get(dim, '')
        base = '实测ICIR' if dim in MEASURED_DIMS else '未测（离线不可得）'
        rows.append((cur, dim, s6, s12, sc, base))
    for cur, dim, s6, s12, sc, base in rows:
        lines.append('| %s | %s | %s | %s | %s | %s |'
                     % (dim, cur, s6, s12, sc, base))
    lines.append('')
    lines.append('综合建议权重 = 6M/12M ICIR 均值归一化（实测维度）后重归一化到 100；' + UNMEASURED_NOTE + '。')
    lines.append('')
    lines.append('## 假设验证')
    lines.append('')
    lines.append('1. 费率 TCO IC 最稳定为正？' + ('是' if combined_icir.get('tco') is not None and combined_icir['tco'] > 0.3 else '待观察/否')
                 + '（综合 ICIR=%s）' % _fmt(combined_icir.get('tco')))
    lines.append('2. 同类排名/规模 IC≈0？数据不可得，未测（生存者偏差：当前持仓/观察池产品本身即幸存者）。')
    lines.append('3. 夏普（风格中性化后）仅弱正？'
                 + ('符合' if combined_icir.get('sharpe') is not None and 0.0 < combined_icir['sharpe'] < 0.4 else '待观察')
                 + '（综合 ICIR=%s；alpha_sub 综合=%s）'
                 % (_fmt(combined_icir.get('sharpe')), _fmt(combined_icir.get('alpha_sub'))))
    lines.append('')
    lines.append('## 局限（point-in-time 声明）')
    lines.append('')
    lines.append('- 生存者偏差：产品均为当前持仓/关注池（幸存者），历史回测乐观偏置。')
    lines.append('- 规模重述：无历史规模序列，规模因子未测。')
    lines.append('- 费率时点：TCO 用当前费率档，历史费率调整无法还原。')
    lines.append('- 复权近似：净值缺日用 ffill（净值滞后/停牌日收益=0 近似）；分红再投资按 fund_nav_history 重建口径。')
    lines.append('- 基准时点：指数点位为收盘价序列，无成分调整处理（指数点位本身无前视）。')
    lines.append('- IC 截面小（产品池 ~%d 个），ICIR 估计噪声大，结论仅作权重微调参考，不自动改权重表。' % len(products))
    lines.append('')
    # ===== 进阶诊断板块（修改 4-7 集成）=====
    if aligned is not None:
        # --- ICIR bootstrap 置信区间 ---
        lines.append('## ICIR Bootstrap 置信区间')
        lines.append('')
        lines.append('基于重采样（n_boot=2000，seed=42）的 ICIR 95% 置信区间，量化小样本 ICIR 点估的不确定性（区间跨 0 → 预测力不稳健）。')
        lines.append('')
        for H in sorted(rows_by_h.keys()):
            rows = rows_by_h[H]
            lines.append('### 前瞻 %d 个月' % H)
            lines.append('')
            lines.append('| 因子 | 样本n | ICIR点估 | CI下限 | CI上限 | 跨0? |')
            lines.append('|---|---|---|---|---|---|')
            for fac in FACTORS:
                ics = [ic for (t, f, ic) in rows if f == fac]
                if len(ics) < 5:
                    continue
                arr_s = pd.Series(ics)
                m = float(arr_s.mean())
                s = float(arr_s.std(ddof=1)) if len(arr_s) > 1 else 0.0
                pt = m / s if s > 0 else 0.0
                ci = bootstrap_icir_ci(ics)
                if ci is None:
                    continue
                cross_zero = '是' if ci['lo'] <= 0.0 <= ci['hi'] else '否'
                lines.append('| %s | %d | %s | %s | %s | %s |'
                             % (FAC_NAMES.get(fac, fac), len(ics), _fmt(pt),
                                _fmt(ci['lo']), _fmt(ci['hi']), cross_zero))
            lines.append('')
        # --- IC 半衰期 ---
        lines.append('## IC 半衰期（因子预测力衰减）')
        lines.append('')
        lines.append('多前瞻期（5/10/21/63/126/250 交易日）IC 均值拟合指数衰减 |IC|≈exp(-h/τ)，半衰期=τ·ln2；R² 越高衰减拟合越可信。')
        lines.append('')
        lines.append('| 因子 | 半衰期(交易日) | 拟合R² |')
        lines.append('|---|---|---|')
        for fac in FACTORS:
            hl = ic_half_life(aligned, D, fac, years=args.years)
            lines.append('| %s | %s | %s |'
                         % (FAC_NAMES.get(fac, fac),
                            _fmt(hl['half_life'], 1) if hl['half_life'] is not None else '—',
                            _fmt(hl['r_squared'])))
        lines.append('')
        # --- Conditional IC（分市场状态）---
        regimes = regime_split(D, bench_series, window=250) if bench_series is not None else {}
        if regimes:
            n_bull = sum(1 for v in regimes.values() if v == 'bull')
            n_bear = sum(1 for v in regimes.values() if v == 'bear')
            n_side = sum(1 for v in regimes.values() if v == 'sideways')
            lines.append('## Conditional IC（分市场状态）')
            lines.append('')
            lines.append('按沪深300 250 日滚动收益划分状态（bull>10%% / bear<-10%% / 其余 sideways），样本日数 bull=%d / bear=%d / sideways=%d。'
                         % (n_bull, n_bear, n_side))
            lines.append('')
            for H in sorted(rows_by_h.keys()):
                cond = conditional_ic(aligned, D, H_DAYS[H], args.step, args.embargo, args.min_n,
                                     args.years, regimes)
                lines.append('### 前瞻 %d 个月' % H)
                lines.append('')
                lines.append('| 状态 | 因子 | n | IC均值 | ICIR | IC>0占比 |')
                lines.append('|---|---|---|---|---|---|')
                for regime_name in ('bull', 'bear', 'sideways'):
                    agg = cond.get(regime_name, {})
                    for fac in FACTORS:
                        if fac not in agg:
                            continue
                        a = agg[fac]
                        if a['n'] == 0:
                            continue
                        lines.append('| %s | %s | %d | %s | %s | %.0f%% |'
                                     % (regime_name, FAC_NAMES.get(fac, fac), a['n'],
                                        _fmt(a['mean']), _fmt(a['icir']), a['pos'] * 100.0))
                lines.append('')
        else:
            lines.append('## Conditional IC（分市场状态）')
            lines.append('')
            lines.append('基准序列不足 250 个交易日，无法划分市场状态，跳过。')
            lines.append('')
        # --- Portfolio backtest ---
        lines.append('## Portfolio Backtest（因子→组合）')
        lines.append('')
        lines.append('每截面按因子值降序选 top 分位（top_pctile=0.67）等权构建组合，对比全样本等权基准；标签为风格中性超额，故 cumret 为累计超额收益。')
        lines.append('')
        for H in sorted(rows_by_h.keys()):
            lines.append('### 前瞻 %d 个月' % H)
            lines.append('')
            lines.append('| 因子 | 截面数 | 累计超额 | 年化夏普 | 最大回撤 | 信息比率 |')
            lines.append('|---|---|---|---|---|---|')
            for fac in FACTORS:
                pb = portfolio_backtest(aligned, D, fac, H_DAYS[H], min_n=args.min_n, years=args.years)
                if pb is None:
                    continue
                lines.append('| %s | %d | %s | %s | %s | %s |'
                             % (FAC_NAMES.get(fac, fac), pb['n_periods'], _fmt(pb['cumret']),
                                _fmt(pb['sharpe']), _fmt(pb['max_dd']),
                                _fmt(pb['ir']) if pb['ir'] is not None else '—'))
            lines.append('')
    return chr(10).join(lines)


def main():
    ap = argparse.ArgumentParser(description='IC backtest for score_candidate V3.1 factor weights')
    ap.add_argument('--codes', default='',
                    help='comma separated fund codes; default = all products in config.json (deduped)')
    ap.add_argument('--horizon', default='6,12', help='forward horizons in months (6/12, comma separated)')
    ap.add_argument('--years', type=int, default=5, help='years of history to test')
    ap.add_argument('--step', type=int, default=60, help='walk-forward step in trading days')
    ap.add_argument('--embargo', type=int, default=20, help='purge/embargo: min window spacing (trading days)')
    ap.add_argument('--min-n', type=int, default=4, help='min products per cross-section')
    ap.add_argument('--rf', type=float, default=DEFAULT_RF, help='annual risk-free rate')
    ap.add_argument('--no-cache', action='store_true', help='skip reports/ic_cache')
    ap.add_argument('--no-write', action='store_true', help='do not write markdown report')
    args = ap.parse_args()
    use_cache = not args.no_cache
    cfg = load_config()
    if args.codes.strip():
        codes = [c.strip() for c in args.codes.split(',') if c.strip()]
    else:
        codes = default_codes(cfg)
    if not codes:
        print('no codes to test; use --codes or fill config.json clients products')
        return 1
    horizons = []
    for h in str(args.horizon).split(','):
        try:
            hh = int(h)
            if hh in (6, 12):
                horizons.append(hh)
        except Exception:
            pass
    horizons = sorted(set(horizons)) or [6]
    print('== IC 回测：%d 个产品，前瞻 %s 个月，近 %d 年，step=%d embargo=%d min_n=%d =='
          % (len(codes), args.horizon, args.years, args.step, args.embargo, args.min_n))
    from main import DataFetcher      # 复用 main 层：nav_adj 复权重建（A7 契约）
    import rules
    fetcher = DataFetcher()
    bench_pool = {}
    for sym in ('sh000300', 'sh000905', 'sh000923'):
        bench_pool[sym] = load_bench(fetcher, sym, use_cache)
    bench_pool['composite'] = composite_bench(fetcher, use_cache)
    bench_pool['gold'] = load_gold_bench(fetcher, use_cache)
    # 沪深300 日收益率序列 → regime_split 市场状态划分用（修改 6 集成）
    bench_ret_series = None
    _bdf = bench_pool.get('sh000300')
    if _bdf is not None and len(_bdf) >= 2:
        _bidx = pd.DatetimeIndex(_bdf['date'])
        _bclose = pd.Series(_bdf['close'].values, index=_bidx).sort_index()
        bench_ret_series = _bclose.pct_change(fill_method=None)
    holding_years = 2.0
    try:
        hy = (cfg.get('rules') or {}).get('candidate_holding_years', 2.0)
        holding_years = float(hy)
    except Exception:
        pass
    raw_prods = []
    skipped = []
    for code in codes:
        meta = product_meta(cfg, code) or {}
        nav = load_nav(fetcher, code, use_cache)
        if nav is None:
            skipped.append('%s 净值不足' % code)
            continue
        fees_str = load_fees(fetcher, code, use_cache)
        ptype = str(meta.get('type') or 'mixed').strip().lower()
        name = str(meta.get('name') or '')
        bench_index = str(meta.get('bench_index') or '')
        bc = None
        if ptype == 'equity':
            bc = bench_pool.get('sh000905' if bench_index == 'csi500' else 'sh000300')
        elif ptype == 'bond':
            bc = bench_pool.get('sh000923')
        elif ptype == 'gold':
            bc = bench_pool.get('gold')
        else:
            bc = bench_pool.get('composite')
        if bc is None or len(bc) < MIN_WIN:
            skipped.append('%s 基准不可用' % code)
            continue
        tco = None
        try:
            tco = rules._tco_score({'fees': fees_str or '无'},
                                   {'rules': {'candidate_holding_years': holding_years}})
        except Exception:
            tco = None
        tco_ongoing = None
        try:
            import re
            m = re.search(r'管理([\d.]+)%', fees_str or '')
            if m:
                mg = float(m.group(1)) / 100.0
                total = mg * holding_years
                tco_ongoing = max(0.0, min(1.0, 1.0 - min(1.0, total / 0.02)))
        except Exception:
            tco_ongoing = None
        raw_prods.append({'code': code, 'name': name, 'type': ptype, 'bench_index': bench_index,
                          'nav': nav, 'bench_close': pd.Series(bc['close'].values, index=bc['date']),
                          'fees': fees_str, 'tco': tco, 'tco_ongoing': tco_ongoing,
                          'group': rules._peer_group({'type': ptype, 'name': name})})
    if not raw_prods:
        print('no products with data; skipped: ' + ';'.join(skipped))
        return 1
    D = pd.DatetimeIndex(sorted(set().union(*[set(p['nav']['date']) for p in raw_prods],
                                             *[set(p['bench_close'].index) for p in raw_prods])))
    aligned = []
    for p in raw_prods:
        factors = product_factors(p['nav'], p['bench_close'], args.rf)
        nav_ff, fresh_nav = align(p['nav']['date'], p['nav']['nav_adj'], D)
        fac = {}
        for k in ('tail', 'sharpe', 'alpha', 'win_ret'):
            ff, fr = align(p['nav']['date'], factors[k], D)
            fac[k] = (ff, fr)
        bench_ff, bench_fresh = align(p['bench_close'].index, p['bench_close'].values, D)
        aligned.append({'code': p['code'], 'name': p['name'], 'type': p['type'], 'group': p['group'],
                        'tco': p['tco'], 'tco_ongoing': p['tco_ongoing'], 'nav': nav_ff, 'fresh': fresh_nav, 'fac': fac,
                        'bench': bench_ff, 'bench_fresh': bench_fresh})
    print('统一日历 %s ~ %s（%d 交易日）；有效产品 %d；跳过：%s'
          % (str(D[0])[:10], str(D[-1])[:10], len(D), len(aligned),
             ';'.join(skipped) if skipped else '无'))
    results = {}
    rows_by_h = {}
    ts_by_h = {}
    for H in horizons:
        rows, ts = walk_ic(aligned, D, H_DAYS[H], args.step, args.embargo, args.min_n, args.years)
        agg = aggregate(rows)
        results[H] = agg
        rows_by_h[H] = rows
        ts_by_h[H] = ts
        print('== 前瞻 %d 个月：有效截面 %d 个（每个截面产品数 >= %d） ==' % (H, len(ts), args.min_n))
        if not ts:
            print('（有效产品 %d 个 < min_n=%d，无法形成足够横截面；建议扩大产品池或调低 --min-n）'
                  % (len(aligned), args.min_n))
            continue
        order = sorted(agg.keys(), key=lambda f: agg[f]['icir'], reverse=True)
        print('%-12s %6s %8s %8s %8s %8s  %s' % ('因子', '截面数', 'IC均值', 'ICstd', 'ICIR', 'IC>0', '结论'))
        for fac in order:
            a = agg[fac]
            print('%-12s %6d %8.3f %8.3f %8.3f %6.0f%%  %s'
                  % (FAC_NAMES.get(fac, fac), a['n'], a['mean'], a['std'], a['icir'],
                     a['pos'] * 100.0, verdict(a['icir'])))
        do = deoverlap_icir(rows, H_DAYS[H], args.min_n)
        if do:
            print('无重叠标签稳健版 ICIR：%s'
                  % '；'.join('%s=%.3f' % (FAC_NAMES.get(f, f), v) for f, v in sorted(do.items(), key=lambda z: -z[1])))
        print('')
    weights = {}
    combined_icir = {}
    for fac in FACTORS:
        vals = [results[h].get(fac, {}).get('icir') for h in horizons]
        vals = [v for v in vals if v is not None]
        combined_icir[fac] = float(np.mean(vals)) if vals else None
    for h in (6, 12, 99):
        if h == 99:
            icir_map = {f: combined_icir[f] for f in FACTORS}
        elif h in results:
            icir_map = results[h]
        else:
            icir_map = {f: None for f in FACTORS}
        weights[h] = suggest_weights(icir_map)
    print('== 权重校准（权重 ∝ ICIR；实测维度归一化，未测维度维持现权，重归一化到 100）==')
    print('%-12s %8s %8s %8s %8s  %s' % ('维度', '当前', '6M建议', '12M建议', '综合建议', '依据'))
    for dim in ('tco', 'alpha', 'tail', 'marginal', 'stability', 'tradability'):
        print('%-12s %8s %8s %8s %8s  %s'
              % (dim, CURRENT_WEIGHTS[dim], weights[6].get(dim, ''), weights[12].get(dim, ''),
                 weights[99].get(dim, ''), '实测ICIR' if dim in MEASURED_DIMS else '未测(离线不可得)'))
    print('')
    if not args.no_write:
        try:
            os.makedirs(REPORT_DIR, exist_ok=True)
            path = os.path.join(REPORT_DIR, 'ic_report_' + date.today().isoformat() + '.md')
            md = build_report(args, [a['code'] for a in aligned], D, results, rows_by_h, ts_by_h,
                              skipped, weights, combined_icir, aligned=aligned, bench_series=bench_ret_series)
            with io.open(path, 'w', encoding='utf-8') as f:
                f.write(md)
            print('报告已写：' + path)
        except Exception as e:
            print('报告写入失败：%s' % e)
    return 0


if __name__ == '__main__':
    sys.exit(main())