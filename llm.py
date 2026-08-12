# -*- coding: utf-8 -*-
"""LLM 解读层：DeepSeek API 调用 + AdvisorGatekeeper 硬过滤
静态程序负责采集与规则信号（权威），本模块负责把结构化数据解读成面向一般客户的人话。
LLM 不可用时返回 None，调用方降级为纯静态报告。
"""
import json
import os
import re

import requests

DEFAULT_BASE_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"  # DeepSeek 性价比主力模型别名（当前映射 deepseek-v4-flash）

BLOCKED_REPLACE = "（系统已屏蔽越权语句）"
BAD_ID_REPLACE = "（系统已屏蔽错误引用规则语句）"

# 黑名单模式：命中所在句子 → 删除替换（架构师 v1.0 Gatekeeper）
BLACKLIST_PATTERNS = [
    r"分批|分\s*2\s*次|分\s*3\s*次|分步|逐步建仓|抄底|逃顶|梭哈",
    r"建议.{0,10}(买入|加仓|建仓|申购|追加|赎回|卖出)",
    r"可以(考虑|尝试).{0,10}(买入|加仓|建仓|申购|追加)",
    r"逢低|择机|伺机",
    # V3：定量预测与纪律软化词（动作类已覆盖；方向倾向词天然放行）
    r"预计|预测|(反弹|上涨|下跌|回调|企稳|回升)空间|目标点位|目标价|明日",
    r"可以不执行|不执行也行|不操作也行|可以观望|选择观望|再等等|再看看|情况特殊|特殊时期",
]
_NUM_RE = re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?")
_SNAP_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_ID_RE = re.compile(r"[A-Z]{2,}(?:-[A-Z0-9]+)+")


def _base_url():
    return os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL


def _model():
    return os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


