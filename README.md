# 每日投顾报告（market_advisor）

规则化投顾日报：每天收盘后采集市场数据，**规则引擎输出确定性信号，AI 只做受约束解读**，推送到微信。**只出建议不自动交易；出入金均由你手动执行。**

## 架构

```
config.json (持仓/关注池/规则/结算参数)
  → DataFetcher (49项数据源: 腾讯/乐咕/上金所/天天基金/雪球/财联社/央行/上交所等)
  → analyze_product (每产品: 净值/收益/回撤/排名/费率/权益暴露/HHI)
  → rules.py (权益目标阶梯 + 市场预警 + 动态止盈V2.2 + 订单簿 + 组合诊断)
  → narrative.CRO (大白话因果链，不调用LLM)
  → news_alert (财联社电报实体匹配，一级/二级警报)
  → llm.generate_insights → AdvisorGatekeeper 三层硬过滤 → 插入各板块
  → 报告组装 (MD + HTML含Chart.js图表 + 精简版)
  → Server酱 → 微信
```

## 模块

| 文件 | 职责 |
|---|---|
| `main.py` | 主编排：数据采集→规则决策→报告组装→推送（含降级兜底/状态板块/图表数据） |
| `rules.py` | 确定性策略引擎：权益目标/市场预警/动态止盈V2.2/订单簿/组合诊断/评分排序 |
| `llm.py` | DeepSeek 解读 + AdvisorGatekeeper（黑名单正则+数字审计+规则ID白名单，>30%回退CRO） |
| `narrative.py` | CRO 叙事：规则信号→人话因果链 + 规则原理锚点 + 术语速查 |
| `news_alert.py` | 财联社电报实体匹配警报（按持仓占比设门槛） |
| `state_store.py` | 状态与记忆：在途资金/冷却期/止盈留痕/推荐日志/状态快照/知识库 |
| `html_render.py` | Markdown→HTML（表格+Chart.js三图表，无第三方依赖） |
| `style.py` | Markdown 美化（中英数字间加空格） |
| `diagnose_position.py` | 基金仓位穿透诊断工具（手工运行） |

## 云部署（GitHub Actions，推荐）

1. push 到 GitHub（公开仓库，敏感数据只放 Secret）
2. Settings → Secrets and variables → Actions 添加：
   - `ADVISOR_CONFIG`：完整 `config.json` 内容（参考 `config.example.json`）
   - `DEEPSEEK_API_KEY`：DeepSeek key
   - `SERVERCHAN_KEY`：Server酱 SendKey（可选，仅失败通知用）
3. 每个交易日北京时间 15:35 自动运行；报告存档 artifacts 90 天；`knowledge_base/` 自动 commit 回写（跨日持久）；Pages 部署完整版网页（`https://Evanlei2025.github.io/market-advisor/latest.html`）

## 本地部署

1. `pip install -r requirements.txt`（akshare==1.18.81 已锁定）
2. 编辑 `config.json`（见 config.example.json 结构）
3. `python main.py --push-off` 试运行 → `python main.py` 正式推送

## 常用命令

```powershell
python main.py --push-off      # 试运行，只生成报告不推送
python main.py                 # 正常（按 config 推送）
python test_tp.py              # 止盈算法 15 场景单测
```

## 决策规则（透明公开，rules.py 权威，AI 无权修改）

- **权益目标**：沪深300 PE分位阶梯（≥95%→5% / ≥90%→20% / ≥80%+动量负→30%）+ 中证500（≥75%+动量<-5%→5%）+ 创业板50代理（≥90%→5%）→ **MIN-MERGE 取最保守**；EP 安全阀动态阈值（随10年国债分位 5%~15%）；AI 可在浮动带内微调（预警期禁用）
- **市场预警 STORM-5**：权益目标触及5%且由阶梯信号触发 → 买入冻结
- **动态止盈 V2.2**：`T_eff=clamp(min(T_pre,T_cap),6%,30%)`，T_pre=T_base×F_vol×F_hold×F_sector；上限门按基准分位压缩；三档穿越（单向取最高档）+峰值回撤保护+短仓地板+费后口径+<7天份额豁免；**影子模式6个月观察期（2026-08-06起），信号仅记录不执行**；近5交易日同档位去重
- **再平衡**：偏离目标区间触发；**无止损**（客户不买个股无爆仓风险，回撤保护兜底）
- **买入约束**：市场预警冻结 / 冷却期 / 在途资金未到账不可用 / 关注池零持仓可推荐买入（评分排序+余额约束）
- **基准对比**：沪深300+中证全债按目标配置复合基准，输出 alpha/beta/信息比率

## 数据源（49 项，全部免费，失败自动降级）

| 数据 | 来源（主→备） |
|---|---|
| 指数行情 | 腾讯 → akshare |
| 指数 PE 分位 | 乐咕（沪深300/中证500/上证红利/创业板50） |
| 基金净值/仓位/HHI | 天天基金（净值+股票仓位穿透+转债+行业配置） |
| 基金资料/费率/业绩 | 雪球 |
| 黄金 | 上金所 Au99.99 → 沪金主力 AU0 |
| 债券收益率/LPR | 英为财情 → 新浪中债；央行 |
| 宏观扩展 | 美债10Y / SC原油 / 沪铜 / CPI / PPI / PMI / 社融 |
| 新闻 | 财联社电报 |
| 汇率/两融/标普 | 外汇牌价 / 上交所 / 新浪 |

## 测试与质量

- `test_tp.py`：止盈算法 15 场景单测（动态档位基准），改动 rules.py 后必须全过
- 数据新鲜度校验：净值/行情滞后自动标注+头部警告；核心数据严重缺失红字提示
- 顶层异常兜底：任何崩溃输出降级报告并推送
- 报告末尾「系统运行状态」板块：49 项数据源成败一览

## 免责声明

本程序按固定规则自动生成参考信息，不构成投资建议。投资有风险，决策需自行判断。
