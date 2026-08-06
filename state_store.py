# -*- coding: utf-8 -*-
"""StateStore —— 状态与记忆层（state-memory 职责域）。
双轨制：本地自动写 state.json；云端（GitHub Actions）无跨日磁盘，
状态与在途资金以 config（Secret）人工维护为准，本模块只读兼容。
提供：在途资金表、最近指令/执行状态、冷却期判定、留痕表读写、知识库读取。
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
    """两日期之间的交易日数（akshare 交易日历，失败回退自然日/1.4 近似）"""
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        cal = set(df["trade_date"].astype(str))
        return sum(1 for d in (d1, d2) if d in cal)
    except Exception:
        try:
            return max(1, int((d2 - d1).days / 1.4))
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


# ---------------- 留痕表（止盈信号/算法偏差） ----------------
def record_trace(entry):
    """留痕七字段：{T_eff, T_pre, v_bench, 因子值, 信号日, 执行日, 费后净收益}
    写入 knowledge_base/traces.json（追加）。云端只读调用方负责传参。"""
    try:
        os.makedirs(KB_DIR, exist_ok=True)
        path = os.path.join(KB_DIR, "traces.json")
        data = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        data.append(entry)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log_state(f"[TRACE] 已记录: {entry.get('code','?')} T_eff={entry.get('T_eff')}")
    except Exception as e:
        log_state(f"[WARN] 留痕失败: {e}")


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
