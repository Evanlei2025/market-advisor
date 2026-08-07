# -*- coding: utf-8 -*-
"""TestGatekeeper 回归测试：AdvisorGatekeeper 三层硬过滤 + 整体闸门（第二阶段测试补缺）。
回归重点：_NUM_RE 负向后行断言——Au99.99 的 99.99 不得被当数字审计，
但 904.92 仍必须逐字存在于快照；黑名单 V3 预测/软化词；规则 ID 白名单；
>30% 整体回退闸门。纯本地、无网络、无外部依赖，可独立运行 python test_gatekeeper.py。"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import llm
from llm import gatekeeper_filter, _split_sentences, BLOCKED_REPLACE


def run():
    passed = 0

    def check(name, cond, extra=""):
        nonlocal passed
        if cond:
            passed += 1
            print("PASS " + name + " " + extra)
        else:
            print("FAIL " + name + " " + extra)

    # ---------- 场景1：Au99.99 放行（_NUM_RE 负向后行，回归核心） ----------
    text1 = "Au99.99报904.92元"
    out1, aud1 = gatekeeper_filter(text1, "Au99.99 904.92", [])
    check("1a Au99.99+价格均在快照→原句放行", out1 == text1 and aud1 == [], "out=" + repr(out1))
    check("1b _NUM_RE 跳过 Au99.99 内嵌数字", llm._NUM_RE.findall(text1) == ["904.92"],
          "nums=" + repr(llm._NUM_RE.findall(text1)))
    out1c, aud1c = gatekeeper_filter(text1, "Au99.99", [])
    check("1c 快照缺价格→数字审计拦截",
          out1c is None and any("数字审计:904.92" in a for a in aud1c), "aud=" + repr(aud1c))

    # ---------- 场景2：基金代码+数字放行（数字审计为子串匹配） ----------
    text2 = "006195近1年收益20%"
    out2, aud2 = gatekeeper_filter(text2, "006195 区间收益20%", [])
    check("2 基金代码+收益均在快照→放行", out2 == text2 and aud2 == [], "out=" + repr(out2))

    # ---------- 场景3：编造数字拦截 ----------
    out3, aud3 = gatekeeper_filter("沪深300 PE为8.5，建议关注", "沪深300指数 2026-08-07", [])
    check("3 编造数字8.5→拦截且审计记录数字",
          out3 is None and any("数字审计" in a and "8.5" in a for a in aud3), "aud=" + repr(aud3))

    # ---------- 场景4：日期放行 ----------
    text4 = "今日为2026-08-07，数据已更新"
    out4, aud4 = gatekeeper_filter(text4, "2026-08-07 平稳", [])
    check("4 日期逐字在快照→放行", out4 == text4 and aud4 == [], "out=" + repr(out4))

    # ---------- 场景5：黑名单逐条拦截（V3 含预测词/软化词） ----------
    bl_cases = [
        ("5a 建议买入", "建议买入。"),
        ("5b 抄底", "现在抄底。"),
        ("5c 分批建仓", "分批建仓。"),
        ("5d 逢低加仓", "逢低加仓。"),
        ("5e 预计明日反弹(V3预测词)", "预计明日反弹。"),
        ("5f 不过你也可以选择观望(V3软化词)", "不过你也可以选择观望。"),
    ]
    for name, t in bl_cases:
        o, a = gatekeeper_filter(t, "", [])
        check(name, o is None and len(a) == 1 and "黑名单" in a[0], "aud=" + repr(a))

    # ---------- 场景6：规则 ID 白名单 ----------
    out6, aud6 = gatekeeper_filter("触发规则: TP-YIELD-1", "TP-YIELD-1 19.2", ["TP-YIELD-1"])
    check("6a 白名单内规则ID→放行", out6 == "触发规则: TP-YIELD-1" and aud6 == [], "out=" + repr(out6))
    out6b, aud6b = gatekeeper_filter("触发规则: FAKE-RULE-9", "TP-YIELD-1 9.0%", ["TP-YIELD-1"])
    check("6b 白名单外规则ID→拦截",
          out6b is None and any("ID白名单" in a and "FAKE-RULE-9" in a for a in aud6b),
          "aud=" + repr(aud6b))

    # ---------- 场景7：方向倾向放行（V3，倾向词非黑名单，数字全在快照） ----------
    text7 = "当前环境倾向：防御，因 PMI 53.0 站上荣枯线但中证500 20日动量-6.9%"
    snap7 = "PMI 53.0 中证500 2026-08-07 近5日趋势：沪深300累计下跌6.9%"
    out7, aud7 = gatekeeper_filter(text7, snap7, [])
    check("7 方向倾向句(数字逐字在快照)→放行", out7 == text7 and aud7 == [],
          "out=" + repr(out7) + " aud=" + repr(aud7))

    # ---------- 场景8：整体闸门 >30% → None（调用方回退 CRO 叙事） ----------
    text8 = "今日行情平稳。沪深300 PE为8.5，建议关注。分批建仓。市场震荡整理。FAKE-RULE-9 触发。"
    out8, aud8 = gatekeeper_filter(text8, "2026-08-07 平稳 9.5%", [])
    check("8a 5句中3句被拦(60%)→整体None", out8 is None, "out=" + repr(out8))
    check("8b 拦截审计逐句记录", len(aud8) == 3 and any("黑名单" in a for a in aud8),
          "aud=" + repr(aud8))

    # ---------- 场景9：边界 ----------
    out9a, aud9a = gatekeeper_filter("", "", [])
    out9b, aud9b = gatekeeper_filter(None, "", [])
    check("9a 空文本/None 均返回空结果", out9a == "" and aud9a == [] and out9b == "" and aud9b == [])
    text9 = "第一句。第二句。第三句"
    out9, aud9 = gatekeeper_filter(text9, "2026-08-07", [])
    check("9b 全部通过→原句以句号连接且保序", out9 == "第一句。第二句。第三句" and aud9 == [],
          "out=" + repr(out9))
    text9c = ("市场平稳运行。分批建仓。资金面中性。建议买入。黄金横盘整理。逢低加仓。"
              "债市窄幅波动。宏观数据未更新。股指窄幅震荡。今日无异常")
    out9c, aud9c = gatekeeper_filter(text9c, "2026-08-07", [])
    check("9c 30%精确边界(3/10)不触发回退",
          out9c is not None and out9c.count(BLOCKED_REPLACE) == 3
          and out9c.startswith("市场平稳运行。") and "债市窄幅波动。宏观数据未更新" in out9c,
          "out=" + repr(out9c))

    # ---------- 场景10：_split_sentences 行为 ----------
    s10 = _split_sentences("第一句。第二句！第三句？第四句；第五句" + chr(10) + "第六句")
    check("10a 换行参与切分共6句",
          s10 == ["第一句", "第二句", "第三句", "第四句", "第五句", "第六句"], "s10=" + repr(s10))
    s10b = _split_sentences("甲。。乙；；丙")
    check("10b 连续分隔符不产生空句", s10b == ["甲", "乙", "丙"], "s10b=" + repr(s10b))
    s10c = _split_sentences("PMI 53.0 站上荣枯线，但中证500近5日累计-6.9%，市场已在回调")
    check("10c 中文逗号不切分(整句为1句)", len(s10c) == 1, "n=" + repr(len(s10c)))

    print("")
    print("通过 " + str(passed) + "/23")
    return passed == 23


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
