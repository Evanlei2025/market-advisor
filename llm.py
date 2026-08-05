# -*- coding: utf-8 -*-
"""LLM 解读层：DeepSeek API 调用
静态程序负责采集与规则信号（权威），本模块负责把结构化数据解读成面向一般客户的人话。
LLM 不可用时返回 None，调用方降级为纯静态报告。
"""
import json
import os
import re

import requests

DEFAULT_BASE_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"  # DeepSeek 性价比主力模型别名（当前映射 deepseek-v4-flash）


def _base_url():
    return os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL


def _model():
    return os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


SYSTEM_PROMPT = """你是一位资深买方资产配置分析师，服务对象是"一般客户"：不熟悉金融术语的普通投资者。你为理财顾问团队提供报告解读，最终会以通俗语言推送给客户。

## 你的投资方法论（必须遵守）
1. 估值分位均值回归：PE 处于历史高分位（>70%）意味着安全边际低，倾向谨慎；低分位（<30%）才有增配价值
2. 趋势尊重：价格低于长期均线（120日）时不过度乐观
3. 再平衡纪律：资产比例偏离目标超过5个百分点才调整，买入分批执行（2-3周），不一次性梭哈
4. 风险控制：不追高、不满仓单一资产、黄金是卫星仓（上限10%）
5. 避免频繁操作：多数交易日的最佳动作是"不操作"
6. 理财产品判断：结合净值趋势（60日均线）、近期收益、最大回撤、同类排名、费用（买入/卖出/管理费）综合判断。高费率产品短期操作不划算；排名长期靠后（后1/3）提示考虑同类更优产品

## 语言要求（最重要）
- 面向一般客户：用大白话，术语首次出现必须用类比解释（如"最大回撤——历史上从高点最多跌了多少"）
- 每个板块先用一句话结论，再给理由
- 建议必须可落地：说清"做什么、做多少、分几步、什么时候做"，如"如果想追加，分3次，每次间隔一周，每次不超过总仓位的5%"
- 不使用"技术性反弹""均值回归""期限利差"等术语；不用英文；不使用感叹号和营销话术

## 你的职责
- 输入是静态程序采集的当日市场数据、产品数据与规则引擎给出的权威信号
- 把数据和信号解读成客户能听懂的话，结合要闻补充语境
- 规则引擎的信号和金额是权威结论，你不得更改、不得质疑其数值

## 输出纪律（违反任何一条都是严重错误）
- 不预测具体点位、不承诺任何收益、不推荐任何个股
- 对要闻中的个股只陈述事实，不做买卖建议
- 语气客观克制，不制造焦虑，不渲染"抄底""逃顶"
- **数字纪律（最重要）：解读中出现的所有行情数字（涨跌幅、点位、收益率、金额、分位数等）必须且只能引用"当日市场数据"与"理财产品数据"两节中的数值，逐字照抄，禁止换算、禁止修改、禁止使用要闻中的任何数字**
- 要闻中提到的涨跌、成交额、涨停家数等数字均为盘中或不完整信息，一律不得引用
- 不编造数据：任何输入中没有的数值都不得出现，宁可说"输入数据未提供"
- **去重纪律（违反即废稿）：**
  - overview 是全文唯一的总览：禁止出现任何指数名称和具体数值（涨跌幅、点位、分位），只写"今天整体感受+对客户的意义"，最多40字
  - 各板块解读禁止复述自己板块数据行已列出的数值（数据行客户已看到）；禁止与 overview 或其他板块解读内容重复
  - 解读只允许引用最多1个数值来支撑观点，其余聚焦"意味着什么、影响什么、怎么办"
- **要闻纪律：news 与 product_news 中提到的每一条新闻，都必须是上文"财联社当日要闻"列表中出现的（可概括原话），不得提及输入中不存在的任何新闻事件或板块行情；若列表中没有与产品行业相关的新闻，product_news 写"今日无直接相关要闻"**
- **短句纪律：所有解读使用短句，每句不超过20字，句号或分号后必须换行（每条解读文本内为多行短句）；禁止超长单行文本**
- **长度硬约束：overview≤40字；market/valuation/bond/gold/macro 各≤70字；news≤90字；action≤170字；risk 每条≤45字；产品建议每条≤70字。超出即废稿**
- 产品操作建议必须结合该产品"费用"与"同类排名"：如费率较高则明确提示"短期进出不划算，适合长期持有"
- **产品建议纪律：advice 内不得出现任何其他产品编号或名称；只针对本产品**

## 输出格式
只输出一个 JSON 对象（不要输出任何其他文字、不要 markdown 代码块）：
{
  "overview": "今日一句话总结（≤40字，禁指数名禁数值）",
  "market": "市场概览解读（≤70字，最多1个数值，讲结构/风格/资金，不重复数据行）",
  "valuation": "估值温度解读（≤70字）",
  "bond": "债市解读（≤70字）",
  "gold": "黄金解读（≤70字）",
  "macro": "宏观与资金面解读（≤70字）",
  "news": "要闻解读（≤90字）：挑1-2条对客户投资有实际影响的",
  "action": "操作建议解读（≤170字）：分四部分写，先写今天做什么或不做什么，再写原因（1-2句），然后写执行细节（分批节奏、每次比例），最后写什么情况下触发下一步动作。每部分换行",
  "products": [
    {"code": "产品编号", "advice": "该产品的操作建议（≤70字，可落地，结合费用与排名，不得提及其他产品）"}
  ],
  "product_news": "行业新闻板块（≤120字）：从要闻中挑选与客户持有产品行业相关的新闻，逐条解读对相关产品的影响",
  "risks": ["风险1（≤45字）", "风险2（≤45字）", "风险3（≤45字）"]
}"""


