# -*- coding: utf-8 -*-
"""CRO（ChiefRulesOfficer）——统一叙事引擎（架构师 v1.0）。
职责：把规则信号转化为逻辑自洽、不容置疑的最终叙事。
- headline（今日一句话）、主叙事段、风暴/EP 状态行、指令锚定语、分隔声明
- 今日指令摘要（四状态模板 + 触发链）
本模块是确定性输出，不调用 LLM，不依赖外部数据。AI 解读只能在其基线内解释。
"""
import rules


class CROInput:
    """CRO 输入：由 main.py 组装（全部来自规则引擎与采集数据，无 AI 参与）"""

    def __init__(self, **kw):
        self.orders = kw.get("orders", [])
        self.equity_target = kw.get("equity_target", 0.4)
        self.storm_active = kw.get("storm_active", False)
        self.storm_reasons = kw.get("storm_reasons", [])
        self.ep_lock = kw.get("ep_lock", False)
        self.ep_pctile = kw.get("ep_pctile")
        self.triggers = kw.get("triggers", [])          # list[{id, text}]
        self.product_sells = kw.get("product_sells", [])  # list[{code,name,rule_id,dd,stop_line}]
        self.bond_sig = kw.get("bond_sig")              # 债市战术信号描述（仅参考，不输出观点）


