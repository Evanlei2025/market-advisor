# -*- coding: utf-8 -*-
"""完整版报告 → HTML 网页（iOS 原生质感渲染，无第三方依赖）。

自包含单页：GitHub Pages 直接可展示。
设计语言：Apple HIG —— System Colors / Inset Grouped 分组卡片 / 深色模式跟随系统 /
毛玻璃导航条 / 8pt 间距网格 / tabular-nums 数字 / prefers-reduced-motion。

7.1 大改版：
- 设计令牌双主题（CSS 变量，@media prefers-color-scheme 跟随系统）
- 章节卡片化（h2 分组头 + Inset Grouped 白色圆角卡）+ 章节折叠/展开（默认展开
  「今日一句话/理财产品跟踪/今日跟投指令」其余折叠）+ sticky 胶囊目录条（当前章节高亮）
- 「今日一句话」整卡蓝色渐变浸入（hero，方案 A：渐变+蓝阴影，无硬切）
- 文字数据可视化（不改 markdown 内容，纯渲染层解析）：
  分位温度条（PE/利率/股债性价比等「分位 N%」→ 蓝-橙-红色阶条，插入所在段尾）
  区间收益条（产品「近X ±Y%」→ 红涨绿跌横向条）
  组合诊断数字格（波动率/回撤/VaR95 → iOS 健康 App 风格数字卡）
  涨跌着色（🟢/🔴 → 绿/红语义色）、规则信号徽章（✅/👀/⚠️ → 彩色 pill）
- 「｜」分隔一律换行（列表行拆段成行 / 段落·表格·引用内换行；推送 md 不受影响）
- Chart.js 深色自适应（脚本读 CSS 变量取色）
"""
import html as html_lib
import json
import re

_TABLE_ROW = re.compile(r"^\|(.+)\|$")
_SEP_ROW = re.compile(r"^\|[\s:|-]+\|$")

# ---------- 可视化行模式（纯渲染层，不动 markdown） ----------
# 「分位 N%」首次出现 → 温度条（估值/利率/性价比分位等）
_FENWEI = re.compile(r"分位\s*(\d{1,3}(?:\.\d+)?)\s*[%％]")
# 区间收益行：区间收益：近1周 +2.2% ｜ 近1月 -3.9% ...（该行保留 chips 卡片，不拆段）
_RET_ROW = re.compile(r"区间收益\s*[：:]\s*(.+)$")
_RET_ITEM = re.compile(r"(近1周|近1月|近3月|近6月|近1年)\s*([+-]?\d+(?:\.\d+)?)\s*%")
# 组合诊断数字格：- 年化波动率 8.3%｜人话：...
_METRIC_ROW = re.compile(
    r"^(- )?(年化波动率|250日最大回撤|日度 VaR95)\s+([+-]?\d+(?:\.\d+)?)\s*%"
    r"([^｜|]*?)\s*[｜|]人话\s*[：:]?\s*(.*)$")
# 涨跌着色：🟢/🔴 紧跟 ±N%
_UPDOWN = re.compile(r"([🟢🔴])\s*([+-]?\d+(?:\.\d+)?\s*%)")
# 规则信号徽章：规则信号：✅ **持有**
_SIGNAL = re.compile(r"规则信号\s*[：:]\s*(✅|👀|⚠️)\s*\*\*(.+?)\*\*")
# 产品标题行（裸段落，以 ** 开头，非术语速查列表行）
_PROD_ROW = re.compile(r"^\*\*(.+?)\*\*(.*)$")
# 注释弱化行
_NOTE_LI = re.compile(r"^(注[：:]|执行窗口)")
_NOTE_P = re.compile(r"^\*(.+)$")

_TERM_ROW = re.compile(r"^\*\*(.+?)\*\*[：:]\s*")

# ---------- 活指标卡片化（变量=蓝卡；静态文案=默认色） ----------
# 主卡：「：」锚或白名单行词，后接数值/短文字+数字 → 数值区 .val 卡
_VAL_LEAD = re.compile(r"^[+-]?[\d¥]")
_VAL_LINE = re.compile(r"^(净值|近1年最大回撤|同类排名|费用|权益暴露|组合市值|上次反馈)\s*")
_VAL_CUT_CHARS = "，；→⚠️"
# 细粒度卡（决策依据行）：PE分位 N%、权益目标/上限 = N%
_VAL_FINE_PE = re.compile(r"PE分位\s*([\d.]+%[^，；→]*)")
_VAL_FINE_EQ = re.compile(r"(?:权益目标|权益上限)\s*[=＝]?\s*[^%，；→]{0,12}?(\d+%?)")

# 默认展开的章节（其余折叠；目录可随时点开）
_OPEN_GROUPS = {"今日一句话", "理财产品跟踪", "今日跟投指令"}

