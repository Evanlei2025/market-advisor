# -*- coding: utf-8 -*-
"""StateStore —— 状态与记忆层（state-memory 职责域）。
双轨制：本地自动写 state.json（最近指令/执行状态、冷却期）；云端（GitHub Actions）
无跨日磁盘，状态与在途资金以 config（Secret）人工维护为准，本模块只读兼容。
跨日持久通道：knowledge_base/ 下 JSON（云端 Actions commit 回写）——
  traces.json（止盈留痕）、recommendations.json（推荐日志）、
  feedback.json（执行回执，双写）、manager_snapshots.json（经理任期快照，双写）、
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


# ---------------- kb 通用读写（云端跨日持久通道辅助） ----------------
# feedback / manager_snapshots 双写通道：state.json 为本地兼容缓存（gitignore），
# knowledge_base/ 下文件随云端 Actions commit 回写跨日持久（事实源）。
FEEDBACK_KB_PATH = os.path.join(KB_DIR, 'feedback.json')
MANAGER_SNAPSHOT_KB_PATH = os.path.join(KB_DIR, 'manager_snapshots.json')


def _kb_read(key, file, default):
    '读 kb 文件顶层 key：文件存在且为合法 JSON dict 且 key 值非 None → 返回该值；否则返回 default（静默降级，不抛异常）。'
    try:
        if os.path.exists(file):
            with open(file, encoding='utf-8') as f:
                data = json.load(f)
                v = data.get(key) if isinstance(data, dict) else None
                if v is not None:
                    return v
    except Exception:
        pass
    return default


def _kb_write(key, data, file):
    '整文件写 kb 文件：读旧 doc（保留其他 key）→ 更新目标 key → 写回（ensure_ascii=False, indent=2）。失败仅记日志，不抛异常。'
    try:
        os.makedirs(KB_DIR, exist_ok=True)
        doc = {}
        try:
            if os.path.exists(file):
                with open(file, encoding='utf-8') as f:
                    old = json.load(f)
                    if isinstance(old, dict):
                        doc = old
        except Exception:
            doc = {}
        doc[key] = data
        with open(file, 'w', encoding='utf-8') as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log_state('[WARN] kb 写入失败: ' + str(e))
        return False



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
        import aktime
        df = aktime.call_with_timeout(ak.tool_trade_date_hist_sina, timeout=90)
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


def in_cooldown(cfg, state=None, client=None):
    '距上次调仓是否在冷却期。优先级：config.portfolio.last_rebalance_date（云端人工维护）> state.json 的 clients.<client>.last_rebalance_date（客户维度）> 顶层 last_rebalance_date（旧单客户形态）。两者皆无 → 放行。'
    st = state or load_state()
    last = cfg.get('portfolio', {}).get('last_rebalance_date') or ''
    if not last:
        cl_raw = ''
        if client and isinstance(st, dict) and isinstance(st.get('clients'), dict):
            cl = st['clients'].get(client)
            if isinstance(cl, dict):
                cl_raw = cl.get('last_rebalance_date') or ''
        last = cl_raw or (st.get('last_rebalance_date') or '' if isinstance(st, dict) else '')
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


def min_stat_mv(cfg):
    '组合统计代表性阈值（单位元，默认 50000）：cfg.rules.diagnostics.min_stat_mv；缺失/异常 → 50000。'
    try:
        v = cfg.get('rules', {}).get('diagnostics', {}).get('min_stat_mv')
        if v is None:
            return 50000.0
        return float(v)
    except Exception:
        return 50000.0


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

DEFAULT_CLIENT = 'Evan_Lei'

# 客户改名登记表：历史名称 → 当前名称。客户改名时在此登记；
# 仅读取层归一（历史条目读取时视同新客户），不改写历史文件。
CLIENT_ALIASES = {'Jing_Wang': 'Echo_Wang', 'NULL_Xue': 'QunHui_Xue'}


def _norm_client(entry):
    '条目所属客户：无 client 字段的旧条目一律视为 Evan_Lei；有 client 字段先查改名映射表归一（读取兼容，不改写历史文件）。'
    if not isinstance(entry, dict):
        return DEFAULT_CLIENT
    raw = str(entry.get('client') or DEFAULT_CLIENT)
    return CLIENT_ALIASES.get(raw, raw)


def _client_of(entry, client):
    'client=None → 不过滤；否则按 _norm_client 归一化后匹配。'
    if client is None:
        return True
    return _norm_client(entry) == client


# ---------------- 留痕表（止盈信号/算法偏差） ----------------
TRACES_PATH = os.path.join(KB_DIR, 'traces.json')


def _trace_dedup_key(t):
    '留痕去重键 (signal_date, code, action, client)，client 经 _norm_client 归一；字段缺失/异常 → None（不可去重，绝不误并）。'
    try:
        if not isinstance(t, dict):
            return None
        sd = str(t.get('signal_date', ''))[:10].strip()
        code = str(t.get('code', '')).strip()
        act = str(t.get('action', '')).strip()
        if not sd or not code or not act:
            return None
        return (sd, code, act, _norm_client(t))
    except Exception:
        return None


def _dedup_traces(traces):
    '读取层归一：同去重键保留最后一条（按遍历顺序覆盖，后见覆盖前见；防跨 git 并发合并残留）。'
    try:
        out = []
        pos = {}
        for t in traces:
            k = _trace_dedup_key(t)
            if k is None:
                out.append(t)
                continue
            if k in pos:
                out[pos[k]] = t
            else:
                pos[k] = len(out)
                out.append(t)
        return out
    except Exception:
        return traces


def kb_read_traces():
    '读取留痕表（升序 list）；缺失返回 []。读取层按 (signal_date, code, action, client) 去重归一：同键保留最后一条（list 升序，最后=最新；防跨 git 并发合并残留）。'
    try:
        if os.path.exists(TRACES_PATH):
            with open(TRACES_PATH, encoding='utf-8') as f:
                data = json.load(f)
                if not isinstance(data, list):
                    return []
                return _dedup_traces(data)
    except Exception:
        pass
    return []


def recent_tp_actions(traces=None, days=5, client=None):
    '最近 days 交易日内每产品最近一次止盈 action（排除今天）。返回 {code: action}；用于同档位信号去重（V型回落二次穿越防护）。client=None 不过滤；旧条目（无 client 字段）按 Evan_Lei 计。'
    traces = traces if traces is not None else kb_read_traces()
    out = {}
    today = date.today()
    for t in reversed(traces):
        if not isinstance(t, dict) or not _client_of(t, client):
            continue
        code = str(t.get('code', ''))
        act = str(t.get('action', ''))
        sd = str(t.get('signal_date', ''))[:10]
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


def get_recent_signals(days=5, client=None):
    '最近 days 个交易日内的信号回顾（3.3 补充）：读 traces.json，窗口按 trading_days_between(signal_date, 今日) <= days 判定（含今日，忽略未来日期）；按 (code, action, signal_date) 去重；返回简化条目 [code/action/signal_date]，按 signal_date 倒序；窗口内无记录 → []。client=None 不过滤；旧条目按 Evan_Lei。'
    traces = kb_read_traces()
    today = date.today()
    seen = set()
    out = []
    for t in reversed(traces):
        if not isinstance(t, dict) or not _client_of(t, client):
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


def tp_streak_days(code, client=None):
    '止盈信号持续天数（V3，用户反馈“006195 止盈信号触发第几天了？”——去重隐藏了重复信号，报告显示“持续中（第 N 天）”）。口径：读 traces.json，过滤 _client_of 且 code 匹配且 signal_date 为 ISO 日期且 <= 今天，取最近一个信号日；N = trading_days_between(最近信号日, 今天) + 1；最近信号日距今 > 10 个交易日 → 0（已过期不算“持续中”）；无记录/异常 → 0。'
    try:
        code = str(code or '').strip()
        if not code:
            return 0
        today = date.today()
        best = None
        for t in kb_read_traces():
            if not isinstance(t, dict) or not _client_of(t, client):
                continue
            if str(t.get('code', '')).strip() != code:
                continue
            sd = str(t.get('signal_date', ''))[:10]
            if not _is_iso_date(sd):
                continue
            d = date.fromisoformat(sd)
            if d > today:
                continue
            if best is None or d > best:
                best = d
        if best is None:
            return 0
        n = trading_days_between(best, today)
        if n > 10:
            return 0
        return n + 1
    except Exception:
        return 0


def record_trace(entry, client=None):
    '留痕七字段：{T_eff, T_pre, v_bench, 因子值, 信号日, 执行日, 费后净收益} 写入 knowledge_base/traces.json。按去重键 (signal_date, code, action, client) 判重：已存在 → 原位更新（合并字段刷新，保留原条目位置）；不存在 → 追加；键字段缺失 → 直接追加（绝不误并）。新条目带 client 字段（None 时按 Evan_Lei）。'
    try:
        os.makedirs(KB_DIR, exist_ok=True)
        data = kb_read_traces()
        if isinstance(entry, dict):
            entry['client'] = client or DEFAULT_CLIENT
        key = _trace_dedup_key(entry) if isinstance(entry, dict) else None
        updated = False
        if key is not None:
            for i, t in enumerate(data):
                if _trace_dedup_key(t) == key:
                    data[i] = entry
                    updated = True
                    break
        if not updated:
            data.append(entry)
        with open(TRACES_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        mode = '（原位更新）' if updated else ''
        log_state('[TRACE] 已记录: ' + str(entry.get('code', '?')) + ' T_eff=' + str(entry.get('T_eff')) + mode)
    except Exception as e:
        log_state('[WARN] 留痕失败: ' + str(e))


# ---------------- 执行回执历史（feedback：state.json 本地缓存 + knowledge_base/feedback.json 双写） ----------------


def record_feedback(client, date, status, note=""):
    '追加一条执行回执（条目 {client, date, status, note}；client 用 _norm_client 归一，None → Evan_Lei）。双写：state.json 的 feedback 数组（本地兼容缓存）与 knowledge_base/feedback.json（云端跨日持久，事实源）；state.json 写失败不阻塞 kb 写，任一失败仅记日志。'
    try:
        entry = {'client': _norm_client({'client': client}),
                 'date': str(date),
                 'status': str(status),
                 'note': str(note)}
        # 本地 state.json（缓存；写失败不阻塞 kb 双写）
        try:
            st = load_state()
            fb = st.get('feedback')
            if not isinstance(fb, list):
                fb = []
            fb.append(entry)
            st['feedback'] = fb
            save_state(st)
        except Exception as e:
            log_state('[WARN] 回执 state.json 写入失败: ' + str(e))
        # kb 双写（事实源，随云端 Actions commit 跨日持久）
        fb = _kb_read('feedback', FEEDBACK_KB_PATH, [])
        if not isinstance(fb, list):
            fb = []
        fb.append(entry)
        _kb_write('feedback', fb, FEEDBACK_KB_PATH)
        log_state('[FEEDBACK] 已记录: ' + str(status) + ' @' + str(date))
    except Exception as e:
        log_state('[WARN] 回执写入失败: ' + str(e))


def get_feedback(client=None, limit=1):
    '返回该客户最近 limit 条执行回执（按 date 倒序；为隔离必须按客户过滤，client=None → 默认 DEFAULT_CLIENT）。读路径：knowledge_base/feedback.json 优先（存在且 feedback 为 list → 用它，事实源），缺失/损坏 → 回退 state.json；两处皆缺失/损坏 → []。返回 list[dict]。'
    try:
        fb = _kb_read('feedback', FEEDBACK_KB_PATH, None)
        if not isinstance(fb, list):
            fb = load_state().get('feedback')
        if not isinstance(fb, list):
            return []
        cl = _norm_client({'client': client})
        out = [f for f in fb if isinstance(f, dict) and _norm_client(f) == cl]
        out.sort(key=lambda x: str(x.get('date', '')), reverse=True)
        return out[:limit]
    except Exception:
        return []


# ---------------- 经理快照（manager_snapshots：state.json 本地缓存 + knowledge_base/manager_snapshots.json 双写） ----------------
# 候选评分「经理任期维度」数据层：免费源只有经理名字符串（如「马芳 姚加红」）无任职日期，
# 经理变更侦测自己攒任期：每次跑批按 code 存快照，字符串变化那天 = 新任期起点，之后自然累积。
# 冷启动无历史 → 调用方三态处理（已知≥12月不惩罚 / 已知<12月×0.85 / 未知×0.95+标签）。
MANAGER_SNAPSHOT_MAX = 200


def _apply_manager_snapshot(snaps, code, mgr, when_str, cl):
    '快照更新+清理（state.json / kb 双写共用）：manager 为空/None → 只更新 last_seen 不动 since；code 无历史 → since_date=when_str；已有历史且 manager 变化 → since_date=when_str（新任期起点）+ 更新 manager；相同 → 仅刷 last_seen。超过 MANAGER_SNAPSHOT_MAX 保留最近 last_seen 的。返回新 dict。'
    if not isinstance(snaps, dict):
        snaps = {}
    prev = snaps.get(code)
    if isinstance(prev, dict) and prev.get('since_date') and _is_iso_date(str(prev.get('since_date'))[:10]):
        old_mgr = str(prev.get('manager', '') or '').strip()
        if mgr and mgr != old_mgr:
            prev['manager'] = mgr
            prev['since_date'] = when_str
        elif mgr:
            prev['manager'] = mgr
        prev['last_seen'] = when_str
        prev['client'] = cl
        snaps[code] = prev
    else:
        snaps[code] = {'manager': mgr, 'since_date': when_str,
                       'last_seen': when_str, 'client': cl}
    if len(snaps) > MANAGER_SNAPSHOT_MAX:
        items = sorted(snaps.items(),
                       key=lambda kv: (str(kv[1].get('last_seen', '')) if isinstance(kv[1], dict) else '', kv[0]),
                       reverse=True)
        snaps = dict(items[:MANAGER_SNAPSHOT_MAX])
    return snaps


def record_manager_snapshot(code, manager_str, client=None, when=None):
    '经理变更侦测快照：key 直接用 code（经理是基金属性不是客户属性，多客户共用快照），写入时记录 client 便于审计。双写：state.json（本地兼容缓存）与 knowledge_base/manager_snapshots.json（云端跨日持久，事实源）；>200 清理双写都执行；state.json 写失败不阻塞 kb 写。'
    try:
        code = str(code or '').strip()
        if not code:
            return
        if when:
            try:
                d = date.fromisoformat(str(when)[:10])
                when_str = d.isoformat()
            except Exception:
                when_str = date.today().isoformat()
        else:
            when_str = date.today().isoformat()
        mgr = str(manager_str or '').strip()
        cl = _norm_client({'client': client})
        # 本地 state.json（缓存；写失败不阻塞 kb 双写）
        try:
            st = load_state()
            s1 = st.get('manager_snapshots')
            if not isinstance(s1, dict):
                s1 = {}
            st['manager_snapshots'] = _apply_manager_snapshot(s1, code, mgr, when_str, cl)
            save_state(st)
        except Exception as e:
            log_state('[WARN] manager snapshot state.json 写入失败: ' + str(e))
        # kb 双写（事实源，随云端 Actions commit 跨日持久）
        s2 = _kb_read('manager_snapshots', MANAGER_SNAPSHOT_KB_PATH, {})
        if not isinstance(s2, dict):
            s2 = {}
        _kb_write('manager_snapshots', _apply_manager_snapshot(s2, code, mgr, when_str, cl),
                  MANAGER_SNAPSHOT_KB_PATH)
        log_state('[MGR] snapshot ' + code + ': ' + (mgr or '<empty>'))
    except Exception as e:
        log_state('[WARN] manager snapshot failed: ' + str(e))


def manager_snapshot(code):
    '读取经理快照 {manager, since_date, last_seen}。读路径：knowledge_base/manager_snapshots.json 优先（存在且 manager_snapshots 为 dict → 用它，事实源），缺失/损坏 → 回退 state.json；两处皆缺失/损坏/无记录/since_date 非法/manager 为空（从未见过经理名，等同未知）→ None。'
    try:
        code = str(code or '').strip()
        if not code:
            return None
        snaps = _kb_read('manager_snapshots', MANAGER_SNAPSHOT_KB_PATH, None)
        if not isinstance(snaps, dict):
            snaps = load_state().get('manager_snapshots')
        if not isinstance(snaps, dict):
            return None
        cur = snaps.get(code)
        if not isinstance(cur, dict):
            return None
        mgr = str(cur.get('manager', '') or '').strip()
        since = str(cur.get('since_date', '') or '')[:10]
        seen = str(cur.get('last_seen', '') or '')[:10]
        if not mgr or not _is_iso_date(since):
            return None
        return {'manager': mgr, 'since_date': since, 'last_seen': seen}
    except Exception:
        return None


def manager_tenure_days(code, config_since=None):
    '现任经理任职天数（int）：config_since（config products.manager_since，YYYY-MM-DD）非空且为合法日期 → 用它算（人工维护优先）；否则用快照 since_date 算；都无 → None（三态判定用：已知≥12月不惩罚 / 已知<12月×0.85 / 未知×0.95+标签）。config_since 非法日期 → 回退快照；异常全部返回 None。'
    try:
        code = str(code or '').strip()
        if not code:
            return None
        since = None
        cs = str(config_since or '').strip()[:10]
        if cs and _is_iso_date(cs):
            since = date.fromisoformat(cs)
        else:
            snap = manager_snapshot(code)
            if snap:
                since = date.fromisoformat(snap['since_date'])
        if since is None:
            return None
        return max(0, (date.today() - since).days)
    except Exception:
        return None


def shadow_stats(cfg, client=None):
    '影子模式进度统计（3.5）。起始日优先级：cfg.rules.take_profit.shadow_mode_started_date → cfg.shadow_mode_started_date → traces.json 最早 trace 的 signal_date → None。返回 {start, days, signals, products}；signals/products 按 client 过滤（client=None 不过滤；旧条目按 Evan_Lei）。'
    start = (cfg.get('rules', {}).get('take_profit', {}).get('shadow_mode_started_date')
             or cfg.get('shadow_mode_started_date') or '')
    start = _norm_date_str(start)
    traces = [t for t in kb_read_traces() if isinstance(t, dict) and _client_of(t, client)]
    if not start:
        dates = []
        for t in traces:
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


def kb_append_recommendations(entries, client=None):
    '追加当日推荐（自动加 date 与 client 字段），写回 recommendations.json。云端每次运行后由 Actions commit 回写，实现跨日持久。'
    if not entries:
        return
    try:
        os.makedirs(KB_DIR, exist_ok=True)
        data = kb_read_recommendations()
        today = date.today().isoformat()
        for e in entries:
            name = str(e.get('name') or e.get('industry') or '').strip()
            if not name:
                continue
            data.append({'date': today, 'name': name,
                         'type': str(e.get('type', '') or '').strip(),
                         'reason': str(e.get('reason', ''))[:120],
                         'client': client or DEFAULT_CLIENT})
        with open(REC_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log_state('[REC] 推荐日志追加 ' + str(len(entries)) + ' 条')
    except Exception as e:
        log_state('[WARN] 推荐日志写入失败: ' + str(e))


def count_recent_recommendations(name, days=30, client=None):
    """近 days 天内某名称被推荐次数（client 非 None 时仅统计该客户）"""
    if not name:
        return 0
    cutoff = (date.today() - __import__("datetime").timedelta(days=days)).isoformat()
    n = 0
    for r in kb_read_recommendations():
        if str(r.get("name", "")).strip() == name and str(r.get("date", ""))[:10] >= cutoff:
            if client is None or _client_of(r, client):
                n += 1
    return n


# ---------------- 知识库读取 ----------------
def get_yesterday_recommendations(client=None):
    '昨日推荐回顾（3.3）：读 knowledge_base/recommendations.json，返回严格早于今日的最大 date（上一个日期）的条目列表；文件缺失/损坏/无历史条目/全空 → []。同 date 多条都返回，不含今日。按 client 过滤（client=None 不过滤；旧条目无 client 字段按 Evan_Lei）。'
    recs = [r for r in kb_read_recommendations() if isinstance(r, dict) and _client_of(r, client)]
    today = date.today().isoformat()
    prev = ''
    for r in recs:
        d = str(r.get('date', ''))[:10]
        if _is_iso_date(d) and d < today and d > prev:
            prev = d
    if not prev:
        return []
    return [r for r in recs if str(r.get('date', ''))[:10] == prev]


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
    '读取每日状态快照历史（按 date 升序）；缺失/损坏返回 []。读取层按 (date, _norm_client) 去重归一：同键保留最后一条（按遍历顺序覆盖，后见覆盖前见；防跨 git 并发合并残留；client 经 _norm_client 归一）。'
    try:
        if os.path.exists(STATE_HISTORY_PATH):
            with open(STATE_HISTORY_PATH, encoding='utf-8') as f:
                data = json.load(f)
                if not isinstance(data, list):
                    return []
                out = []
                pos = {}
                for e in data:
                    if not isinstance(e, dict):
                        out.append(e)
                        continue
                    dkey = str(e.get('date', ''))
                    if not dkey:
                        out.append(e)
                        continue
                    k = (dkey, _norm_client(e))
                    if k in pos:
                        out[pos[k]] = e
                    else:
                        pos[k] = len(out)
                        out.append(e)
                return out
    except Exception:
        pass
    return []


def write_state_snapshot(entry, client=None):
    '每次运行结束时调用一次（3.4）：把当日状态快照写入 knowledge_base/state_history.json（云端 Actions commit 回写 → 跨日持久）。entry 为 dict，本方法不校验具体键，只做通用存储；可选字段 storm_active（bool）、eq_target（float）：调用方传什么存什么（不强制），供 was_storm_yesterday() 昨日风暴对照读取；新条目自动带 client 字段（None 时按 Evan_Lei；旧条目无 client 字段同样按 Evan_Lei 处理）。数组按 date 升序追加；同 date 且同 client 已存在则原位覆盖；只保留最近 60 条。写失败不抛异常，仅记日志。'
    try:
        os.makedirs(KB_DIR, exist_ok=True)
        data = kb_read_state_history()
        if isinstance(entry, dict):
            entry['client'] = client or DEFAULT_CLIENT
        dkey = str(entry.get('date', '')) if isinstance(entry, dict) else ''
        ckey = _norm_client(entry)
        if isinstance(entry, dict) and dkey:
            for idx, e in enumerate(data):
                if isinstance(e, dict) and str(e.get('date', '')) == dkey and _norm_client(e) == ckey:
                    data[idx] = entry
                    break
            else:
                data.append(entry)
        else:
            data.append(entry)
        data.sort(key=lambda e: (str(e.get('date', '')) if isinstance(e, dict) else '',
                                 _norm_client(e) if isinstance(e, dict) else ''))
        data = data[-STATE_HISTORY_KEEP:]
        with open(STATE_HISTORY_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log_state('[SNAP] 状态快照已写: ' + (dkey or '<no-date>'))
    except Exception as e:
        log_state('[WARN] 状态快照写入失败: ' + str(e))


def get_last_rebalance_date(cfg, client=None):
    '上次调仓日期（YYYY-MM-DD 字符串或 None；3.4 冷却期跨日持久辅助）。兼容 string / dict 两种配置形式；优先级：config.portfolio.last_rebalance_date → 本地 state.json（clients.<client>.last_rebalance_date 客户维度优先，顶层 last_rebalance_date 回退）→ 云端快照 knowledge_base/state_history.json（按 client 过滤；旧条目按 Evan_Lei；client=None 不过滤）。不改动 in_cooldown 既有逻辑，仅在本方法内做兼容读取。'
    raw = cfg.get('portfolio', {}).get('last_rebalance_date') or ''
    if not raw:
        st = load_state()
        cl_raw = ''
        if client and isinstance(st.get('clients'), dict):
            cl = st['clients'].get(client)
            if isinstance(cl, dict):
                cl_raw = cl.get('last_rebalance_date') or ''
        raw = cl_raw or st.get('last_rebalance_date') or ''
    if not raw:
        for e in reversed(kb_read_state_history()):
            if isinstance(e, dict) and e.get('last_rebalance_date') and _client_of(e, client):
                raw = e['last_rebalance_date']
                break
    return _norm_date_str(raw)


# ---------------- 昨日风暴对照（V3） ----------------


def was_storm_yesterday(client=None):
    '昨日风暴对照（V3）：读 state_history.json，找该客户日期 < 今天的最近一条快照，返回其 storm_active（True/False）；缺失或非 bool → None（未知）。client=None 不过滤（旧条目按 Evan_Lei）。'
    try:
        today = date.today().isoformat()
        best = None
        for e in kb_read_state_history():
            if not isinstance(e, dict) or not _client_of(e, client):
                continue
            d = str(e.get('date', ''))[:10]
            if not _is_iso_date(d) or d >= today:
                continue
            if best is None or d > best:
                best = e
        if best is None:
            return None
        v = best.get('storm_active')
        return v if isinstance(v, bool) else None
    except Exception:
        return None
