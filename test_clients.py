# -*- coding: utf-8 -*-
"""多客户改造单元测试：get_clients 解析、deep_merge 覆盖语义、
state_store 客户维度隔离（旧条目归并 Evan_Lei）。"""
import json
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import main
import state_store


def run():
    passed = 0

    def check(name, cond, extra=""):
        nonlocal passed
        if cond:
            passed += 1
            print(f"PASS {name} {extra}")
        else:
            print(f"FAIL {name} {extra}")

    cfg = json.load(open("config.json", encoding="utf-8"))
    clients = main.get_clients(cfg)
    check("四客户解析", len(clients) == 4, f"n={len(clients)}")
    check("客户ID顺序", [c["id"] for c in clients] == ["Evan_Lei", "Harley_Lei", "NULL_Xue", "Echo_Wang"],
          str([c["id"] for c in clients]))

    evan = clients[0]["cfg"]
    check("Evan products=3", len(evan.get("products", [])) == 3)
    check("Evan holdings=3(含现金)", len(evan.get("holdings", [])) == 3)
    check("Evan target 0.4", evan.get("target", {}).get("equity", {}).get("base") == 0.4)
    check("Evan tier_gap 调优保留", evan.get("rules", {}).get("take_profit", {}).get("tier_gap") == [0.05, 0.1])
    check("Evan 在途资金迁移", any(t.get("code") == "006195" and t.get("side") == "赎回"
                                 for t in evan.get("transactions", [])))
    check("Evan 观察池保留", any(p.get("status") == "observe" for p in evan.get("products", [])))

    harley = clients[1]["cfg"]
    check("Harley 关注池=2", len(harley.get("products", [])) == 2
          and {p.get("code") for p in harley.get("products", [])} == {"001480", "675123"},
          str([p.get("code") for p in harley.get("products", [])]))
    check("Harley 仅现金持仓", len(harley.get("holdings", [])) == 1
          and harley["holdings"][0]["type"] == "cash")
    check("Harley 无 tier_gap 覆盖", "tier_gap" not in harley.get("rules", {}).get("take_profit", {}))
    check("Harley 继承全局规则参数", harley.get("rules", {}).get("take_profit", {}).get("base", {}).get("equity") == 0.18)
    check("Harley 继承默认 target", harley.get("target", {}).get("equity", {}).get("base") == 0.4)
    check("Harley 继承 push", bool(harley.get("push", {}).get("serverchan_key")))

    nx = clients[2]["cfg"]
    check("NULL_Xue 关注池=4", len(nx.get("products", [])) == 4
          and {p.get("code") for p in nx.get("products", [])} == {"025687", "006195", "016347", "022720"},
          str([p.get("code") for p in nx.get("products", [])]))
    check("NULL_Xue 仅现金持仓", len(nx.get("holdings", [])) == 1
          and nx["holdings"][0]["type"] == "cash")

    jing = clients[3]["cfg"]
    check("Echo_Wang 空壳", len(jing.get("products", [])) == 0
          and len(jing.get("holdings", [])) == 1
          and jing["holdings"][0]["type"] == "cash")
    check("Echo_Wang 客户级push覆盖", jing.get("push", {}).get("serverchan_key", "").startswith("SCT392325"))
    check("Harley 客户级push覆盖", harley.get("push", {}).get("serverchan_key", "").startswith("SCT390799"))
    check("Evan 全局push继承", evan.get("push", {}).get("serverchan_key", "").startswith("SCT390761"))

    base = {"a": {"x": 1, "y": 2}, "l": [1, 2]}
    over = {"a": {"y": 3}, "l": [9]}
    out = main.deep_merge(base, over)
    check("merge 客户覆盖", out["a"] == {"x": 1, "y": 3} and out["l"] == [9])
    check("merge 不改原对象", base["a"] == {"x": 1, "y": 2} and base["l"] == [1, 2])

    old = {"target": {"equity": {"base": 0.3, "band": 0.05}}, "holdings": [], "products": []}
    oc = main.get_clients(old)
    check("旧配置 default 兼容", len(oc) == 1 and oc[0]["id"] == "default"
          and oc[0]["cfg"]["target"]["equity"]["base"] == 0.3)

    tmp = tempfile.mkdtemp(prefix="kb_test_")
    state_store.KB_DIR = tmp
    state_store.TRACES_PATH = os.path.join(tmp, "traces.json")
    state_store.REC_PATH = os.path.join(tmp, "recommendations.json")
    state_store.STATE_HISTORY_PATH = os.path.join(tmp, "state_history.json")

    state_store.record_trace({"code": "A", "action": "tp_1of3", "signal_date": "2026-08-06", "T_eff": 0.1},
                             client="Evan_Lei")
    state_store.record_trace({"code": "A", "action": "tp_1of3", "signal_date": "2026-08-06", "T_eff": 0.1},
                             client="Harley_Lei")
    state_store.record_trace({"code": "B", "action": "tp_dd", "signal_date": "2026-08-06", "T_eff": 0.1})
    evan_t = [t for t in state_store.kb_read_traces()
              if state_store._client_of(t, "Evan_Lei")]
    check("traces 旧条目归 Evan", len(evan_t) == 2, f"n={len(evan_t)}")
    recent_e = state_store.recent_tp_actions(days=5, client="Evan_Lei")
    recent_h = state_store.recent_tp_actions(days=5, client="Harley_Lei")
    check("recent_tp 隔离", "A" in recent_e and "B" in recent_e and "A" in recent_h
          and "B" not in recent_h)

    state_store.kb_append_recommendations([{"name": "医药", "type": "industry", "reason": "x"}],
                                          client="Evan_Lei")
    state_store.kb_append_recommendations([{"name": "医药", "type": "industry", "reason": "x"}],
                                          client="NULL_Xue")
    c_e = state_store.count_recent_recommendations("医药", days=30, client="Evan_Lei")
    c_n = state_store.count_recent_recommendations("医药", days=30, client="NULL_Xue")
    c_all = state_store.count_recent_recommendations("医药", days=30)
    check("推荐计数隔离", c_e == 1 and c_n == 1 and c_all == 2, f"e={c_e} n={c_n} all={c_all}")

    state_store.write_state_snapshot({"date": "2026-08-07", "orders": [], "total_mv": 100},
                                     client="Evan_Lei")
    state_store.write_state_snapshot({"date": "2026-08-07", "orders": [], "total_mv": 200},
                                     client="Harley_Lei")
    check("快照同日多客户共存", len(state_store.kb_read_state_history()) == 2)

    s_e = state_store.shadow_stats({}, client="Evan_Lei")
    s_h = state_store.shadow_stats({}, client="Harley_Lei")
    check("shadow_stats 隔离", s_e["signals"] == 2 and s_h["signals"] == 1,
          f"e={s_e['signals']} h={s_h['signals']}")

    print(f"\n通过 {passed} 项")
    return passed


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
