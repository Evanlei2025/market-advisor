# -*- coding: utf-8 -*-
"""LLM 解读层：DeepSeek API 调用
静态程序负责采集与规则信号（权威），本模块负责把结构化数据解读成人话。
LLM 不可用时返回 None，调用方降级为纯静态报告。
"""
import json
import os

import requests

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"

SYSTEM_PROMPT = """你是一位资深的买方资产配置分析师，服务对象是偏稳健的个人投资者（股债平衡型配置：权益45%、债券45%、黄金10%）。

## 你的投资方法论（必须遵守）
1. 估值分位均值回归：PE 处于历史高分位（>70%）意味着安全边际低，倾向谨慎；低分位（<30%）才有增配价值
2. 趋势尊重：价格低于长期均线（120日）时不过度乐观
3. 再平衡纪律：资产比例偏离目标超过5个百分点才调整，买入分批执行（2-3周），不一次性梭哈
4. 风险控制：不追高、不满仓单一资产、黄金是卫星仓（上限10%）
5. 避免频繁操作：多数交易日的最佳动作是"不操作"

## 你的职责
- 输入是静态程序采集的当日市场数据与规则引擎给出的权威信号
- 你要做的是：把数据和信号解读成投资者能听懂的话，补充市场语境（结合要闻）
- 规则引擎的信号和金额是权威结论，你不得更改、不得质疑其数值

## 输出纪律（违反任何一条都是严重错误）
- 不预测具体点位、不承诺任何收益、不推荐任何个股
- 对要闻中的个股只陈述事实，不做买卖建议
- 语气客观克制，不制造焦虑，不渲染"抄底""逃顶"
- **数字纪律（最重要）：解读中出现的所有行情数字（涨跌幅、点位、收益率、金额、分位数等）必须且只能引用"当日市场数据"一节中的数值，逐字照抄，禁止换算、禁止修改、禁止使用要闻中的任何数字**
- 要闻中提到的涨跌、成交额、涨停家数等数字均为盘中或不完整信息，一律不得引用
- 不编造数据：任何输入中没有的数值都不得出现，宁可说"输入数据未提供"
- 每条解读 60-120 字，中文，书面语
- 如果某板块数据缺失，对应解读写"今日该板块数据未更新"

## 输出格式
只输出一个 JSON 对象（不要输出任何其他文字、不要 markdown 代码块）：
{
  "overview": "今日综述，100-150字：市场整体发生了什么、机构情绪如何（结合两融/汇率/外盘）、对组合的含义",
  "market": "市场速览解读",
  "valuation": "估值温度解读",
  "bond": "债市解读",
  "gold": "黄金解读",
  "macro": "宏观与资金面解读",
  "news": "要闻解读：挑2-3条对A股/配置有实际影响的，说明潜在影响，其余跳过",
  "action": "操作建议解读：基于规则信号解释今天该做什么、为什么，执行时注意什么（不超过150字）",
  "risk": "风险观察，60-100字：当前最值得警惕的1-2个风险点"
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

    parts = [
        "## 当日市场数据（静态程序采集，权威数值，解读必须逐字引用）",
        f"- 指数: {fmt_sig(ctx.get('indexes', {}))}",
        f"- 估值(PE十年分位): {fmt_sig(ctx.get('valuation', {}))}",
        f"- 债市: {fmt_sig(ctx.get('bond', {}))}",
        f"- 黄金: {fmt_sig(ctx.get('gold', {}))}",
        f"- 宏观: {fmt_sig(ctx.get('macro', {}))}",
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


def call_deepseek(api_key, user_prompt, timeout=120):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 2500,
        "response_format": {"type": "json_object"},
    }
    r = requests.post(
        API_URL,
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


def render_insights(insights):
    """把 LLM 输出渲染成报告 markdown 板块"""
    lines = []
    L = lines.append
    keys = [
        ("market", "市场速览"),
        ("valuation", "估值温度"),
        ("bond", "债市与利率"),
        ("gold", "黄金"),
        ("macro", "宏观与资金面"),
        ("news", "要闻解读"),
        ("action", "操作建议解读"),
        ("risk", "风险观察"),
    ]
    L(f"## AI 今日综述")
    L(f"> {insights.get('overview', '')}")
    L(f"\n## AI 板块解读")
    for key, title in keys:
        val = (insights.get(key) or "").strip()
        if val:
            L(f"\n**{title}**")
            L(f"> {val}")
    L(f"\n---")
    L(f"*以上 AI 解读由大模型生成，仅供参考，不构成投资建议。*")
    return "\n".join(lines)
