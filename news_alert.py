# -*- coding: utf-8 -*-
"""AnomalyAlertService —— 新闻哨兵（架构师 v1.0，V3 迭代）。
原则：财联社数据仅输入本模块。LLM 管道和日报数据板块永远看不到新闻。
默认无输出；仅当命中一级（直接冲击持仓重仓标的）或二级（行业/宏观冲击且与
当日纪律同向）警报时才返回结构化警报，由日报条件渲染。
V3（用户反馈驱动）：
- build_entity_table 支持 product_ctxs（每个 ctx 带 top10=[{stock_name, weight}]），
  重仓股实体携带关联基金元信息（code/name/weight），命中时警报追加指向性提示
  （该标的为 XX 基金前十大重仓股之一，异动可能影响该基金净值，不编造涨跌方向）。
- format_news_hits(alerts) 把警报转成供 LLM 解读的简化字符串列表（集成者注入 ctx）。
"""
import logging
import re
from datetime import datetime

log = logging.getLogger("news_alert")

# V3：指向性提示的展示上限
_MAX_FUND_PER_STOCK = 2   # 同一股票关联基金超过 N 只时，取占比最高的前 N 只
_MAX_ASSOC_LINES = 3      # 一条警报中追加的关联提示行数上限（与 names[:3] 对齐）


def _normalize_stock_name(v):
    """stock_name 容错：None/空/nan/NaN 一律返回空串"""
    if v is None:
        return ""
    s = str(v).strip()
    if s in ("nan", "None", "NaN", "NONE", "none"):
        return ""
    return s


def _register_top10_stocks(table, fcode, fname, top10):
    """把一只基金的前十大重仓股注入实体表（stocks 集合 + stock_meta 元信息）。
    top10 为 [{stock_name, weight}]（weight 可为 None）。
    返回成功注入的股票数；top10 缺失/为空/全为无效条目时返回 0（调用方回退旧路径）。
    同一基金重复条目按 code 去重。
    """
    if not top10:
        return 0
    added = 0
    for item in top10:
        try:
            name = _normalize_stock_name(item.get("stock_name") if isinstance(item, dict) else None)
        except Exception:
            name = ""
        if not name:
            continue
        weight = None
        try:
            w = item.get("weight") if isinstance(item, dict) else None
            if w is not None and str(w).strip() not in ("", "nan", "None"):
                weight = float(w)
        except (TypeError, ValueError):
            weight = None
        table["stocks"].add(name)
        meta = table.setdefault("stock_meta", {}).setdefault(name, [])
        if not any(m["code"] == fcode for m in meta):
            meta.append({"code": fcode, "name": fname, "weight": weight})
        added += 1
    return added


def _sort_funds(meta):
    """关联基金按占比降序（weight 为 None 排最后），同 code 去重保序"""
    seen = set()
    out = []
    for m in sorted(meta, key=lambda x: (x.get("weight") is None, -(x.get("weight") or 0.0))):
        if m["code"] not in seen:
            seen.add(m["code"])
            out.append(m)
    return out


