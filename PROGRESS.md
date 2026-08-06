# 项目进度记录（2026-08-06 更新）

## 项目：每日投顾报告 → 客户定制推送服务（market_advisor）

**架构一句话：规则引擎权威、AI 只做解读。** 每天 15:35 自动采集市场数据，rules.py 输出确定性信号（权益目标阶梯、市场预警、动态止盈 V2.2、订单簿），narrative.py 转大白话（CRO 叙事），llm.py 调 DeepSeek 解读但被 AdvisorGatekeeper 三层硬过滤拦截越权内容，main.py 编排全链路并推送微信（Server酱）。只出建议不自动交易。

## 已完成 ✅

### 架构与核心（V2.2，2026-08-06）
- **六子智能体**（`.opencode/agents/`）：data-fetcher / rule-engineer / narrative-writer / llm-engineer / news-sentinel / state-memory，并行审查+分工实现
- **市值口径算账**：份额×最新净值（shares/成本/批次台账）
- **动态止盈 V2.2**：`T_eff = clamp(min(T_pre, T_cap), 6%, 30%)`；T_pre = T_base×F_vol×F_hold×F_sector；上限门一票否决（基准分位>70%压缩）；F_sector 景气因子（近3月超额+HHI动态加权）；三档穿越（单向、取最高档）+峰值回撤保护（峰值≥T_eff 且回撤≥clamp(0.35σ,4%,12%)，大赚转亏也触发）+短仓地板 max(T_eff×持有年,6%)+费后口径+<7天份额豁免
- **止损已取消**（客户不买个股、无爆仓风险）；对外只称"市场预警/买入冻结"（STORM-5）
- **关注池买入**（零持仓持续跟踪+余额约束）+冷却期（默认5交易日）+在途资金模型（确认T+1/到账T+2，per_product>per_platform>default）
- **报告结构**：今日一句话 → 指令摘要（触发链）→ 温度表 → 产品跟踪 → 今日跟投指令（资金时间线/影子信号/执行窗口）→ 组合诊断（基准对比 alpha/beta/IR）→ 决策依据（规则ID+人话原理）→ 指标温度表 → 执行回执 → 术语速查；精简版推送 + 完整版 HTML（Chart.js 三图表）
- **知识库**：products/ 档案（成立概述+近半年详述）、watchlist、traces.json 止盈留痕（七字段）、recommendations.json 推荐日志、state_history.json 状态快照——云端 commit 回写跨日持久

### 升级指令书六阶段（2026-08-06，commit 54c9528）
1. **止血**：Gatekeeper 数字审计负向后行断言（修复 Au99.99 误拦截，编造数字仍拦截）；推送 3700 字节裁剪（核心板块优先）；数据新鲜度（净值/指数滞后标注+头部警告）；akshare 锁 1.18.81；列名防御
2. **可靠性**：指数/黄金备选源；顶层 try-except 降级报告+推送；「系统运行状态」板块（49 项数据源成败一览）；workflow pip check + 失败 Server酱通知
3. **学习地基**：影子模式进度显示（第N天/累计M次/涉及K产品）；state_history.json 快照；「昨日信号回顾」板块（学习闭环，数据积累后自动出现）
4. **AI 深度**：max_tokens 5000；注入近5日指数趋势（禁引数字双保险）+昨日回顾；跨市场联动纪律
5. **投资逻辑**：组合基准对比（沪深300+中证全债复合基准，alpha/beta/IR）；买入候选 score_candidate 评分排序；创业板估值（创业板50成分代理，LAD-CYB-90）；EP 安全阀动态阈值（随利率分位 5%~15%）；止盈回撤保护放宽+tier_gap 随波动率动态；validate_config+入口防护
6. **体验**：HTML 表格支持 + Chart.js 三图表（估值温度/仓位饼图/组合净值vs基准）；移动端自适应

### 云托管（2026-08-05 上线）
- GitHub Actions daily-report：cron '35 7 * * 1-5' UTC（北京15:35），注入 Secret：ADVISOR_CONFIG / DEEPSEEK_API_KEY / SERVERCHAN_KEY（失败通知）
- 报告存档 artifacts 90天；knowledge_base commit 回写；Pages 部署（Evanlei2025.github.io/market-advisor/latest.html）
- Server酱推送实测成功（code=0，微信已收到）
- 本地计划任务已删除（防重复推送）

### 测试
- test_tp.py 止盈算法 15 场景（动态档位基准）：单档/跳档/边界/等值/高位不重复/极端亏损/首日缺失/NaN防护/回撤保护/纯债豁免/高估值压缩/去重窗口/转亏回撤/高波动档距——全过
- 端到端（--push-off + AI）：预警、止盈信号、人话版、叙事、基准对比、状态板块全部正常

## 当前状态 🔭

- **止盈影子模式 6 个月观察期**（2026-08-06 起，仅记录不执行），报告中显示进度
- 云端 cron 每日 15:35 运行，traces/recommendations/state_history 跨日积累

## 待办（下一步）🔜

- [ ] 云端新版首跑验证：微信推送 + Pages 网页图表显示正常（含 state_history.json 首次回写）
- [ ] 用户确认 GitHub Secret SERVERCHAN_KEY 已添加（Actions 失败通知用）
- [ ] C 迭代观察：AI 行业推荐板块（宁缺毋滥纪律生效中，连续未输出）
- [ ] D 迭代：多平台结算参数校准（等用户提供且慢/支付宝实测数据）
- [ ] 知识库建档扩展：重点关注产品持续更新档案
- [ ] 影子模式期满后：信号命中率统计、止盈算法转正式

## 关键技术点备忘

- 数据源 49 项全部免费：腾讯行情（指数主源）/akshare（指数备选、PE乐咕、债券、宏观扩展：美债10Y/SC原油/沪铜/CPI/PPI/PMI/社融）/上金所黄金（备选沪金主力AU0）/天天基金（净值、股票仓位穿透、行业HHI）/雪球（资料/费率/业绩）/财联社电报/央行LPR/外汇/两融/标普500
- 乐咕 PE 支持指数名：上证50/沪深300/上证380/创业板50/中证500/上证180/深证红利/深证100/中证1000/上证红利/中证100/中证800（创业板用"创业板50"代理，报告脚注透明标注）
- 规则 ID 体系（Gatekeeper 白名单同源）：TP-YIELD-1/2/3、TP-DD、LAD-CSI300-80/90/95、LAD-CSI500-75、LAD-CYB-90、MIN-MERGE、EP-CAP-10（动态阈值）、STORM-5、REB-EQ、REB-BOND、BUY-NEW；止损 SL-* 已全部删除
- 推送限制：Server酱约3800字节，build_compact 按核心板块优先裁剪；免费额度 5 条/天
- 云端无跨日磁盘，跨日持久唯一通道 = knowledge_base git 回写（traces/recommendations/state_history）
- 用户持仓：006195 国金量化多因子股票A（233.55份）、014846 博时恒乐债券A（1407.30份）、003504 已清仓仅观察、现金0；在途：006195 赎回 116.78份 8-10 到账约¥351；平台=招商证券
- 用户操作习惯：出入金手动执行；config 每次交易后需更新（Secret ADVISOR_CONFIG 为云端事实源）
