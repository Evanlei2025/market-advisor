# -*- coding: utf-8 -*-
"""执行回执记录工具（V3 执行回执反馈机制的手动录入入口）。
用法：
  python record_feedback.py <client> <date> <status> [note]
  python record_feedback.py Evan_Lei 2026-08-10 已执行 "003504 部分卖出"
status 取值：已执行 / 部分 / 未执行（可传任意描述，仅做记录展示）。
记录写入 state.json 的 feedback 数组，报告「执行回执」板块显示最近一条。
"""
import sys

import state_store


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    client = sys.argv[1]
    date = sys.argv[2]
    status = sys.argv[3]
    note = " ".join(sys.argv[4:])
    state_store.record_feedback(client, date, status, note)
    rec = state_store.get_feedback(client, limit=1)
    if rec:
        r = rec[0]
        print(f"已记录：{r['client']} {r['date']} {r['status']}"
              + (f"（{r['note']}）" if r.get("note") else ""))
    else:
        print("记录失败：get_feedback 未返回（请检查 state.json 可写）")
        sys.exit(1)


if __name__ == "__main__":
    main()
