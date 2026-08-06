# -*- coding: utf-8 -*-
"""StateStore —— 状态与记忆层（state-memory 职责域）。
双轨制：本地自动写 state.json（最近指令/执行状态、冷却期）；云端（GitHub Actions）
无跨日磁盘，状态与在途资金以 config（Secret）人工维护为准，本模块只读兼容。
跨日持久通道：knowledge_base/ 下 JSON（云端 Actions commit 回写）——
  traces.json（止盈留痕）、recommendations.json（推荐日志）、
  state_history.json（每日状态快照：在途资金/冷却期/上次调仓日期等，最近 60 条）。
提供：在途资金表、最近指令/执行状态、冷却期判定、留痕表读写、知识库读取、
     影子模式进度统计、状态快照读写、昨日推荐/近期信号回顾。
"""
import json
import os
from datetime import date, datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "logs", "state.log")
KB_DIR = os.path.join(BASE_DIR, "knowledge_base")


def log_state(msg):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


# ---------------- 本地 state.json ----------------
def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_state(f"[WARN] state 保存失败: {e}")


def get(key, default=None):
    return load_state().get(key, default)


def set_(key, value):
    st = load_state()
    st[key] = value
    save_state(st)


# ---------------- 在途资金表 ----------------
def pending_cash(cfg):
    """在途资金：config.transactions 中 settle_date 未到（或未知）的赎回。
    返回 [{"code","name","amount_est","settle_date","apply_date","shares"}]
    amount_est 缺失时由调用方按净值估算。
    """
    out = []
    today = date.today()
    for tx in cfg.get("transactions", []) or []:
        if tx.get("side") != "赎回":
            continue
        sd = str(tx.get("settle_date", ""))[:10]
        if not sd:
            out.append(tx)
            continue
        try:
            if date.fromisoformat(sd) > today:
                out.append(tx)
        except Exception:
            out.append(tx)
    return out


def settled_cash(cfg):
    """已到账（settle_date <= 今日）的赎回合计（供买入余额计算）。"""
    total = 0.0
    today = date.today()
    for tx in cfg.get("transactions", []) or []:
        if tx.get("side") != "赎回":
            continue
        sd = str(tx.get("settle_date", ""))[:10]
        if not sd:
            continue
        try:
            if date.fromisoformat(sd) <= today:
                total += float(tx.get("amount", 0) or 0)
        except Exception:
            continue
    return total


# ---------------- 冷却期 ----------------
def trading_days_between(d1, d2):
    """两日期之间的交易日数（含区间内全部交易日；akshare 日历，失败回退自然日/1.4 近似）"""
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        cal = set(df["trade_date"].astype(str))
        s1, s2 = d1.isoformat(), d2.isoformat()
        if s1 > s2:
            s1, s2 = s2, s1
        return sum(1 for d in cal if s1 <= d <= s2)
    except Exception:
        try:
            return max(1, int(abs((d2 - d1).days) / 1.4))
        except Exception:
            return 1


def cooldown_days(cfg, code=None):
    """冷却期天数：产品覆盖 > 平台默认 > 全局默认（默认 5 交易日）"""
    cd = cfg.get("rules", {}).get("cooldown", {})
    return int(cd.get("per_product", {}).get(code or "", 0) or
               cd.get("default_days", 5) or 5)


def in_cooldown(cfg, state=None):
    """距上次调仓是否在冷却期。优先级：config.portfolio.last_rebalance_date（云端人工维护）
    > 本地 state.json。两者皆无 → 放行。"""
    st = state or load_state()
    last = (cfg.get("portfolio", {}).get("last_rebalance_date")
            or st.get("last_rebalance_date") or "")
    if not last:
        return False
    try:
        d1 = date.fromisoformat(str(last)[:10])
        d2 = date.today()
        if trading_days_between(d1, d2) > cooldown_days(cfg):
            return False
        return True
    except Exception:
        return False


# ---------------- 通用小工具 ----------------
def _is_iso_date(s):
    """是否为合法 ISO 日期（YYYY-MM-DD）"""
    try:
        date.fromisoformat(str(s)[:10])
        return True
    except Exception:
        return False


def _norm_date_str(v):
    """把 last_rebalance_date 等字段的 string / dict 形式归一为 YYYY-MM-DD；
    dict 兼容 date / date_str / last_rebalance_date / value 键的 dict 形式；
    无法解析返回 None。"""
    if isinstance(v, dict):
        for k in ('date', 'date_str', 'last_rebalance_date', 'value'):
            if v.get(k):
                v = v[k]
                break
        else:
            return None
    if not v:
        return None
    s = str(v).strip()[:10]
    try:
        return date.fromisoformat(s).isoformat()
    except Exception:
        return None



# ---------------- 留痕表（止盈信号/算法偏差） ----------------
TRACES_PATH = os.path.join(KB_DIR, "traces.json")