# ---------- 多级目录分组（新章节自动归入兜底组「参考附录」） ----------
_TOC_CATEGORIES = [
    ("核心指令", ["今日一句话", "今日跟投指令", "今日指令解读", "⚠ 异常事件预警", "异常事件预警", "执行回执"]),
    ("市场概况", ["市场速览", "估值温度", "债市与利率", "宏观与资金面", "黄金"]),
    ("持仓分析", ["理财产品跟踪", "组合诊断", "决策依据"]),
    ("参考附录", ["指标温度表", "术语速查", "系统运行状态"]),
]
_TOC_FALLBACK = "参考附录"


def _scan_counts(markdown_text):
    """预扫描：每章节的裸 ** 产品标题行数 + 术语速查组行数（折叠 open 决策与按钮文案用）"""
    prods, terms = {}, {}
    cur = None
    for ln in markdown_text.splitlines():
        s = ln.strip()
        if s.startswith("## "):
            cur = s[3:].strip()
            prods.setdefault(cur, 0)
        elif cur is not None:
            if s.startswith("**") and not s.startswith("- ") and not s.startswith(">"):
                prods[cur] = prods.get(cur, 0) + 1
            elif cur == "术语速查" and s.startswith("- "):
                terms[cur] = terms.get(cur, 0) + 1
    return prods, terms


def _build_toc_panel(groups):
    """多级目录面板 HTML（悬浮按钮 + 按逻辑分组链接）；无章节（入口页）返回空"""
    if not groups:
        return ""
    cats, used = [], set()
    for cat, pats in _TOC_CATEGORIES:
        items = [g for g in groups if g not in used
                 and any(g.startswith(p) or p in g for p in pats)]
        if items:
            cats.append((cat, items))
            used.update(items)
    rest = [g for g in groups if g not in used]
    if rest:
        cats.append((_TOC_FALLBACK, rest))
    idx = {g: i + 1 for i, g in enumerate(groups)}
    parts = ['<aside class="toc-panel">',
             '<button type="button" class="toc-toggle" aria-label="目录">目录</button>',
             '<nav class="toc-nav">']
    for cat, items in cats:
        parts.append(f'<details class="toc-cat" open><summary>{html_lib.escape(cat)}</summary>')
        for g in items:
            parts.append(f'<a href="#sec-{idx[g]}">{html_lib.escape(g)}</a>')
        parts.append("</details>")
    parts.append("</nav></aside>")
    return "".join(parts)


def _pct_hue(n):
    """分位 → 温度色相：0%=210(蓝) … 100%=0(红)，iOS 财富温度约定"""
    return max(0, min(210, int(210 - 2.1 * n)))


def _inline(text):
    t = html_lib.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"\*(.+?)\*", r"<em>\1</em>", t)
    t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
    t = t.replace("｜", "<br/>")
    return t


def _li_meta(raw):
    """信号徽章截取：返回 (prefix_html, 剩余 raw)"""
    m = _SIGNAL.search(raw)
    if not m:
        return "", raw
    cls = {"✅": "b-ok", "👀": "b-watch", "⚠️": "b-warn"}.get(m.group(1), "b-warn")
    prefix = "规则信号：<span class='badge %s'>%s</span>" % (cls, _inline(m.group(2)))
    return prefix, (raw[:m.start()] + raw[m.end():])


def _pct_html(seg):
    """分位温度条 HTML（含分位段的段尾插入）；「PE分位」由细卡覆盖，跳过温度条"""
    m = _FENWEI.search(seg)
    if not m:
        return ""
    if m.start() > 0 and seg[m.start() - 1] == "E":
        return ""
    n = float(m.group(1))
    if n > 100:
        return ""
    hue = _pct_hue(n)
    return ("<span class='pct'><span class='pct-fill' "
            "style='width:%.0f%%;background:hsl(%d,100%%,55%%)'></span></span>"
            "<span class='pct-num'>%.0f%%</span>" % (n, hue, n))


def _cut_val(s):
    """活值区截断：深度 0 处遇截断符停止；括号内（千位逗号/日期等）跳过"""
    depth = 0
    for i, ch in enumerate(s):
        if ch in "（(":
            depth += 1
        elif ch in "）)":
            depth = max(0, depth - 1)
        elif depth == 0 and ch in _VAL_CUT_CHARS:
            return s[:i], s[i:]
    return s, ""


def _val_head(rest, max_pref):
    """数值区起点判定：数字/符号开头，或 ≤max_pref 字文字前缀后紧跟数字"""
    s = rest.strip()
    if not s:
        return None
    if _VAL_LEAD.match(s):
        return s
    if re.match(r"^[^\d%s]{1,%d}?[+-]?\d" % (re.escape(_VAL_CUT_CHARS), max_pref), s):
        return s
    return None


