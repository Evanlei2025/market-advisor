# -*- coding: utf-8 -*-
"""报告样式：纯 Markdown 美化（Server酱微信通道只支持 Markdown，不支持 HTML）
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
