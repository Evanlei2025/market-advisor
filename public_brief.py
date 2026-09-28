"""Build public market briefs and an allowlisted static site."""
from __future__ import annotations

import html
from datetime import date
from pathlib import Path


def week_id(day):
    iso = date.fromisoformat(day).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _signed(n):
    return f"{n:+.1f}%"


def overview(research):
    lines = []
    for name, item in research["assets"].items():
        proxy = "（指数价格，非基金收益）" if name == "中证全债" else ""
        lines.append(f"{name}{proxy}近5日{_signed(item['returns']['5'])}、近20日{_signed(item['returns']['20'])}（截至{item['date']}）")
    gold = research.get("gold")
    if gold:
        lines.append(f"黄金现货近20日{_signed(gold['returns']['20'])}（截至{gold['date']}）")
    bond = research.get("bond_y10")
    if bond:
        lines.append(f"十年国债收益率{bond['yield_pct']:.2f}%（截至{bond['date']}；收益率不是债券收益）")
    else:
        lines.append("债券收益率数据暂缺；不判断债市机会")
    cash = research.get("cash_shibor")
    if cash:
        lines.append(f"现金利率参考：3月期Shibor {cash['rate_pct']:.2f}%（{cash['date']}；非存款或基金收益）")
    else:
        lines.append("现金参考利率暂缺；不进行收益排名")
    for name, item in research.get("macro", {}).items():
        lines.append(f"{name}同比{item['yoy_pct']:+.1f}%（统计期{item['period']}；月度数据不要求每日变化）")
    return lines


def asset_readout(research):
    return [f"{name}｜{view['state']}。依据：{view['reason']}。风险：{view['risk']}。截至{view['asof']}。"
            for name, view in research.get("asset_views", {}).items()]


def highlighted(research):
    items = [(name, item) for name, item in research["sectors"].items()
             if item["state"] == "优先研究"]
    return sorted(items, key=lambda pair: pair[1]["relative20_pp"], reverse=True)[:3]


def _sector_line(name, item):
    d = item["data"]
    valuation = (f"PE {d['pe']:.1f}" if d.get("pe") else
                 f"PB {d['pb']:.1f}" if d.get("pb") else "估值资料暂缺")
    return (f"{name}｜{item['state']}｜相对沪深300近20日{item['relative20_pp']:+.1f}个百分点；"
            f"近5日成交参与比{d.get('participation', 0):.2f}；{valuation}（仅展示，不跨行业比高低）。"
            f"支持：{'；'.join(item['evidence'])}。反面：这些是价格与成交观察，不能证明企业盈利改善；"
            f"{item['risk']}。失效条件：{item['invalidate']}。截至{d['date']}。")


def recap(research, previous, observations):
    lines = []
    if previous:
        for name in research["assets"]:
            curr = research["assets"][name]
            prev = previous.get("assets", {}).get(name)
            if prev and prev.get("date") != curr["date"] and prev.get("close", 0) > 0:
                diff = (curr["close"] / prev["close"] - 1) * 100
                lines.append(f"{name}较上次观察({_signed(diff)})，数据从{prev['date']}至{curr['date']}")
    for entry in observations[-3:]:
        name = entry["entity"]
        current = research["sectors"].get(name)
        path = entry.get("path", [])
        if current and entry.get("reference_close") and path:
            last = path[-1]
            change = (last["close"] / entry["reference_close"] - 1) * 100
            adverse = (min(p["close"] for p in path) / entry["reference_close"] - 1) * 100
            ref_bench = entry.get("reference_benchmark")
            relative = ((last["benchmark"] / ref_bench - 1) * 100) if ref_bench else None
            spans = "；".join(
                f"{n}个交易日后{_signed((path[n]['close'] / entry['reference_close'] - 1) * 100)}"
                for n in (5, 20, 60) if len(path) > n)
            lines.append(f"上期关注{name}：发布后已过{len(path)-1}个交易日，指数较参考收盘价{_signed(change)}，"
                         + (f"相对基准{change-relative:+.1f}个百分点，" if relative is not None else "")
                         + (f"{spans}；" if spans else "观察期未满5个交易日；")
                         + f"期间最低相对参考价{_signed(adverse)}；当前{current['state']}。"
                           "仅为指数观察，缺失交易日不计入样本，不是客户持仓收益。")
        elif current:
            lines.append(f"上期关注{name}：后续观察期未满；当前{current['state']}")
    return lines or ["尚无可比较的已发布观察，从本期开始建立记录。"]


