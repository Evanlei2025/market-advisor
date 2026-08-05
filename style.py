# -*- coding: utf-8 -*-
"""报告样式：银行 App 界面风格（招行红 + 白底高对比），适用于 Server酱（支持 HTML）"""

BANK_RED = "#C3272B"
BANK_DARK = "#3D3D3D"
BANK_GRAY = "#757575"
BANK_GREEN = "#00875A"


def style_report(md: str) -> str:
    """把纯 markdown 报告转成银行风 HTML 版"""
    out = []
    for line in md.split("\n"):
        s = line.rstrip()
        if not s:
            out.append("")
        elif s.startswith("## "):
            out.append(f'<font color="{BANK_RED}"><b>▍{s[3:].strip()}</b></font>')
        elif s.startswith("# "):
            out.append(f'<font color="{BANK_RED}"><b>{s[2:].strip()}</b></font>')
        elif s.startswith("> "):
            out.append(f'<font color="{BANK_DARK}">{s[2:].strip()}</font>')
        elif s.startswith("---"):
            out.append('<hr>')
        elif s.startswith("- **"):
            out.append(f'<font color="{BANK_DARK}"><b>{s[2:]}</b></font>')
        else:
            out.append(f'<font color="{BANK_DARK}">{s}</font>')
    return "\n".join(out)