def _val_split(seg, max_pref):
    """主卡拆分：返回 (标签, 数值区, 剩余) 或 None；「分位 N%」位置让给温度条"""
    idx = -1
    for c in "：:":
        i = seg.find(c)
        if i >= 0 and (idx < 0 or i < idx):
            idx = i
    if 0 < idx <= 14:
        label, rest = seg[:idx], seg[idx + 1:]
    else:
        m = _VAL_LINE.match(seg)
        if not m:
            return None
        label, rest = m.group(1), seg[m.end():]
    head = _val_head(rest, max_pref)
    if head is None:
        return None
    val, tail = _cut_val(head)
    if not val or _FENWEI.search(val):
        return None
    return label, val, tail


def _ud(text):
    return _UPDOWN.sub(lambda mm: "<span class='ud %s'>%s</span>" % (
        "up" if mm.group(1) == "🔴" else "dn", mm.group(2)), text)


def _seg_html(seg, cards):
    """段 → HTML：主卡（标签+蓝卡同行）+ 细卡 + 涨跌着色 + 温度条"""
    if cards:
        m = _val_split(seg, 8)
        if m:
            label, val, tail = m
            h = _ud(_inline(label))
            vh = _ud(_inline(val))
            th = _ud(_inline(tail)) if tail else ""
            out = h + "<span class='val'>" + vh + "</span>" + th
        else:
            out = _ud(_inline(seg))
    else:
        out = _ud(_inline(seg))
    out = _VAL_FINE_PE.sub(lambda mm: "PE分位<span class='val'>" + _inline(mm.group(1)) + "</span>", out)
    out = _VAL_FINE_EQ.sub(
        lambda mm: mm.group(0)[:len(mm.group(0)) - len(mm.group(1))] + "<span class='val'>" + _inline(mm.group(1)) + "</span>",
        out)
    return out


def _render_li_body(line, cards=True):
    """列表行内容：徽章 + 涨跌着色 + 「｜」拆段成行 + 活值卡 + 分位条 + 区间收益 chips"""
    raw = line[2:].strip()
    prefix_html, raw = _li_meta(raw)
    m = _RET_ROW.search(raw)
    if m:
        out = _inline(raw)
        chips = []
        for label, num in _RET_ITEM.findall(m.group(1)):
            v = float(num)
            w = max(6, min(60, abs(v) * 4))
            cls = "pos" if v >= 0 else "neg"
            shown = num if num.startswith(("+", "-")) else ("+" if v >= 0 else "") + num
            chips.append(
                "<span class='chip'><i class='%s' style='width:%dpx'></i>"
                "<b>%s %s%%</b></span>" % (cls, w, label, shown))
        if chips:
            out = _RET_ROW.sub("", out, count=1)
            out += "<span class='ret-chips'>" + "".join(chips) + "</span>"
        return prefix_html + out
    lines = []
    for seg in (s.strip() for s in raw.split("｜")):
        if not seg:
            continue
        h = _seg_html(seg, cards)
        h += _pct_html(seg)
        lines.append(f"<span class='li-line'>{h}</span>")
    return prefix_html + "".join(lines)


def _render_p(line):
    """裸段落行：说明行 → .note；其余普通段落（产品标题行由 render 主循环拦截处理）"""
    raw = line.strip()
    if _NOTE_P.match(raw) or raw.startswith("*"):
        return "<p class='note'>%s</p>" % _inline(raw)
    return "<p>%s</p>" % _inline(raw)