def build_entity_table(fetcher, products, cfg, product_ctxs=None):
    """实体匹配表：持仓基金重仓股/重仓债名称 + config.news_watch 词表。
    V3 签名：build_entity_table(fetcher, products, cfg, product_ctxs=None)。
    - product_ctxs 提供时：优先用其 top10（[{stock_name, weight}]）注册重仓股实体，
      并为每只股票挂关联基金元信息（一只股票可属于多只基金，保留全部，占比降序）；
      某产品 top10 缺失/为空时回退 akshare API（与旧版行为完全一致）。
    - product_ctxs 为 None 时：完全走旧逻辑（akshare API 拉取）。
    返回 {"stocks": set, "bonds": set, "industries": set, "macro_kw": set,
          "stock_meta": {股票名: [{"code", "name", "weight"}, ...]（占比降序）}}
    """
    watch = cfg.get("news_watch", {})
    table = {
        "stocks": set(),
        "bonds": set(),
        "industries": {str(x).strip() for x in watch.get("industries", []) if x},
        "macro_kw": {str(x).strip() for x in watch.get("macro_kw", []) if x},
        "stock_meta": {},
    }
    # 建立 fund_code -> product_ctx 索引（容错解析）
    ctx_by_code = {}
    for pc in product_ctxs or []:
        try:
            c = str(pc.get("code") or pc.get("fund_code") or "").strip()
            if c:
                ctx_by_code[c] = pc
        except Exception as e:
            log.warning("product_ctx 解析失败: %s", str(e)[:80])
    for p in products:
        fcode = p.get("fund_code", "")
        if not fcode:
            continue
        pc = ctx_by_code.get(fcode) or ctx_by_code.get(str(p.get("code", "")))
        if pc is not None:
            fname = pc.get("name") or p.get("name") or fcode
        else:
            fname = p.get("name") or fcode
        registered = 0
        if pc is not None:
            try:
                registered = _register_top10_stocks(table, fcode, fname, pc.get("top10"))
            except Exception as e:
                log.warning("top10 重仓股实体注册失败 %s: %s", fcode, str(e)[:80])
        if not registered:
            # 旧路径：akshare 拉取（top10 缺失时行为与旧版完全一致；带超时防挂死）
            try:
                df = fetcher._ak(fetcher.ak.fund_portfolio_hold_em, symbol=fcode, date=str(datetime.now().year))
                if df is not None and not df.empty and "股票名称" in df.columns:
                    for name in df["股票名称"].head(10).astype(str):
                        if name and name not in ("nan", "None"):
                            table["stocks"].add(name.strip())
            except Exception as e:
                log.warning("重仓股实体获取失败 %s: %s", fcode, str(e)[:80])
        try:
            dfb = fetcher._ak(fetcher.ak.fund_portfolio_bond_hold_em, symbol=fcode, date=str(datetime.now().year))
            if dfb is not None and not dfb.empty and "债券名称" in dfb.columns:
                for name in dfb["债券名称"].head(10).astype(str):
                    if name and name not in ("nan", "None"):
                        table["bonds"].add(name.strip())
        except Exception as e:
            log.warning("重仓债实体获取失败 %s: %s", fcode, str(e)[:80])
    # 关联基金按占比降序整理（供展示取前 N 只）
    for name in list(table.get("stock_meta", {}).keys()):
        table["stock_meta"][name] = _sort_funds(table["stock_meta"][name])
    return table


def _generate_aliases(name):
    """生成实体别名集合：全称 + 去括号简化 + 前N字（仅中文）。
    3、4字前缀对≥3字名生成；2字前缀仅对≥4字名生成，避免
    "中国平安"→"中国"误匹配"中国经济数据"等无关文本。
    中文金融新闻常用简称/别称（如"宁德"代指"宁德时代"），
    纯全称匹配会漏命中。最小前缀 2 字防止短名误匹配（"债"不会匹配所有债券名）。
    """
    if not name:
        return set()
    aliases = {name}
    # 去括号（中英文括号均处理）
    base = re.sub(r'[（(].*?[)）]', '', name).strip()
    if base and base != name:
        aliases.add(base)
    # 提取纯中文部分，生成前缀别名。
    # 3、4字前缀：对≥3字名生成（3字前缀已足够具体，不易误匹配）。
    # 2字前缀：仅对≥4字名生成，避免"中国平安"→"中国"误命中"中国经济数据"等无关文本。
    cn = re.sub(r'[^\u4e00-\u9fa5]', '', base)
    if len(cn) >= 3:
        for n in (3, 4):
            if len(cn) > n:
                aliases.add(cn[:n])
    if len(cn) >= 4:
        aliases.add(cn[:2])
    return aliases


def match_entities(news_text, table):
    """在新闻文本中匹配实体（含别名）。命中后记录原始全称（非别名），
    保持下游 stock_meta 查找一致。无命中返回 []。
    """
    hits = []
    for kind, names in (("stock", table.get("stocks", set())),
                        ("bond", table.get("bonds", set())),
                        ("industry", table.get("industries", set())),
                        ("macro", table.get("macro_kw", set()))):
        for name in names:
            if not name:
                continue
            matched = False
            for alias in _generate_aliases(name):
                if alias and alias.lower() in news_text.lower():
                    hits.append({"kind": kind, "name": name})  # 记录原始全称
                    matched = True
                    break
            if matched:
                continue
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


