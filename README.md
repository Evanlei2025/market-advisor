# 每日投顾报告（market_advisor）

规则化投顾日报：每天收盘后采集市场数据，**规则引擎输出确定性信号，AI 只做受约束解读**，推送到微信。**只出建议不自动交易；出入金均由你手动执行。**

**多客户模式**：一个实例服务多个客户。`config.json` 全局段（push/rules 出厂默认/settlement/news_watch）+ `clients` 字典（每客户：target/products/holdings/transactions/组合名）。每客户独立执行：产品分析→指令→叙事→AI 解读→报告→推送；市场数据一次采集共享。当前客户：`Evan_Lei`（真实持仓）、`Harley_Lei`、`NULL_Xue`（有关注池、持仓待确认）、`Echo_Wang`（空壳）。客户接入流程见 `docs/CLIENT_ONBOARDING.md`（含 Server酱注册与 SendKey 获取指南）。

## 架构

```
config.json (全局段 + clients 多客户配置)
  → get_clients()/deep_merge() (客户子配置 = 全局段 + 客户覆盖)
  → DataFetcher (49项数据源: 腾讯/乐咕/上金所/天天基金/雪球/财联社/央行/上交所等) [市场数据共享一次]
  → run_client() 逐客户:
      analyze_product (每产品: 净值/收益/回撤/排名/费率/权益暴露/HHI, 净值同code缓存)
      rules.py (权益目标阶梯 + 市场预警 + 动态止盈V2.2 + 订单簿 + 组合诊断)
      narrative.CRO (大白话因果链，不调用LLM)
      news_alert (财联社电报实体匹配，一级/二级警报)
      llm.generate_insights → AdvisorGatekeeper 三层硬过滤 → 插入各板块
      → 报告组装 (MD + HTML含Chart.js图表 + 精简版)
      → Server酱 → 微信 (每客户一条, 标题带客户名)
```

## 模块

| 文件 | 职责 |
|---|---|
| `main.py` | 主编排：市场采集（共享）→ get_clients/run_client 逐客户（产品分析→指令→AI→报告→推送）→ 客户索引页 |
| `rules.py` | 确定性策略引擎：权益目标/市场预警/动态止盈V2.2/订单簿/组合诊断/评分排序 |
| `llm.py` | DeepSeek 解读 + AdvisorGatekeeper（黑名单正则+数字审计+规则ID白名单，>30%回退CRO）；客户名注入 |
| `narrative.py` | CRO 叙事：规则信号→人话因果链 + 规则原理锚点 + 术语速查 |
| `news_alert.py` | 财联社电报实体匹配警报（实体表按客户 products 构建，天然客户隔离） |
| `state_store.py` | 状态与记忆：在途资金/冷却期/止盈留痕/推荐日志/状态快照（全部按 client 隔离，旧数据归 Evan_Lei） |
| `html_render.py` | Markdown→HTML（表格+Chart.js三图表，无第三方依赖） |
| `style.py` | **已废弃**（早期 Markdown 美化，其 `## 标题→**标题**` 转换会破坏报告结构，由 html_render+compact 取代；仅存档） |
| `diagnose_position.py` | 基金仓位穿透诊断工具（手工运行） |

## 云部署（GitHub Actions，推荐）

1. push 到 GitHub（公开仓库，敏感数据只放 Secret）
2. Settings → Secrets and variables → Actions 添加：
   - `ADVISOR_CONFIG`：完整 `config.json` 内容（含 `clients` 多客户结构，参考 `config.example.json`）
   - `DEEPSEEK_API_KEY`：DeepSeek key
   - `SERVERCHAN_KEY`：Server酱 SendKey（可选，仅失败通知用）
3. 每个交易日北京时间 15:35 自动运行（一次运行遍历全部客户）；报告存档 artifacts 90 天；`knowledge_base/` 自动 commit 回写（跨日持久，按客户隔离）；Pages 部署：`https://Evanlei2025.github.io/market-advisor/`（客户入口 index.html → 各客户 `/<客户ID>/latest.html`）

## 本地部署

1. `pip install -r requirements.txt`（akshare==1.18.81 已锁定）
2. 编辑 `config.json`（见 config.example.json 结构）
3. `python main.py --push-off` 试运行 → `python main.py` 正式推送

