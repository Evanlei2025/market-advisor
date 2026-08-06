# -*- coding: utf-8 -*-
"""CRO（ChiefRulesOfficer）——统一叙事引擎（V2.2）。
职责：把规则信号转化为逻辑自洽、不容置疑的最终叙事（全大白话，指标→决策因果可见）。
本模块是确定性输出，不调用 LLM，不依赖外部数据。AI 解读只能在其基线内解释。
"""
import rules


# ---- 规则原理锚点：每条规则 ID 配一句大白话（指标变化 → 决策影响的因果） ----
RULE_PRINCIPLES = {
    "TP-YIELD-1": "这只基金已经赚到了我们设定的目标，先卖掉三分之一，把一部分赚到的钱装进口袋。",
    "TP-YIELD-2": "还在继续涨，再卖掉三分之一，让剩下的一半继续赚钱。",
    "TP-YIELD-3": "涨得差不多了，把剩下的全部卖掉，落袋为安。",
    "TP-DD": "之前赚到过目标收益，现在利润明显缩水，先把赚到的保住。",
    "LAD-CSI300-80": "整个市场比过去十年80%的时间都贵，这时候买股票容易买贵，所以少配一点。",
    "LAD-CSI300-90": "市场贵得离谱（90%分位以上），进一步压缩股票仓位。",
    "LAD-CSI300-95": "市场贵到历史极值，几乎不买股票了。",
    "LAD-CSI500-75": "中证500比过去十年75%的时间都贵，而且最近20天还在跌，先躲开。",
    "MIN-MERGE": "多个指标同时亮灯，取最保守的那一个，宁少勿多。",
    "EP-CAP-10": "买股票比买债券多赚的差价已经压得很薄，性价比不高，先不加股票。",
    "STORM-5": "市场同时出现'太贵'和'跌得急'两个危险信号，暂时冻结买入、拿着现金观望，等信号解除。",
    "REB-EQ": "按计划把各块钱的分配调回目标比例（股票不够了，补一点）。",
    "REB-BOND": "按计划把各块钱的分配调回目标比例（稳健部分不够了，补一点）。",
    "BUY-NEW": "关注池里的产品到了值得买入的位置，按计划建仓。",
}

# ---- 术语速查：指标 → 一句大白话 ----
TERM_GLOSSARY = {
    "PE（市盈率）": "公司一年赚的钱，你出多少钱买，代表'贵不贵'。",
    "PE分位": "现在的价格在历史上排在什么位置：80%分位=比过去十年80%的时间都贵。",
    "最大回撤": "历史上从最高点最多跌过多少，衡量'最坏的时候'。",
    "止损线": "跌得超过这个线就撤退（已取消，本系统不再止损，仅止盈）。",
    "止盈线": "赚到目标收益就落袋的线，按市场情况动态调整。",
    "权益暴露": "你的钱里有多大比例实质押在股票上（含基金内股票）。",
    "年化波动率": "一年里收益上蹿下跳的平均幅度，衡量'坐过山车'的程度。",
    "VaR95": "按历史规律，一天最坏可能亏多少（95%情况下不会超过）。",
    "股债性价比(EP)": "买股票比买债券多赚多少，衡量'股票划不划算'。",
    "冷却期": "两次操作之间必须间隔的天数，避免来回折腾白交手续费。",
    "在途资金": "赎回后还没到账的钱，一般要等几个工作日。",
}


class CROInput:
    """CRO 输入：由 main.py 组装（全部来自规则引擎与采集数据，无 AI 参与）"""

    def __init__(self, **kw):
        self.orders = kw.get("orders", [])
        self.equity_target = kw.get("equity_target", 0.4)
        self.storm_active = kw.get("storm_active", False)
        self.storm_reasons = kw.get("storm_reasons", [])
        self.ep_lock = kw.get("ep_lock", False)
        self.ep_pctile = kw.get("ep_pctile")
        self.triggers = kw.get("triggers", [])
        self.product_sells = kw.get("product_sells", [])
        self.bond_sig = kw.get("bond_sig")
        self.tp_signals = kw.get("tp_signals", {})   # {code: {"name","amount","reason"}}
        self.cooldown_active = kw.get("cooldown_active", False)
        self.bond_codes = set(kw.get("bond_codes", []) or [])


