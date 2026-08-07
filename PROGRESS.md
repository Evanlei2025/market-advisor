# 项目进度记录（2026-08-07 更新）

## 项目：每日投顾报告 → 客户定制推送服务（market_advisor）

**架构一句话：规则引擎权威、AI 只做解读。** 每天 15:35 自动采集市场数据，rules.py 输出确定性信号（权益目标阶梯、市场预警、动态止盈 V2.2、订单簿），narrative.py 转大白话（CRO 叙事），llm.py 调 DeepSeek 解读但被 AdvisorGatekeeper 三层硬过滤拦截越权内容，main.py 编排全链路并推送微信（Server酱）。只出建议不自动交易。**多客户模式**：每客户独立组合/指令/报告/推送（详见下方）。

## 已完成 ✅

### 多客户改造（2026-08-07）
- **config 结构**：全局段（push/rules 出厂默认/settlement/news_watch）+ `clients` 字典（Evan_Lei 原数据迁入含调优参数 tier_gap；Harley_Lei/NULL_Xue 空壳客户，不继承 Evan 调优，走出厂默认）
- **get_clients()/deep_merge()**：客户子配置 = 全局段+客户段深度合并（list 客户覆盖）；旧形态配置向后兼容
- **主流程重构**：市场数据（指数/估值/EP/债市/宏观/黄金/新闻）一次采集共享，`run_client()` 逐客户执行产品分析（净值同 code 缓存）→ 指令 → 叙事 → 哨兵 → AI 解读（空客户跳过）→ 报告 → 推送 → 快照；单客户异常不拖垮全链路
- **状态客户化**：traces/recommendations/state_history 条目带 client 字段，函数支持 client 过滤，旧条目归并 Evan_Lei；推荐计数按客户隔离；冷却期/在途经客户子配置天然隔离；快照按 (date, client) 去重
- **输出与推送**：`reports/<客户ID>/report_日期.md/.html + latest.html`；根 index.html 客户入口页；每客户一条推送（标题带客户名，链接指向各自 latest.html）
- **修复**：空客户 eq_target NameError 隐患（B1 发现）、_client_of 布尔比较 bug、llm.py 客户名注入（向后兼容验证）
- **测试**：test_clients.py 25 项全过（四客户解析/merge 语义/旧配置兼容/状态隔离）；test_tp.py 15/15 回归；端到端四客户四报告+索引页生成成功
- **客户数据导入（2026-08-07）**：Harley_Lei 关注池 001480 财通成长优选混合A、675123 西部利得汇逸债券C；NULL_Xue 关注池 025687 国泰半导体制造精选混合发起C、006195、016347 招商中证煤炭等权指数(LOF)E、022720 广发港股通央企红利ETF联接C；新增 Jing_Wang 空壳客户；持仓/平台未知，长期留空
- **客户接入指南**：docs/CLIENT_ONBOARDING.md（注册 Server酱/获取 SendKey 全流程，可转发客户；客户级 push 覆盖天然支持每客户独立微信推送）

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
- 报告存档 artifacts 90天；knowledge_base commit 回写；Pages 部署（Evanlei2025.github.io/market-advisor/ → index.html 客户入口，各客户 latest.html 独立页面）
- Server酱推送实测成功（code=0，微信已收到）
- 本地计划任务已删除（防重复推送）

### 测试
- test_tp.py 止盈算法 15 场景（动态档位基准）：单档/跳档/边界/等值/高位不重复/极端亏损/首日缺失/NaN防护/回撤保护/纯债豁免/高估值压缩/去重窗口/转亏回撤/高波动档距——全过
- test_clients.py 多客户 25 项：get_clients 四客户解析/客户子配置合并（Evan 调优保留、新客户走默认）/旧配置兼容/deep_merge 不改原对象/状态按客户隔离（旧条目归 Evan）/快照同日共存/推荐计数隔离
- 端到端（--push-off + AI）：四客户四份报告 + 客户索引页 + 空客户降级报告全部正常（016347 雪球资料无数据自动降级）