def build_user_prompt(ctx):
    def fmt_sig(items):
        return "\n".join(f"- {k}: {v}" for k, v in items.items()) if items else "- 无"

    news_lines = []
    for n in ctx.get("news", [])[:20]:
        text = n.get("content", n.get("title", ""))
        if len(text) > 120:
            text = text[:120] + "…"
        news_lines.append(f"- {text}")

    product_lines = []
    for p in ctx.get("products", []):
        if p.get("unavailable"):
            product_lines.append(f"- {p['code']} {p.get('name', '')}: 非公募产品，自动数据不可用，平台:{p.get('platform','')}，人工备注:{p.get('notes','')}")
        else:
            product_lines.append(
                f"- {p['code']} {p.get('name', '')}（{p.get('type', '')}类，编号对应客户关注产品）"
                f" 最新净值:{p.get('nav_latest', '无')} 区间收益:{p.get('returns', '无')}"
                f" 近1年最大回撤:{p.get('max_dd', '无')} 同类排名:{p.get('ranking', '无')}"
                f" 规模:{p.get('scale', '无')} 成立:{p.get('inception', '无')} 基金经理:{p.get('manager', '无')}"
                f" 费用:{p.get('fees', '无')} 规则信号:{p.get('signal', '无')} 平台:{p.get('platform', '无')} 人工备注:{p.get('notes', '无')}"
            )

    parts = [
        "## 当日市场数据（静态程序采集，权威数值，解读必须逐字引用）",
        f"- 指数: {fmt_sig(ctx.get('indexes', {}))}",
        f"- 估值(PE十年分位): {fmt_sig(ctx.get('valuation', {}))}",
        f"- 债市: {fmt_sig(ctx.get('bond', {}))}",
        f"- 黄金: {fmt_sig(ctx.get('gold', {}))}",
        f"- 宏观: {fmt_sig(ctx.get('macro', {}))}",
        "",
        "## 理财产品数据（静态程序采集，权威数值，解读必须逐字引用）",
        "\n".join(product_lines) if product_lines else "- 今日无产品数据",
        "",
        "## 财联社当日要闻（仅用于理解市场语境，其中的数字一律不可引用）",
        "\n".join(news_lines) if news_lines else "- 无",
        "",
        "## 规则引擎权威信号（不得更改）",
        f"- 权益: {ctx.get('signal_equity', '无数据')}",
        f"- 债券: {ctx.get('signal_bond', '无数据')}",
        f"- 黄金: {ctx.get('signal_gold', '无数据')}",
        f"- 组合偏离与再平衡: {ctx.get('rebalance', '无数据')}",
        f"- 持仓表现: {ctx.get('holdings_perf', '无数据')}",
        "",
        "请按系统指令输出解读 JSON。",
    ]
    return "\n".join(parts)


