# -*- coding: utf-8 -*-
"""【已废弃，勿再接入】报告样式美化（早期推送美化方案）。
废弃原因（2026-08-07 技术治理确认）：
- style_report 会把 `## 标题` 转换为 `**标题**`（去掉 markdown 标题语法），
  接入会直接破坏 split_blocks（按 ## 切板块）、build_compact（推送裁剪）、
  html_render（HTML 渲染）对报告结构的全部假设；
- 该职责已由 html_render（完整版 HTML）+ build_compact（推送精简版）取代，
  main.py 的 `import style` 已删除。
历史功能说明（保留存档）：
- 加粗标题、引用块、分隔线营造银行 App 层次感；涨跌色用 emoji（🔴红涨 🟢绿跌）
- 中英数字间加空格，给微信渲染器提供更多断点，改善手机端生硬换行
"""
import re

_NUM_SPACE = [
    # 中文后跟数字（沪深300 → 沪深 300；近1周 → 近 1周）
    (re.compile(r"([\u4e00-\u9fff])([0-9])"), r"\1 \2"),
    # 数字后跟中文（300点 → 300 点）
    (re.compile(r"([0-9])([\u4e00-\u9fff])"), r"\1 \2"),
    # % 后接中文（21.9%最 → 21.9% 最）
    (re.compile(r"(%)([\u4e00-\u9fff])"), r"\1 \2"),
]


def _spaceify(s):
    for pat, rep in _NUM_SPACE:
        s = pat.sub(rep, s)
    return s


def style_report(md: str) -> str:
    out = []
    for line in md.split("\n"):
        s = line.rstrip()
        if not s:
            out.append("")
        elif s.startswith("## "):
            out.append(f"**▍{s[3:].strip()}**")
        elif s.startswith("# "):
            out.append(f"**{s[2:].strip()}**")
        elif s.startswith("> "):
            out.append(f"> {_spaceify(s[2:].strip())}")
        elif s.startswith("---"):
            out.append("")
            out.append("——————————————")
        elif s.startswith("- "):
            out.append(_spaceify(s))
        else:
            out.append(_spaceify(s))
    return "\n".join(out)