def _fund_text(f):
    """单只关联基金的提示片段：基金名（code）（占净值 X%）"""
    code = f.get("code", "")
    name = f.get("name") or code
    w = f.get("weight")
    weight_part = f'（占净值 {w:g}%）' if w is not None else ""
    return f'{name}（{code}）{weight_part}'


def _stock_assoc_lines(table, hits):
    """重仓股命中 → 指向性提示行列表（≤ _MAX_ASSOC_LINES 行，每行基金 ≤ _MAX_FUND_PER_STOCK 只）。
    提示语不编造涨跌方向：新闻含涨停/题材时一律"可能影响该基金净值"。
    """
    meta_map = table.get("stock_meta") or {}
    lines = []
    for h in hits:
        if h["kind"] != "stock":
            continue
        funds = meta_map.get(h["name"]) or []
        if not funds:
            continue
        funds_desc = "；".join(_fund_text(f) for f in funds[:_MAX_FUND_PER_STOCK])
        lines.append(
            f'- 关联：该标的为{funds_desc}前十大重仓股，'
            f'板块/个股异动可能影响该基金净值（仅供参考，非决策指令）'
        )
        if len(lines) >= _MAX_ASSOC_LINES:
            break
    return lines


def _alert_products(table, hits):
    """该警报关联的基金列表 [{code, name}]（按占比降序、去重），供 format_news_hits 用"""
    meta_map = table.get("stock_meta") or {}
    out = []
    seen = set()
    for h in hits:
        if h["kind"] != "stock":
            continue
        for f in meta_map.get(h["name"]) or []:
            c = f.get("code")
            if c and c not in seen:
                seen.add(c)
                out.append({"code": c, "name": f.get("name") or c})
    return out


def process_news(news_list, table, today_orders):
    """扫描财联社电报，返回 alerts 列表或 None。
    alert: {level: 1|2, content: str, original: str, note: str, products: [{code, name}]}
    - level 判定 / 二级同向 note 逻辑与 v1.0 完全一致（阈值不变）。
    - V3：命中重仓股（stock）且带关联基金元信息时，content 追加指向性提示行；
      products 为该警报关联基金（无关联时为 []）。
    """
    alerts = []
    for item in news_list or []:
        text = f'{item.get("title", "")} {item.get("content", "")}'.strip()
        if not text:
            continue
        hits = match_entities(text, table)
        if not hits:
            continue
        names = "、".join(h["name"] for h in hits[:3])
        if is_direct_holding(hits):
            content = f'持仓相关标的 {names} 出现新闻事件，可能影响对应基金净值表现。'
            assoc = _stock_assoc_lines(table, hits)
            if assoc:
                content += '\n' + '\n'.join(assoc)
            alerts.append({
                "level": 1,
                "content": content,
                "original": item.get("content", item.get("title", "")),
                "note": "",
                "products": _alert_products(table, hits),
            })
        else:
            direction = evaluate_direction(text, today_orders)
            if direction == "ALIGNED":
                alerts.append({
                    "level": 2,
                    "content": f'{names} 相关新闻与今日纪律方向一致（印证决策方向）。',
                    "original": item.get("content", item.get("title", "")),
                    "note": "与今日纪律同向，印证决策方向",
                    "products": [],
                })
            else:
                log.info("二级警报不纳入（%s）: %s", direction, text[:60])
    return alerts if alerts else None


def format_news_hits(alerts):
    """把 alerts 转成供 LLM 解读的简化字符串列表（集成者注入 LLM ctx），如：
    ["- [一级警报] 持仓相关标的 中际旭创 出现新闻事件……（关联 003504）"]
    无警报（None/[]）返回 None。
    """
    if not alerts:
        return None
    out = []
    for a in alerts:
        lvl = "一级警报" if a.get("level") == 1 else "二级警报"
        codes = []
        for p in a.get("products") or []:
            c = p.get("code")
            if c and c not in codes:
                codes.append(c)
            if len(codes) >= 3:
                break
        suffix = f'（关联 {"/".join(codes)}）' if codes else ""
        out.append(f'- [{lvl}] {a.get("content", "").replace(chr(10), " ")}{suffix}')
    return out