def _charts_html(charts):
    """Chart.js 图表容器（CDN 加载失败时页面正文不受影响）"""
    if not charts:
        return ""
    boxes, init = [], []
    if charts.get("valuation"):
        vals = charts["valuation"]["values"]
        colors = ",".join("'hsl(%d,100%%,55%%)'" % _pct_hue(v) for v in vals)
        boxes.append('<div class="chart-box"><h3>估值温度（PE 十年分位 %）</h3>'
                     '<canvas id="chVal"></canvas></div>')
        init.append(
            "new Chart(document.getElementById('chVal'),{type:'bar',"
            "data:{labels:%s,datasets:[{data:%s,backgroundColor:[%s],"
            "borderRadius:6,maxBarThickness:34}]},"
            "options:{plugins:{legend:{display:false}},"
            "scales:{y:{beginAtZero:true,max:100,grid:{color:cGrid},"
            "ticks:{color:cFg,font:{size:11}}},"
            "x:{grid:{display:false},ticks:{color:cFg,font:{size:12}}}}},"
            "animation:{duration:400}})" % (
                json.dumps(charts["valuation"]["labels"], ensure_ascii=False),
                json.dumps(vals), colors))
    if charts.get("allocation"):
        boxes.append('<div class="chart-box"><h3>组合仓位构成（市值口径）</h3>'
                     '<canvas id="chAlloc"></canvas></div>')
        init.append(
            "new Chart(document.getElementById('chAlloc'),{type:'doughnut',"
            "data:{labels:%s,datasets:[{data:%s,backgroundColor:[cBlue,cGreen,cOrange,cPurple,cRed],"
            "borderColor:cCard,borderWidth:3}]},"
            "options:{cutout:'62%%',plugins:{legend:{position:'bottom',"
            "labels:{color:cFg,font:{size:12},padding:12,usePointStyle:true,pointStyleWidth:8}}},"
            "animation:{duration:400}}})" % (
                json.dumps(charts["allocation"]["labels"], ensure_ascii=False),
                json.dumps(charts["allocation"]["values"])))
    if charts.get("nav"):
        nv = charts["nav"]
        ds = ("{label:'组合',data:%s,borderColor:cBlue,backgroundColor:cBlueFill,"
              "fill:true,borderWidth:2,pointRadius:0,tension:0.35}" % json.dumps(nv["values"]))
        if nv.get("bench"):
            ds += (",{label:'基准',data:%s,borderColor:cOrange,fill:false,borderWidth:2,"
                   "borderDash:[5,4],pointRadius:0,tension:0.35}" % json.dumps(nv["bench"]))
        boxes.append('<div class="chart-box"><h3>组合净值走势（近250日，起点=100）</h3>'
                     '<canvas id="chNav"></canvas></div>')
        init.append(
            "new Chart(document.getElementById('chNav'),{type:'line',"
            "data:{labels:%s,datasets:[%s]},"
            "options:{plugins:{legend:{labels:{color:cFg,font:{size:12},usePointStyle:true,"
            "pointStyleWidth:8,boxHeight:3}}},"
            "scales:{x:{grid:{display:false},ticks:{color:cFg,font:{size:10},maxTicksLimit:6}},"
            "y:{grid:{color:cGrid},ticks:{color:cFg,font:{size:11}}}},"
            "animation:{duration:500}}})" % (
                json.dumps(nv["labels"]), ds))
    if not boxes:
        return ""
    return ("<script src=\"https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js\"></script>\n"
            "<script>\nvar cs=getComputedStyle(document.body);\n"
            "var cGrid=cs.getPropertyValue('--chart-grid').trim()||'#E5E5EA';\n"
            "var cFg=cs.getPropertyValue('--chart-fg').trim()||'#8E8E93';\n"
            "var cCard=cs.getPropertyValue('--card').trim()||'#FFFFFF';\n"
            "var cBlue=cs.getPropertyValue('--blue').trim()||'#007AFF';\n"
            "var cGreen=cs.getPropertyValue('--green').trim()||'#34C759';\n"
            "var cOrange=cs.getPropertyValue('--orange').trim()||'#FF9500';\n"
            "var cPurple='#AF52DE';\nvar cRed=cs.getPropertyValue('--red').trim()||'#FF3B30';\n"
            "var cBlueFill=cBlue+'1F';\n"
            "</script>\n"
            "<div class=\"charts\">" + "\n".join(boxes) + "</div>\n"
            "<script>document.addEventListener('DOMContentLoaded',function(){"
            + ";".join(init) + "});</script>")