class CRO:
    def __init__(self, inp):
        self.i = inp

    def state(self):
        """STORM(市场预警) / EP / REGULAR / IDLE / SIGNAL(影子信号)"""
        if self.i.storm_active:
            return "STORM"
        if self.i.ep_lock:
            return "EP"
        if self.i.tp_signals:
            return "SIGNAL"
        if self.i.orders:
            return "REGULAR"
        return "IDLE"

    def get_headline(self):
        s = self.state()
        if s == "STORM":
            return "市场预警：估值与动量同时达警戒，今日买入冻结，持有现金观望。"
        if s == "EP":
            return "战略防御：股票性价比偏低，权益上限锁定10%，稳健部分照常。"
        if s == "SIGNAL":
            return "止盈信号（观察期）：有产品达到目标收益，建议落袋部分利润。"
        if s == "REGULAR":
            if any(o.get("stop") for o in self.i.orders):
                return "止盈信号触发：建议部分落袋。"
            if any(o.get("side") == "买入" and o.get("code") in self.i.bond_codes for o in self.i.orders):
                return "再平衡：按纪律增配稳健部分。"
            if any(o.get("side") == "卖出" for o in self.i.orders):
                return "减配信号触发：部分锁定收益。"
            if any(o.get("side") == "买入" for o in self.i.orders):
                return "再平衡：按纪律增配权益。"
            return "今日有再平衡指令。"
        if self.i.cooldown_active:
            return "今日处于操作冷却期，维持当前持仓。"
        return "今日无规则触发，维持当前持仓。"

    def get_narrative(self):
        s = self.state()
        if s == "STORM":
            return ("市场预警已激活：估值'太贵'与下跌速度'太急'两个信号同时出现。"
                    "纪律要求此刻只持有现金，暂停买入任何产品，等待下一个无风险信号。"
                    "这不是恐慌，而是用纪律躲开最危险的一段。")
        if s == "EP":
            ep_txt = ""
            for t in self.i.triggers:
                if t.get("id") == "EP-CAP-10" and "已激活" in t.get("text", ""):
                    ep_txt = t["text"]
                    break
            return (f"当前股债性价比进入极端区域（EP分位<10%），触发资产配置安全阀（{ep_txt}）。"
                    f"根据纪律，权益仓位上限强制约束至 10%。这不是清仓信号，而是基于估值纪律的均衡调整。")
        if s == "SIGNAL":
            names = "、".join(v.get("name", k) for k, v in self.i.tp_signals.items())
            return (f"观察期内止盈信号：{names} 已达到动态目标收益线。"
                    f"当前为影子模式（观察期 6 个月），信号仅记录、不生成执行指令，"
                    f"期满评估后转正式。")
        if s == "REGULAR":
            if any(o.get("side") == "买入" and o.get("rule_id") == "REB-BOND" for o in self.i.orders):
                return ("组合的稳健部分低于目标比例，今日按纪律买入稳健产品补足仓位。"
                        "此决策由组合风险管理驱动，是当前约束下的最优安排。")
            if any(o.get("side") == "买入" for o in self.i.orders):
                return "今日按纪律增配，恢复组合目标比例。此决策由规则引擎驱动，不改动、不商量。"
        return None

    def get_storm_status_line(self):
        if not self.i.storm_active:
            return ""
        return ("⚠️ 市场预警：买入冻结已激活 → 暂停一切买入，现金冻结。"
                "解锁条件：下一无风险交易日自动判定。")

    def get_ep_status_line(self):
        if not self.i.ep_lock or self.i.storm_active:
            return ""
        ep = f"（分位 {self.i.ep_pctile*100:.0f}%）" if self.i.ep_pctile is not None else ""
        return (f"战略预警：[EP-CAP-10] 股债性价比进入极值区域{ep}，权益上限锁定 10%，"
                f"暂停权益增持；稳健部分再平衡照常执行。")

    def get_anchor_text(self):
        return "注：今日市场数据为指令执行前的收盘快照，不改变既定纪律。此纪律独立于单日涨跌。"

    def get_separator(self):
        return "以上为本次确定性指令，不可更改。以下是对此指令的解读，帮助您理解并坚定执行。"

    def principle_of(self, rid):
        return RULE_PRINCIPLES.get(rid, "")

    def plain_basis(self, triggers, orders):
        """决策依据人话版：每条触发/指令附一句原理"""
        out = []
        for t in triggers or []:
            p = self.principle_of(t.get("id", ""))
            out.append(f"[{t.get('id')}] {t.get('text', '')}" + (f"｜人话：{p}" if p else ""))
        for o in orders or []:
            rid = o.get("rule_id") or ""
            p = self.principle_of(rid)
            if p:
                out.append(f"[{rid}] {o.get('reason', '')}｜人话：{p}")
        return out

    # ---------- 今日指令摘要 ----------
    def _chain_line(self, rid, text):
        return f"[{rid}] {text}"

    def _storm_chain(self):
        items = []
        sel_ids = {"LAD-CSI300-95", "LAD-CSI500-75", "MIN-MERGE"}
        for t in self.i.triggers:
            if t.get("id") in sel_ids:
                items.append(self._chain_line(t["id"], t["text"]))
        items.append("[STORM-5] 权益目标触及5%下限 → 买入冻结")
        items.append("人话：市场太贵且跌得急，先拿现金观望")
        out = [items[0]]
        out += ["→ " + it for it in items[1:]]
        return out

    def get_summary(self):
        """完整摘要块（含标题），由 main.py 置顶插入"""
        s = self.state()
        out = ["## 今日指令摘要", f"> {self.get_headline()}"]
        if s == "STORM":
            out.append(">")
            out.append("> **触发链**：" + "\n> ".join(self._storm_chain()))
            out.append(">")
            out.append(f"> {self.get_anchor_text()}")
        elif s == "EP":
            ep_trig = next((t for t in self.i.triggers if t.get("id") == "EP-CAP-10"), None)
            out.append(">")
            out.append(f"> **触发链**：[EP-CAP-10] {ep_trig.get('text', '') if ep_trig else 'EP溢价分位 < 10%'}")
            out.append(">")
            out.append("> 此为估值纪律驱动的均衡调整，与市场预警的买入冻结有本质区别。")
        elif s == "SIGNAL":
            for code, v in self.i.tp_signals.items():
                out.append(">")
                out.append(f"> **止盈信号**：{v.get('name', code)} 建议落袋约 {v.get('amount', 0):,.0f} 元（影子模式，仅记录）")
            out.append(">")
            out.append("> 人话：" + RULE_PRINCIPLES.get("TP-YIELD-1", ""))
        elif s == "REGULAR":
            o = self.i.orders[0]
            out.append(">")
            out.append(f"> **触发链**：[{o.get('rule_id', '?')}] {o.get('reason', '')}")
            p = self.principle_of(o.get("rule_id", ""))
            if p:
                out.append(">")
                out.append(f"> 人话：{p}")
        return "\n".join(out)