## 当前状态 🔭

- **止盈影子模式 6 个月观察期**（2026-08-06 起，仅记录不执行），报告中显示进度
- 云端 cron 每日 15:35 运行，traces/recommendations/state_history 跨日积累（按客户隔离）
- 客户：Evan_Lei（006195/014846/003504 关注池+真实持仓）；Harley_Lei（关注 001480/675123）；NULL_Xue（关注 025687/006195/016347/022720）；Jing_Wang（空壳）——后三者持仓/平台未知，长期留空

## 待办（下一步）🔜

- [ ] 云端四客户版首跑验证（更新 Secret ADVISOR_CONFIG 后触发）：微信四条推送 + Pages 客户入口页四链接
- [ ] 向 Harley_Lei / NULL_Xue / Jing_Wang 发送 docs/CLIENT_ONBOARDING.md，索要 Server酱 SendKey；收到后配 per-client push 并更新 Secret
- [ ] 客户持仓信息到位后：填 holdings，按各自风险偏好设 target（出厂默认起步，非 Evan 调优值）
- [ ] 用户确认 GitHub Secret SERVERCHAN_KEY 已添加（Actions 失败通知用）
- [ ] C 迭代观察：AI 行业推荐板块（宁缺毋滥纪律生效中，连续未输出）
- [ ] D 迭代：多平台结算参数校准（等用户提供且慢/支付宝实测数据）
- [ ] 知识库建档扩展：重点关注产品持续更新档案
- [ ] 影子模式期满后：信号命中率统计、止盈算法转正式
- [ ] news_alert「按持仓占比设警报门槛」：README 已承诺但代码未实现（文档漂移），实现时分子/分母用客户级市值

## 关键技术点备忘

- 数据源 49 项全部免费：腾讯行情（指数主源）/akshare（指数备选、PE乐咕、债券、宏观扩展：美债10Y/SC原油/沪铜/CPI/PPI/PMI/社融）/上金所黄金（备选沪金主力AU0）/天天基金（净值、股票仓位穿透、行业HHI）/雪球（资料/费率/业绩）/财联社电报/央行LPR/外汇/两融/标普500
- 乐咕 PE 支持指数名：上证50/沪深300/上证380/创业板50/中证500/上证180/深证红利/深证100/中证1000/上证红利/中证100/中证800（创业板用"创业板50"代理，报告脚注透明标注）
- 规则 ID 体系（Gatekeeper 白名单同源）：TP-YIELD-1/2/3、TP-DD、LAD-CSI300-80/90/95、LAD-CSI500-75、LAD-CYB-90、MIN-MERGE、EP-CAP-10（动态阈值）、STORM-5、REB-EQ、REB-BOND、BUY-NEW；止损 SL-* 已全部删除
- 推送限制：Server酱约3800字节，build_compact 按核心板块优先裁剪；免费额度 5 条/天
- 云端无跨日磁盘，跨日持久唯一通道 = knowledge_base git 回写（traces/recommendations/state_history）
- 用户持仓（Evan_Lei）：006195 国金量化多因子股票A（233.55份）、014846 博时恒乐债券A（1407.30份）、003504 已清仓仅观察、现金0；在途：006195 赎回 116.78份 8-10 到账约¥351；平台=招商证券
- 多客户纪律：新客户参数用出厂默认（非 Evan 调优值，如 tier_gap 动态算法接管）；Harley_Lei/NULL_Xue/Jing_Wang 目标仓位待各自产品信息到位后设定
- 客户推送：全局 push 默认推服务方本人微信；客户提供 SendKey 后在其客户段加 push 覆盖（客户级优先），实现每客户独立微信接收
- 用户操作习惯：出入金手动执行；config 每次交易后需更新（Secret ADVISOR_CONFIG 为云端事实源）
