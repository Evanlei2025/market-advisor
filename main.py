# -*- coding: utf-8 -*-
"""每日投顾报告生成器（V3：用户反馈驱动的模板重构——砍重复、补闭环）
数据采集 -> 规则决策 -> 完整版报告 + 精简版推送 + HTML 网页
"""
import argparse
import copy
import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import date, datetime
import html
import io

import pandas as pd
import requests

import html_render
import llm
import narrative
import news_alert
import rules
import state_store
import aktime

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
FRESH_TOLERANCE_DAYS = 3  # 数据最后日期距今天超过该自然日数 → 判定滞后

# 数据源运行状态（报告「系统运行状态」板块数据）
FETCH_STATUS = {}


def _mark_fetch(name, ok, detail=""):
    FETCH_STATUS[name] = "ok" if ok else detail or f"fail:{name}"


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


def deep_merge(base, override):
    """深度递归合并配置：子 dict 递归合并，list/标量直接覆盖（override 优先）。
    返回新 dict，不改动 base/override 原对象。"""
    if isinstance(base, dict) and isinstance(override, dict):
        out = copy.deepcopy(base)
        for k, v in override.items():
            if k in out:
                out[k] = deep_merge(out[k], v)
            else:
                out[k] = copy.deepcopy(v)
        return out
    return copy.deepcopy(override)


def get_clients(cfg):
    """多客户配置视图：cfg 含非空 clients dict → 逐客户生成
    {'id', 'display_name', 'cfg': deep_merge(全局段, 客户段)}，保持 clients 键插入顺序；
    旧形态配置（无 clients）→ 单客户 default 兼容视图。
    sub_cfg 与旧单客户 cfg 形态一致（push/rules/settlement/news_watch/target/
    portfolio/products/holdings/transactions），另附 client_display 供叙事/LLM 使用。"""
    clients = cfg.get('clients') if isinstance(cfg, dict) else None
    if not isinstance(clients, dict) or not clients:
        base_cfg = dict(cfg) if isinstance(cfg, dict) else {}
        base_cfg['client_display'] = '客户组合'
        return [{'id': 'default', 'display_name': '客户组合', 'cfg': base_cfg}]
    base = {k: v for k, v in cfg.items() if k != 'clients'}
    out = []
    for cid, csec in clients.items():
        csec = csec if isinstance(csec, dict) else {}
        disp = csec.get('display_name') or cid
        sub = deep_merge(base, csec)
        sub['client_display'] = disp
        out.append({'id': cid, 'display_name': disp, 'cfg': sub})
    return out


def _pick_cols(df, *candidates):
    """列名防御：返回第一个存在于 df.columns 的候选列名，全部缺失返回 None（1.6 类型安全）"""
    if df is None:
        return None
    for c in candidates:
        if c in df.columns:
            return c
    return None