SYSTEM_PROMPT = """你是投顾日报的“指令解读员”与“风险提示员”，服务对象是“一般客户”：不熟悉金融术语的普通投资者。

## 核心角色定位
- 你不是投资顾问。你**不产生任何投资建议**，只负责两件事：①把规则引擎的指令解读成大白话；②把当日数据串成逻辑链，提示方向与风险。
- 系统没有“建议”，只有不容置疑的指令和帮助用户理解指令的解读。
- 规则引擎的信号、金额、规则 ID 是权威结论，你不得更改、不得质疑、不得增减。
- 你的解读必须比“CRO 基线叙事”多走一步：讲清原因与后续观察点，绝不整段复读。

## 语言要求
- 面向一般客户：用大白话，术语首次出现必须用类比解释（如“最大回撤——历史上从高点最多跌了多少”）
- **因果配额**：market/valuation/bond/gold/macro/action/产品解读 每个板块最多 1 句“为什么”——解释指标变化如何影响决策（如“PE 分位高=买贵了，所以减少股票”），必须绑定快照中的数值，句式“因为…所以…”；**summary 板块不受此限**（summary 本身即逻辑链，可连续推演）
- 短句纪律：每句不超过20字，句号或分号后必须换行
- 语气客观克制，不制造焦虑，不使用感叹号和营销话术

## AI 综述板块（summary）——允许方向倾向，禁止买卖指令
- 定位：把当日宏观数据串成一条因果链，让客户一眼看懂“今天发生了什么、环境往哪偏”。
- 内容三要素（按序组织，至少包含两项）：
  1. **宏观因果链**：如“PMI 53.0 站上荣枯线，制造业扩张，周期方向偏暖；但中证500近5日累计-6.9%，市场已在回调”——至少引用 2 个快照数值，数字逐字照抄快照
  2. **跨市场关系**：如“10年国债收益率处低位→债价偏贵→股票相对吸引力上升”（股债、汇率-资本流动等，绑定快照数值）
  3. **新闻传导**：引用“新闻命中”条目（如“重仓股 X 出现负面新闻，或拖累对应基金”）；只准引用注入的命中条目，禁止编造新闻
- **结尾方向倾向句（必须）**：“当前环境倾向：防御”、“当前环境倾向：中性”、“当前环境倾向：积极”三选一收尾，紧跟一句依据，依据绑定快照数值（如“当前环境倾向：防御，因估值分位 92% 且近5日持续走低”）。
- **允许**：定性方向倾向（防御/中性/积极）与逻辑推演。
- **严格禁止**：具体买卖动作建议（“建议买入 X”“逢低加仓”）、点位/涨跌预测（“预计明日反弹”“下跌空间有限”）、编造快照之外的数字、预测某产品净值走势。
- 长度：≤180 字。

## 指令解读板块（action）——沙箱三类允许内容，只准输出以下三类
1. **指令复述与确认**：如“今日卖出 006195 国金量化多因子股票A，金额 10,000 元。这是系统发出的确定性指令。”
   - 只可简提指令本身，**禁止逐字复述“CRO 基线叙事”或“今日一句话”headline**（客户已看到，无需重复）
2. **触发原因解释（必须绑定规则 ID）**：格式“"触发规则: TP-YIELD-1" 持有收益已达 19.2%，超过动态目标线 18%，规则建议卖出 1/3。”
   - 规则 ID 只能引用“CRO 基线叙事”和“今日触发的规则ID列表”中真实存在的，禁止自造
3. **纪律成本风险提示 + 后续观察点**：如“落袋后市场可能继续上涨，这是纪律必须承担的成本。后续观察：该产品近1月动量是否止跌。”
   - 禁止在风险提示中隐含“可以不执行”或“建议等待更好时机”的暗示

## 动态目标微调（唯一允许 AI 参与决策的字段）
- 字段 equity_target_advice：可输出一个“权益目标仓位建议”百分比数字（0~1 或 0~100）。
- 约束：该值**必须落在“权益目标浮动区间”内**（快照提供）；超出区间的值视为废稿，系统回退规则引擎基准。
- 依据：知识库中产品的持仓/风格概况 + 快照中的估值/性价比数据；必须自圆其说（给出不超过 40 字理由）。

## 严格禁止
- 任何操作动作建议（“建议分2次买入”“可以考虑逢低加仓”）
- 任何市场预测（“预计明日将反弹”“下跌空间有限”）
- 任何对纪律的质疑或软化（“不过你也可以选择观望”“但当前情况特殊”）
- 任何未在“当日数据快照”中出现的数字（所有数字必须逐字照抄快照）

## 各板块解读纪律
- **债券纪律**：债券解读必须与“CRO 基线叙事”完全一致。指令买入债券时，必须解读为“组合风险管理驱动的战略配置（非主动看多债市）”；严禁输出相悖观点，严禁“配置价值显现”“债市可增”“建议加仓债券”等战术看多措辞
- **止盈信号纪律**：止盈信号处于观察期时，必须解读为“观察信号，仅记录暂不执行”，严禁暗示“可以悄悄执行”
- **去重纪律**：各板块解读禁止复述自己板块数据行已列出的数值（数据行客户已看到）；每板块最多引用1个数值支撑观点（summary 除外，summary 是全篇综述可引用多个）
- **数据缺失纪律**：某板块缺失时写“今日该板块数据未更新”，严禁编造
- **产品解读纪律**：products 中每条 advice 只针对本产品，不得提及其他产品编号或名称；每条必须结合快照数值做到至少两项：a) 类型与实际风险不匹配（如“权益暴露 52%，高于债券型常规”）；b) 近期与长期背离（如“近1年+20% 但近3月-13.8%，动能转弱”）；c) 若“新闻命中”涉及本产品重仓股，点出新闻传导（如“重仓股 X 负面新闻或拖累净值”）。有依据才写，无依据不编
- **跨市场联动纪律**：市场速览或宏观板块最多 1 句跨市场联动（如“利率下行往往利好债券”），必须绑定快照数值，不增加字数上限之外的长度（保持各板块≤70字硬约束不变）

## 长度硬约束（超出即废稿）
market/valuation/bond/gold/macro 各≤70字；summary≤180字；action≤200字；risk 每条≤45字；产品解读每条≤70字。

## 行业与产品关注（recommendations）
- 输出近期值得关注的行业（或关注池内产品），最多 5 项；不涉及个股。
- 每条 reason 必须引用快照中的数值（如“PMI 53.0 站上荣枯线”），≤50 字。
- 无值得推荐时输出空数组 []。宁缺毋滥。

## 输出格式
只输出一个 JSON 对象（不要输出任何其他文字、不要 markdown 代码块）：
{
  "summary": "AI综述（≤180字，宏观因果链+跨市场+新闻传导，结尾必须为 当前环境倾向：防御/中性/积极 并附依据）",
  "market": "市场概览解读（≤70字，含1句为什么）",
  "valuation": "估值温度解读（≤70字，含1句为什么）",
  "bond": "债市解读（≤70字，与CRO基线叙事一致）",
  "gold": "黄金解读（≤70字）",
  "macro": "宏观与资金面解读（≤70字）",
  "action": "今日指令解读（≤200字，仅三类允许内容，不复读CRO叙事）",
  "equity_target_advice": {"value": 0.38, "reason": "（≤40字，理由简洁）"},
  "recommendations": [{"industry": "行业/产品名", "reason": "（≤50字，引用快照数值）"}],
  "products": [
    {"code": "产品编号", "advice": "该产品的指令解读（≤70字，结合费用与排名，点出类型/风险匹配与近远期背离，只针对本产品）"}
  ],
  "risks": ["风险1（≤45字）", "风险2（≤45字）", "风险3（≤45字）"]
}"""


