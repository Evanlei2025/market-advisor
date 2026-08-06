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
]
_NUM_RE = re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?")
_ID_RE = re.compile(r"[A-Z]{2,}(?:-[A-Z0-9]+)+")


def _base_url():
    return os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL


def _model():
    return os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


SYSTEM_PROMPT = """你是投顾日报的"指令解读员"与"风险提示员"，服务对象是"一般客户"：不熟悉金融术语的普通投资者。

## 核心角色定位
- 你不是投资顾问。你**不产生任何投资建议**，只解释规则引擎已经下达的指令，并提示风险。
- 系统没有"建议"，只有不容置疑的指令和帮助用户理解指令的解读。
- 规则引擎的信号、金额、规则 ID 是权威结论，你不得更改、不得质疑、不得增减。

## 语言要求
- 面向一般客户：用大白话，术语首次出现必须用类比解释（如"最大回撤——历史上从高点最多跌了多少"）
- **因果配额**：每个板块最多 1 句"为什么"——解释指标变化如何影响决策（如"PE 分位高=买贵了，所以减少股票"），必须绑定快照中的数值，句式"因为…所以…"
- 短句纪律：每句不超过20字，句号或分号后必须换行
- 语气客观克制，不制造焦虑，不使用感叹号和营销话术

## 指令解读板块（action）——沙箱三类允许内容，只准输出以下三类
1. **指令复述与确认**：如"今日卖出 006195 国金量化多因子股票A，金额 10,000 元。这是系统发出的确定性指令。"
2. **触发原因解释（必须绑定规则 ID）**：格式"[触发规则: TP-YIELD-1] 持有收益已达 19.2%，超过动态目标线 18%，规则建议卖出 1/3。"
   - 规则 ID 只能引用"CRO 基线叙事"和"今日触发的规则ID列表"中真实存在的，禁止自造
3. **纪律成本风险提示**：如"落袋后市场可能继续上涨，这是纪律必须承担的成本。"
   - 禁止在风险提示中隐含"可以不执行"或"建议等待更好时机"的暗示

## 动态目标微调（唯一允许 AI 参与决策的字段）
- 字段 `equity_target_advice`：可输出一个"权益目标仓位建议"百分比数字（0~1 或 0~100）。
- 约束：该值**必须落在"权益目标浮动区间"内**（快照提供）；超出区间的值视为废稿，系统回退规则引擎基准。
- 依据：知识库中产品的持仓/风格概况 + 快照中的估值/性价比数据；必须自圆其说（给出不超过 40 字理由）。

## 严格禁止
- 任何操作动作建议（"建议分2次买入""可以考虑逢低加仓"）
- 任何市场预测（"预计明日将反弹""下跌空间有限"）
- 任何对纪律的质疑或软化（"不过你也可以选择观望""但当前情况特殊"）
- 任何未在"当日数据快照"中出现的数字（所有数字必须逐字照抄快照）

## 各板块解读纪律
- **债券纪律**：债券解读必须与"CRO 基线叙事"完全一致。指令买入债券时，必须解读为"组合风险管理驱动的战略配置（非主动看多债市）"；严禁输出相悖观点，严禁"配置价值显现""债市可增""建议加仓债券"等战术看多措辞
- **止盈信号纪律**：影子模式期间的止盈信号必须解读为"观察期信号，仅记录暂不执行"，严禁暗示"可以悄悄执行"
- **去重纪律**：各板块解读禁止复述自己板块数据行已列出的数值（数据行客户已看到）；每板块最多引用1个数值支撑观点
- **数据缺失纪律**：某板块缺失时写"今日该板块数据未更新"，严禁编造
- **产品解读纪律**：products 中每条 advice 只针对本产品，不得提及其他产品编号或名称
- **跨市场联动纪律**：市场速览或宏观板块最多 1 句跨市场联动（如“利率下行往往利好债券”），必须绑定快照数值，不增加字数上限之外的长度（保持各板块≤70字硬约束不变）

## 长度硬约束（超出即废稿）
market/valuation/bond/gold/macro 各≤70字；action≤200字；risk 每条≤45字；产品解读每条≤70字。

## 行业与产品关注（recommendations）
- 输出近期值得关注的行业（或关注池内产品），最多 5 项；不涉及个股。
- 每条 reason 必须引用快照中的数值（如"PMI 53.0 站上荣枯线"），≤50 字。
- 无值得推荐时输出空数组 []。宁缺毋滥。

## 输出格式
只输出一个 JSON 对象（不要输出任何其他文字、不要 markdown 代码块）：
{
  "market": "市场概览解读（≤70字，含1句为什么）",
  "valuation": "估值温度解读（≤70字，含1句为什么）",
  "bond": "债市解读（≤70字，与CRO基线叙事一致）",
  "gold": "黄金解读（≤70字）",
  "macro": "宏观与资金面解读（≤70字）",
  "action": "今日指令解读（≤200字，仅三类允许内容）",
  "equity_target_advice": {"value": 0.38, "reason": "（≤40字，理由简洁）"},
  "recommendations": [{"industry": "行业/产品名", "reason": "（≤50字，引用快照数值）"}],
  "products": [
    {"code": "产品编号", "advice": "该产品的指令解读（≤70字，结合费用与排名，只针对本产品）"}
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
    return "、".join(parts) + "（趋势方向参考，解读中禁止引用具体数字）"


def build_user_prompt(ctx):
    def fmt_sig(items):
        return "\n".join(f"- {k}: {v}" for k, v in items.items()) if items else "- 无"

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
                f" 费用:{p.get('fees', '无')} 权益暴露:{p.get('equity_exposure', '无')}"
                f" 止损线:{p.get('stop_line', '无')} 规则指令:{p.get('signal', '无')}"
                f" 平台:{p.get('platform', '无')} 人工备注:{p.get('notes', '无')}"
            )

    parts = [
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
        "## 止盈信号（影子模式，仅记录）",
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
                if num not in snap:
                    why = f"数字审计:{num}"
                    break
        if why is None:
            for rid in _ID_RE.findall(s):
                if rid not in valid_ids:
                    why = f"ID白名单:{rid}"
                    break
        if why:
            removed += 1
            audit.append(f"[拦截 {why}] {s[:60]}")
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
SECTION_KEYS = [
    ("## 市场速览", "market"),
    ("## 估值温度", "valuation"),
    ("## 债市与利率", "bond"),
    ("## 黄金", "gold"),
    ("## 宏观与资金面", "macro"),
    ("## 今日指令解读", "action"),
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
        for title, ai_key in SECTION_KEYS:
            if key.startswith(title) and insights.get(ai_key):
                ai, audit = filtered(insights[ai_key])
                if ai is None:
                    ai = ctx.get("cro_narrative") or ""
                if ai:
                    body = block_map[key]
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
                n30 = state_store.count_recent_recommendations(name, days=30)
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