class DataFetcher:
    """行情用腾讯源(稳定)，估值用乐咕，债券/黄金用官方源，宏观扩展指标失败不阻塞。
    备选链：主源失败 → 备选源 → 返回 None（调用方降级，2.1）"""

    def __init__(self):
        import akshare as ak
        self.ak = ak
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        self._top10_cache = {}  # 基金重仓股进程内缓存 {fund_code: list|None}（A4）
        self._purchase_cache = None  # 全市场申赎状态表缓存（A7：进程内只拉一次）
        self._holder_cache = {}  # 持有人结构缓存 {fund_code: dict|None}
        self._scale_cache = {}  # 规模历史缓存 {fund_code: list|None}
        self._leverage_cache = {}  # 债基杠杆缓存 {fund_code: float|None}
        self._leverage_detail = {}  # 杠杆明细 {fund_code: {repo, period}}
        self._turnover_cache = {}  # 换手率缓存 {fund_code: dict|None}
        self._partner_cache = {}  # A/C 配对缓存 {fund_code: partner|None}
        self._fees_cache = {}  # 份额费率缓存 {fund_code: dict|None}
        self._partner_info_cache = {}  # 配对+费率缓存 {fund_code: {code, fees}|None}
        self._fund_name_df = None  # 全市场份额列表缓存（fund_name_em 一次拉取）

    def _ak(self, fn, *args, timeout=90, **kwargs):
        """akshare 调用统一超时保护（ak 内部 requests 无 timeout，云端网络下可能挂死）。
        超时/异常原样抛出，由 fetch_section 重试与降级处理。"""
        return aktime.call_with_timeout(fn, timeout, *args, **kwargs)

    def index_daily(self, tx_symbol, days=320):
        try:
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
        except Exception:
            # 备选源：akshare 指数日线（2.1）
            try:
                raw = self._ak(self.ak.stock_zh_index_daily, symbol=tx_symbol)
                if raw is None or raw.empty:
                    return None
                df = raw.tail(days).reset_index(drop=True)
                df = df[["date", "open", "close", "high", "low", "volume"]].copy()
                for c in df.columns[1:]:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
                df["pct"] = df["close"].pct_change() * 100
                return df
            except Exception:
                return None

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
                item = {'name': f[1], 'close': float(f[3]), 'pct': float(f[32])}
                # 实测 qt.gtimg.cn 字段位：f[37]=成交额(万元)，sh000300=56028693万≈5602.9亿；f[6] 是成交量(手)
                try:
                    amt_wan = float(f[37]) if len(f) > 37 and str(f[37]).strip() else 0.0
                except (TypeError, ValueError):
                    amt_wan = 0.0
                item['amount'] = amt_wan  # 原始单位：万元（调用处自行换算亿元）
                out[f[2]] = item
        return out

    def pe_history(self, symbol):
        df = self._pe_lg(symbol)
        if df is None or df.empty:
            return df
        c_date = _pick_cols(df, "日期", "trade_date")
        c_pe = _pick_cols(df, "滚动市盈率", "市盈率", "pe")
        if c_date is None or c_pe is None:
            return None
        df = df[[c_date, c_pe]].copy()
        df.columns = ["date", "pe"]
        df["date"] = pd.to_datetime(df["date"])
        cutoff = pd.Timestamp(f"{datetime.now().year - PE_HIST_YEARS}-01-01")
        return df[df["date"] >= cutoff].dropna()

    def pe_history_full(self, symbol):
        df = self._pe_lg(symbol)
        if df is None or df.empty:
            return df
        c_date = _pick_cols(df, "日期", "trade_date")
        c_pe = _pick_cols(df, "滚动市盈率", "市盈率", "pe")
        if c_date is None or c_pe is None:
            return None
        df = df[[c_date, c_pe]].copy()
        df.columns = ["date", "pe"]
        df["date"] = pd.to_datetime(df["date"])
        return df.dropna()

    def _pe_lg(self, symbol):
        """乐咕PE（支持：上证50/沪深300/上证380/创业板50/中证500/上证180/深证红利/深证100/中证1000/上证红利/中证100/中证800）"""
        try:
            df = self._ak(self.ak.stock_index_pe_lg, symbol=symbol)
            if df is not None and not df.empty:
                return df
        except TimeoutError:
            raise
        except Exception:
            pass
        return None

    def bond_rates(self):
        df = self._ak(self.ak.bond_zh_us_rate, )
        c_date = _pick_cols(df, "日期")
        c_y2 = _pick_cols(df, "中国国债收益率2年")
        c_y10 = _pick_cols(df, "中国国债收益率10年")
        if c_date is None or c_y2 is None or c_y10 is None:
            return None
        df = df[[c_date, c_y2, c_y10]].dropna()
        df.columns = ["date", "y2", "y10"]
        df["date"] = pd.to_datetime(df["date"])
        cutoff = pd.Timestamp(f"{datetime.now().year - PE_HIST_YEARS}-01-01")
        return df[df["date"] >= cutoff]

    def bond_rates_cn(self):
        df = self._ak(self.ak.bond_gb_zh_sina, symbol="中国10年期国债")
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
            df = self._ak(self.ak.fund_portfolio_bond_hold_em, symbol=fund_code, date=str(datetime.now().year))
            if df is None or df.empty or "占净值比例" not in df.columns:
                return 0.0
            mask = df["债券名称"].astype(str).str.contains("转债", na=False)
            return float(df.loc[mask, "占净值比例"].sum()) / 100.0
        except TimeoutError:
            raise
        except Exception:
            return 0.0

    def fund_top10(self, fund_code):
        # 基金季报前十大重仓股（最新季度）：返回 [stock_name/weight 列表] 或 None，weight 为占净值比例(%)保留1位小数，缺失为 None。
        # 接口：ak.fund_portfolio_hold_em(symbol, date=今年)（实测列：股票名称/占净值比例/季度，季度形如 2026年2季度股票投资明细）。
        # 缓存链：内存 → 接口成功(写 knowledge_base/products/<code>_top10.json) → 缓存文件 → None。
        # 债券/货基无股票持仓 → None；拉取失败绝不阻塞产品分析。
        cache_path = os.path.join(BASE_DIR, 'knowledge_base', 'products', f'{fund_code}_top10.json')
        if fund_code in self._top10_cache:
            return self._top10_cache[fund_code]
        try:
            df = self._ak(self.ak.fund_portfolio_hold_em, symbol=fund_code, date=str(datetime.now().year))
            if df is not None and not df.empty and '季度' in df.columns:
                d = df.copy()
                q = d['季度'].astype(str).str.extract(r'(\d{4})年(\d{1,2})季度')
                d['_y'] = pd.to_numeric(q[0], errors='coerce')
                d['_q'] = pd.to_numeric(q[1], errors='coerce')
                d = d.dropna(subset=['_y', '_q'])
                if not d.empty:
                    k = d['_y'] * 100 + d['_q']
                    latest = d[k == k.max()]
                    c_name = _pick_cols(latest, '股票名称')
                    c_w = _pick_cols(latest, '占净值比例')
                    if c_name is not None and c_w is not None:
                        stocks = []
                        for _, r in latest.sort_values(c_w, ascending=False).head(10).iterrows():
                            w = r[c_w]
                            stocks.append({'stock_name': str(r[c_name]),
                                           'weight': round(float(w), 1) if w == w and w is not None else None})
                        if stocks:
                            kmax = int(k.max())
                            quarter = f'{kmax // 100}Q{kmax % 100}'
                            self._top10_cache[fund_code] = stocks
                            try:
                                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                                payload = {'quarter': quarter, 'stocks': stocks,
                                           '_comment': '基金季报前十大重仓股缓存：quarter 形如 2026Q2；stocks 为 [{stock_name, weight}]，weight 为占净值比例(%)，缺失为 null'}
                                with open(cache_path, 'w', encoding='utf-8') as f:
                                    json.dump(payload, f, ensure_ascii=False, indent=2)
                            except Exception:
                                pass
                            return stocks
        except Exception:
            pass
        # 接口失败 → 读缓存文件（quarter 存在即用）
        try:
            if os.path.exists(cache_path):
                with open(cache_path, encoding='utf-8') as f:
                    data = json.load(f)
                if data.get('quarter') and isinstance(data.get('stocks'), list):
                    self._top10_cache[fund_code] = data['stocks']
                    return data['stocks']
        except Exception:
            pass
        self._top10_cache[fund_code] = None
        return None

    # ---- 迭代六：持有人结构 / 规模历史 / 债基杠杆 / 换手率 / A-C 份额配对（全部静默降级，进程内缓存） ----
    @staticmethod
    def _f10_pct(v):
        # F10 百分比解析：---/NaN/空 -> None；10.42% -> 0.1042（小数）
        try:
            if v is None or v != v:
                return None
            s = str(v).strip().replace('%', '').replace(',', '')
            if s in ('', '---', 'nan', 'None'):
                return None
            return float(s) / 100.0
        except (TypeError, ValueError):
            return None
    @staticmethod
    def _f10_num(v):
        # F10 数值解析：---/NaN/空 -> None；740,194,641.92 -> 740194641.92
        try:
            if v is None or v != v:
                return None
            s = str(v).strip().replace(',', '')
            if s in ('', '---', 'nan', 'None'):
                return None
            return float(s)
        except (TypeError, ValueError):
            return None
    def _f10_table(self, fund_code, ftype, extra=''):
        # 天天基金 F10 底层接口：FundArchivesDatas.aspx?type=<ftype>&code=<code><extra>&rt=<random>
        # 必须带 Referer 头（否则 404）；返回体为 JS 变量包裹的 HTML 片段（content 字段）-> pandas.read_html。失败返回 None。
        try:
            url = ('https://fundf10.eastmoney.com/FundArchivesDatas.aspx' 
                   '?type=%s&code=%s%s&rt=%d' % (ftype, fund_code, extra, int(time.time() * 1000)))
            headers = {'Referer': 'https://fundf10.eastmoney.com/%s_%s.html' % (ftype, fund_code),
                       'User-Agent': self.session.headers.get('User-Agent', 'Mozilla/5.0')}
            r = self.session.get(url, headers=headers, timeout=15)
            m = re.search('content:"(.*?)"', r.text, re.S)
            if not m or '<table' not in m.group(1):
                m = re.search('content:"(<table.*?</table>)"', r.text, re.S)
            if not m:
                return None
            c = m.group(1).replace(chr(92) + chr(39), chr(39)).replace(chr(92) + chr(34), chr(34))
            c = html.unescape(c)
            if '<table' not in c:
                return None
            tables = pd.read_html(io.StringIO(c))
            return tables if tables else None
        except Exception:
            return None
    def holder_structure(self, fund_code):
        # 持有人结构（F10 type=cyrjg，半年报披露）：返回最新一期 dict 或 None
        # 字段 report_date/inst_ratio/personal_ratio/internal_ratio（比例均为小数；--- 缺失 -> None）
        if fund_code in self._holder_cache:
            return self._holder_cache[fund_code]
        out = None
        try:
            tables = self._f10_table(fund_code, 'cyrjg')
            if tables:
                df = tables[0]
                c_date = _pick_cols(df, '公告日期')
                c_inst = _pick_cols(df, '机构持有比例')
                c_pers = _pick_cols(df, '个人持有比例')
                c_int = _pick_cols(df, '内部持有比例')
                if c_date and c_inst and c_pers and c_int and len(df) > 0:
                    row = df.iloc[0]  # 倒序：最新报告期在第一行
                    out = {'report_date': str(row[c_date])[:10],
                           'inst_ratio': self._f10_pct(row[c_inst]),
                           'personal_ratio': self._f10_pct(row[c_pers]),
                           'internal_ratio': self._f10_pct(row[c_int])}
        except Exception:
            out = None
        self._holder_cache[fund_code] = out
        return out
    def scale_history(self, fund_code):
        # 规模历史（F10 type=gmbd，季度）：返回全量升序序列或 None
        # 元素 {date, net_assets, shares, change_rate}（亿元/亿份/小数变动率）
        # 历史行固定不回改（天然 point-in-time）；仅最新行随披露刷新
        if fund_code in self._scale_cache:
            return self._scale_cache[fund_code]
        out = None
        try:
            tables = self._f10_table(fund_code, 'gmbd')
            if tables:
                df = tables[0]
                c_date = _pick_cols(df, '日期')
                c_sh = _pick_cols(df, '期末总份额（亿份）', '期末总份额')
                c_na = _pick_cols(df, '期末净资产（亿元）', '期末净资产')
                c_cr = _pick_cols(df, '净资产变动率')
                if c_date and c_na:
                    rows = []
                    for _, r in df.iterrows():
                        d = str(r[c_date]).strip()[:10]
                        if not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
                            continue
                        rows.append({'date': d,
                                     'net_assets': self._f10_num(r[c_na]),
                                     'shares': self._f10_num(r[c_sh]) if c_sh else None,
                                     'change_rate': self._f10_pct(r[c_cr]) if c_cr else None})
                    rows = [x for x in rows if x.get('net_assets') is not None]
                    if rows:
                        rows.sort(key=lambda x: x['date'])
                        out = rows
        except Exception:
            out = None
        self._scale_cache[fund_code] = out
        return out
    def bond_leverage(self, fund_code):
        # 债基杠杆（F10 type=zcfzb&showtype=1&year=<当年>）：杠杆率 = 最近报告期 资产总计/所有者权益合计（小数）
        # 注意 showtype=0 只返回科目名空表，必须 showtype=1；卖出回购金融资产款（正回购来源）存 _leverage_detail
        if fund_code in self._leverage_cache:
            return self._leverage_cache[fund_code]
        leverage = None
        try:
            extra = '&showtype=1&year=%d' % datetime.now().year
            tables = self._f10_table(fund_code, 'zcfzb', extra)
            if tables:
                df = pd.concat(tables, ignore_index=True)
                date_cols = [c for c in df.columns if re.match(r'^\d{4}-\d{2}-\d{2}$', str(c))]
                item_cols = [c for c in df.columns if c not in date_cols]
                if date_cols and item_cols:
                    latest = date_cols[0]  # 表按报告期倒序，首列为最新
                    items = df[item_cols].fillna('').astype(str)
                    item = items.apply(lambda r: ''.join(str(v) for v in r), axis=1)
                    def row_val(key):
                        m = df.loc[item == key, latest]
                        if len(m) == 0:
                            return None
                        return self._f10_num(m.iloc[0])
                    assets = row_val('资产总计')
                    equity = row_val('所有者权益合计')
                    repo = row_val('卖出回购金融资产款')
                    if assets is not None and equity and equity > 0:
                        leverage = assets / equity
                        self._leverage_detail[fund_code] = {
                            'repo': repo, 'period': latest,
                            'assets': assets, 'equity': equity}
        except Exception:
            leverage = None
        self._leverage_cache[fund_code] = leverage
        return leverage
    def _portfolio_change(self, fund_code, indicator, year):
        # fund_portfolio_change_em 容错包装：当年未披露返回空表并 KeyError -> None
        try:
            df = self._ak(self.ak.fund_portfolio_change_em, symbol=fund_code, indicator=indicator, date=str(year))
            if df is None or df.empty or '本期累计买入金额' not in df.columns:
                return None
            return df
        except TimeoutError:
            raise
        except Exception:
            return None
    def turnover(self, fund_code):
        # 换手率（akshare fund_portfolio_change_em 累计买入/卖出，半年报/年报）：
        # 取最新披露期（4季度=年报/2季度=半年报）行合计，换手率 = min(买入, 卖出) / 期初净资产；
        # 期初净资产取 scale_history 同一年份最早报告期；无则用占期初净值比例列合计近似；再不行 None。
        # 纯债/货币无股票披露 -> None（正常）。返回 {period, value}（value 为倍率小数）或 None
        if fund_code in self._turnover_cache:
            return self._turnover_cache[fund_code]
        out = None
        try:
            y = datetime.now().year
            buy_df = self._portfolio_change(fund_code, '累计买入', y) or self._portfolio_change(fund_code, '累计买入', y - 1)
            sell_df = self._portfolio_change(fund_code, '累计卖出', y) or self._portfolio_change(fund_code, '累计卖出', y - 1)
            if buy_df is not None and sell_df is not None and len(buy_df) > 0 and len(sell_df) > 0:
                def latest_total(d):
                    q = d['季度'].astype(str).str.extract(r'^(\d{4})年(\d{1,2})季度')
                    k = pd.to_numeric(q[0], errors='coerce') * 10 + pd.to_numeric(q[1], errors='coerce')
                    d = d.assign(_k=k).dropna(subset=['_k'])
                    if d.empty:
                        return None, None, None
                    kmax = d['_k'].max()
                    sub = d[d['_k'] == kmax]
                    total = float(pd.to_numeric(sub['本期累计买入金额'], errors='coerce').sum())
                    ratio = None
                    if '占期初基金资产净值比例' in sub.columns:
                        ratio = float(pd.to_numeric(sub['占期初基金资产净值比例'], errors='coerce').sum())
                    return kmax, total, ratio
                kmax, buy_total, buy_ratio = latest_total(buy_df)
                _, sell_total, _ = latest_total(sell_df)
                if kmax is not None and buy_total is not None and sell_total is not None:
                    beg_nav = None
                    hist = self.scale_history(fund_code)
                    yr = int(kmax) // 10
                    if hist:
                        same_yr = [h for h in hist if str(h.get('date', '')).startswith(str(yr))]
                        if same_yr:
                            beg_nav = same_yr[0].get('net_assets')  # 升序 -> 最早报告期
                    if beg_nav is None and buy_ratio is not None and buy_ratio > 0:
                        beg_nav = buy_total / (buy_ratio / 100.0) / 10000.0  # 万元 -> 亿元（占净值比例近似）
                    if beg_nav is not None and beg_nav > 0:
                        t = min(buy_total, sell_total) / (beg_nav * 10000.0)
                        period = '%d年报' % yr if int(kmax) % 10 == 4 else '%d半年报' % yr
                        out = {'period': period, 'value': round(t, 2)}
        except Exception:
            out = None
        self._turnover_cache[fund_code] = out
        return out
    def _fund_name_map(self):
        # 全市场份额列表（fund_name_em，约 5-7s）：进程内只拉一次；失败 None（配对静默降级）
        if self._fund_name_df is None:
            try:
                df = self._ak(self.ak.fund_name_em, )
                self._fund_name_df = df if (df is not None and not df.empty and '基金简称' in df.columns) else None
            except Exception:
                self._fund_name_df = None
        return self._fund_name_df
    def ac_partner(self, fund_code):
        # A/C 份额配对：按 fund_name_em 简称去 A/C/E 后缀配对同家族其他份额 -> partner code；无 -> None
        if fund_code in self._partner_cache:
            return self._partner_cache[fund_code]
        partner = None
        try:
            df = self._fund_name_map()
            if df is not None:
                code_s = str(fund_code).strip()
                row = df[df['基金代码'].astype(str) == code_s]
                if len(row) > 0:
                    fam = re.sub(r'[ACE]$', '', str(row.iloc[0]['基金简称']).strip())
                    if fam:
                        others = df[df['基金代码'].astype(str) != code_s]
                        hit = others[others['基金简称'].astype(str).apply(
                            lambda n: re.sub(r'[ACE]$', '', str(n).strip()) == fam)]
                        if len(hit) > 0:
                            partner = str(hit.iloc[0]['基金代码']).strip()
        except Exception:
            partner = None
        self._partner_cache[fund_code] = partner
        return partner
    def partner_fees(self, partner_code):
        # 配对份额费率（雪球 detail_info_xq，与 fund_fees 同源）：
        # 管理费在「其他费用」含管理费、申购费在「买入规则」首档、赎回费在「卖出规则」<7天档。
        # 返回 {mgmt, trustee, sales, purchase, short_redeem}（小数，缺失字段 None）；全缺 -> None
        if partner_code in self._fees_cache:
            return self._fees_cache[partner_code]
        out = None
        try:
            df = self._ak(self.ak.fund_individual_detail_info_xq, symbol=partner_code)
            if df is not None and not df.empty and '费用类型' in df.columns:
                recs = df.to_dict('records')
                has_lt7 = any('<7' in str(r.get('条件或名称', '')) for r in recs)
                fees = {'mgmt': None, 'trustee': None, 'sales': None,
                        'purchase': None, 'short_redeem': None}
                for row in recs:
                    t = str(row.get('费用类型', ''))
                    cond = str(row.get('条件或名称', ''))
                    raw = str(row.get('费用', ''))
                    try:
                        v = float(raw.replace('%', '').strip()) / 100.0
                    except (TypeError, ValueError):
                        continue
                    if t == '买入规则' and fees['purchase'] is None and v < 0.1:
                        fees['purchase'] = v  # 首档费率（500万+ 1000元封顶行跳过）
                    elif t == '卖出规则' and fees['short_redeem'] is None:
                        if '<7' in cond:
                            fees['short_redeem'] = v  # <7天档（短期惩罚档）
                        elif not has_lt7:
                            fees['short_redeem'] = v  # 无 <7 档 -> 首行兜底
                    elif t == '其他费用':
                        if '管理费' in cond:
                            fees['mgmt'] = v
                        elif '托管费' in cond:
                            fees['trustee'] = v
                        elif '销售服务费' in cond:
                            fees['sales'] = v
                if any(v is not None for v in fees.values()):
                    out = fees
        except Exception:
            out = None
        self._fees_cache[partner_code] = out
        return out
    def ac_partner_info(self, fund_code):
        # TCO 择优数据：{code: partner_code, fees: partner_fees}；配对失败或费率全缺 -> None
        if fund_code in self._partner_info_cache:
            return self._partner_info_cache[fund_code]
        out = None
        try:
            pc = self.ac_partner(fund_code)
            if pc:
                pf = self.partner_fees(pc)
                if pf:
                    out = {'code': pc, 'fees': pf}
        except Exception:
            out = None
        self._partner_info_cache[fund_code] = out
        return out
    def fund_industry_hhi(self, fund_code):
        """行业集中度 HHI：基金行业配置（证监会大类）前三大权重平方和（归一化）。
        返回 0~1；数据缺失/异常返回 None。"""
        try:
            df = self._ak(self.ak.fund_portfolio_industry_allocation_em, symbol=fund_code, date=str(datetime.now().year))
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
        except TimeoutError:
            raise
        except Exception:
            return None

    def gold_daily(self, days=320):
        try:
            df = self._ak(self.ak.spot_hist_sge, symbol="Au99.99")
            df = df[["date", "close"]]
            return df.tail(days).reset_index(drop=True)
        except Exception:
            # 备选源：沪金主力连续（2.1）
            try:
                df = self._ak(self.ak.futures_main_sina, symbol="AU0")
                if df is None or len(df) < 2:
                    return None
                df = df[["日期", "收盘价"]].copy()
                df.columns = ["date", "close"]
                df["close"] = pd.to_numeric(df["close"], errors="coerce")
                df = df.dropna()
                return df.tail(days).reset_index(drop=True)
            except Exception:
                return None

    def cls_news(self):
        df = self._ak(self.ak.stock_info_global_cls, )
        return df[["标题", "内容", "发布时间"]].dropna()

    def lpr(self):
        df = self._ak(self.ak.macro_china_lpr, )
        return df.iloc[-1]

    def usd_cny(self):
        df = self._ak(self.ak.fx_spot_quote, )
        row = df[df["货币对"] == "USD/CNY"].iloc[0]
        return float(row["买报价"])

    def fund_daily(self, fund_code):
        df = self._ak(self.ak.fund_open_fund_info_em, symbol=fund_code, indicator="单位净值走势")
        df = df[["净值日期", "单位净值", "日增长率"]].tail(3)
        return df.reset_index(drop=True)

    def fund_nav_history(self, fund_code):
        # 复权序列 nav_adj（分红再投资口径）：r_t = (A_t - A_{t-1}) / NAV_{t-1}，A=累计净值，NAV=单位净值
        # 异常护栏：|r_t| > 15% → log 告警并回退 growth/100（growth 为 NaN → 0）；首日 r_t = 0
        # 累计净值拉取失败/全空 → nav_adj = nav（列始终存在，下游无需判列缺失）
        df = self._ak(self.ak.fund_open_fund_info_em, symbol=fund_code, indicator="单位净值走势")
        c_date = _pick_cols(df, "净值日期")
        c_nav = _pick_cols(df, "单位净值")
        c_growth = _pick_cols(df, "日增长率")
        if c_date is None or c_nav is None:
            return None
        df = df[[c_date, c_nav, c_growth]].dropna(subset=[c_date, c_nav]).copy() if c_growth else df[[c_date, c_nav]].dropna(subset=[c_date, c_nav]).copy()
        df.columns = ["date", "nav"] + (["growth"] if c_growth else ["growth"])
        if c_growth is None:
            df["growth"] = float("nan")
        df = df.reset_index(drop=True)
        df["nav_adj"] = df["nav"]  # 默认降级：复权列 = 单位净值
        try:
            dfc = self._ak(self.ak.fund_open_fund_info_em, symbol=fund_code, indicator="累计净值走势")
            c_cdate = _pick_cols(dfc, "净值日期")
            c_cum = _pick_cols(dfc, "累计净值")
            if c_cdate is not None and c_cum is not None and dfc is not None and not dfc.empty:
                cum = dfc[[c_cdate, c_cum]].dropna().copy()
                cum.columns = ["date", "cum"]
                m = pd.merge(df, cum, on="date", how="left")
                if m["cum"].notna().sum() >= 2:
                    m["cum"] = m["cum"].ffill()
                    r = (m["cum"] - m["cum"].shift(1)) / m["nav"].shift(1)
                    bad = r.abs() > 0.15
                    if bad.any():
                        log("[WARN] " + fund_code + " 复权日收益异常 " + str(int(bad.sum())) + " 行(|r|>15%)，回退日增长率")
                        g = m["growth"] / 100.0
                        fb = g.where(g.notna(), 0.0)
                        r = r.where(~bad, fb)
                    r = r.fillna(0.0)  # 首日/前值缺失 → 0
                    nav0 = float(m["nav"].iloc[0]) if not pd_isna(m["nav"].iloc[0]) else 1.0
                    m["nav_adj"] = (1.0 + r).cumprod() * nav0
                    if m["nav_adj"].notna().sum() >= 2:
                        df = df.merge(m[["date", "nav_adj"]], on="date", how="left", suffixes=("", "_m"))
                        df["nav_adj"] = df["nav_adj_m"].fillna(df["nav"])
                        df = df.drop(columns=["nav_adj_m"])
        except Exception as e:
            log("[WARN] " + fund_code + " 累计净值/复权重建失败(" + str(e) + ")，nav_adj 降级为 nav")
        return df

    def purchase_status_map(self):
        # 全市场基金申赎状态表（东财 fund_purchase_em，约 2.7 万行，10-30 秒）
        # 进程内缓存：多客户/多产品只拉一次；失败返回 {}（静默降级，不阻塞）
        if self._purchase_cache is None:
            cache = {}
            try:
                df = self._ak(self.ak.fund_purchase_em, )
                if df is not None and not df.empty and "基金代码" in df.columns:
                    def _s(v):
                        return "" if v is None or v != v else str(v)
                    for rec in df.to_dict("records"):
                        code = str(rec.get("基金代码", "")).strip()
                        if not code:
                            continue
                        ma = rec.get("购买起点")
                        dl = rec.get("日累计限定金额")
                        cache[code] = {
                            "purchase": _s(rec.get("申购状态")),
                            "redeem": _s(rec.get("赎回状态")),
                            "min_amount": float(ma) if ma is not None and ma == ma else None,
                            "daily_limit": float(dl) if dl is not None and dl == dl else None,
                        }
            except Exception as e:
                log("[WARN] 全市场申赎状态拉取失败(" + str(e) + ")，降级为空表")
            self._purchase_cache = cache
        return self._purchase_cache

    def fund_profile(self, fund_code):
        df = self._ak(self.ak.fund_individual_basic_info_xq, symbol=fund_code)
        return dict(zip(df["item"], df["value"]))

    def fund_fees(self, fund_code):
        df = self._ak(self.ak.fund_individual_detail_info_xq, symbol=fund_code)
        return df.to_dict("records")

    def fund_achievement(self, fund_code):
        df = self._ak(self.ak.fund_individual_achievement_xq, symbol=fund_code)
        return df.to_dict("records")

    def market_pe(self):
        df = self._ak(self.ak.stock_market_pe_lg, symbol="上证")
        return df

    def us_index(self):
        df = self._ak(self.ak.index_us_stock_sina, symbol=".INX").tail(2)
        df = df[["date", "close"]].reset_index(drop=True)
        df["pct"] = df["close"].pct_change() * 100
        return df

    def margin_sse(self):
        start = (datetime.now() - pd.Timedelta(days=10)).strftime("%Y%m%d")
        df = self._ak(self.ak.stock_margin_sse, start_date=start, end_date=datetime.now().strftime("%Y%m%d"))
        return df.head(3)

    # ---- 宏观扩展指标（V2.2 扩容；全部失败不阻塞） ----
    def us_bond_10y(self):
        """美债10年收益率（英为中美国债收益率表）"""
        df = self._ak(self.ak.bond_zh_us_rate, )
        col = "美国国债收益率10年"
        if col not in df.columns:
            return None
        v = df[col].dropna()
        if v.empty:
            return None
        return float(v.iloc[-1])

    def crude_oil(self):
        """上期能源原油主力（SC0）"""
        df = self._ak(self.ak.futures_main_sina, symbol="SC0")
        if df is None or len(df) < 2:
            return None
        cur = float(df["收盘价"].iloc[-1])
        prev = float(df["收盘价"].iloc[-2])
        return cur, (cur / prev - 1)

    def copper(self):
        """沪铜主力（CU0，国际铜价定价锚）"""
        df = self._ak(self.ak.futures_main_sina, symbol="CU0")
        if df is None or len(df) < 2:
            return None
        cur = float(df["收盘价"].iloc[-1])
        prev = float(df["收盘价"].iloc[-2])
        return cur, (cur / prev - 1)

    def _macro_yoy(self, ak_fn, col_filter, date_col='月份'):
        # 宏观同比序列通用解析：取统计期最新一行，返回 (value, 'YYYY-MM')。
        # 实测 CPI/PPI 接口按时间倒序（head 为最新），必须按月份解析取最大期，不能用 iloc[-1]：
        # 旧实现取到 2008-01 的 7.08%，正是用户看到的 +7.1% 假数据。
        # 合理性校验：|value| > 15 或 NaN → None（防脏数据误报）。
        try:
            df = self._ak(getattr(self.ak, ak_fn))
            col = [c for c in df.columns if col_filter(c)]
            if not col or date_col not in df.columns:
                return None
            d = df[[date_col, col[0]]].copy().dropna()
            if d.empty:
                return None
            m = d[date_col].astype(str).str.extract(r'(\d{4})[年\-/]?(\d{1,2})')
            d['_k'] = pd.to_numeric(m[0], errors='coerce') * 100 + pd.to_numeric(m[1], errors='coerce')
            d = d.dropna(subset=['_k']).sort_values('_k')
            if d.empty:
                return None
            v = float(d[col[0]].iloc[-1])
            if pd_isna(v) or abs(v) > 15:
                return None
            mm = re.search(r'(\d{4})[年\-/]?(\d{1,2})', str(d[date_col].iloc[-1]))
            period = f'{int(mm.group(1)):04d}-{int(mm.group(2)):02d}' if mm else ''
            return v, period
        except Exception:
            return None

    def cpi(self):
        # CPI 同比(%)：返回 (value, period)；异常/缺失 → None
        return self._macro_yoy('macro_china_cpi', lambda c: '同比' in str(c))

    def ppi(self):
        # PPI 同比(%)：返回 (value, period)；异常/缺失 → None
        return self._macro_yoy('macro_china_ppi', lambda c: '同比' in str(c))

    def pmi(self):
        df = self._ak(self.ak.macro_china_pmi, )
        col = [c for c in df.columns if "制造业" in str(c)]
        if not col:
            return None
        v = df[col[0]].dropna()
        return float(v.iloc[-1]) if not v.empty else None

    def shrzgm(self):
        # 社会融资规模增量（单位即亿元）：返回 dict(value=亿元, month='YYYY-MM')；异常 → None。
        # 实测列 社会融资规模增量 单位就是亿元（如 202601=72185 亿），旧代码 shrz/1e8 导致显示 0 亿。
        # 合理性校验：值 <= 0 或 NaN → None（宏观板块显示 数据缺失 而非 0 亿）。
        try:
            df = self._ak(self.ak.macro_china_shrzgm, )
            col = [c for c in df.columns if '社会融资规模' in str(c) and '累计' not in str(c)]
            if not col or '月份' not in df.columns:
                return None
            d = df[['月份', col[0]]].copy().dropna()
            if d.empty:
                return None
            m = d['月份'].astype(str).str.extract(r'(\d{4})[年\-/]?(\d{1,2})')
            d['_k'] = pd.to_numeric(m[0], errors='coerce') * 100 + pd.to_numeric(m[1], errors='coerce')
            d = d.dropna(subset=['_k']).sort_values('_k')
            if d.empty:
                return None
            v = float(d[col[0]].iloc[-1])
            if pd_isna(v) or v <= 0:
                return None
            mm = re.search(r'(\d{4})[年\-/]?(\d{1,2})', str(d['月份'].iloc[-1]))
            month = f'{int(mm.group(1)):04d}-{int(mm.group(2)):02d}' if mm else ''
            return {'value': v, 'month': month}
        except Exception:
            return None


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
            r = fn()
            _mark_fetch(name, True)
            return r
        except Exception as e:
            _mark_fetch(name, False, f"{type(e).__name__}")
            if isinstance(e, TimeoutError):
                # 接口永久挂死（超时）：重试大概率同样挂死，直接降级，不浪费 3×90s
                log(f"[WARN] {name} 超时({str(e)[:60]})，跳过重试直接降级")
                break
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