_CSS = """
:root {
  --bg: #F2F2F7; --card: #FFFFFF; --card2: #F7F7FA;
  --text: #1C1C1E; --text2: #3A3A3C; --text3: #8E8E93; --text4: #C7C7CC;
  --sep: #E5E5EA; --sep2: rgba(60,60,67,.12);
  --blue: #007AFF; --green: #34C759; --orange: #FF9500; --red: #FF3B30;
  --nav-bg: rgba(246,246,250,.72);
  --shadow: 0 1px 3px rgba(0,0,0,.06), 0 6px 16px rgba(0,0,0,.05);
  --chart-grid: #E5E5EA; --chart-fg: #8E8E93;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #000000; --card: #1C1C1E; --card2: #242426;
    --text: #F2F2F7; --text2: #D1D1D6; --text3: #AEAEB2; --text4: #48484A;
    --sep: #38383A; --sep2: rgba(84,84,88,.4);
    --blue: #0A84FF; --green: #32D74B; --orange: #FF9F0A; --red: #FF453A;
    --nav-bg: rgba(22,22,24,.72);
    --shadow: 0 1px 2px rgba(0,0,0,.5), 0 6px 18px rgba(0,0,0,.35);
    --chart-grid: #38383A; --chart-fg: #AEAEB2;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Microsoft YaHei", sans-serif;
  background: var(--bg); color: var(--text);
  margin: 0; line-height: 1.55; font-size: 17px;
  -webkit-font-smoothing: antialiased;
  animation: fadeIn .3s ease both;
}
@keyframes fadeIn { from { opacity: 0 } to { opacity: 1 } }
.navbar {
  position: sticky; top: 0; z-index: 10;
  background: var(--nav-bg);
  -webkit-backdrop-filter: blur(20px) saturate(1.8); backdrop-filter: blur(20px) saturate(1.8);
  border-bottom: .5px solid var(--sep2);
}
@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {
  .navbar { background: var(--bg); }
}
.nav-inner { width: min(100% - 40px, 1150px); margin: 0 auto; padding: 10px 0;
             display: flex; align-items: baseline; gap: 10px; }
.nav-brand { font-size: 17px; font-weight: 700; letter-spacing: .2px; }
.nav-sub { font-size: 13px; color: var(--text3); }
.page { width: min(100% - 40px, 1150px); margin: 0 auto; padding: 20px 0 48px; }
h1 {
  font-size: 34px; line-height: 1.15; font-weight: 700; letter-spacing: -0.3px;
  margin: 10px 4px 4px; padding: 0; border: none;
}
/* ---------- 多级目录面板（悬浮按钮 + 分组导航） ---------- */
.toc-panel { position: fixed; bottom: 20px; right: 20px; z-index: 20; }
.toc-toggle {
  width: 48px; height: 48px; border-radius: 50%;
  background: var(--blue); color: #fff; border: none;
  font-size: 14px; font-weight: 600; cursor: pointer;
  box-shadow: 0 4px 14px rgba(0,122,255,.3);
  -webkit-tap-highlight-color: transparent;
}
.toc-nav {
  display: none; position: absolute; bottom: 56px; right: 0;
  width: 224px; max-height: 70vh; overflow-y: auto;
  background: var(--card); border-radius: 16px;
  box-shadow: var(--shadow); padding: 10px 0;
}
.toc-panel.open .toc-nav { display: block; }
.toc-cat { padding: 0 12px; }
.toc-cat summary {
  font-size: 13px; font-weight: 700; color: var(--text2);
  padding: 8px 4px 6px; cursor: pointer; list-style: none;
}
.toc-cat summary::-webkit-details-marker { display: none; }
.toc-cat summary::before { content: "▸ "; font-size: 11px; color: var(--text3); }
.toc-cat[open] summary::before { content: "▾ "; }
.toc-cat a {
  display: block; font-size: 13px; color: var(--text3);
  padding: 6px 4px 6px 18px; text-decoration: none;
  border-radius: 8px;
}
.toc-cat a:hover { background: var(--card2); color: var(--text); }
@media (min-width: 1400px) {
  .toc-panel { position: fixed; top: 100px; right: 40px; bottom: auto; }
  .toc-toggle { display: none; }
  .toc-nav { display: block; position: static; bottom: auto; width: 230px; }
}
@media (max-width: 640px) {
  .toc-panel { bottom: 14px; right: 14px; }
}
/* ---------- 章节（折叠手风琴） ---------- */
.group-h {
  font-size: 15px; font-weight: 600; color: var(--text);
  margin: 22px 8px 8px; padding: 2px 4px; cursor: pointer;
  display: flex; align-items: center; gap: 6px;
  -webkit-tap-highlight-color: transparent;
  scroll-margin-top: 96px;
  user-select: none;
}
.group-h .chev {
  font-size: 12px; color: var(--text3); transition: transform .25s ease;
  display: inline-block;
}
.group-h.open .chev { transform: rotate(90deg); }
.group {
  background: var(--card); border-radius: 16px; box-shadow: var(--shadow);
  overflow: hidden; display: none;
  animation: rise .4s ease both;
}
.group.open { display: block; }
.group.hero {
  background: linear-gradient(180deg, rgba(0,122,255,.16), var(--card) 82%);
  box-shadow: 0 2px 10px rgba(0,122,255,.14), 0 10px 28px rgba(0,122,255,.08);
}
@media (prefers-color-scheme: dark) {
  .group.hero { background: linear-gradient(180deg, rgba(10,132,255,.30), var(--card) 82%); }
}
@keyframes rise { from { opacity: 0; transform: translateY(8px) } to { opacity: 1; transform: none } }
.group h3 { font-size: 13px; font-weight: 600; color: var(--text3);
            margin: 0; padding: 12px 16px 0; letter-spacing: .2px; }
.group > p { margin: 0; padding: 10px 16px; }
.group > p:empty { display: none; }
.group > p + p { border-top: .5px solid var(--sep2); }
/* 产品折叠块（details/summary 原生折叠） */
.prod-detail { border-radius: 12px; margin: 6px 12px; }
.prod-detail > summary {
  display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;
  background: var(--card2); font-size: 16px; font-weight: 700;
  border-radius: 12px; padding: 10px 14px; cursor: pointer;
  list-style: none; -webkit-tap-highlight-color: transparent;
}
.prod-detail > summary::-webkit-details-marker { display: none; }
.prod-detail > summary::after {
  content: "▾"; margin-left: auto; font-size: 12px; color: var(--text3);
  align-self: center;
}
.prod-detail:not([open]) > summary::after { content: "▸"; }
.prod-detail > summary strong { font-size: 16px; font-weight: 700; }
.prod-detail > summary .prod-type { font-size: 13px; font-weight: 500; color: var(--text3); }
.prod-detail > ul { border-top: .5px solid var(--sep2); margin-top: 0; }
/* 术语速查折叠 */
.fold-section { padding: 4px 0; }
.fold-btn {
  font-size: 14px; color: var(--blue); cursor: pointer;
  font-weight: 600; list-style: none; padding: 12px 16px;
  -webkit-tap-highlight-color: transparent;
}
.fold-btn::-webkit-details-marker { display: none; }
.fold-btn::after { content: " ▸"; font-size: 12px; }
.fold-section[open] .fold-btn::after { content: " ▾"; }
ul { list-style: none; margin: 0; padding: 0; }
li {
  display: flex; flex-direction: column; gap: 2px;
  padding: 12px 16px; font-size: 15px;
}
li + li { border-top: .5px solid var(--sep2); }
.li-line { display: block; line-height: 1.5; }
.li-line + .li-line { margin-top: 4px; }
li .pct { display: inline-block; vertical-align: middle; width: 88px; height: 6px;
          border-radius: 999px; background: var(--sep); overflow: hidden; margin-left: 6px; }
li .pct-fill { display: block; height: 100%; border-radius: 999px;
               transition: width .5s ease; }
li .pct-num { display: inline-block; vertical-align: middle; min-width: 40px;
              text-align: right; font-size: 13px; font-weight: 600; color: var(--text3);
              font-variant-numeric: tabular-nums; margin-left: 4px; }
li .ud { font-weight: 600; font-variant-numeric: tabular-nums; }
li .ud.up { color: var(--red); }
li .ud.dn { color: var(--green); }
strong { font-weight: 600; }
/* 活指标短卡（变量=蓝；静态文案=默认色；卡内涨跌红绿优先） */
.val {
  display: inline-block; vertical-align: baseline;
  background: rgba(0, 122, 255, .09); color: var(--blue);
  border-radius: 8px; padding: 1px 8px; font-weight: 600;
  font-variant-numeric: tabular-nums; white-space: normal;
}
.val .ud.up { color: var(--red); }
.val .ud.dn { color: var(--green); }
.val em { font-style: normal; }
@media (prefers-color-scheme: dark) {
  .val { background: rgba(10, 132, 255, .20); color: #0A84FF; }
}
.badge { display: inline-block; padding: 2px 10px; border-radius: 999px;
         font-size: 13px; font-weight: 600; margin-left: 4px; }
.b-ok { background: rgba(52,199,89,.14); color: var(--green); }
.b-watch { background: rgba(255,149,0,.16); color: var(--orange); }
.b-warn { background: rgba(255,59,48,.13); color: var(--red); }
.ret-chips { flex: 0 0 auto; display: flex; gap: 14px; align-items: flex-end;
             margin-top: 6px; padding: 8px 10px; border-radius: 12px;
             background: var(--card2); align-self: flex-start; }
.chip { display: flex; flex-direction: column; align-items: center; gap: 3px; }
.chip b { font-size: 11px; font-weight: 600; color: var(--text2);
          font-variant-numeric: tabular-nums; }
.chip i { height: 4px; border-radius: 999px; min-width: 6px; }
.chip i.pos { background: var(--red); }
.chip i.neg { background: var(--green); }
.warn-line { color: var(--red); }
.warn-line strong { color: var(--red); }
.note, .note em { color: var(--text3); font-size: 14px; }
.metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
               gap: 1px; background: var(--sep2); }
.metric { background: var(--card); padding: 14px 16px; }
.metric .m-val { font-size: 26px; font-weight: 700; letter-spacing: -0.4px;
                 font-variant-numeric: tabular-nums; }
.metric .m-label { font-size: 13px; color: var(--text3); margin-top: 2px; }
.metric .m-sub { font-size: 11px; color: var(--text4); }
.metric .m-note { font-size: 13px; color: var(--text2); margin-top: 8px; line-height: 1.45; }
blockquote {
  margin: 0; padding: 14px 16px; font-size: 15px;
  border-left: 3px solid var(--blue); border-radius: 0 12px 12px 0;
  background: rgba(0,122,255,.08); color: var(--text);
}
.group > blockquote { border-radius: 0; }
.group.hero > blockquote {
  border-left: none; background: none; border-radius: 0;
  padding: 20px; font-size: 17px; font-weight: 600;
}
table { border-collapse: collapse; width: 100%; margin: 0; font-size: 14px; }
thead th {
  font-size: 12px; font-weight: 600; color: var(--text3); text-align: left;
  padding: 8px 16px; background: var(--card2);
}
tbody td { padding: 11px 16px; border-top: .5px solid var(--sep2);
           vertical-align: top; }
tbody tr:first-child td { border-top: none; }
td:first-child { font-weight: 600; }
pre { margin: 0; padding: 12px 16px; background: var(--card2); overflow-x: auto;
      font-size: 13px; }
hr { border: none; margin: 0; }
.charts { display: flex; flex-wrap: wrap; gap: 12px; padding: 0 16px; }
.chart-box { flex: 1 1 320px; min-width: 280px; border-radius: 12px;
             padding: 12px; background: var(--card2); }
.chart-box canvas { width: 100% !important; height: 240px !important; }
.chart-box h3 { padding: 4px 4px 8px; }
/* 入口页（无分组）：ul 直接成卡片，链接行带 chevron */
.page > ul { background: var(--card); border-radius: 16px; box-shadow: var(--shadow);
             overflow: hidden; margin: 16px 0; }
.page > ul a { display: flex; align-items: center; justify-content: space-between;
               text-decoration: none; color: var(--text); }
.page > ul a::after { content: "›"; color: var(--text4); font-size: 22px;
                      font-weight: 400; margin-left: 8px; }
code { background: var(--card2); padding: 1px 6px; border-radius: 6px; font-size: 13px; }
@media (max-width: 640px) {
  h1 { font-size: 30px; }
  .page { width: min(100% - 24px, 1150px); padding: 14px 0 40px; }
  .nav-inner { width: min(100% - 24px, 1150px); padding: 10px 0; }
}
@media (prefers-reduced-motion: reduce) {
  body, .group { animation: none; }
  .pct-fill { transition: none; }
  .group-h .chev { transition: none; }
}
"""