def kb_read_traces():
    """读取留痕表（升序 list）；缺失返回 []"""
    try:
        if os.path.exists(TRACES_PATH):
            with open(TRACES_PATH, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def recent_tp_actions(traces=None, days=5):
    """最近 days 交易日内每产品最近一次止盈 action（排除今天）。
    返回 {code: action}；用于同档位信号去重（V型回落二次穿越防护）。"""
    traces = traces if traces is not None else kb_read_traces()
    out = {}
    today = date.today()
    for t in reversed(traces):
        code = str(t.get("code", ""))
        act = str(t.get("action", ""))
        sd = str(t.get("signal_date", ""))[:10]
        if not code or not act or not sd:
            continue
        try:
            d = date.fromisoformat(sd)
        except Exception:
            continue
        if d >= today:
            continue
        if code not in out and trading_days_between(d, today) <= days:
            out[code] = act
    return out


def get_recent_signals(days=5):
    """最近 days 个交易日内的信号回顾（3.3 补充）：读 traces.json，
    窗口按 trading_days_between(signal_date, 今日) <= days 判定（含今日，忽略未来日期）；
    按 (code, action, signal_date) 去重；返回简化条目 [code/action/signal_date]，
    按 signal_date 倒序；窗口内无记录 → []。"""
    traces = kb_read_traces()
    today = date.today()
    seen = set()
    out = []
    for t in reversed(traces):
        if not isinstance(t, dict):
            continue
        code = str(t.get('code', '')).strip()
        act = str(t.get('action', '')).strip()
        sd = str(t.get('signal_date', ''))[:10]
        if not code or not act or not _is_iso_date(sd):
            continue
        d = date.fromisoformat(sd)
        if d > today:
            continue
        if trading_days_between(d, today) > days:
            continue
        key = (code, act, sd)
        if key in seen:
            continue
        seen.add(key)
        out.append({'code': code, 'action': act, 'signal_date': sd})
    out.sort(key=lambda x: x['signal_date'], reverse=True)
    return out



def record_trace(entry):
    """留痕七字段：{T_eff, T_pre, v_bench, 因子值, 信号日, 执行日, 费后净收益}
    写入 knowledge_base/traces.json（追加）。"""
    try:
        os.makedirs(KB_DIR, exist_ok=True)
        data = kb_read_traces()
        data.append(entry)
        with open(TRACES_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log_state(f"[TRACE] 已记录: {entry.get('code','?')} T_eff={entry.get('T_eff')}")
    except Exception as e:
        log_state(f"[WARN] 留痕失败: {e}")


def shadow_stats(cfg):
    """影子模式进度统计（3.5）。
    起始日优先级：cfg.rules.take_profit.shadow_mode_started_date
      → cfg.shadow_mode_started_date → traces.json 最早 trace 的 signal_date → None。
    返回 {start: YYYY-MM-DD 或 None, days: 自然日数（start 到今日，None 时 0）,
          signals: traces 中 signal_date >= start 的条数（字符串比较，start None 时 0）,
          products: 涉及产品 code 去重数}；无数据/异常 → 全默认。"""
    start = (cfg.get('rules', {}).get('take_profit', {}).get('shadow_mode_started_date')
             or cfg.get('shadow_mode_started_date') or '')
    start = _norm_date_str(start)
    traces = kb_read_traces()
    if not start:
        dates = []
        for t in traces:
            if isinstance(t, dict):
                sd = str(t.get('signal_date', ''))[:10]
                if _is_iso_date(sd):
                    dates.append(sd)
        start = min(dates) if dates else None
    if not start:
        return {'start': None, 'days': 0, 'signals': 0, 'products': 0}
    try:
        days = max(0, (date.today() - date.fromisoformat(start)).days)
    except Exception:
        days = 0
    n = 0
    codes = set()
    for t in traces:
        if not isinstance(t, dict):
            continue
        sd = str(t.get('signal_date', ''))[:10]
        if _is_iso_date(sd) and sd >= start:
            n += 1
            code = str(t.get('code', '')).strip()
            if code:
                codes.add(code)
    return {'start': start, 'days': days, 'signals': n, 'products': len(codes)}

shadow_mode_stats = shadow_stats  # 兼容升级指令书 3.5 中 shadow_mode_stats() 的等价别名


# ---------------- 推荐日志（近一月推荐次数：用户"次数暗示"需求） ----------------


REC_PATH = os.path.join(KB_DIR, "recommendations.json")


def kb_read_recommendations():
    """读取推荐日志 [{date, name, type, reason}]；缺失返回 []"""
    try:
        if os.path.exists(REC_PATH):
            with open(REC_PATH, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def kb_append_recommendations(entries):
    """追加当日推荐（自动加 date），写回 recommendations.json。
    云端每次运行后由 Actions commit 回写，实现跨日持久。"""
    if not entries:
        return
    try:
        os.makedirs(KB_DIR, exist_ok=True)
        data = kb_read_recommendations()
        today = date.today().isoformat()
        for e in entries:
            name = str(e.get("name") or e.get("industry") or "").strip()
            if not name:
                continue
            data.append({"date": today, "name": name,
                         "type": str(e.get("type", "") or "").strip(),
                         "reason": str(e.get("reason", ""))[:120]})
        with open(REC_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log_state(f"[REC] 推荐日志追加 {len(entries)} 条")
    except Exception as e:
        log_state(f"[WARN] 推荐日志写入失败: {e}")


def count_recent_recommendations(name, days=30):
    """近 days 天内某名称被推荐次数"""
    if not name:
        return 0
    cutoff = (date.today() - __import__("datetime").timedelta(days=days)).isoformat()
    n = 0
    for r in kb_read_recommendations():
        if str(r.get("name", "")).strip() == name and str(r.get("date", ""))[:10] >= cutoff:
            n += 1
    return n


# ---------------- 知识库读取 ----------------
def get_yesterday_recommendations():
    """昨日推荐回顾（3.3）：读 knowledge_base/recommendations.json，
    返回所有条目中严格早于今日的最大 date（上一个日期）的条目列表；
    文件缺失/损坏/无历史条目/全空 → []。注意：同 date 多条都返回，不含今日。"""
    recs = kb_read_recommendations()
    today = date.today().isoformat()
    prev = ''
    for r in recs:
        if not isinstance(r, dict):
            continue
        d = str(r.get('date', ''))[:10]
        if _is_iso_date(d) and d < today and d > prev:
            prev = d
    if not prev:
        return []
    return [r for r in recs if isinstance(r, dict) and str(r.get('date', ''))[:10] == prev]



def kb_product(code):
    """读取 knowledge_base/products/<code>.md 摘要（前 N 行概述 + 近况节）。
    返回字符串或空。"""
    try:
        path = os.path.join(KB_DIR, "products", f"{code}.md")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        parts = []
        for ln in lines:
            if ln.startswith("## ") and ln.strip() != "## 近况":
                break
            parts.append(ln)
        return "\n".join(parts)
    except Exception:
        return ""


def kb_watchlist():
    """读取 knowledge_base/watchlist.json（关注池元数据），缺失返回空 dict。"""
    try:
        path = os.path.join(KB_DIR, "watchlist.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


# ---------------- 状态快照（state_history.json，云端跨日持久通道） ----------------
STATE_HISTORY_PATH = os.path.join(KB_DIR, 'state_history.json')
STATE_HISTORY_KEEP = 60


def kb_read_state_history():
    """读取每日状态快照历史（按 date 升序）；缺失/损坏返回 []"""
    try:
        if os.path.exists(STATE_HISTORY_PATH):
            with open(STATE_HISTORY_PATH, encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def write_state_snapshot(entry):
    """每次运行结束时调用一次（3.4）：把当日状态快照写入
    knowledge_base/state_history.json（云端 Actions commit 回写 → 跨日持久）。
    entry 为 dict（含 date/orders/target_alloc/pending_cash/total_mv/tp_signals 等键，
    由调用方组装；本方法不校验具体键，只做通用存储）。
    数组按 date 升序追加；同 date 已存在则原位覆盖（保持最新）；
    只保留最近 60 条（超出截断最旧）。文件损坏/不存在 → 重建空数组。
    写失败不抛异常，仅记日志。"""
    try:
        os.makedirs(KB_DIR, exist_ok=True)
        data = kb_read_state_history()
        dkey = str(entry.get('date', '')) if isinstance(entry, dict) else ''
        if isinstance(entry, dict) and dkey:
            for idx, e in enumerate(data):
                if isinstance(e, dict) and str(e.get('date', '')) == dkey:
                    data[idx] = entry
                    break
            else:
                data.append(entry)
        else:
            data.append(entry)
        data.sort(key=lambda e: (str(e.get('date', '')) if isinstance(e, dict) else ''))
        data = data[-STATE_HISTORY_KEEP:]
        with open(STATE_HISTORY_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log_state('[SNAP] 状态快照已写: ' + (dkey or '<no-date>'))
    except Exception as e:
        log_state('[WARN] 状态快照写入失败: ' + str(e))


def get_last_rebalance_date(cfg):
    """上次调仓日期（YYYY-MM-DD 字符串或 None；3.4 冷却期跨日持久辅助）。
    兼容 string / dict 两种配置形式；优先级：
      config.portfolio.last_rebalance_date → 本地 state.json
      → 云端快照 knowledge_base/state_history.json（最近一次快照，git 回写跨日持久）。
    不改动 in_cooldown 既有逻辑，仅在本方法内做兼容读取。"""
    raw = (cfg.get('portfolio', {}).get('last_rebalance_date')
           or load_state().get('last_rebalance_date') or '')
    if not raw:
        for e in reversed(kb_read_state_history()):
            if isinstance(e, dict) and e.get('last_rebalance_date'):
                raw = e['last_rebalance_date']
                break
    return _norm_date_str(raw)