def _ret_col_local(df):
    # 复权收益列选择：优先 rules.ret_col（规则引擎统一口径，A7 契约），否则本地兜底 nav_adj→nav
    _rc = getattr(rules, "ret_col", None)
    if _rc is not None:
        try:
            return _rc(df)
        except Exception:
            pass
    if "nav_adj" in df.columns:
        return df["nav_adj"]
    return df["nav"]


def fetch_once(fn, name):
    try:
        r = fn()
        _mark_fetch(name, True)
        return r
    except Exception as e:
        _mark_fetch(name, False, f"{type(e).__name__}")
        log(f"[WARN] {name} 失败({type(e).__name__})，切换备选源")
        return None


def pd_isna(v):
    try:
        return v != v
    except Exception:
        return False


def is_stale_date(date_str, tolerance_days=FRESH_TOLERANCE_DAYS):
    """数据新鲜度校验（1.4）：最后日期距今天超过容差自然日 → 滞后。异常输入不误报"""
    try:
        d = date.fromisoformat(str(date_str)[:10])
    except Exception:
        return False
    return (date.today() - d).days > tolerance_days


TYPE_LABELS = {"bond": "债券型", "money": "现金管理", "equity": "权益型", "gold": "黄金型", "other": "理财"}
def _macro_direction(nm, val, verbose=False):
    """B4 关键宏观指标方向句（名词解释之外的一层决策指向）；无规则/异常 → ''。
    verbose=True：绑定当日数值（宏观板块行尾用）；verbose=False：纯结论（指标温度表用，不复述数值）。"""
    try:
        if nm == "PMI":
            return ("制造业扩张（站上荣枯线），周期方向偏暖" if val >= 50
                    else "制造业收缩，周期方向偏冷")
        if nm == "CPI":
            return ("物价温和，货币空间充裕" if abs(val) < 3
                    else "物价偏高，货币宽松空间受限")
        if nm == "PPI":
            return ("工业品涨价，企业利润预期改善" if val > 0
                    else "工业品通缩，企业利润承压")
        if nm == "社融":
            return f"信用扩张（社融增量{val:,.0f}亿）" if verbose else "信用扩张"
        if nm == "美债10Y":
            if val >= 4.5:
                return "高位，压制全球股票估值"
            if val < 4.0:
                return "回落，压制缓解"
            return ""
    except Exception:
        pass
    return ""