def _fmt_5d_trend(trend):
    if not trend:
        return "未提供"
    parts = []
    for name, chg in trend.items():
        s = str(chg).strip()
        num = s.lstrip("+-")
        try:
            v = abs(float(num.rstrip("%").strip()))
        except ValueError:
            v = None
        if v is not None and v < 1e-9:
            parts.append(name + "累计持平")
        elif s.startswith("-"):
            parts.append(name + "累计下跌" + num)
        else:
            parts.append(name + "累计上涨" + num)
    return "、".join(parts) + "（趋势方向参考；仅 AI 综述 summary 可引用其具体数值，其余板块禁止引用）"


def _fmt_top10(top10):
    """产品前十大重仓摘要（<=5只）：ctx'products'[i]'top10' = [{stock_name, weight}]，weight 为占净值比例(%)或 None"""
    try:
        if not top10:
            return "无数据"
        parts = []
        for s in list(top10)[:5]:
            name = str(s.get("stock_name", "")).strip()
            if not name:
                continue
            w = s.get("weight")
            parts.append(f"{name}({w}%)" if w is not None else name)
        return "、".join(parts) if parts else "无数据"
    except Exception:
        return "无数据"


def _fmt_news_hits(hits):
    """news_hits 契约：list[str] 或 None，每项形如 - [一级警报] 内容摘要（关联 006195 等）"""
    try:
        if not hits:
            return ['- 今日无相关新闻命中']
        out = []
        for h in hits:
            s = str(h).strip()
            if s:
                out.append(s if s.startswith("- ") else f"- {s}")
        return out
    except Exception:
        return []


