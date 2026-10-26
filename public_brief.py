"""Render one local market report as Markdown text and a standalone HTML page."""
from __future__ import annotations

from datetime import date
import html


def week_id(day):
    iso = date.fromisoformat(day).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _signed(value):
    return f"{value:+.1f}%"


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
        lines.append(f"{name}同比{item['yoy_pct']:+.1f}%（统计期{item['period']}；发布时间{item['published_at']}）")
    return lines


def asset_readout(research):
    return [f"{name}｜{view['state']}。依据：{view['reason']}。风险：{view['risk']}。截至{view['asof']}。"
            for name, view in research.get("asset_views", {}).items()]


def highlighted(research):
    items = [(name, item) for name, item in research["sectors"].items()
             if item["state"] == "优先研究"]
    return sorted(items, key=lambda pair: pair[1]["relative20_pp"], reverse=True)[:3]


def _sector_line(name, item):
    data = item["data"]
    valuation = (f"PE {data['pe']:.1f}" if data.get("pe") else
                 f"PB {data['pb']:.1f}" if data.get("pb") else "估值资料暂缺")
    participation = data.get("participation")
    volume = f"{participation:.2f}" if participation is not None else "暂缺"
    return (f"{name}｜{item['state']}｜相对沪深300近20日{item['relative20_pp']:+.1f}个百分点；"
            f"近5日成交参与比{volume}；{valuation}（仅展示，不跨行业比高低）。"
            f"支持：{'；'.join(item['evidence'])}。反面：这些是价格与成交观察，不能证明企业盈利改善；"
            f"{item['risk']}。失效条件：{item['invalidate']}。截至{data['date']}。")


def local_text(research):
    asof = research["asof"]
    title = f"市场简报｜{week_id(asof)}（本地按需生成）"
    parts = [title,
             f"行情截至{asof}（最近已完成交易日）；生成于{research['generated_at']}（Asia/Shanghai）。",
             f"本周视角截至{asof}，只反映目前已完成的交易日，不代表整周收盘结果。",
             "\n大类资产：", *asset_readout(research), *overview(research), "\n行业观察："]
    counts = {state: sum(item["state"] == state for item in research["sectors"].values())
              for state in ("优先研究", "等待确认", "暂缓追涨", "资料不足")}
    parts.append("本次行业状态：" + "；".join(f"{name} {count} 个" for name, count in counts.items()) + "。")
    sectors = highlighted(research)
    parts.extend(_sector_line(name, item) for name, item in sectors)
    if not sectors:
        parts.append("本期没有同时满足相对强度、成交参与、趋势与估值数据完整性要求的行业；不凑推荐。")
    overheated = sorted(name for name, item in research["sectors"].items()
                        if item["state"] == "暂缓追涨")
    if overheated:
        parts.append("暂缓追涨：" + "、".join(overheated[:8]) + "。短期涨幅过快或回撤较深，不把上涨当买点。")
    if research.get("news"):
        parts.append("\n新闻线索（截至生成时；仅记录，不用于解释行情）：")
        parts.extend(f"{story['source']} {story['published'][:16]} {story['title']}"
                     for story in research["news"])
    parts.extend(["\n研究口径：指数与行业均按最近已完成交易日计算近5/20/60个交易日表现；周中运行不补造整周数据。",
                  "筛选条件仅用于公开市场研究，不是经过验证的买入时机或收益预测，不对应个人持仓。"])
    if research["quality"]:
        parts.append("数据缺口：" + "、".join(research["quality"]))
    return title, "\n".join(parts)


def markdown_page(title, body):
    return "# " + title + "\n\n" + body.replace(title + "\n", "", 1).strip() + "\n"


def html_page(title, body):
    headings = {"大类资产：", "行业观察：",
                "新闻线索（截至生成时；仅记录，不用于解释行情）："}
    paragraphs = "\n".join(
        "<h2>" + html.escape(line.strip().rstrip("：")) + "</h2>" if line.strip() in headings
        else "<p>" + html.escape(line) + "</p>"
        for line in body.splitlines() if line.strip())
    return ("<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title>"
            '<style>body{font:16px/1.7 system-ui,sans-serif;color:#17232b;background:#f6f8f7;margin:0}'
            'main{max-width:760px;margin:auto;padding:28px 20px 70px}h1{font-size:1.7rem;line-height:1.3}'
            'h2{font-size:1.15rem;margin:32px 0 6px;border-left:3px solid #188277;padding-left:10px}'
            'p{margin:0;padding:11px 0;border-bottom:1px solid #d9e0df;overflow-wrap:anywhere}'
            'small{color:#465a62}@media(prefers-color-scheme:dark){body{background:#141b20;color:#e6eeed}'
            'p{border-color:#34434a}small{color:#abc0c2}}</style>'
            f'<main><h1>{html.escape(title)}</h1><small>公开市场研究 · 行情日与生成时间见正文</small>'
            f'{paragraphs}</main></html>')