def _product_status_tag(p, pctx, tp_map, client_id):
    """B6c 产品状态标签：✅持有（含止盈观察）/ 👀观察中 / ⚠️止盈信号观察中。"""
    try:
        code = p.get("code", "")
        tp = tp_map.get(code) if isinstance(tp_map, dict) else None
        if tp:
            n = state_store.tp_streak_days(code, client=client_id)
            if tp.get("repeat"):
                return f"⚠️ **止盈信号观察中（第 {n} 天，仅记录不执行）**"
            return f"✅ **持有**（止盈信号观察 · 第 {n} 天）"
        if p.get("status") == "observe":
            return "👀 **观察中**"
        return "✅ **持有**"
    except Exception:
        return "✅ **持有**"


def _gap_reason_line(storm_active, ep_lock, cfg, total_exp, eq_target):
    """B7b 实际 vs 目标权益暴露差距说明；差距小（≤5pp）或数据缺失 → None。"""
    try:
        if eq_target is None or total_exp is None or total_exp != total_exp:
            return None
        gap = abs(total_exp - eq_target)
        if gap <= 0.05:
            return None
        if storm_active:
            reason = "买入冻结"
        elif ep_lock:
            reason = "EP 锁定"
        elif state_store.pending_cash(cfg):
            reason = "资金在途"
        else:
            return (f"- 实际权益暴露 {total_exp*100:.0f}% vs 目标 {eq_target*100:.0f}%，差 {gap*100:.0f}pp；"
                    f"差额将通过后续再平衡指令恢复")
        return (f"- 实际权益暴露 {total_exp*100:.0f}% vs 目标 {eq_target*100:.0f}%，差 {gap*100:.0f}pp；"
                f"因{reason}今日无法调整，解冻后按纪律恢复")
    except Exception:
        return None


def _bench_compare_line(returns_map, weights_map, bench_ret_series, bench_weights):
    """B8b 近1月/近3月组合 vs 基准累计收益（按当前权重近似）。
    组合日收益 = Σ(权重×产品日收益)，与加权基准按日期 inner 对齐；
    数据不足/异常 → None（静默省略该行，绝不阻塞）。"""
    try:
        if not returns_map or not weights_map or not bench_ret_series or not bench_weights:
            return None
        parts = []
        for code, r in returns_map.items():
            w = weights_map.get(code)
            if not w or w <= 0:
                continue
            parts.append(pd.Series(r) * float(w))
        if not parts:
            return None
        port = pd.concat(parts, axis=1).ffill().fillna(0.0).sum(axis=1)
        bench = None
        for bname, s in bench_ret_series.items():
            w = bench_weights.get(bname)
            if not w:
                continue
            part = pd.Series(s) * float(w)
            bench = part if bench is None else bench.add(part, fill_value=0.0)
        if bench is None:
            return None
        m = pd.merge(port.to_frame("p"), bench.to_frame("b"),
                     left_index=True, right_index=True, how="inner").dropna()
        if len(m) < 70:
            return None

        def cum(n):
            if len(m) <= n:
                return None
            return (float((1.0 + m["p"].iloc[-n:]).prod() - 1.0),
                    float((1.0 + m["b"].iloc[-n:]).prod() - 1.0))
        c21, c63 = cum(21), cum(63)
        if c21 is None or c63 is None:
            return None
        return (f"- 近1月组合 {c21[0]*100:+.2f}% vs 基准 {c21[1]*100:+.2f}%；"
                f"近3月组合 {c63[0]*100:+.2f}% vs 基准 {c63[1]*100:+.2f}%（按当前权重近似）")
    except Exception:
        return None


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


def analyze_product(fetcher, p, cfg_ref, bench_pctile_map, nav_cache=None):
    """单个产品分析：返回 (report_lines, ctx_dict)。失败不阻塞。
    nav_cache：跨客户共享净值缓存 {fund_code: DataFrame}，同一产品只拉一次。"""
    code = p.get("code", "?")
    name = p.get("name") or ""
    ptype = p.get("type", "other")
    fcode = p.get("fund_code", "")
    ctx = {"code": code, "name": name, "type": ptype,
           "platform": p.get("platform", ""), "notes": p.get("notes", ""),
           "status": p.get("status", "held"),
           "manager_since": p.get("manager_since"),
           "purchase_status": None, "purchase_meta": None}
    if not fcode:
        return ([f"- {code} {name}: 银行理财/券商产品，自动数据不可用（需人工维护）"],
                {**ctx, "unavailable": True})

    nav = None
    if nav_cache is not None:
        nav = nav_cache.get(fcode)
    if nav is None:
        nav = fetch_section(lambda: fetcher.fund_nav_history(fcode), f"净值{fcode}")
        if nav_cache is not None and nav is not None:
            nav_cache[fcode] = nav
    achievement = fetch_section(lambda: fetcher.fund_achievement(fcode), f"业绩{fcode}") or []
    profile = fetch_section(lambda: fetcher.fund_profile(fcode), f'资料{fcode}') or {}
    top10 = fetch_section(lambda: fetcher.fund_top10(fcode), f'重仓{fcode}')
    # ---- 迭代六：持有人结构/规模历史/债基杠杆/换手率/A-C配对（失败静默 None，绝不阻塞） ----
    ctx['holder'] = fetch_section(lambda: fetcher.holder_structure(fcode), f'持有人{fcode}')
    ctx['scale_hist'] = fetch_section(lambda: fetcher.scale_history(fcode), f'规模{fcode}')
    ctx['leverage'] = fetch_section(lambda: fetcher.bond_leverage(fcode), f'杠杆{fcode}')
    ctx['turnover'] = fetch_section(lambda: fetcher.turnover(fcode), f'换手{fcode}')
    ctx['partner'] = fetch_section(lambda: fetcher.ac_partner_info(fcode), f'配对{fcode}')

    if nav is None or nav.empty or len(nav) < 30:
        return ([f"- {code} {name}: 净值数据不可用"], {**ctx, "unavailable": True})

    n = _ret_col_local(nav)  # 复权口径（nav_adj，A7）：区间收益/最大回撤均按分红再投资口径
    def rb(days):
        return (n.iloc[-1] / n.iloc[-1 - days] - 1) if len(nav) > days else None
    r1w, r1m, r3m, r6m, r1y = rb(5), rb(21), rb(63), rb(126), rb(250)
    max_dd = float((n / n.cummax() - 1).min())
    latest = nav.iloc[-1]
    nav_date = str(latest['date'])[:10]
    stale = is_stale_date(nav_date)
    nav_str = f"{float(latest['nav']):.4f}（{safe_format(float(latest['growth']) if not pd_isna(latest['growth']) else None, '{:+.2f}')}，净值日期 {nav_date}）"
    if stale:
        nav_str += " ⚠️数据滞后"

    ps_map = fetch_once(lambda: fetcher.purchase_status_map(), "申赎状态") or {}
    if fcode in ps_map:
        ctx["purchase_status"] = ps_map[fcode].get("purchase")
        ctx["purchase_meta"] = ps_map[fcode]
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
            if ptype == "equity" and stock_pos < 0.3:
                # ETF 联接/指数基金的股票仓位披露不全（常解析出 0~2%），equity 类兜底 0.8
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

    line1 = f"**{code} {fname}**（{type_label}）"
    if p.get("status") == "observe":
        line1 += " ｜ **零持仓·仅观察**（持续跟踪，逢机提醒买入）"
    line2 = f"- 净值 {nav_str}"
    line3 = "- 区间收益：" + " ｜ ".join(x for x in [
        f"近1周 {pct(r1w)}", f"近1月 {pct(r1m)}", f"近3月 {pct(r3m)}",
        f"近6月 {pct(r6m)}", f"近1年 {pct(r1y)}"])
    line9 = f"- 近1年最大回撤 {max_dd*100:.1f}%"
    line10 = "- " + "，".join(x for x in [f"同类排名 {ranking}" if ranking else "",
                                           f"规模 {scale}" if scale else "",
                                           f"成立 {inception}" if inception else "",
                                           f"经理 {manager}" if manager else ""] if x)
    line11 = "- " + "，".join(x for x in [f"费用 {fees_str}" if fees_str else "",
                                           f"平台 {p.get('platform','')}" if p.get("platform") else "",
                                           f"备注 {p.get('notes','')}" if p.get("notes") else ""] if x)
    line12 = f"- {exp_str}"
    if position_date:
        line12 += f" ｜ 仓位数据截至季报 {position_date}（可能滞后1-3个月）"
    if ptype == "bond" and equity_exposure is not None and equity_exposure >= 0.2:
        line12 += f"\n- ⚠️ 实际风险高于债券型常规水平（权益暴露 {equity_exposure*100:.1f}%），请按混合型产品看待"
    if r1y is not None and r3m is not None and r1y * r3m < 0 and abs(r3m) >= 0.05:
        _w = "转弱" if r1y > 0 else "转强"
        line12 += f"\n- ⚠️ 近3月走势与近1年趋势背离（近1年 {r1y*100:+.1f}%，近3月 {r3m*100:+.1f}%），近期动能{_w}"
    # B6c：状态标签行由 run_client 在止盈判定后统一生成（原「持有观察」硬编码已移除）
    ctx.update({
        "unavailable": False, "name": fname,
        "nav_latest": nav_str,
        "returns": f"近1周{pct(r1w)} 近1月{pct(r1m)} 近3月{pct(r3m)} 近6月{pct(r6m)} 近1年{pct(r1y)}",
        "max_dd": f"{max_dd*100:.1f}%",
        "nav_stale": stale,
        "ranking": ranking or "无",
        "scale": scale, "inception": inception, "manager": manager,
        "fees": fees_str or "无",
        "equity_exposure": equity_exposure,
        "bench_pctile": bench_pctile_map.get(code),
        "r3m": r3m,
        "hhi": hhi,
        "top10": top10,
        "nav_series": nav_series,
        "action": "hold",
    })
    return ([line1, line2, line3, line9, line10, line11, line12], ctx)


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


