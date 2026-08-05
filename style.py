# -*- coding: utf-8 -*-
"""报告样式：纯 Markdown 美化（Server酱微信通道只支持 Markdown，不支持 HTML）
用加粗标题、引用块、分隔线营造银行 App 的层次感；涨跌色用 emoji（🔴红涨 🟢绿跌）。
"""


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
            out.append(f"> {s[2:].strip()}")
        elif s.startswith("---"):
            out.append("")
            out.append("——————————————")
        else:
            out.append(s)
    return "\n".join(out)