def call_deepseek(api_key, user_prompt, timeout=180):
    payload = {
        "model": _model(),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 3000,
        "response_format": {"type": "json_object"},
    }
    r = requests.post(
        _base_url(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    r.raise_for_status()
    j = r.json()
    content = j["choices"][0]["message"]["content"]
    data = json.loads(content)
    usage = j.get("usage", {})
    return data, usage


def generate_insights(ctx):
    """入口：返回 (insights_dict, usage_info) 或 (None, None)。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None, "未配置 DEEPSEEK_API_KEY"
    try:
        user_prompt = build_user_prompt(ctx)
        data, usage = call_deepseek(api_key, user_prompt)
        used = usage.get("total_tokens", 0)
        return data, f"tokens={used}"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"


# 板块标题与 AI 字段的对应（插入报告用）
SECTION_KEYS = [
    ("## 市场速览", "market"),
    ("## 估值温度", "valuation"),
    ("## 债市与利率", "bond"),
    ("## 黄金", "gold"),
    ("## 宏观与资金面", "macro"),
    ("## 市场要闻", "news"),
    ("## 今日操作建议", "action"),
]


def insert_insights(report, insights, product_names=None):
    """把 AI 解读插入对应静态板块末尾；无对应板块的字段追加到报告末尾"""
    product_names = product_names or {}
    sections = re.split(r"(?m)^(## [^\n]+)$", report)
    block_map = {}
    for i in range(1, len(sections), 2):
        block_map[sections[i]] = sections[i + 1]

    used = set()
    for key in list(block_map.keys()):
        for title, ai_key in SECTION_KEYS:
            if key.startswith(title) and insights.get(ai_key):
                body = block_map[key]
                ai = f"\n> {insights[ai_key].strip()}"
                # AI 解读插在板块末尾的免责声明分隔线之前
                if "\n---" in body:
                    head, _, tail = body.partition("\n---")
                    block_map[key] = head + ai + "\n---" + tail
                else:
                    block_map[key] = body + ai
                used.add(ai_key)
                break

    product_advice = insights.get("products") or []
    product_lines = []
    if product_advice:
        product_lines.append("")
        product_lines.append("## 产品操作建议")
        for i, pa in enumerate(product_advice):
            if i > 0:
                product_lines.append("")
            pcode = pa.get("code", "")
            pname = product_names.get(pcode, "")
            label = f"{pcode} {pname}".strip()
            product_lines.append(f"**{label}**")
            product_lines.append(f"> {pa.get('advice', '')}")
    if insights.get("product_news"):
        product_lines.append("")
        product_lines.append("## 行业新闻解读")
        product_lines.append(f"> {insights['product_news'].strip()}")

    # overview 置顶
    overview = (insights.get("overview") or "").strip()
    risks = insights.get("risks") or []
    if not risks and insights.get("risk"):
        risks = [str(insights["risk"])]
    tail = []
    if risks:
        tail.append("")
        tail.append("## 风险观察")
        for i, r in enumerate(risks, 1):
            tail.append(f"{i}. {str(r).strip()}")

    out = []
    if overview:
        out.append(f"## 今日一句话")
        out.append(f"> {overview}")
        out.append("")
    if block_map.get("## 市场速览") is not None:
        out.append("## 市场速览" + block_map["## 市场速览"])
        out.append("")
        del block_map["## 市场速览"]
    for title, body in block_map.items():
        if body.strip():
            out.append(title + body)
            out.append("")
    out.extend(product_lines)
    out.extend(tail)
    out.append("")
    out.append("---")
    out.append("")
    out.append("*AI 解读由大模型生成，仅供参考，不构成投资建议。*")
    return "\n".join(out)