class CRO:
    def __init__(self, inp):
        self.i = inp
        self._reason_text = {
            "STORM-5": "权益仓位触及 5% 最低下限",
            "STORM-STOP": "产品触发无条件清仓",
        }

    # ---------- 决策分支（顺序不可变：storm → ep → 常规） ----------
    def state(self):
        """返回当前叙事状态标识：STORM-A(清仓) / STORM-B(5%) / EP / REGULAR / IDLE"""
        if self.i.storm_active:
            return "STORM-A" if "STORM-STOP" in self.i.storm_reasons else "STORM-B"
        if self.i.ep_lock:
            return "EP"
        if self.i.orders:
            return "REGULAR"
        return "IDLE"

    def get_headline(self):
        s = self.state()
        if s == "STORM-A":
            return "风暴安全锁已激活：清仓触发，现金冻结，等待下一无风险信号。"
        if s == "STORM-B":
            return "风暴安全锁已激活：权益仓位降至最低，现金冻结，等待下一无风险信号。"
        if s == "EP":
            return "战略防御：权益上限锁定10%，固收再平衡照常。"
        if s == "REGULAR":
            if any(o.get("stop") for o in self.i.orders):
                return "止损信号触发：清仓权益，转入固收。"
            if any(o.get("side") == "卖出" for o in self.i.orders):
                return "止盈/减配信号触发：部分锁定收益。"
            if any(o.get("side") == "买入" and o.get("rule_id") == "REB-BOND" for o in self.i.orders):
                return "再平衡：按纪律增配固收。"
            if any(o.get("side") == "买入" for o in self.i.orders):
                return "再平衡：按纪律增配权益。"
            return "今日有再平衡指令。"
        return "今日无规则触发，维持当前持仓。"

    def get_narrative(self):
        s = self.state()
        if s in ("STORM-A", "STORM-B"):
            reasons = [self._reason_text.get(r, "") for r in (self.i.storm_reasons or []) if self._reason_text.get(r)]
            reason = "，且".join(reasons) if reasons else "当前信号"
            txt = (f"风暴安全锁已激活。{reason}后，全部现金被暂时冻结，暂停买入任何资产（含固收）。"
                   f"等待下一个无风险信号的交易日再执行再平衡。纪律要求我们此刻只持有现金。")
            if self.i.ep_lock:
                txt += ("今日市场动量与估值双双触发最高警报。尽管从纯资产比较看，固收相对估值正常，"
                        "但纪律要求此刻现金为王。")
            return txt
        if s == "EP":
            ep_txt = ""
            for t in self.i.triggers:
                if t.get("id") == "EP-CAP-10" and "已激活" in t.get("text", ""):
                    ep_txt = t["text"]
                    break
            return (f"当前股债性价比已进入极端区域（EP分位<10%），触发资产配置安全阀"
                    f"（{ep_txt}）。根据纪律，权益仓位上限强制约束至 10%。"
                    f"这不是清仓信号，而是基于估值纪律的均衡调整：卖出部分权益、按目标配置买入固收，"
                    f"以维持纪律性平衡。这与市场崩溃时的风暴安全锁有本质区别。")
        if s == "REGULAR":
            if any(o.get("side") == "买入" and o.get("rule_id") == "REB-BOND" for o in self.i.orders):
                cause = "组合出现资金富余" if any(o.get("stop") for o in self.i.orders) else "固收低于目标仓位"
                return (f"尽管当前利率水平偏低、战术赔率不足，但组合因{cause}出现资金缺口。"
                        f"为确保风险敞口不被动收缩至过度保守状态，今日按纪律买入固收以完成战略仓位回补。"
                        f"此决策由组合风险管理驱动，是当前约束下的唯一最优解。")
            if any(o.get("side") == "买入" for o in self.i.orders):
                return "今日按纪律增配权益，以恢复组合目标仓位。此决策由规则引擎驱动，不改动、不商量。"
        return None

    def get_storm_status_line(self):
        if not self.i.storm_active:
            return ""
        rid = "/".join(self.i.storm_reasons) if self.i.storm_reasons else "STORM"
        if self.i.ep_lock:
            return (f"⚠️ 风暴安全锁：已激活（[{rid}]）→ 现金冻结，暂停一切买入（含固收）。"
                    f"EP极端估值信号已记录，但不干预风暴锁决策。")
        return (f"⚠️ 风暴安全锁：已激活（[{rid}]）→ 现金冻结，暂停一切买入。"
                f"解锁条件：下一无风险交易日自动判定。")

    def get_ep_status_line(self):
        if not self.i.ep_lock or self.i.storm_active:
            return ""
        ep = f"（分位 {self.i.ep_pctile*100:.0f}%）" if self.i.ep_pctile is not None else ""
        return (f"战略预警：[EP-CAP-10] 股债性价比进入极值区域{ep}，权益上限锁定 10%，"
                f"暂停一切权益增持；固收再平衡照常执行。")

    def get_anchor_text(self):
        """指令锚定语：市场数据为指令执行前的收盘快照，纪律独立于单日涨跌"""
        return "注：今日市场数据为指令执行前的收盘快照，不改变既定纪律。此纪律独立于单日涨跌。"

    def get_separator(self):
        return "以上为本次确定性指令，不可更改。以下是对此指令的解读，帮助您理解并坚定执行。"

    # ---------- 今日指令摘要（内容分析师定稿：四状态 + 触发链） ----------
    def _chain_line(self, rid, text):
        return f"[{rid}] {text}"

    def _storm_chain(self):
        """状态A/B 触发链：从真实触发数据组装，禁止硬编码"""
        items = []
        if "STORM-STOP" in self.i.storm_reasons and self.i.product_sells:
            ps = self.i.product_sells[0]
            items.append(self._chain_line(ps.get("rule_id", "SL"),
                                          f"{ps.get('name', '')} 净值距250日高点回撤 {ps.get('dd', 0)*100:.1f}% > {ps.get('stop_line', 0)*100:.0f}%"))
            items.append("[STORM-STOP] 无条件清仓")
            items.append("StormLock 激活，全部买入冻结。")
        else:
            sel_ids = {"LAD-CSI300-95", "LAD-CSI500-75", "MIN-MERGE"}
            for t in self.i.triggers:
                if t.get("id") in sel_ids:
                    items.append(self._chain_line(t["id"], t["text"]))
            items.append("[STORM-5] 权益目标触及5%下限")
            items.append("StormLock 激活，全部买入冻结。")
        out = [items[0]]
        out += ["→ " + it for it in items[1:]]
        return out

    def get_summary(self):
        """完整摘要块（含标题），由 main.py 置顶插入"""
        s = self.state()
        out = ["## 今日指令摘要", f"> {self.get_headline()}"]
        if s in ("STORM-A", "STORM-B"):
            out.append(">")
            out.append("> **触发链**：" + "\n> ".join(self._storm_chain()))
            out.append(">")
            out.append(f"> {self.get_anchor_text()}")
        elif s == "EP":
            ep_trig = next((t for t in self.i.triggers if t.get("id") == "EP-CAP-10"), None)
            out.append(">")
            out.append(f"> **触发链**：[EP-CAP-10] {ep_trig.get('text', '') if ep_trig else 'EP溢价分位 < 10%'}")
            out.append(">")
            out.append("> 此为估值纪律驱动的均衡调整，与市场崩溃时的风暴安全锁有本质区别。")
        elif s == "REGULAR":
            o = self.i.orders[0]
            out.append(">")
            out.append(f"> **触发链**：[{o.get('rule_id', '?')}] {o.get('reason', '')}")
        return "\n".join(out)
