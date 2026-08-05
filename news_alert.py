# -*- coding: utf-8 -*-
"""AnomalyAlertService —— 新闻哨兵（架构师 v1.0）。
原则：财联社数据仅输入本模块。LLM 管道和日报数据板块永远看不到新闻。
默认无输出；仅当命中一级（直接冲击持仓重仓标的）或二级（行业/宏观冲击且与
当日纪律同向）警报时才返回结构化警报，由日报条件渲染。
"""
import logging
import re
from datetime import datetime

log = logging.getLogger("news_alert")


def build_entity_table(fetcher, products, cfg):
    """实体匹配表：持仓基金重仓股/重仓债名称 + config.news_watch 词表。
    返回 {"stocks": set, "bonds": set, "industries": set, "macro_kw": set}
    """
    watch = cfg.get("news_watch", {})
    table = {
        "stocks": set(),
        "bonds": set(),
        "industries": {str(x).strip() for x in watch.get("industries", []) if x},
        "macro_kw": {str(x).strip() for x in watch.get("macro_kw", []) if x},
    }
    for p in products:
        fcode = p.get("fund_code", "")
        if not fcode:
            continue
        try:
            df = fetcher.ak.fund_portfolio_hold_em(symbol=fcode, date=str(datetime.now().year))
            if df is not None and not df.empty and "股票名称" in df.columns:
                for name in df["股票名称"].head(10).astype(str):
                    if name and name not in ("nan", "None"):
                        table["stocks"].add(name.strip())
        except Exception as e:
            log.warning("重仓股实体获取失败 %s: %s", fcode, str(e)[:80])
        try:
            dfb = fetcher.ak.fund_portfolio_bond_hold_em(symbol=fcode, date=str(datetime.now().year))
            if dfb is not None and not dfb.empty and "债券名称" in dfb.columns:
                for name in dfb["债券名称"].head(10).astype(str):
                    if name and name not in ("nan", "None"):
                        table["bonds"].add(name.strip())
        except Exception as e:
            log.warning("重仓债实体获取失败 %s: %s", fcode, str(e)[:80])
    return table


def match_entities(news_text, table):
    """返回命中的实体清单 [{kind, name}]，无命中返回 []"""
    hits = []
    for kind, names in (("stock", table["stocks"]), ("bond", table["bonds"]),
                        ("industry", table["industries"]), ("macro", table["macro_kw"])):
        for name in names:
            if name and name.lower() in news_text.lower():
                hits.append({"kind": kind, "name": name})
    return hits


def is_direct_holding(hits):
    """一级警报：直接冲击持仓（重仓股/重仓债）"""
    return any(h["kind"] in ("stock", "bond") for h in hits)


def evaluate_direction(news_text, today_orders):
    """二级警报方向判断（架构师 v1.0）：
    "同向"指新闻事件对规则指令所指向资产的影响是正面的（印证决策方向）。
    若无法明确判断，默认不纳入。
    """
    if not today_orders:
        return "UNCLEAR"
    sells = [o for o in today_orders if o.get("side") == "卖出"]
    bond_buys = [o for o in today_orders if o.get("side") == "买入" and o.get("rule_id") == "REB-BOND"]
    text = news_text.lower()
    risk_kw = ["风险", "紧张", "升级", "制裁", "战争", "冲突", "违约", "流动性", "危机", "暴跌", "熔断", "挤兑", "冻结"]
    dovish_kw = ["降息", "宽松", "增持", "买入", "流入", "稳定", "扶持", "释放流动性"]
    if sells and any(k in text for k in risk_kw):
        return "ALIGNED"  # 清仓/减仓 + 风险升级 → 印证离场决策
    if bond_buys and any(k in text for k in dovish_kw):
        return "ALIGNED"  # 买入固收 + 宽松信号 → 印证买入决策
    return "UNCLEAR"


def process_news(news_list, table, today_orders):
    """扫描财联社电报，返回 alerts 列表或 None。
    alert: {level: 1|2, content: str, original: str, note: str}
    """
    alerts = []
    for item in news_list or []:
        text = f"{item.get('title', '')} {item.get('content', '')}".strip()
        if not text:
            continue
        hits = match_entities(text, table)
        if not hits:
            continue
        names = "、".join(h["name"] for h in hits[:3])
        if is_direct_holding(hits):
            alerts.append({
                "level": 1,
                "content": f"持仓相关标的 {names} 出现新闻事件，可能影响对应基金净值表现。",
                "original": item.get("content", item.get("title", "")),
                "note": "",
            })
        else:
            direction = evaluate_direction(text, today_orders)
            if direction == "ALIGNED":
                alerts.append({
                    "level": 2,
                    "content": f"{names} 相关新闻与今日纪律方向一致（印证决策方向）。",
                    "original": item.get("content", item.get("title", "")),
                    "note": "与今日纪律同向，印证决策方向",
                })
            else:
                log.info("二级警报不纳入（%s）: %s", direction, text[:60])
    return alerts if alerts else None