_UI_JS = """
<script>
document.addEventListener('DOMContentLoaded',function(){
  var open = {};
  document.querySelectorAll('.group.open').forEach(function(g){ open[g.id] = 1; });
  function sync(h){
    var id = h.id, on = !!open[id];
    h.classList.toggle('open', on);
    var g = document.getElementById('g' + id);
    if (g) g.classList.toggle('open', on);
  }
  document.querySelectorAll('.group-h').forEach(function(h){
    sync(h);
    h.addEventListener('click', function(){
      open[h.id] = open[h.id] ? 0 : 1; sync(h);
    });
  });
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var tp = document.querySelector('.toc-panel');
  if (tp) {
    var tgl = tp.querySelector('.toc-toggle');
    if (tgl) tgl.addEventListener('click', function(){ tp.classList.toggle('open'); });
    tp.querySelectorAll('.toc-cat a').forEach(function(a){
      a.addEventListener('click', function(){
        var id = a.getAttribute('href').slice(1);
        var h = document.getElementById(id);
        if (h && !open[id]) { open[id] = 1; sync(h); }
        if (h) h.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
        tp.classList.remove('open');
      });
    });
  }
});
</script>
"""


def render(markdown_text, title="每日投顾报告", charts=None):
    """markdown → HTML 页面（iOS 原生质感；多级目录/折叠卡片/列表/表格/分隔线/段落/图表/可视化）。"""
    body = []
    groups = []
    prod_counts, term_counts = _scan_counts(markdown_text)
    in_list = False
    in_blockquote = False
    in_code = False
    in_table = False
    in_group = False
    in_prod_detail = False
    in_fold_section = False
    cur_heading = ""
    prod_idx = 0
    metrics = []
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

    def close_metrics():
        nonlocal metrics
        if metrics:
            body.append("<div class='metric-grid'>" + "".join(metrics) + "</div>")
            metrics = []

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

    def close_prod_detail():
        nonlocal in_prod_detail
        if in_prod_detail:
            body.append("</details>")
            in_prod_detail = False

    def close_group():
        nonlocal in_group, in_fold_section
        if in_group:
            close_list(); close_quote(); close_table(); close_metrics(); close_prod_detail()
            if in_fold_section:
                body.append("</details>")
                in_fold_section = False
            body.append("</div>")
            in_group = False

    def open_group(heading):
        nonlocal in_group, in_fold_section, cur_heading, prod_idx
        close_group()
        n = len(groups) + 1
        groups.append(heading)
        cur_heading = heading
        prod_idx = 0
        is_open = heading in _OPEN_GROUPS
        cls = "group-h open" if is_open else "group-h"
        gcls = "group open" if is_open else "group"
        if heading == "今日一句话":
            gcls += " hero"
        body.append(f'<h2 class="{cls}" id="sec-{n}" data-i="{n}">'
                    f'{_inline(heading)}<span class="chev">▸</span></h2>')
        if heading == "术语速查":
            n_terms = term_counts.get(heading, 0)
            body.append(f"<div class='{gcls}' id='gsec-{n}'>"
                        f"<details class='fold-section'><summary class='fold-btn'>"
                        f"展开术语说明（{n_terms} 条）</summary>")
            in_fold_section = True
        else:
            body.append(f"<div class='{gcls}' id='gsec-{n}'>")
        in_group = True

    for raw in markdown_text.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            if in_code:
                body.append("</pre>")
                in_code = False
            else:
                close_list(); close_quote(); close_table(); close_metrics()
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
                close_list(); close_quote(); close_metrics()
                in_table = True
                table_rows = []
            table_rows.append(cells)
            continue
        if _TABLE_ROW.match(line) and _SEP_ROW.match(line):
            continue
        if in_table:
            close_table()
        if not line.strip():
            close_list(); close_quote(); close_metrics()
            continue
        if line.startswith("# "):
            close_group()
            body.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.startswith("## "):
            open_group(line[3:])
        elif line.startswith("### "):
            close_list(); close_quote(); close_metrics()
            body.append(f"<h3>{_inline(line[4:])}</h3>")
        elif line.startswith("> "):
            close_list(); close_metrics()
            if not in_blockquote:
                body.append("<blockquote>")
                in_blockquote = True
            body.append(_inline(line[2:]) + "<br/>")
        elif line.startswith("- ") or line.startswith("* "):
            close_quote()
            if in_table:
                close_table()
            close_metrics()
            mm = _METRIC_ROW.match(line)
            if mm:
                metrics.append(
                    "<div class='metric'><div class='m-val'>%s%%</div>"
                    "<div class='m-label'>%s</div>%s"
                    "<div class='m-note'>人话：%s</div></div>" % (
                        mm.group(3), mm.group(2),
                        ("<div class='m-sub'>%s</div>" % _inline(mm.group(4).strip())) if mm.group(4).strip() else "",
                        _inline(mm.group(5).strip())))
                continue
            if not in_list:
                body.append("<ul>")
                in_list = True
            inner = line[2:].strip()
            content = _render_li_body(line, cards=not (re.match(r"^⚠️", inner) or _NOTE_LI.match(inner)))
            if re.match(r"^⚠️", inner):
                content = "<span class='warn-line'>" + content + "</span>"
            elif _NOTE_LI.match(inner):
                content = "<span class='note'>" + content + "</span>"
            body.append(f"<li>{content}</li>")
        elif re.match(r"^\d+\.\s", line):
            close_quote()
            close_list()
            close_metrics()
            body.append(f"<p>{_inline(re.sub(r'^\d+\.\s', '', line))}</p>")
        elif line.startswith("---"):
            close_list(); close_quote(); close_metrics()
            body.append("<hr/>")
        else:
            close_list(); close_quote(); close_metrics()
            m = _PROD_ROW.match(line.strip())
            if m and not _TERM_ROW.match(line.strip()):
                close_prod_detail()
                prod_idx += 1
                total = prod_counts.get(cur_heading, 0)
                open_attr = " open" if (total <= 2 or prod_idx == 1) else ""
                inner = _inline(m.group(1))
                tail = _inline(m.group(2)) if m.group(2) else ""
                body.append(
                    f"<details class='prod-detail'{open_attr}>"
                    f"<summary class='prod'>{inner}"
                    f"{tail and ('<span class=\"prod-type\">%s</span>' % tail) or ''}</summary>")
                in_prod_detail = True
            else:
                body.append(_render_p(line))
    close_group()
    close_list(); close_quote(); close_table()
    if in_code:
        body.append("</pre>")

    parts = title.split()
    brand, sub = "投顾日报", ""
    if len(parts) >= 2:
        brand = parts[0]
        sub = " · ".join(parts[-2:]) if len(parts) >= 3 else parts[-1]
    else:
        brand = title

    toc_panel = _build_toc_panel(groups)

    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="color-scheme" content="light dark"/>
<meta name="theme-color" content="#F2F2F7" media="(prefers-color-scheme: light)"/>
<meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)"/>
<title>{html_lib.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<header class="navbar"><div class="nav-inner">
<span class="nav-brand">{html_lib.escape(brand)}</span><span class="nav-sub">{html_lib.escape(sub)}</span>
</div></header>
<div class="page">
{_charts_html(charts)}
{''.join(body)}
</div>
{toc_panel}
{_UI_JS}
</body>
</html>"""
    return page