## 常用命令

```powershell
python main.py --push-off      # 试运行，只生成报告不推送（全部客户）
python main.py                 # 正常（按 config 推送，每客户一条）
python test_tp.py              # 止盈算法 15 场景单测
python test_clients.py         # 多客户 28 项单测（配置解析/状态隔离）
python test_rules.py           # 规则引擎 76 项单测（权益目标/评分引擎/订单簿/诊断）
python test_gatekeeper.py      # Gatekeeper 23 项单测（数字审计/黑名单/白名单/闸门）
```

## 决策规则（透明公开，rules.py 权威，AI 无权修改）

- **权益目标**：沪深300 PE分位阶梯（≥95%→5% / ≥90%→20% / ≥80%+动量负→30%）+ 中证500（≥75%+动量<-5%→5%）+ 创业板50代理（≥90%→5%）→ **MIN-MERGE 取最保守**；EP 安全阀动态阈值（随10年国债分位 5%~15%）；AI 可在浮动带内微调（预警期禁用）
- **市场预警 STORM-5**：权益目标触及5%且由阶梯信号触发 → 买入冻结
- **动态止盈 V2.2**：`T_eff=clamp(min(T_pre,T_cap),6%,30%)`，T_pre=T_base×F_vol×F_hold×F_sector；上限门按基准分位压缩；三档穿越（单向取最高档）+峰值回撤保护+短仓地板+费后口径+<7天份额豁免；**影子模式6个月观察期（2026-08-06起），信号仅记录不执行**；近5交易日同档位去重
- **再平衡**：偏离目标区间触发；**无止损**（客户不买个股无爆仓风险，回撤保护兜底）
- **买入约束**：市场预警冻结 / 冷却期 / 在途资金未到账不可用 / 关注池零持仓可推荐买入（**V3.1 多维评分排序**：边际贡献20+TCO18+风格中性超额17（贝叶斯收缩）+尾部风险15+运作稳定性15+可交易性15，同类组内百分位+硬门槛过滤：成立<18月/暂停申购·限大额/规模<2亿 出局；经理任期三态；换手抑制≥5分）
- **基准对比**：沪深300+中证全债按目标配置复合基准，输出 alpha/beta/信息比率

## 数据源（49 项，全部免费，失败自动降级）

| 数据 | 来源（主→备） |
|---|---|
| 指数行情 | 腾讯 → akshare |
| 指数 PE 分位 | 乐咕（沪深300/中证500/上证红利/创业板50） |
| 基金净值/仓位/HHI | 天天基金（净值+累计净值复权+股票仓位穿透+转债+行业配置） |
| 基金资料/费率/业绩 | 雪球（含成立时间/规模/经理名） |
| 基金申赎状态 | 天天基金（申购/赎回状态、购买起点、日限额） |
| 黄金 | 上金所 Au99.99 → 沪金主力 AU0 |
| 债券收益率/LPR | 英为财情 → 新浪中债；央行 |
| 宏观扩展 | 美债10Y / SC原油 / 沪铜 / CPI / PPI / PMI / 社融 |
| 新闻 | 财联社电报 |
| 汇率/两融/标普 | 外汇牌价 / 上交所 / 新浪 |

## 测试与质量

- `test_tp.py`：止盈算法 15 场景单测（动态档位基准），改动 rules.py 后必须全过
- `test_clients.py`：多客户 28 项单测（get_clients 解析/客户子配置合并/状态按客户隔离）
- `test_rules.py`：规则引擎 76 项（权益目标阶梯/EP 安全阀/评分引擎 V3.1 全维度/组合诊断/订单簿含硬门槛与换手抑制）
- `test_gatekeeper.py`：Gatekeeper 23 项（Au99.99 负向后行回归/编造数字/黑名单/规则 ID 白名单/方向倾向放行/>30% 闸门）
- 数据新鲜度校验：净值/行情滞后自动标注+头部警告；核心数据严重缺失红字提示
- 顶层异常兜底：任何崩溃输出降级报告并推送；单客户异常不拖垮其他客户
- 报告末尾「系统运行状态」板块：49 项数据源成败一览

## 免责声明

本程序按固定规则自动生成参考信息，不构成投资建议。投资有风险，决策需自行判断。