def build_compact(blocks, page_url, date_str, has_products=False):
    """精简版推送：只列「用户关注的产品 + 今日操作建议」两板块。
    空用户（无持仓且无关注产品）→ 正文空白，仅剩详情网址。
    推送字节上限保护（1.3）：超过 3700 字节时从产品板块尾部裁剪。"""
    want = ["理财产品跟踪", "今日跟投指令"]
    CORE = ["今日跟投指令"]
    MAX_BYTES = 3700

    if not has_products or not blocks.get("理财产品跟踪", "").strip():
        return ("# 每日投顾报告（精简版）" + date_str + "\n\n"
                "📄 完整版报告: " + page_url + "\n"
                "*本报告由本地程序按固定规则自动生成，仅供参考，不构成投资建议。*")

    def assemble(selected):
        o = [f"# 每日投顾报告（精简版）{date_str}", ""]
        for w in selected:
            if w in blocks and blocks[w].strip():
                o.append(f"## {w}")
                o.append(blocks[w].strip())
                o.append("")
        o.append("---")
        o.append("")
        o.append(f"📄 完整版报告（含全部数据明细）: {page_url}")
        o.append("*本报告由本地程序按固定规则自动生成，仅供参考，不构成投资建议。*")
        return "\n".join(o)

    selected = [w for w in want if w in blocks and blocks[w].strip()]
    compact = assemble(selected)
    # 超长裁剪：先整体去掉产品板块细节行（仅保留「规则信号」状态行），仍超则裁掉产品板块
    while len(compact.encode("utf-8")) > MAX_BYTES:
        prod = blocks.get("理财产品跟踪", "")
        kept = [ln for ln in prod.splitlines() if ln.startswith("- 规则信号") or ln.startswith("**")]
        if kept and kept != prod.splitlines():
            blocks["理财产品跟踪"] = "\n".join(kept)
            compact = assemble(selected)
            continue
        if "理财产品跟踪" in selected:
            selected = [w for w in selected if w != "理财产品跟踪"]
            compact = assemble(selected)
            continue
        break
    if len(compact.encode("utf-8")) > MAX_BYTES:
        compact += "\n\n⚠️ 推送内容超长，已自动裁剪，完整报告请查看 GitHub Pages"
    return compact


_ARGS = None


def build_chart_data(ctx, mv_map, returns_map, weights_map, bench_ret_series, total_mv, bench_weights=None):
    """6.1 HTML 图表数据：估值条形图 / 仓位饼图 / 组合净值 vs 基准 / 区间收益 / 宏观雷达 / EP 仪表盘
    （数据不足返回空 dict）"""
    out = {}
    pe = {}
    for key, label in (("csi300_pe_pctile", "沪深300"),
                       ("csi500_pe_pctile", "中证500"),
                       ("csi_cyb_pe_pctile", "创业板50")):
        v = ctx.get(key)
        if v is not None:
            pe[label] = round(v * 100, 1)
    if pe:
        out["valuation"] = {"labels": list(pe), "values": list(pe.values())}
    if mv_map:
        alloc = {}
        for code, mv in mv_map.items():
            if mv is None:
                continue
            if code == "cash":
                alloc["现金"] = round(float(mv), 0)
            else:
                alloc[str(code)] = round(float(mv), 0)
        if alloc and total_mv > 0:
            out["allocation"] = {"labels": list(alloc), "values": list(alloc.values())}
    if returns_map and weights_map and total_mv > 0:
        frames = []
        for code, r in returns_map.items():
            if code in weights_map and weights_map.get(code):
                frames.append(pd.Series(r) * float(weights_map[code]))
        if frames:
            port = pd.concat(frames, axis=1).ffill().fillna(0.0).sum(axis=1)
            port = port[port.abs() < 0.5]
            if len(port) > 30:
                cum = (1.0 + port).cumprod() * 100.0
                bb = None
                if bench_ret_series:
                    for bname, s in bench_ret_series.items():
                        w = bench_weights.get(bname) if bench_weights else None
                        if not w:
                            continue
                        part = pd.Series(s).reindex(port.index).ffill().fillna(0.0) * float(w)
                        bb = part if bb is None else bb + part
                bb_cum = ((1.0 + bb).cumprod() * 100.0) if bb is not None else None
                # 降采样至 ≤80 点（保留首尾 + 等距抽样，页面体积与渲染性能）
                MAX_NAV_POINTS = 80
                if len(cum) > MAX_NAV_POINTS:
                    step = len(cum) / (MAX_NAV_POINTS - 2)
                    indices = [0] + [min(len(cum) - 1, int(i * step))
                                     for i in range(1, MAX_NAV_POINTS - 1)] + [len(cum) - 1]
                    indices = sorted(set(indices))
                    cum = cum.iloc[indices]
                    if bb_cum is not None:
                        bb_cum = bb_cum.iloc[indices]
                nav = {"labels": [str(d)[:10] for d in cum.index],
                       "values": [round(v, 1) for v in cum.values]}
                if bb_cum is not None:
                    nav["bench"] = [round(v, 1) for v in bb_cum.values]
                out["nav"] = nav
    # 产品区间收益横向对比（近1周/近1月/近3月）
    if returns_map:
        ret_labels, ret_1w, ret_1m, ret_3m = [], [], [], []
        for code, r in returns_map.items():
            if len(r) < 66:
                continue
            s = pd.Series(r).dropna()
            if len(s) < 66:
                continue
            ret_labels.append(str(code))
            ret_1w.append(round(float((1 + s.iloc[-5:]).prod() - 1) * 100, 1))
            ret_1m.append(round(float((1 + s.iloc[-22:]).prod() - 1) * 100, 1))
            ret_3m.append(round(float((1 + s.iloc[-66:]).prod() - 1) * 100, 1))
        if ret_labels:
            out["product_returns"] = {"labels": ret_labels, "datasets": [
                {"label": "近1周", "data": ret_1w},
                {"label": "近1月", "data": ret_1m},
                {"label": "近3月", "data": ret_3m}]}
    # 宏观因子雷达（归一化 0-100）
    mraw = ctx.get("macro_raw") or {}
    macro = {}
    pmi = mraw.get("PMI")
    if pmi is not None:
        macro["PMI景气"] = round(max(0, min(100, (float(pmi) - 45) * 10)), 1)
    cpi = mraw.get("CPI")
    if cpi is not None:
        macro["CPI通胀"] = round(max(0, min(100, float(cpi) * 20 + 50)), 1)
    ppi = mraw.get("PPI")
    if ppi is not None:
        macro["PPI工业"] = round(max(0, min(100, float(ppi) * 10 + 50)), 1)
    if ctx.get("y10_pctile") is not None:
        macro["利率水平"] = round(float(ctx["y10_pctile"]) * 100, 1)
    if ctx.get("ep_premium_pctile") is not None:
        macro["股债性价比"] = round(float(ctx["ep_premium_pctile"]) * 100, 1)
    if len(macro) >= 3:
        out["macro_radar"] = {"labels": list(macro), "values": list(macro.values())}
    # 股债性价比仪表盘（半环）
    if ctx.get("ep_premium_pctile") is not None:
        out["ep_gauge"] = {"value": round(float(ctx["ep_premium_pctile"]) * 100, 1),
                           "label": "股债性价比分位"}
    return out