def build_user_prompt(ctx):
    def fmt_sig(items):
        return "\n".join(f"- {k}: {v}" for k, v in items.items()) if items else "- 无"

    # 多客户支持：客户显示名仅在存在时注入，缺失时输出与单客户版本完全一致
    client_display = (ctx.get("client_display") or "").strip()
    owner_label = f"客户 {client_display} 关注产品" if client_display else "客户关注产品"

    product_lines = []
    for p in ctx.get("products", []):
        if p.get("unavailable"):
            product_lines.append(f"- {p['code']} {p.get('name', '')}: 非公募产品，自动数据不可用，平台:{p.get('platform','')}，人工备注:{p.get('notes','')}")
        else:
            product_lines.append(
                f"- {p['code']} {p.get('name', '')}（{p.get('type', '')}类，编号对应{owner_label}）"
                f" 最新净值:{p.get('nav_latest', '无')} 区间收益:{p.get('returns', '无')}"
                f" 近1年最大回撤:{p.get('max_dd', '无')} 同类排名:{p.get('ranking', '无')}"
                f" 规模:{p.get('scale', '无')} 成立:{p.get('inception', '无')} 基金经理:{p.get('manager', '无')}"
                f" 费用:{p.get('fees', '无')} 权益暴露:{p.get('equity_exposure', '无')}"
                f" 止损线:{p.get('stop_line', '无')} 规则指令:{p.get('signal', '无')}"
                f" 平台:{p.get('platform', '无')} 人工备注:{p.get('notes', '无')}"
                 f" 重仓股:{_fmt_top10(p.get('top10'))}"
            )

    parts = []
    if client_display:
        parts.append(
            "## 客户\n"
            f"- 本报告解读对象为客户 {client_display} 的组合，以下数据、指令与解读均以该客户为准。"
        )
        parts.append("")
    parts.extend([
        "## 当日数据快照（静态程序采集，权威数值，解读必须逐字引用，禁止出现快照之外的任何数字）",
        f"- 指数: {fmt_sig(ctx.get('indexes', {}))}",
        f"- 估值(PE十年分位): {fmt_sig(ctx.get('valuation', {}))}",
        f"- 债市: {fmt_sig(ctx.get('bond', {}))}",
        f"- 黄金: {fmt_sig(ctx.get('gold', {}))}",
        f"- 宏观: {fmt_sig(ctx.get('macro', {}))}",
        "",
        "## 近5日指数趋势（辅助理解，非权威数值，解读时禁用这些数字）",
        "- " + _fmt_5d_trend(ctx.get("index_5d_trend")),
        "",
        "## 权益目标浮动区间（AI 微调唯一允许的决策字段）",
        f"- 浮动区间: {ctx.get('equity_band', '未提供')}（equity_target_advice 必须落在该区间内）",
        f"- 规则引擎基准: {ctx.get('equity_target', '未提供')}",
        "",
        "## 止盈信号（观察 · 仅记录）",
        f"- {ctx.get('tp_signal_text', '今日无止盈信号')}",
        "",
        "## 昨日信号回顾（今日验证用，禁止编造）",
        "- " + (ctx.get("yesterday_recap") or "无"),
        "",
        "## 理财产品数据（静态程序采集，权威数值，解读必须逐字引用）",
        "\n".join(product_lines) if product_lines else "- 今日无产品数据",
        "",
        "## 知识库产品档案摘要（辅助理解产品特性，不作为指令来源）",
        f"- {ctx.get('kb_summary', '无档案')}",
        "",
        "## 规则引擎今日跟投指令（权威结论，你必须原样解释，不得修改、不得质疑）",
        f"- 指令清单: {ctx.get('order_book', '无')}",
        f"- 调仓后目标仓位: {ctx.get('target_alloc', '无')}",
        f"- 组合诊断: {ctx.get('diagnostics', '无')}",
        f"- 决策依据: {ctx.get('decision_basis', '无')}",
        f"- 债券指令状态: {ctx.get('bond_order_state', '今日无债券买卖指令')}（债券解读必须与此状态一致，严禁相悖观点）",
        "",
        "## CRO 基线叙事（ChiefRulesOfficer 的权威叙述，你的解读必须与其精神一致，不得产生相悖观点）",
        f"- {ctx.get('cro_headline', '无')}",
        f"- 状态行: {ctx.get('storm_status_line') or ctx.get('ep_status_line') or '今日无预警/防御状态'}",
        f"- 叙事段: {ctx.get('cro_narrative', '无')}",
        "",
        "## 今日触发的规则ID列表（Gatekeeper 白名单：解读中出现的所有规则ID只能来自这里）",
        f"- {', '.join(ctx.get('rule_ids', [])) or '无'}",
        "",
        "",
    ])
    # ---- V3：新闻命中 + AI 综述要求（追加在规则ID列表之后） ----
    news_lines = _fmt_news_hits(ctx.get("news_hits"))
    if news_lines:
        parts.append("## 新闻命中（哨兵筛出的与持仓/关注相关的新闻，解读传导链时引用，禁止编造新闻）")
        parts.extend(news_lines)
        parts.append("")
    parts.append("## AI 综述要求（summary 字段）")
    parts.append("- 把 宏观数据→逻辑链→方向倾向 串成一段因果链，至少引用 2 个快照数值（数字必须逐字照抄快照）。")
    parts.append("- 可纳入跨市场关系（股债、汇率-资本流动）与新闻传导（引用上方新闻命中条目，禁止编造新闻）。")
    parts.append("- 结尾必须给出方向倾向句：当前环境倾向：防御 / 中性 / 积极（三选一），并附一句绑定快照数值的依据。")
    parts.append("- 全文 ≤180 字；禁止具体买卖建议与点位/涨跌预测；禁止逐字复述 CRO 叙事。")
    parts.append("")
    parts.append("请按系统指令输出解读 JSON。")
    return "\n".join(parts)


