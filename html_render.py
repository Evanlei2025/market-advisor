# -*- coding: utf-8 -*-
"""完整版报告 → HTML 网页（极简 markdown 渲染，无第三方依赖）。
生成自包含单页：GitHub Pages 直接可展示。
"""
import html as html_lib
import re


def _inline(text):
    t = html_lib.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"\*(.+?)\*", r"<em>\1</em>", t)
    t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
    return t


def render(markdown_text, title="每日投顾报告"):
    """markdown → HTML 页面（标题/引用/列表/表格/分隔线/段落）。"""
    body = []
    in_list = False
    in_blockquote = False
    in_code = False

    def close_list():
        nonlocal in_list
        if in_list:
            body.append("</ul>")
            in_list = False

    def close_quote():
        nonlocal in_blockquote
        if in_blockquote:
            body.append("</blockquote>")
            in_blockquote = False

    for raw in markdown_text.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            if in_code:
                body.append("</pre>")
                in_code = False
            else:
                close_list(); close_quote()
                body.append("<pre>")
                in_code = True
            continue
        if in_code:
            body.append(html_lib.escape(line))
            continue
        if not line.strip():
            close_list(); close_quote()
            body.append("<p></p>")
            continue
        if line.startswith("# "):
            close_list(); close_quote()
            body.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.startswith("## "):
            close_list(); close_quote()
            body.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("### "):
            close_list(); close_quote()
            body.append(f"<h3>{_inline(line[4:])}</h3>")
        elif line.startswith("> "):
            close_list()
            if not in_blockquote:
                body.append("<blockquote>")
                in_blockquote = True
            body.append(_inline(line[2:]) + "<br/>")
        elif line.startswith("- ") or line.startswith("* "):
            close_quote()
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{_inline(line[2:])}</li>")
        elif re.match(r"^\d+\.\s", line):
            close_quote()
            close_list()
            body.append(f"<p>{_inline(re.sub(r'^\d+\.\s', '', line))}</p>")
        elif line.startswith("---"):
            close_list(); close_quote()
            body.append("<hr/>")
        else:
            close_list(); close_quote()
            body.append(f"<p>{_inline(line)}</p>")
    close_list(); close_quote()
    if in_code:
        body.append("</pre>")

    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html_lib.escape(title)}</title>
<style>
body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
       max-width: 860px; margin: 0 auto; padding: 16px; color: #222; line-height: 1.7; }}
h1 {{ font-size: 1.5em; border-bottom: 2px solid #eee; padding-bottom: 8px; }}
h2 {{ font-size: 1.2em; margin-top: 28px; color: #1a4f8b; }}
h3 {{ font-size: 1.05em; }}
blockquote {{ border-left: 4px solid #c8d8ec; margin: 8px 0; padding: 4px 12px;
             background: #f7fafd; color: #444; }}
ul {{ padding-left: 20px; }}
pre {{ background: #f5f5f5; padding: 10px; overflow-x: auto; }}
hr {{ border: none; border-top: 1px solid #ddd; margin: 24px 0; }}
code {{ background: #f0f0f0; padding: 1px 4px; border-radius: 3px; }}
</style>
</head>
<body>
{''.join(body)}
</body>
</html>"""
    return page