def main():
    """顶层兜底（2.3）：任何未捕获异常 → 输出降级报告并尝试推送，绝不静默崩溃"""
    try:
        _main()
    except Exception as e:
        log(f"[ERROR] 主流程异常: {type(e).__name__}: {str(e)[:200]}")
        log(f"[ERROR] {traceback.format_exc()}")
        try:
            cfg = load_config()
            today = date.today().isoformat()
            degraded = (f"# 每日投顾报告 {today}（降级版）\n\n"
                        f"## 今日一句话\n> 今日系统数据处理出现异常，请以 App 持仓与行情为准，暂不执行任何操作。\n\n"
                        f"## 今日跟投指令\n- 异常日纪律：维持当前持仓，不做任何买卖；明日报告恢复后再评估。\n\n"
                        f"## 执行回执\n- 执行后请告知：已执行 / 部分 / 未执行（用于记录下次冷却期与在途资金）\n\n"
                        f"---\n*本报告由本地程序按固定规则自动生成，仅供参考，不构成投资建议。*")
            os.makedirs(REPORT_DIR, exist_ok=True)
            md_path = os.path.join(REPORT_DIR, f"report_{today}.md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(degraded)
            log(f"[WARN] 已输出降级报告: {md_path}")
            if _ARGS is None or not _ARGS.push_off:
                channel = cfg.get("push", {}).get("channel", "none")
                if channel == "wecom" and cfg.get("push", {}).get("wecom_webhook"):
                    push_wecom(cfg["push"]["wecom_webhook"], f"投顾报告异常 {today}", degraded)
                elif channel == "serverchan" and cfg.get("push", {}).get("serverchan_key"):
                    push_serverchan(cfg["push"]["serverchan_key"], f"投顾报告异常 {today}", degraded)
        except Exception as e2:
            log(f"[ERROR] 降级报告也失败: {type(e2).__name__}: {str(e2)[:100]}")


def args_parse():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push-off", action="store_true", help="不推送，仅生成报告")
    return ap


def _main():
    global _ARGS
    _ARGS = args_parse().parse_args()
    args = _ARGS

    cfg = load_config()

    if not is_trading_day():
        log("今天非交易日，跳过报告")
        return

    fetcher = DataFetcher()
    today = date.today().isoformat()
    lines = []
    L = lines.append
    ctx = {"news": []}
    bench_weights = None

    # ---------- 1. 指数速览（完整版明细 + 策略输入） ----------
    indexes = [
        ('沪深300', 'sh000300'), ('中证500', 'sh000905'),
        ('上证红利', 'sh000015'), ('创业板指', 'sz399006'),
    ]
    pe_indexes = ["沪深300", "中证500", "上证红利", "创业板指"]
    PE_SYMBOL = {"沪深300": "沪深300", "中证500": "中证500", "上证红利": "上证红利", "创业板指": "创业板50"}
    L(f"## 市场速览")
    idx_ctx = {}
    index_5d_trend = {}
    bench_ret_series = {}
    stale_indexes = []
    for name, tx in indexes:
        df = fetch_section(lambda t=tx: fetcher.index_daily(t), name)
        if df is None or df.empty:
            L(f"- {name}: 数据获取失败")
            continue
        last = df.iloc[-1]
        close = last["close"]
        pct = last["pct"] if last["pct"] == last["pct"] else 0.0
        emoji = "🔴" if pct >= 0 else "🟢"
        idx_date = str(last['date'])[:10]
        line = f'- {name}: {close:.2f} {emoji}{pct:+.2f}%'
        if is_stale_date(idx_date, tolerance_days=2):
            stale_indexes.append(name)
            line += f'（数据截至 {idx_date} ⚠️）'
        # 成交额（腾讯实时，展示性字段；失败静默降级，绝不阻塞）
        rq = fetch_once(lambda t=tx: fetcher.realtime_quotes([t]), f'{name}实时')
        if rq:
            q = rq.get(tx[2:]) or next(iter(rq.values()), None)
            if q:
                try:
                    amt_wan = float(q.get('amount') or 0.0)
                except (TypeError, ValueError):
                    amt_wan = 0.0
                if amt_wan > 0:
                    line += f'（成交额 {amt_wan / 10000:,.0f}亿）'
        idx_ctx[name] = f'{close:.2f}（{pct:+.2f}%）'
        if len(df) > 6:
            d5 = float(close) / float(df["close"].iloc[-6]) - 1
            index_5d_trend[name] = f"{d5*100:+.1f}%"
            if name == "沪深300":
                bench_ret_series["沪深300"] = (
                    df.set_index(pd.to_datetime(df["date"]))["close"].pct_change().dropna())
        if name not in pe_indexes:
            L(line)
            continue
        pe = fetch_section(lambda s=PE_SYMBOL[name]: fetcher.pe_history(s), f"{PE_SYMBOL[name]}PE")
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
                elif name == "创业板指":
                    ctx["csi_cyb_pe_pctile"] = float(r)
                    ctx["csi_cyb_mom20"] = float(m20)
                pe_tag = "偏高" if r > 0.7 else ("中性" if r >= 0.3 else "低估")
                if abs(m20) < 0.0005:
                    mom_txt = f"20日动量 {m20:+.1%}（近20天累计持平）"
                else:
                    _dir = "上涨" if m20 > 0 else "下跌"
                    mom_txt = f"20日动量 {m20:+.1%}（近20天累计{_dir}约{abs(m20)*100:.1f}%）"
                line += f" ｜ PE 分位 {r*100:.0f}%（{pe_tag}），{mom_txt}"
        L(line)
    ctx["index_5d_trend"] = index_5d_trend
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
        pe = fetch_section(lambda s=PE_SYMBOL[name]: fetcher.pe_history(s), f"{PE_SYMBOL[name]}PE")
        if pe is None or pe.empty:
            val_lines.append(f"- {name}: 无数据")
            continue
        last_pe = pe.iloc[-1]["pe"]
        r = pct_rank(pe["pe"], last_pe)
        if r is None or pd_isna(last_pe):
            val_lines.append(f"- {name}: 无数据")
            continue
        tag = "低估" if r < 0.3 else ("合理" if r <= 0.7 else "偏高")
        pe_full = fetch_section(lambda s=PE_SYMBOL[name]: fetcher.pe_history_full(s), f"{PE_SYMBOL[name]}PE全史")
        sens = ""
        if pe_full is not None and not pe_full.empty:
            r_full = pct_rank(pe_full["pe"], last_pe)
            if r_full is not None:
                sens = f"（全历史分位 {r_full*100:.0f}%）"
        label = f"{name}*" if name == "创业板指" else name
        val_lines.append(f"- {label}: PE {last_pe:.2f}，近{PE_HIST_YEARS}年分位 {r*100:.0f}%（{tag}）{sens}")
    if "创业板指" in pe_indexes:
        val_lines.append("*创业板指以创业板50成分股PE为代理（乐咕口径）")
    mpe = fetch_section(fetcher.market_pe, "全市场PE")
    if mpe is not None and not mpe.empty:
        val_lines.append(f"- 全市场(上证): PE {mpe.iloc[-1]['平均市盈率']:.2f}")
    val_lines.append("- 注：近10年分位为决策主依据（更能反映当前市场环境）；全历史分位供参考，两者差异源于历史估值中枢变化。")
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
        ctx["y10_pctile"] = r
        if r < 0.3:
            bond_sig = -1.0
            L(f"- 债券研判: 利率处低位、债价偏贵；战略方向以「今日跟投指令」为准")
        elif r > 0.7:
            bond_sig = 1.0
            L(f"- 债券研判: 利率处高位、债价便宜；战略方向以「今日跟投指令」为准")
        else:
            bond_sig = 0.0
            L(f"- 债券研判: 中性")
        # B3：债市 → 持仓衔接句（利率分位 → 对你组合的含义）
        if bond_sig < 0:
            L(f"  → 对你组合的含义：利率分位 {r*100:.0f}%，债价偏贵，债券仓位维持目标、暂不追加（具体指令见「今日跟投指令」）")
        elif bond_sig > 0:
            L(f"  → 对你组合的含义：利率分位 {r*100:.0f}%，债价便宜，债券仓位可维持或视指令调整（具体指令见「今日跟投指令」）")
        else:
            L(f"  → 对你组合的含义：利率分位 {r*100:.0f}%，债价中性，债券仓位维持目标（具体指令见「今日跟投指令」）")
    lpr_row = fetch_section(fetcher.lpr, "LPR")
    if lpr_row is not None:
        L(f"- LPR: 1年期 {lpr_row['LPR1Y']:.2f}%，5年期 {lpr_row['LPR5Y']:.2f}%（{str(lpr_row['TRADE_DATE'])[:10]}）")
        bond_ctx["LPR"] = f"1Y {lpr_row['LPR1Y']:.2f}%，5Y {lpr_row['LPR5Y']:.2f}%"
    ctx["bond"] = bond_ctx
    ctx["signal_bond"] = stance_label(bond_sig) if bond_sig is not None else "无数据"

    # ---- rf_annual 无风险利率（score_candidate 输入；三级降级链：y2 → y10-0.4 → 0.015）----
    # y2/y10 单位均为百分点，需 /100 转小数（如 y2=1.65 → 0.0165）；NaN 检查用 v == v
    rf_annual = 0.015
    if bond is not None and not bond.empty:
        try:
            last = bond.iloc[-1]
            y2v = float(last["y2"]) if ("y2" in last.index and last["y2"] == last["y2"]) else float("nan")
            y10v = float(last["y10"]) if ("y10" in last.index and last["y10"] == last["y10"]) else float("nan")
            if y2v == y2v:
                rf_annual = y2v / 100.0
                log("[INFO] rf_annual 用 2Y: " + str(round(rf_annual, 4)))
            elif y10v == y10v:
                rf_annual = (y10v - 0.4) / 100.0
                log("[INFO] rf_annual 用 10Y-0.4: " + str(round(rf_annual, 4)))
            else:
                log("[INFO] rf_annual 无 y2/y10，用默认 0.015")
        except Exception as e:
            log("[WARN] rf_annual 计算失败(" + str(e) + ")，用默认 0.015")
            rf_annual = 0.015
    ctx["rf_annual"] = rf_annual

    # 中证全债指数（组合基准对比用；失败不阻塞，基准字段降级为 None）
    bdf = fetch_once(lambda: fetcher.index_daily("sh000923"), "中证全债")
    if bdf is not None and not bdf.empty and len(bdf) > 60:
        bench_ret_series["中证全债"] = (
            bdf.set_index(pd.to_datetime(bdf["date"]))["close"].pct_change().dropna())

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
    macro_raw = {}
    us10 = fetch_section(fetcher.us_bond_10y, "美债10Y")
    if us10 is not None:
        ext.append(("美债10Y", f"{us10:.2f}%"))
        macro_ctx["美债10Y"] = f"{us10:.2f}%"
        macro_raw["美债10Y"] = float(us10)
    oil = fetch_section(fetcher.crude_oil, "SC原油")
    if oil is not None:
        ext.append(("SC原油", f"{oil[0]:,.0f}（{safe_format(oil[1], '{:+.2f}')}）"))
        macro_ctx["SC原油"] = f"{oil[0]:,.0f}"
    copper = fetch_section(fetcher.copper, "沪铜")
    if copper is not None:
        ext.append(("沪铜", f"{copper[0]:,.0f}（{safe_format(copper[1], '{:+.2f}')}）"))
        macro_ctx["沪铜"] = f"{copper[0]:,.0f}"
    cpi_v = fetch_section(fetcher.cpi, 'CPI')
    if cpi_v is not None:
        cpi_val, cpi_period = cpi_v
        cpi_s = f'{cpi_val:+.1f}%'
        if cpi_period:
            cpi_s += f'（统计期 {cpi_period}）'
        ext.append(('CPI', cpi_s))
        macro_ctx['CPI'] = cpi_s
        macro_raw['CPI'] = float(cpi_val)
    else:
        ext.append(('CPI', '⚠️ 数据异常或缺失'))
        macro_ctx['CPI'] = '⚠️ 数据异常或缺失'
    ppi_v = fetch_section(fetcher.ppi, 'PPI')
    if ppi_v is not None:
        ppi_val, ppi_period = ppi_v
        ppi_s = f'{ppi_val:+.1f}%'
        if ppi_period:
            ppi_s += f'（统计期 {ppi_period}）'
        ext.append(('PPI', ppi_s))
        macro_ctx['PPI'] = ppi_s
        macro_raw['PPI'] = float(ppi_val)
    else:
        ext.append(('PPI', '⚠️ 数据异常或缺失'))
        macro_ctx['PPI'] = '⚠️ 数据异常或缺失'
    pmi_v = fetch_section(fetcher.pmi, 'PMI')
    if pmi_v is not None:
        ext.append(('PMI', f'{pmi_v:.1f}'))
        macro_ctx['PMI'] = f'{pmi_v:.1f}'
        macro_raw['PMI'] = float(pmi_v)
    shrz = fetch_section(fetcher.shrzgm, '社融')
    if shrz is not None and shrz.get('value'):
        shrz_val = shrz['value']
        shrz_s = f'{shrz_val / 10000:.2f}万亿' if shrz_val >= 10000 else f'{shrz_val:,.0f}亿'
        shrz_month = shrz.get('month')
        if shrz_month:
            shrz_s += f'（统计期 {shrz_month}）'
        ext.append(('社融', shrz_s))
        macro_ctx['社融'] = shrz_s
        macro_raw['社融'] = float(shrz_val)
    else:
        ext.append(('社融', '⚠️ 数据缺失（月度数据，可能未更新）'))
        macro_ctx['社融'] = '⚠️ 数据缺失（月度数据，可能未更新）'
    macro_dirs = {}
    for nm, val in macro_raw.items():
        d = _macro_direction(nm, val)
        if d:
            macro_dirs[nm] = d
    for nm, v in ext:
        note = MACRO_NOTES.get(nm, "")
        d = _macro_direction(nm, macro_raw.get(nm), verbose=True)
        tail = " ｜ ".join(x for x in (note, d) if x)
        L(f"- {nm}: {v}" + (f" ｜ {tail}" if tail else ""))
    ctx["macro"] = macro_ctx
    ctx["macro_raw"] = macro_raw

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
        if abs(m20) < 0.0005:
            mom_txt = f"20日动量 {m20*100:+.1f}%（近20天累计持平）"
        else:
            _dir = "上涨" if m20 > 0 else "下跌"
            mom_txt = f"20日动量 {m20*100:+.1f}%（近20天累计{_dir}约{abs(m20)*100:.1f}%）"
        L(f"- Au99.99: ¥{last:.2f}，当日 {safe_format(d1, '{:+.2f}')}，{mom_txt}，趋势{trend}")
        gold_ctx["Au99.99"] = f"¥{last:.2f}，趋势{trend}"
        gold_sig = 0.5 if last > ma120 else -0.5
        L(f"- 黄金研判: {'趋势向上，可持有' if trend == '多头' else '趋势向下，暂不加仓'}")
    else:
        L(f"- 无数据")
    # B5：目标仓位口径说明（黄金仅作资产配置环境参考，不单独配置）
    L("- 注：本系统目标仓位仅含权益/固收/现金，暂不单独配置黄金；黄金行情仅作资产配置环境参考。")
    ctx["gold"] = gold_ctx
    ctx["signal_gold"] = stance_label(gold_sig) if gold_sig is not None else "无数据"

    # ---------- 指标温度表（客户看板：结论化，每行一个指标一行结论，≤10行） ----------
    temp_lines = []
    if ctx.get("csi300_pe_pctile") is not None:
        v = ctx["csi300_pe_pctile"]
        tag = "偏高" if v > 0.7 else ("中性" if v >= 0.3 else "低估")
        concl = "买股票容易买贵" if v > 0.7 else ("估值适中" if v >= 0.3 else "比过去多数时间便宜")
        temp_lines.append(f"- 沪深300估值：分位 {v*100:.0f}%（{tag}）→ {concl}")
    pe_mid = []
    for _nm, _key in (("中证500", "csi500_pe_pctile"), ("创业板", "csi_cyb_pe_pctile")):
        v = ctx.get(_key)
        if v is not None:
            _tag = "偏高" if v > 0.7 else ("中性" if v >= 0.3 else "低估")
            pe_mid.append(f"{_nm} {v*100:.0f}%（{_tag}）")
    if pe_mid:
        temp_lines.append(f"- 中证500/创业板估值：" + " ｜ ".join(pe_mid))
    if ep_ctx:
        epc = ep_ctx["pctile"]
        concl = "买股票多赚的差价已经很薄，性价比不高" if epc < 0.3 else "股票相对债券的性价比正常"
        temp_lines.append(f"- 股债性价比：分位 {epc*100:.0f}% → {concl}")
    if bond is not None and not bond.empty and not pd_isna(y10):
        concl = "利率很低，债价偏贵" if r < 0.3 else ("利率较高，债价便宜" if r > 0.7 else "利率中性")
        temp_lines.append(f"- 10年国债：分位 {r*100:.0f}% → {concl}")
    if gold is not None and not gold.empty:
        temp_lines.append(f"- 黄金：{trend}趋势 → {'趋势向上，可持有' if trend == '多头' else '趋势向下，暂不加仓'}")
    for nm in ("PMI", "CPI", "PPI", "社融", "美债10Y"):
        d = macro_dirs.get(nm)
        if d:
            temp_lines.append(f"- {nm}：{d}")

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

    # ---------- 6.5 多客户循环：市场数据共享，以下按客户独立执行 ----------
    mkt_lines = lines
    mkt_ctx = dict(ctx)
    clients = get_clients(cfg)
    if not clients:
        log("[WARN] 配置中无任何客户，跳过")
        return
    nav_cache = {}
    page_links = []
    for cl in clients:
        try:
            report_full, title = run_client(
                fetcher, cl, today, mkt_lines, mkt_ctx,
                bench_ret_series, temp_lines, stale_indexes, news_ctx,
                nav_cache, args.push_off)
            page_links.append({"id": cl["id"], "name": cl["display_name"], "title": title})
            log(f"[CLIENT] {cl['id']} 报告完成")
        except Exception as e:
            log(f"[ERROR] 客户 {cl['id']} 报告失败: {str(e)[:200]}")
            log(traceback.format_exc())
    try:
        write_client_index(page_links)
    except Exception as e:
        log(f"[WARN] 客户索引页生成失败: {str(e)[:80]}")


def page_url_for(client_id):
    return f"https://Evanlei2025.github.io/market-advisor/{client_id}/latest.html"


def write_client_index(page_links):
    """reports/index.html：客户报告入口页（GitHub Pages 首页，每客户最新报告链接）"""
    md = ["# 投顾报告 · 客户入口", ""]
    for p in page_links:
        md.append(f"- [{p['name']}]({p['id']}/latest.html) — {p['title']}")
    md.append("")
    md.append("*各客户报告每日自动生成，点击客户名查看。*")
    os.makedirs(REPORT_DIR, exist_ok=True)
    page = html_render.render("\n".join(md), "投顾报告客户入口")
    with open(os.path.join(REPORT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def run_client(fetcher, cl, today, mkt_lines, mkt_ctx, bench_ret_series,
               temp_lines, stale_indexes, news_ctx, nav_cache, push_off):
    """单个客户完整流程（市场数据已共享）：产品分析 → 组合指令 → 叙事 →
    新闻哨兵 → AI 解读 → 报告输出 → 推送 → 状态快照。"""
    cfg = cl["cfg"]
    client_id = cl["id"]
    client_disp = cl["display_name"]
    lines = list(mkt_lines)
    L = lines.append
    ctx = dict(mkt_ctx)
    ctx["client_id"] = client_id
    ctx["client_display"] = client_disp
    bench_weights = None
    eq_target = None
    storm_active = False
    storm_reasons = []
    ep_lock = False
    cooldown_active = False
    orders = []
    target_alloc = {}
    summary_lines = []
    tp_actions = {}
    tp_ctx_map = {}
    cro = None
    alerts = None
    recap_line = None
    product_ctxs = []
    mv_map = {}
    returns_map = {}
    weights_map = {}
    total_mv = 0
    cash_mv = 0
    ep_thr = 0.10

    if hasattr(rules, "validate_config"):
        try:
            vc = rules.validate_config(cfg)
            if vc:
                log(f"[WARN] 客户 {client_id} 配置校验: {vc}")
        except Exception as e:
            log(f"[WARN] 客户 {client_id} 配置校验失败: {str(e)[:120]}")

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
    product_plines = []
    if products:
        # B6c：先收集每产品的行组（不进 lines），止盈判定完成后统一渲染（状态标签需要 tp_ctx_map）
        for p in products:
            plines, pctx = analyze_product(fetcher, p, cfg, per_code_bench, nav_cache)
            product_plines.append((p, plines, pctx))
            product_ctxs.append(pctx)
            try:
                mname = pctx.get("manager")
                if mname:
                    state_store.record_manager_snapshot(p.get("code", ""), str(mname).strip(), client=client_id)
            except Exception:
                pass
            nav_s = pctx.get("nav_series")
            if nav_s is not None and len(nav_s) > 30:
                _rc = _ret_col_local(nav_s)
                returns_map[p["code"]] = (
                    _rc.set_axis(pd.to_datetime(nav_s["date"])).pct_change().dropna())
    ctx["products"] = product_ctxs

    def render_products(tp_map):
        """产品跟踪板块渲染（板块顺序不变：产品跟踪 → 今日跟投指令；仅渲染动作后移）"""
        if not product_plines:
            return
        L("")
        L(f"## 理财产品跟踪")
        for p, plines, pctx in product_plines:
            for ln in plines:
                L(ln)
            L(f"- 规则信号：{_product_status_tag(p, pctx, tp_map, client_id)}")

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
        render_products({})
        L("")
        L(f"## 今日跟投指令")
        L(f"- 持仓市值为 0，暂无法生成指令")
    else:
        pname = cfg.get("portfolio", {}).get("name") or client_disp
        eq_target, rule_triggers = rules.equity_target(cfg, ctx)
        storm_active, storm_reasons = rules.storm_lock(eq_target, rule_triggers)
        ep_thr = rules.ep_threshold(ctx) if hasattr(rules, "ep_threshold") else 0.10
        ep_lock = (not storm_active) and (ctx.get("ep_premium_pctile") is not None) and (ctx["ep_premium_pctile"] < ep_thr)
        cooldown_active = state_store.in_cooldown(cfg, client=client_id)

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
        # 近5交易日同档位去重（V型回落后二次穿越的防护；trace 仍全量记录供评估）
        _last_tp = state_store.recent_tp_actions(client=client_id)

        tp_ctx_map = {}
        repeat_signals = {}
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
                is_repeat = (_last_tp.get(code) == act)
                if is_repeat:
                    repeat_signals[code] = {"name": pc.get("name", code), "action": act}
                tp_ctx_map[code] = {"action": act, "detail": detail, "rid": rid,
                                    "trace": trace, "repeat": is_repeat}
                state_store.record_trace(trace, client=client_id) if trace else None

        # ---- B6c：产品跟踪板块渲染（tp_ctx_map 已就绪，生成状态标签） ----
        render_products(tp_ctx_map)
        L("")
        L(f"## 今日跟投指令")
        L(f"- {pname}（总市值 ¥{total_mv:,.0f}，跟投份数 {cfg.get('portfolio', {}).get('follow_units', 1)}）")

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
             "rf_annual": ctx.get("rf_annual"),
             "total": total_mv,
             "product_ctx": pctx_map},
            storm_active=storm_active, storm_reasons=storm_reasons, ep_lock=ep_lock,
            cooldown_active=cooldown_active, today=today)

        # ---- 昨日信号回顾（学习闭环：昨日推荐 + 昨日止盈信号；无数据不显示板块） ----
        y_recs = state_store.get_yesterday_recommendations(client=client_id)
        y_sigs = [s for s in state_store.get_recent_signals(days=2, client=client_id)
                  if str(s.get("signal_date", ""))[:10] < today]
        recap_line = None
        try:
            recap_line = cro.get_yesterday_recap_line(y_recs, y_sigs) if hasattr(cro, "get_yesterday_recap_line") else None
        except Exception:
            recap_line = None
        if recap_line:
            L("")
            L(f"## 昨日信号回顾")
            L(f"- {recap_line}")
            for y in y_recs:
                L(f"- 昨日关注：{y.get('name', '')}（{y.get('reason', '')}）｜今日对照见上方「指标温度表」")
            for ys in y_sigs:
                cur = "延续" if any(v.get("code") == ys.get("code") for v in tp_actions.values()) else "未延续"
                L(f"- 昨日止盈观察：{ys.get('code')}（{ys.get('action')}）｜今日信号：{cur}")
            try:
                y_storm = state_store.was_storm_yesterday(client=client_id)
                if y_storm is not None:
                    if y_storm and not storm_active:
                        L(f"- 昨日市场预警（买入冻结）→ 今日已解冻，恢复常规纪律判定")
                    elif y_storm and storm_active:
                        L(f"- 昨日市场预警（买入冻结）→ 今日仍未解除，继续持有现金观望")
                    elif not y_storm and storm_active:
                        L(f"- 昨日无市场预警 → 今日新触发买入冻结")
            except Exception:
                pass

        # ---- CRO 叙事 ----
        bond_codes = {p.get("code", "") for p in products if p.get("type") == "bond"}
        # 止盈信号（含持续中 repeat）：tp_ctx_map 是当日完整信号集合（新触发+持续中），
        # tp_actions 仅今日新动作（repeat 被订单簿去重剔除）——若只传 tp_actions，
        # CRO 会漏掉持续信号导致「今日一句话」误报"无规则触发"
        tp_sig_ctx = {}
        _pname_map = {p.get("code", ""): p.get("name", p.get("code", "")) for p in products}
        for _c, _v in tp_ctx_map.items():
            _a = tp_actions.get(_c)
            tp_sig_ctx[_c] = {
                "name": (_a or {}).get("name") or _pname_map.get(_c, _c),
                "action": _v.get("action"),
                "amount": (_a or {}).get("amount"),
                "streak_days": state_store.tp_streak_days(_c, client=client_id),
            }
        cro = narrative.CRO(narrative.CROInput(
            orders=orders, equity_target=eq_target, storm_active=storm_active,
            storm_reasons=storm_reasons, ep_lock=ep_lock,
            ep_pctile=ctx.get("ep_premium_pctile"),
            ep_thr=ep_thr,
            triggers=rule_triggers, product_sells=product_sells,
            tp_signals=tp_sig_ctx, cooldown_active=cooldown_active,
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
            L("**止盈信号（观察 · 仅记录不执行）**")
            for code, a in tp_actions.items():
                L(f"- {code}：建议落袋约 {a['amount']:,.0f} 元")
                L(f"  原因：{a['reason']}")
            L("*止盈目标线算法处于 6 个月观察期，信号仅供跟踪与评估，暂不生成执行指令。*")
        sstats = state_store.shadow_stats(cfg, client=client_id)
        if sstats.get("start"):
            L(f"- 观察已进行 {sstats['days']} 天（起始 {sstats['start']}，共 6 个月），累计记录 {sstats['signals']} 次信号，涉及 {sstats['products']} 个产品")
        if repeat_signals:
            for code, v in repeat_signals.items():
                n = state_store.tp_streak_days(code, client=client_id)
                L(f"- {v['name']}：止盈信号持续观察中（第 {n} 天 · 仅记录不执行），与近 5 个交易日信号一致，今日不重复展示信号明细")

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
        L(f"- 调仓后目标仓位：权益 {target_alloc.get('equity', 0)*100:.0f}% / 固收 {target_alloc.get('bond', 0)*100:.0f}% / 现金 {target_alloc.get('cash', 0)*100:.0f}%")
        for s in summary_lines:
            L(f"{s}")
        pending = state_store.pending_cash(cfg)
        if pending:
            L("**在途资金提醒**")
            for pc_ in pending:
                L(f"- {pc_.get('code')} 赎回 {pc_.get('shares', 0)} 份，预计 {str(pc_.get('settle_date',''))[:10] or '待确认'} 到账（到账前不可用）")
        cro_narr = cro.get_narrative()
        if cro_narr:
            L("")
            L(f"> {cro_narr}")

        # ---- 新闻哨兵 ----
        try:
            entity_table = news_alert.build_entity_table(fetcher, products, cfg, product_ctxs)
            alerts = news_alert.process_news(news_ctx, entity_table, orders)
        except Exception as e:
            log(f"[WARN] 新闻哨兵失败: {str(e)[:100]}")
            alerts = None
        try:
            ctx["news_hits"] = news_alert.format_news_hits(alerts)
        except Exception:
            ctx["news_hits"] = None
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
                ob.append(f"止盈信号 {code} {a['amount']:,.0f}元（观察）")
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
        diag_data = {"returns": returns_map, "weights": weights_map}
        if bench_ret_series:
            eq_base = cfg.get("target", {}).get("equity", {})
            eq_w = float(eq_base.get("base", 0.40)) if isinstance(eq_base, dict) else float(eq_base)
            bw = {}
            if "沪深300" in bench_ret_series:
                bw["沪深300"] = max(0.0, min(1.0, eq_w))
            if "中证全债" in bench_ret_series:
                bw["中证全债"] = 1.0 - sum(bw.values())
            if bw and sum(bw.values()) > 0:
                diag_data["bench_returns"] = bench_ret_series
                diag_data["bench_weights"] = bw
                bench_weights = bw
        diag = rules.portfolio_diagnostics(
            cfg, diag_data)
        if diag:
            L("")
            L(f"## 组合诊断")
            if total_mv < state_store.min_stat_mv(cfg):
                L(f"- 组合市值 ¥{total_mv:,.0f}（份额×最新净值）⚠️ 规模较小，alpha/beta/VaR 等统计指标参考意义有限，仅供参考")
            else:
                L(f"- 组合市值 ¥{total_mv:,.0f}（份额×最新净值）")
            L(f"- 年化波动率 {diag['vol']*100:.1f}%｜人话：一年里收益上蹿下跳的平均幅度")
            L(f"- 250日最大回撤 {diag['max_dd']*100:.1f}%｜人话：历史上从最高点最多跌过这么多")
            L(f"- 日度 VaR95 {diag['var95']*100:.2f}%（历史分位法）｜人话：按历史规律，一天最坏大概率不会亏超过这个数")
            if diag.get("alpha") is not None:
                bench_names = " + ".join(f"{k}{v*100:.0f}%" for k, v in diag_data.get("bench_weights", {}).items())
                L(f"- vs 基准（{bench_names}）：年化超额收益 {diag['excess_ann']*100:+.2f}%（alpha {diag['alpha']*100:+.2f}%，beta {diag['beta']:.2f}，信息比率 {diag['ir']:.2f}）｜人话：过去一年组合比基准多赚/少赚多少")
            else:
                L(f"- 基准对比：数据不足，暂缺（需≥60个对齐交易日）")
            bl = _bench_compare_line(returns_map, weights_map, bench_ret_series, bench_weights)
            if bl:
                L(bl)
            total_exp = 0.0
            for p in products:
                fcode = p.get("code", "")
                if fcode in weights_map and fcode in pctx_map:
                    exp = pctx_map[fcode].get("equity_exposure") or 0.0
                    total_exp += weights_map[fcode] * exp
            L(f"- 组合实际权益暴露度 {total_exp*100:.1f}%｜人话：你的钱里有 {total_exp*100:.0f}% 实质押在股票上")
            gap_line = _gap_reason_line(storm_active, ep_lock, cfg, total_exp, eq_target)
            if gap_line:
                L(gap_line)
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
        L(f"## 决策依据")
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
        db = []
        if ep:
            db.append("股债性价比：" + ep.get("label", ""))
        db += [f"[{t.get('id')}] {t.get('text')}" for t in rule_triggers]
        db += [f"[{v.get('rid')}] {v.get('detail')}" for v in tp_ctx_map.values()]
        ctx["decision_basis"] = "\n".join(db)

    # ---- 指标温度表（客户看板，独立板块；市场级数据，所有客户均展示） ----
    L("")
    L(f"## 指标温度表")
    L("\n".join(temp_lines) if temp_lines else "- 暂缺")
    if total_mv > 0 and cro is not None:
        L("")
        L(f"> {cro.get_separator()}")

    # ---------- 8. 执行回执区（上次反馈历史 + 待办提示） ----------
    L("")
    L("## 执行回执")
    try:
        fb = state_store.get_feedback(client_id, limit=1)
    except Exception:
        fb = []
    if fb and isinstance(fb[0], dict):
        f0 = fb[0]
        L(f"- 上次反馈：{f0.get('date', '?')} {f0.get('status', '?')}（{f0.get('note', '')}）")
    else:
        L("- 暂无执行反馈记录")
    L("- 执行后请告知：已执行 / 部分 / 未执行（用于记录下次冷却期与在途资金）")

    # ---------- 9. 术语速查 ----------
    L("")
    L("## 术语速查")
    _gl = [f"**{k}**：{v}" for k, v in narrative.TERM_GLOSSARY.items()]
    for i in range(0, len(_gl), 2):
        L("- " + " ｜ ".join(_gl[i:i+2]))

    L("")
    L(f"---")
    L(f"*本报告由本地程序按固定规则自动生成，仅供参考，不构成投资建议。*")
    L(f"*买卖请手动执行；指令基于 T-1 日净值估算，实际成交偏差通常在 ±0.3% 以内，以 App 确认值为准。*")

    report_full = "\n".join(lines)

    # ---------- 10. AI 解读层 ----------
    if eq_target is not None:
        ctx["equity_band"] = "、".join(f"{x*100:.0f}%" for x in rules.target_band(cfg, eq_target)) + f"（以规则目标 {eq_target*100:.0f}% 为中心）"
    else:
        ctx["equity_band"] = "未计算"
    ctx["equity_target"] = f"{eq_target*100:.0f}%（规则引擎基准）" if total_mv > 0 else "未计算"
    kb_parts = []
    for p in products:
        kb = state_store.kb_product(p.get("code", ""))
        if kb:
            kb_parts.append(f"[{p.get('code')}] {kb}")
    ctx["kb_summary"] = "\n".join(kb_parts) if kb_parts else "暂无档案"
    ctx["yesterday_recap"] = recap_line or None
    if tp_actions:
        ctx["tp_signal_text"] = "；".join(f"{k} 建议落袋 {v['amount']:,.0f} 元（观察）" for k, v in tp_actions.items())
    else:
        ctx["tp_signal_text"] = "今日无止盈信号"

    insights, usage_info = (llm.generate_insights(ctx) if (total_mv > 0 and products)
                            else (None, "跳过（无持仓或关注池）"))
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
        state_store.kb_append_recommendations(rec_entries, client=client_id)
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

    # ---------- 11. 今日一句话置顶（headline + 警告 + 触发链 + 锚点，headline 仅此一次） ----------
    # 头部警告（1.4/2.2）：核心数据滞后或严重缺失
    stale_codes = [pc.get("code", "") for pc in product_ctxs if pc.get("nav_stale")]
    warn_lines = []
    if not ctx.get("indexes") or ctx.get("csi300_pe_pctile") is None:
        warn_lines.append("> ⚠️ 本报告核心数据（指数行情/估值）严重缺失，请勿作为操作依据")
    elif stale_codes or stale_indexes:
        warns = []
        if stale_codes:
            warns.append(f"{'、'.join(stale_codes[:3])} 净值更新滞后")
        if stale_indexes:
            warns.append(f"{'、'.join(stale_indexes[:3])} 行情非当日")
        warn_lines.append(f"> ⚠️ 部分数据非最新（{'；'.join(warns)}），请谨慎参考")
    if total_mv > 0 and cro is not None:
        top = [f"## 今日一句话", f"> {cro.get_headline()}"]
        if warn_lines:
            top.append(warn_lines[0])
        top += cro.get_today_block()
        report_full = "\n".join(top) + "\n\n" + report_full

    # ---------- 11.5 系统运行状态（数据源成败一览，B11：失败项按类别标注影响） ----------
    CORE_STATUS_KW = ("沪深300", "中证500", "上证红利", "创业板指", "净值", "国债", "中债",
                      "黄金", "上海金", "EP", "全市场PE")
    ok_n, fail_n = 0, 0
    fail_lines = []
    for nm, st in FETCH_STATUS.items():
        if st == "ok":
            ok_n += 1
        else:
            fail_n += 1
            tag = "（核心数据，谨慎参考）" if any(k in nm for k in CORE_STATUS_KW) else "（仅明细缺失，不影响指令）"
            fail_lines.append(f"- {nm} ✗（{st}）{tag}")
    if FETCH_STATUS:
        report_full += ("\n## 系统运行状态\n"
                        f"- 本次共采集 {len(FETCH_STATUS)} 项：成功 {ok_n}，失败/降级 {fail_n}\n"
                        + ("\n".join(fail_lines) + "\n" if fail_lines else "- 全部数据源正常 ✓\n")
                        + "*核心数据失败时指令按降级数据生成，请谨慎参考；明细类失败仅影响对应板块展示。*")
    try:
        mon_line = rules.candidate_monitor_line()
        if mon_line:
            report_full += f"\n- 候选评分监控：{mon_line}"
    except Exception:
        pass

    # ---------- 12.5 状态快照（3.4：跨日持久化到 knowledge_base，随 Actions commit 回写） ----------
    try:
        state_store.write_state_snapshot({
            "date": today,
            "orders": [{"side": o.get("side"), "code": o.get("code"), "amount": o.get("amount")} for o in orders],
            "target_alloc": target_alloc,
            "pending_cash": state_store.pending_cash(cfg),
            "total_mv": total_mv,
            "tp_signals": [{"code": c, "action": v.get("action")} for c, v in tp_ctx_map.items()],
            "storm_active": storm_active,
            "eq_target": eq_target,
        }, client=client_id)
    except Exception as e:
        log(f"[WARN] 状态快照写入失败: {str(e)[:80]}")

    # ---------- 13. 输出：完整版 md + HTML + 精简版推送（每客户独立目录） ----------
    title = f"{client_disp} 每日投顾报告 {today}"
    cdir = os.path.join(REPORT_DIR, client_id)
    os.makedirs(cdir, exist_ok=True)
    md_path = os.path.join(cdir, f"report_{today}.md")
    html_path = os.path.join(cdir, f"report_{today}.html")
    latest_html = os.path.join(cdir, "latest.html")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n" + report_full)
    try:
        chart_data = build_chart_data(ctx, mv_map, returns_map, weights_map,
                                      bench_ret_series, total_mv, bench_weights)
    except Exception as e:
        chart_data = {}
        log(f"[WARN] 图表数据组装失败: {str(e)[:80]}")
    page = html_render.render(report_full, title, charts=chart_data)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(page)
    with open(latest_html, "w", encoding="utf-8") as f:
        f.write(page)
    log(f"完整版报告已生成: {md_path} / {html_path}")

    if not push_off:
        channel = cfg.get("push", {}).get("channel", "none")
        blocks = split_blocks(report_full)
        compact = build_compact(blocks, page_url_for(client_id), today, has_products=bool(products))
        if channel == "wecom":
            push_wecom(cfg["push"]["wecom_webhook"], title, compact)
        elif channel == "serverchan":
            push_serverchan(cfg["push"]["serverchan_key"], title, compact)
        else:
            log("push.channel = none，未推送（本地报告已保存）")

    return report_full, title


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
            df = aktime.call_with_timeout(ak.tool_trade_date_hist_sina, timeout=90)
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