def call_deepseek(api_key, user_prompt, timeout=180):
    payload = {
        "model": _model(),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 5000,
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


def _split_sentences(text):
    """按句切分（。！？；换行）"""
    return [s.strip() for s in re.split(r"[。！？；\n]+", text) if s.strip()]


def gatekeeper_filter(ai_text, snapshot_data, valid_rule_ids):
    """AdvisorGatekeeper 硬过滤（架构师 v1.0）。
    1. 黑名单正则：命中删句，替换 BLOCKED_REPLACE
    2. 数字审计：文本中所有数字必须逐字存在于 snapshot_data，否则删句
    3. 规则 ID 白名单：[A-Z-]+ 模式必须存在于 valid_rule_ids，否则删句
    4. 整体闸门：被删 >30% 返回 None（调用方回退 CRO 叙事）
    返回 (filtered_text, audit_log)
    """
    if not ai_text:
        return "", []
    valid_ids = set(valid_rule_ids or [])
    snap = snapshot_data or ""
    sents = _split_sentences(ai_text)
    snap_tokens = set(_SNAP_NUM_RE.findall(snap))
    audit = []
    removed = 0
    kept = []
    for s in sents:
        why = None
        for pat in BLACKLIST_PATTERNS:
            if re.search(pat, s):
                why = f"黑名单:{pat}"
                break
        if why is None:
            for num in _NUM_RE.findall(s):
                if num not in snap_tokens:
                    why = f"数字审计:{num}"
                    break
        if why is None:
            for rid in _ID_RE.findall(s):
                if rid not in valid_ids:
                    why = f"ID白名单:{rid}"
                    break
        if why:
            removed += 1
            audit.append(f"[拦截 {why}] 原句: {s}")
            kept.append(BLOCKED_REPLACE)
        else:
            kept.append(s)
    if removed / len(sents) > 0.30:
        return None, audit
    return "。".join(kept), audit


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
# 板块标题与 AI 字段的对应（插入报告用）；宏观板块可插两段：macro 解读 + summary（AI 综述）
SECTION_KEYS = [
    ("## 市场速览", ["market"]),
    ("## 估值温度", ["valuation"]),
    ("## 债市与利率", ["bond"]),
    ("## 黄金", ["gold"]),
    ("## 宏观与资金面", ["macro", "summary"]),
    ("## 今日指令解读", ["action"]),
]


def insert_insights(report, insights, product_names=None, ctx=None):
    """把 AI 解读插入对应静态板块末尾；所有 AI 文本先经 AdvisorGatekeeper 硬过滤"""
    product_names = product_names or {}
    ctx = ctx or {}
    snap_parts = []
    for key in ("indexes", "valuation", "bond", "gold", "macro", "order_book",
                "target_alloc", "diagnostics", "decision_basis", "storm_status_line",
                "ep_status_line", "cro_headline", "cro_narrative"):
        v = ctx.get(key)
        if isinstance(v, dict):
            v = " ".join(f"{k}{x}" for k, x in v.items())
        if v:
            snap_parts.append(str(v))
    trend = ctx.get("index_5d_trend")
    if isinstance(trend, dict):
        snap_parts.append(" ".join(f"{k}{v}" for k, v in trend.items()))
    for p in ctx.get("products", []):
        for k, v in p.items():
            if v is not None:
                snap_parts.append(str(v))
    snapshot_data = " ".join(snap_parts)
    valid_rule_ids = ctx.get("rule_ids", [])

    def filtered(text):
        return gatekeeper_filter(str(text), snapshot_data, valid_rule_ids)

    sections = re.split(r"(?m)^(## [^\n]+)$", report)
    block_map = {}
    for i in range(1, len(sections), 2):
        block_map[sections[i]] = sections[i + 1]

    used = set()
    for key in list(block_map.keys()):
        for title, ai_keys in SECTION_KEYS:
            if not key.startswith(title):
                continue
            for ai_key in ai_keys:
                if not insights.get(ai_key):
                    continue
                ai, audit = filtered(insights[ai_key])
                if ai is None:
                    if ai_key == "summary":
                        # V3：AI 综述宁缺毋滥，不回退 CRO 叙事（避免"复读 CRO"）
                        for a in audit:
                            log_audit(a)
                        used.add(ai_key)
                        continue
                    ai = ctx.get("cro_narrative") or ""
                if ai:
                    body = block_map[key]
                    if ai_key == "summary":
                        ai = f"\n\n**AI 综述**\n> {ai.strip()}"
                    else:
                        ai = f"\n> {ai.strip()}"
                    if "\n---" in body:
                        head, _, tail = body.partition("\n---")
                        block_map[key] = head + ai + "\n---" + tail
                    else:
                        block_map[key] = body + ai
                for a in audit:
                    log_audit(a)
                used.add(ai_key)
            break

    product_advice = insights.get("products") or []
    product_lines = []
    if product_advice:
        product_lines.append("")
        product_lines.append("## 产品指令解读")
        for i, pa in enumerate(product_advice):
            if i > 0:
                product_lines.append("")
            pcode = pa.get("code", "")
            pname = product_names.get(pcode, "")
            label = f"{pcode} {pname}".strip()
            ai, audit = filtered(pa.get("advice", ""))
            if ai is None:
                ai = "该产品今日无指令。纪律要求持有或按指令执行，详见上方「今日跟投指令」。"
            for a in audit:
                log_audit(a)
            product_lines.append(f"**{label}**")
            product_lines.append(f"> {ai.strip() if ai else ''}")

    risks = insights.get("risks") or []
    tail = []
    if risks:
        tail.append("")
        tail.append("## 风险观察")
        for i, r in enumerate(risks, 1):
            ai, audit = filtered(r)
            for a in audit:
                log_audit(a)
            tail.append(f"{i}. {str(ai).strip() if ai else ''}")
    storm_risk = ctx.get("storm_risk_line", "")
    if storm_risk:
        tail.append(f"{len(risks) + 1}. {storm_risk}")

    # ---- 行业与产品关注（AI 推荐 ≤5，经 Gatekeeper 硬过滤） ----
    recs = insights.get("recommendations") or []
    rec_lines = []
    if recs:
        rec_lines.append("")
        rec_lines.append("## 行业与产品关注")
        rec_lines.append("*基于当日数据与知识库研判，仅供参考，不构成投资建议。*")
        shown = 0
        for r in recs[:5]:
            name = str(r.get("industry") or r.get("product") or "").strip()
            txt = str(r.get("reason") or r.get("text") or "").strip()
            if not name or not txt:
                continue
            ai, audit = filtered(f"{name}：{txt}")
            if ai is None:
                continue
            for a in audit:
                log_audit(a)
            try:
                import state_store
                n30 = state_store.count_recent_recommendations(name, days=30, client=ctx.get("client_id"))
            except Exception:
                n30 = 0
            badge = f"（近一月第 {n30 + 1} 次推荐）" if n30 >= 1 else ""
            rec_lines.append("")
            rec_lines.append(f"**{name}**{badge}")
            rec_lines.append(f"> {ai}")
            shown += 1
        if shown == 0:
            rec_lines = []

    out = []
    if block_map.get("## 市场速览") is not None:
        out.append("## 市场速览" + block_map["## 市场速览"])
        out.append("")
        del block_map["## 市场速览"]
    for title, body in block_map.items():
        if body.strip():
            out.append(title + body)
            out.append("")

    # 今日指令解读：静态占位板块缺失时追加（AI 可用场景）
    if not any(k.startswith("## 今日指令解读") for k in block_map) and insights.get("action"):
        ai, audit = filtered(insights["action"])
        if ai is None:
            ai = ctx.get("cro_narrative") or ""
        for a in audit:
            log_audit(a)
        if ai:
            out.append("## 今日指令解读")
            out.append(f"> {ai.strip()}")
            out.append("")

    out.extend(rec_lines)
    out.extend(product_lines)
    # V3 兜底：报告缺失"宏观与资金面"板块时，AI 综述追加到"风险观察"之前
    if insights.get("summary") and "summary" not in used:
        ai, audit = filtered(insights["summary"])
        if ai:
            out.append("")
            out.append("## AI 综述")
            out.append(f"> {ai.strip()}")
            out.append("")
        for a in audit:
            log_audit(a)
    out.extend(tail)
    out.append("")
    out.append("---")
    out.append("")
    out.append("*AI 解读由大模型生成并经过系统过滤，仅供参考，不构成投资建议。*")
    return "\n".join(out)


def log_audit(line):
    import datetime
    try:
        os.makedirs("logs", exist_ok=True)
        with open(os.path.join("logs", "run.log"), "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] [GATEKEEPER] {line}\n")
    except Exception:
        pass
