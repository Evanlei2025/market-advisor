# -*- coding: utf-8 -*-
"""完整版报告 → HTML 网页（极简 markdown 渲染，无第三方依赖）。
生成自包含单页：GitHub Pages 直接可展示。
6.1：支持 markdown 表格；charts 参数注入 Chart.js 图表（净值曲线/仓位饼图/估值条形图）。
"""
import html as html_lib
import json
import re

_TABLE_ROW = re.compile(r"^\|(.+)\|$")
_SEP_ROW = re.compile(r"^\|[\s:|-]+\|$")


def _inline(text):
    t = html_lib.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"\*(.+?)\*", r"<em>\1</em>", t)
    t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
    return t


def _charts_html(charts):
    """Chart.js 图表容器（CDN 加载失败时页面正文不受影响）"""
    if not charts:
        return ""
    boxes, init = [], []
    if charts.get("valuation"):
        boxes.append('<div class="chart-box"><h3>估值温度（PE 十年分位 %）</h3>'
                     '<canvas id="chVal"></canvas></div>')
        init.append(
            "new Chart(document.getElementById('chVal'),{type:'bar',"
            "data:{labels:%s,datasets:[{data:%s,backgroundColor:'#8ab4d8'}]},"
            "options:{plugins:{legend:{display:false}}}})" % (
                json.dumps(charts["valuation"]["labels"], ensure_ascii=False),
                json.dumps(charts["valuation"]["values"])))
    if charts.get("allocation"):
        boxes.append('<div class="chart-box"><h3>组合仓位构成（市值口径）</h3>'
                     '<canvas id="chAlloc"></canvas></div>')
        init.append(
            "new Chart(document.getElementById('chAlloc'),{type:'doughnut',"
            "data:{labels:%s,datasets:[{data:%s}]}})" % (
                json.dumps(charts["allocation"]["labels"], ensure_ascii=False),
                json.dumps(charts["allocation"]["values"])))
    if charts.get("nav"):
        nv = charts["nav"]
        ds = ("{label:'组合',data:%s,borderColor:'#1a4f8b',fill:false,pointRadius:0,tension:0.1}"
              % json.dumps(nv["values"]))
        if nv.get("bench"):
            ds += (",{label:'基准',data:%s,borderColor:'#c0392b',fill:false,pointRadius:0,tension:0.1}"
                   % json.dumps(nv["bench"]))
        boxes.append('<div class="chart-box"><h3>组合净值走势（近250日，起点=100）</h3>'
                     '<canvas id="chNav"></canvas></div>')
        init.append(
            "new Chart(document.getElementById('chNav'),{type:'line',"
            "data:{labels:%s,datasets:[%s]},"
            "options:{scales:{x:{ticks:{maxTicksLimit:8}}}}})" % (
                json.dumps(nv["labels"]), ds))
    if not boxes:
        return ""
    return ("<script src=\"https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js\"></script>\n"
            "<div class=\"charts\">" + "\n".join(boxes) + "</div>\n"
            "<script>document.addEventListener('DOMContentLoaded',function(){"
            + ";".join(init) + "});</script>")


def render(markdown_text, title="每日投顾报告", charts=None):
    """markdown → HTML 页面（标题/引用/列表/表格/分隔线/段落/图表）。"""
    body = []
    in_list = False
    in_blockquote = False
    in_code = False
    in_table = False
    table_rows = []

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

    def close_table():
        nonlocal in_table, table_rows
        if in_table:
            if table_rows:
                head = table_rows[0]
                body.append("<table><thead><tr>" + "".join(
                    f"<th>{_inline(c.strip())}</th>" for c in head) + "</tr></thead><tbody>")
                for row in table_rows[1:]:
                    body.append("<tr>" + "".join(
                        f"<td>{_inline(c.strip())}</td>" for c in row) + "</tr>")
                body.append("</tbody></table>")
            in_table = False
            table_rows = []

    for raw in markdown_text.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            if in_code:
                body.append("</pre>")
                in_code = False
            else:
                close_list(); close_quote(); close_table()
                body.append("<pre>")
                in_code = True
            continue
        if in_code:
            body.append(html_lib.escape(line))
            continue
        m = _TABLE_ROW.match(line)
        if m and not _SEP_ROW.match(line):
            cells = [c for c in m.group(1).split("|")]
            if not in_table:
                close_list(); close_quote()
                in_table = True
                table_rows = []
            table_rows.append(cells)
            continue
        if _TABLE_ROW.match(line) and _SEP_ROW.match(line):
            continue  # 表头分隔行跳过
        if in_table:
            close_table()
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
            close_list(); close_table()
            if not in_blockquote:
                body.append("<blockquote>")
                in_blockquote = True
            body.append(_inline(line[2:]) + "<br/>")
        elif line.startswith("- ") or line.startswith("* "):
            close_quote(); close_table()
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{_inline(line[2:])}</li>")
        elif re.match(r"^\d+\.\s", line):
            close_quote()
            close_list()
            close_table()
            body.append(f"<p>{_inline(re.sub(r'^\d+\.\s', '', line))}</p>")
        elif line.startswith("---"):
            close_list(); close_quote(); close_table()
            body.append("<hr/>")
        else:
            close_list(); close_quote(); close_table()
            body.append(f"<p>{_inline(line)}</p>")
    close_list(); close_quote(); close_table()
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
table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 0.95em; }}
th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; }}
th {{ background: #f0f5fb; }}
.charts {{ display: flex; flex-wrap: wrap; gap: 16px; margin: 16px 0; }}
.chart-box {{ flex: 1 1 320px; min-width: 300px; border: 1px solid #eee;
             border-radius: 8px; padding: 12px; background: #fcfcfc; }}
.chart-box canvas {{ width: 100% !important; height: 260px !important; }}
</style>
</head>
<body>
{_charts_html(charts)}
{''.join(body)}
</body>
</html>"""
    return page