def weekly(research, previous=None, observations=()):
    sectors = highlighted(research)
    parts = [f"市场机会与风险周报｜{week_id(research['date'])}",
             f"行情截至{research['asof']}；研究快照生成于{research['date']}。",
             "\n本周变化：", *recap(research, previous, observations),
             "\n大类资产：", *asset_readout(research), *overview(research), "\n行业观察："]
    parts.extend(_sector_line(name, item) for name, item in sectors)
    if not sectors:
        parts.append("本期没有同时满足相对强度、成交参与、趋势与估值数据完整性要求的行业；不凑推荐。")
    overheated = sorted((name for name, item in research["sectors"].items() if item["state"] == "暂缓追涨"))
    if overheated:
        parts.append("暂缓追涨：" + "、".join(overheated[:5]) + "。短期涨幅过快或回撤较深，不把上涨当买点。")
    if research.get("editor_note"):
        parts.append("\n编辑解读（仅供阅读）：" + research["editor_note"])
    if research.get("news"):
        parts.append("\n同期新闻线索（仅记录，并非涨跌原因）：")
        parts.extend(f"{story['source']} {story['published'][:16]} {story['title']}"
                     for story in research["news"])
    parts.extend(["\n下周观察：关注相对强度是否延续、成交参与是否确认及数据是否如期更新。",
                  "筛选条件仅用于研究，不是经过验证的买入时机或收益预测。"])
    if research["quality"]:
        parts.append("数据缺口：" + "、".join(research["quality"][:5]))
    return "\n".join(parts)


def alert(research, events):
    lines = [f"市场变化提醒｜数据截至{research['asof']}"]
    for event in events[:3]:
        lines.append(f"{event['entity']}：{event['before']}→{event['after']}；"
                     f"近20日相对沪深300{event['relative20_pp']:+.1f}个百分点。"
                     "请看报告中的证据与失效条件。")
    lines.append("仅供公开市场研究，不对应个人持仓或具体交易。")
    return "\n".join(lines)


def html_page(title, body, links=()):
    headings = {"本周变化：", "大类资产：", "行业观察：", "下周观察：",
                "同期新闻线索（仅记录，并非涨跌原因）："}
    paragraphs = "\n".join(
        "<h2>" + html.escape(line.strip().rstrip("：")) + "</h2>" if line.strip() in headings
        else "<p>" + html.escape(line) + "</p>"
        for line in body.splitlines() if line.strip())
    nav = "".join(f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'
                  for label, href in links)
    return ("<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title>"
            '<style>body{font:16px/1.7 system-ui,sans-serif;color:#17232b;background:#f6f8f7;margin:0}'
            'main{max-width:760px;margin:auto;padding:28px 20px 70px}h1{font-size:1.7rem;line-height:1.3}'
            'h2{font-size:1.15rem;margin:32px 0 6px;border-left:3px solid #188277;padding-left:10px}'
            'p{margin:0;padding:11px 0;border-bottom:1px solid #d9e0df;overflow-wrap:anywhere}'
            'nav{display:flex;gap:18px;flex-wrap:wrap}a{color:#146d67}small{color:#465a62}'
            '@media(prefers-color-scheme:dark){body{background:#141b20;color:#e6eeed}'
            'p{border-color:#34434a}a{color:#70c7b7}small{color:#abc0c2}}</style>'
            f'<main><nav>{nav}</nav><h1>{html.escape(title)}</h1><small>公开市场研究 · 数据日期见正文</small>'
            f'{paragraphs}</main></html>')


def render_site(output, weeks, alerts=()):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "weeks"
    archive.mkdir(exist_ok=True)
    links = []
    for wk, text in sorted(weeks.items(), reverse=True):
        if not wk.replace("-W", "").isdigit():
            raise ValueError("invalid week identifier")
        (archive / (wk + ".html")).write_text(
            html_page("周报 " + wk, text, [("全部周报", "../index.html")]), encoding="utf-8")
        links.append(("周报 " + wk, "weeks/" + wk + ".html"))
    newest = max(weeks) if weeks else None
    alert_link = bool(alerts)
    if newest:
        (output / "latest.html").write_text(
            html_page("最新市场周报", weeks[newest], [("全部周报", "index.html")]), encoding="utf-8")
    if alerts:
        alert_dir = output / "alerts"
        alert_dir.mkdir(exist_ok=True)
        for entry in alerts:
            asof = entry["asof"]
            date.fromisoformat(asof)
            (alert_dir / (asof + ".html")).write_text(
                html_page("市场变化 " + asof, entry["body"], [("最新周报", "../latest.html")]),
                encoding="utf-8")
        (output / "alert.html").write_text(
            html_page("近期市场变化", alerts[-1]["body"], [("最新周报", "latest.html")]), encoding="utf-8")
    (output / "index.html").write_text(
        html_page("市场机会与风险", "公开市场周报档案。" if weeks else "首期周报尚未发布。",
                  ([("最新周报", "latest.html")] if newest else [])
                  + ([("近期变化", "alert.html")] if alert_link else []) + links), encoding="utf-8")
