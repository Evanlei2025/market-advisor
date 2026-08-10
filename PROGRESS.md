# 项目进度记录（2026-08-07 更新）

## 项目：每日投顾报告 → 客户定制推送服务（market_advisor）

**架构一句话：规则引擎权威、AI 只做解读。** 每天 15:35 自动采集市场数据，rules.py 输出确定性信号（权益目标阶梯、市场预警、动态止盈 V2.2、订单簿），narrative.py 转大白话（CRO 叙事），llm.py 调 DeepSeek 解读但被 AdvisorGatekeeper 三层硬过滤拦截越权内容，main.py 编排全链路并推送微信（Server酱）。只出建议不自动交易。**多客户模式**：每客户独立组合/指令/报告/推送（详见下方）。

## 已完成 ✅

### 报告 V3 迭代（用户反馈驱动，2026-08-07）
- **砍重复**：删独立「今日指令摘要」板块，置顶合并为「今日一句话」（headline+警告+触发链+锚点，headline 全报告仅 1 次）；指标温度表精简为 ≤10 行结论表（不复述数值）；完整版报告 256→147 行（Evan），推送 compact 纳入「决策依据」板块
- **数据修复**：社融 0 亿 bug（akshare 单位已是亿元，误除 1e8）+ 显示统计期（月度数据）；CPI/PPI 时间倒序 bug（取到 2008/2006 假数据）+ 合理性校验（|v|>15 标异常）；市场速览每指数追加成交额（qt.gtimg f[37]）；「上证红利」替换「中证红利」统一 PE 口径（速览/温度表同一组 4 指数）
- **重仓股数据源**：akshare 季报前十大重仓股（必须传当前年份 date），按 (code,季度) 缓存 knowledge_base/products/<code>_top10.json；新闻哨兵命中重仓股 → 警报附「该标的为 X 基金前十大重仓股（占净值 Y%），异动可能影响净值」；产品 ctx/LlM prompt 注入 top10
- **产品跟踪增强**：状态标签（✅持有 / 👀观察中 / ⚠️止盈信号持续中·第N天）替换硬编码「持有观察」；类型/风险不匹配提醒（003504 债券型但权益暴露 31%）；近远期背离提醒（006195 近1年+20% vs 近3月-13.8%）
- **闭环回顾**：止盈去重改为「持续中（第 N 天）」不隐藏；组合诊断加实际权益暴露 vs 目标差距行（含原因：买入冻结/EP锁定/资金在途）+ 近1月/近3月组合 vs 基准对照 + 规模较小代表性提示（min_stat_mv 默认 5 万，config 可覆盖）；执行回执显示上次反馈历史（state.json feedback 数组，维护者手动记录）
- **AI 做深**：SYSTEM_PROMPT 新增 summary 板块（宏观因果链 ≥2 快照数值 + 跨市场 + 新闻传导 + 方向倾向「防御/中性/积极」，≤180字）；action 禁复读 CRO 叙事；产品解读须点出风险/背离/传导；Gatekeeper 放行方向倾向、新增预测词/软化词黑名单；news_hits 注入 prompt
- **其他**：债市→持仓衔接句、宏观指标决策指向注释（PMI/CPI/PPI/社融/美债）、黄金「暂不单独配置」说明、估值温度口径注、系统运行状态失败项影响分级标注（核心/明细）、动量方向括号标注（近20天累计下跌约7%）
- **验证**：test_tp 15/15、test_clients 28/28、端到端四客户 17 项验收全过（行数/板块/标签/统计期/成交额/温度表四客户齐）
- **config 结构**：全局段（push/rules 出厂默认/settlement/news_watch）+ `clients` 字典（Evan_Lei 原数据迁入含调优参数 tier_gap；Harley_Lei/NULL_Xue 空壳客户，不继承 Evan 调优，走出厂默认）
- **get_clients()/deep_merge()**：客户子配置 = 全局段+客户段深度合并（list 客户覆盖）；旧形态配置向后兼容
- **主流程重构**：市场数据（指数/估值/EP/债市/宏观/黄金/新闻）一次采集共享，`run_client()` 逐客户执行产品分析（净值同 code 缓存）→ 指令 → 叙事 → 哨兵 → AI 解读（空客户跳过）→ 报告 → 推送 → 快照；单客户异常不拖垮全链路
- **状态客户化**：traces/recommendations/state_history 条目带 client 字段，函数支持 client 过滤，旧条目归并 Evan_Lei；推荐计数按客户隔离；冷却期/在途经客户子配置天然隔离；快照按 (date, client) 去重
- **输出与推送**：`reports/<客户ID>/report_日期.md/.html + latest.html`；根 index.html 客户入口页；每客户一条推送（标题带客户名，链接指向各自 latest.html）
- **修复**：空客户 eq_target NameError 隐患（B1 发现）、_client_of 布尔比较 bug、llm.py 客户名注入（向后兼容验证）
- **测试**：test_clients.py 25 项全过（四客户解析/merge 语义/旧配置兼容/状态隔离）；test_tp.py 15/15 回归；端到端四客户四报告+索引页生成成功
- **客户数据导入（2026-08-07）**：Harley_Lei 关注池 001480 财通成长优选混合A、675123 西部利得汇逸债券C；NULL_Xue 关注池 025687 国泰半导体制造精选混合发起C、006195、016347 招商中证煤炭等权指数(LOF)E、022720 广发港股通央企红利ETF联接C；新增 Echo_Wang 空壳客户（原 Jing_Wang 更名）；持仓/平台未知，长期留空
- **客户 SendKey 导入（2026-08-07）**：Harley_Lei、Echo_Wang 各自客户级 push 覆盖（独立微信接收）；Evan_Lei/NULL_Xue 暂走全局 key
- **客户接入指南**：docs/CLIENT_ONBOARDING.md（注册 Server酱/获取 SendKey 全流程，可转发客户；客户级 push 覆盖天然支持每客户独立微信推送）

### 架构与核心（V2.2，2026-08-06）
- **六子智能体**（`.opencode/agents/`）：data-fetcher / rule-engineer / narrative-writer / llm-engineer / news-sentinel / state-memory，并行审查+分工实现
- **市值口径算账**：份额×最新净值（shares/成本/批次台账）
- **动态止盈 V2.2**：`T_eff = clamp(min(T_pre, T_cap), 6%, 30%)`；T_pre = T_base×F_vol×F_hold×F_sector；上限门一票否决（基准分位>70%压缩）；F_sector 景气因子（近3月超额+HHI动态加权）；三档穿越（单向、取最高档）+峰值回撤保护（峰值≥T_eff 且回撤≥clamp(0.35σ,4%,12%)，大赚转亏也触发）+短仓地板 max(T_eff×持有年,6%)+费后口径+<7天份额豁免
- **止损已取消**（客户不买个股、无爆仓风险）；对外只称"市场预警/买入冻结"（STORM-5）
- **关注池买入**（零持仓持续跟踪+余额约束）+冷却期（默认5交易日）+在途资金模型（确认T+1/到账T+2，per_product>per_platform>default）
- **报告结构**：今日一句话（含触发链）→ 市场速览（含成交额/动量方向）→ 估值温度 → 债市与利率（含持仓衔接句）→ 宏观与资金面（含决策指向）→ 黄金 → 理财产品跟踪（含状态标签/风险提醒）→ 今日跟投指令（止盈持续中/在途资金）→ 异常事件预警（含重仓股关联）→ 组合诊断（基准对比/规模提示/近月对照）→ 决策依据 → 指标温度表（结论表）→ 执行回执（上次反馈）→ 术语速查；完整版 ≤160 行；精简版推送 + 完整版 HTML（Chart.js 三图表）
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
- 客户：Evan_Lei（006195/014846/003504 关注池+真实持仓）；Harley_Lei（关注 001480/675123，已配 SendKey）；NULL_Xue（关注 025687/006195/016347/022720）；Echo_Wang（空壳，已配 SendKey）——持仓/平台未知，长期留空
- AI 综述/方向倾向板块：云端实测通过（2026-08-07，核对清单 1-4 符合）；本地无 DEEPSEEK_API_KEY 时跳过（fallback 复读 CRO 叙事文本）

## 技术治理（2026-08-07）

### 已完成 ✅
- **数据去重（P0）**：record_trace 写入前按 (signal_date,code,action,client) 查重（存在则原位更新）；kb_read_traces/kb_read_state_history **读取层去重**（同键保留最后一条，防跨 git 并发合并残留——根因是云端/本地多次运行 + 无条件追加 + git 数组合并，非多客户循环）；存量清理 traces 7→2 条、state_history 7→5 条（删除改名遗留 Jing_Wang 条目）；_norm_client 新增显式改名映射表 `CLIENT_ALIASES={"Jing_Wang":"Echo_Wang"}`（未来改名在此登记，不做"未知归并 Evan"避免污染统计）
- **测试补缺（P1）**：test_rules.py 68/68（equity_target 15 场景+ep_threshold 9+score_candidate 13+portfolio_diagnostics 8+build_order_book 23，零网络，akshare 日历用假实现替换）；test_gatekeeper.py 23/23（Au99.99 放行/编造数字拦截/黑名单含 V3 预测软化词/ID 白名单/方向倾向放行/>30% 闸门/30% 边界精确判定）
- **代码整洁（P1）**：style.py 确认废弃（其 `## 标题→**标题**` 转换会破坏 split_blocks/html_render/build_compact），main.py 删除 `import style`，文件头加废弃注释；requirements.txt 锁定 `requests>=2.31,<3.0`、`pandas>=2.0,<3.0`
- **config 密钥核验（1.3）**：config.json 已在 .gitignore（含中文注释说明）、git ls-files 仅 config.example.json、example 密钥全为占位符——已达标无需改动

### 测试发现待修点（本轮未修，记账）
- `build_order_book` 中 `pending_lines` 生成后未追加进 summary_lines（死代码，pending_cash 不影响输出）
- `add_trading_days` 的 akshare 失败回退 `str + timedelta` 会 TypeError（测试用假实现规避，生产路径 akshare 正常时不会触发）
- `portfolio_diagnostics` 的 `products` 参数实际未使用
- 费率解析口径：`v>1.0 → v/100`（百分数），`v≤1.0 → 原值`（小数）——配置 0.5 会被解析为 50%（**已由 V3.1 重构覆盖：TCO 维度接口边界写死小数口径**）
- score_candidate 的 max_dd 剪裁 `max(0,min(1,1-mdd))`：正负大回撤同分（-0.5 与 -0.05 均 100 分）（**已由 V3.1 重构覆盖：改 1-abs(mdd)**）
- 待修（剩余 3 项）：`build_order_book` 的 pending_lines 死代码；`add_trading_days` 回退 TypeError（str+timedelta）；`portfolio_diagnostics` 的 products 参数未使用

### score_candidate 评分引擎重构（V3.1，2026-08-07，用户深度评审驱动）
- **评审结论**：原算法"用一年的运气给基金颁奖且方向相反"——目标函数矛盾（缺口要权益 beta 却选低波）、夏普未减 rf 给低波产品送分、回撤维度双 bug 失效、50 分押在噪声估计量上、无组合视角
- **P0 止血**：max_dd 解析剥 %（真实链路字符串 "-17.9%" 致维度静默跳过）+ 公式 `1-abs(mdd)`；夏普减 rf（rf 链：`y2/100` → `(y10-0.4)/100` → 0.015 → 0.0，零新增数据源，实测 y2=1.25→0.0125）；σ 年化下限 1%（防波动趋零爆表）；净值一阶自相关 >0.9 平滑检测（摊余成本/平滑产品不进正常池）；成立 <18 个月硬门槛；数据完整度 <80% 出局
- **复权口径（加列不换列）**：`fund_nav_history` 加 `nav_adj` 列——累计净值 → `r_t=(A_t−A_{t−1})/NAV_{t−1}`（分母严格单位净值）→ cumprod 重建复权序列；`|r_t|>15%` 回退 growth 列护栏（免费交叉校验）；拉取失败 nav_adj=nav 降级。迁移 5 处（score_candidate 夏普 / analyze_product max_dd 与区间收益 rb / ewma_vol / returns_map→alpha·beta·IR）；**资金会计 5 处单位净值不动**（estimate_shares/market_value_weights/hold_metrics/nav_map/nav_str）；dynamic_tp_line 的 peak/cur **暂缓**（与成本台账口径错位会误触发 TP-DD，等分红再投资台账，shadow_mode 兜底）。实证：014846 分红债基近1年复权 +6.64% vs 单位 +5.99%；003504 全历史复权 +72.15% vs +63.76%（+8.39pp）
- **P1 结构重构**：同类分组（大类 equity/mixed/bond/gold × 风格 指数/量化/主题/主动）+ winsorize(1%,99%) 后**组内百分位**（模块级 `_candidate_pool` 池机制，无池降级 0.5）；**新权重表 Σ=100**：边际贡献 20（与持仓收益相关性 + top10 重合度）/ TCO 18（管理+托管+销售服务费×持有年数+申购+短期赎回，1−合计/0.02）/ 风格中性超额 17（0.5×夏普+0.5×收缩百分位）/ 尾部风险 15（MaxDD+CVaR95+下行波动）/ 运作稳定性 15（经理任期三态 10 + 规模类型分叉曲线 5：指数/ETF 无上限惩罚、主动有容量上限）/ 可交易性 15（申购状态+日限额+最短持有）；缺失值组内中位数填充 + 完整度折扣 `×(0.9+0.1×完整度)`；合成 `100×Σ(w·s)×Π(penalty)`（尾部/可交易 <0.3 → ×0.6）；**换手抑制**（held 产品排序键 +5 分缓冲）
- **P2**：经验贝叶斯收缩 `λ=τ²/(τ²+SE²)`（SE=sqrt((1+SR²/2)/年数)，τ=组内横截面离散；1 年夏普 SE≈1.2 → 收缩 85%）；**同类排名维度删除**（与夏普共线）；饱和率/缺失率埋点输出
- **硬门槛（cands 过滤）**：成立<18 月 / 暂停申购·限大额（fund_purchase_em 实测 006195="限大额"）/ 规模<2 亿 → 剔除；缺失 fail-open 不误杀
- **经理任期三态**：config products 可选 `manager_since`（人工，优先级最高）+ state_store 经理变更侦测快照（`record_manager_snapshot`/`manager_tenure_days`，经理名变化日=新任期起点，冷启动配合人工字段）；已知≥12 月不惩罚 / <12 月 ×0.85 / 未知 ×0.95 + 报告"经理任职日期未知"标签（与 nav_stale ⚠️ 同模式）
- **昨日风暴对照接线**：状态快照补 `storm_active`/`eq_target` 字段（此前函数支持但调用未传）+ 报告"昨日信号回顾"区显示"昨日市场预警（买入冻结）→ 今日已解冻/仍未解除/新触发"
- **数据源新增**：累计净值走势（复权）、fund_purchase_em 申购状态（全市场表进程内缓存 9.4s/次）、雪球基础信息（成立时间/最新规模/经理名/类型）
- **验证**：test_rules 76/76（G3 24 条含 rf 生效/解析/收缩/门槛 + G5 23 条含四门槛剔除/换手抑制）、四测试全绿；端到端四客户通过；V3.1 自下一个交易日云端 cron 生效

### 迭代四：清理 + IC 回测框架（2026-08-08）
- **记账项 3 项修复**：build_order_book 的 pending_lines 死代码删除（main.py 已有在途资金提醒，避免推送重复字节）；add_trading_days 回退 TypeError 修复（str→date 转换后 +timedelta）；portfolio_diagnostics 删未用 products 参数（签名+调用处+test_rules 7 处同步）
- **record_feedback.py CLI**：`python record_feedback.py <client> <date> <status> [note]` 手动录入执行回执（stdout utf-8 修复）
- **知识库建档扩展**：5 份关注池产品档案（001480 财通成长优选/675123 西部利得汇逸债券C/025687 国泰半导体制造C/016347 招商煤炭等权E/022720 广发港股通央企红利联接C；016347 雪球接口降级由天天基金 pingzhongdata 补全），现有 8 份档案齐
- **ic_backtest.py（IC 回测框架）**：离线 walk-forward + purge/embargo（间距≥20 交易日，无重叠标签稳健版 ICIR）；逐因子 Spearman Rank IC + ICIR；复用 nav_adj 复权与生产同源因子解析；基准映射（equity→沪深300/csi500、bond→中证全债、mixed→50/50）；point-in-time 意识（因子仅用 t 及以前数据，规模/同类排名无历史序列注明未测）；输出 reports/ic_report_<date>.md（gitignore）
- **IC 实证结果（8 产品 2021-2026，样本小仅参考）**：夏普 ≈0（验证"无预测力"预测）；风格中性超额弱正（稳健版 ICIR 0.40/0.41）；尾部风险负向（风险补偿效应）；**费率 IC 负向与"最稳定为正"预测相反**（样本小+类型混杂：债基低费率低收益主导，结论不自动改权重，待样本扩充后重测）
- **监控埋点升级（B2）**：`[candidate_monitor] pool=N group=... sat={维度:饱和率} missing={...} scores=[...]`，饱和率>30% 追加 ⚠️SAT 告警
- 验证：四测试全绿（15/15+28/28+76/76+23/23）；端到端通过

### 迭代五：state 云端持久化 + 数据源审计 + 监控接入（2026-08-09）
- **A1 state.json 云端持久化**：新增 knowledge_base/feedback.json 与 manager_snapshots.json（git 回写通道）；record_feedback/record_manager_snapshot 双写（state.json 本地缓存 + kb 事实源），get_feedback/manager_snapshot 读 kb 优先→state.json 回退；API 签名全不变；>200 清理双写一致
- **经理快照接线修复**（V3.1 缺口：record_manager_snapshot 零调用）：main.py run_client 产品分析后每日记录经理名（7 产品已入 kb，016347 资料接口失败不写属正常降级）；跨日自然积累任期（since 变化日=新任期起点）
- **B3 监控接入报告**：rules.py 新增 `candidate_monitor_line()`（消费式 getter，不破坏 build_order_book 签名）；main.py 系统运行状态板块追加"候选评分监控"行（pool/group/sat/missing/⚠️SAT，无候选评分时不出行）
- **A2 数据源审计**：docs/DATA_SOURCE_AUDIT.md（akshare 1.18.81 实测 5 维度）：持有人结构 ✅F10 cyrjg（半年报，675123 机构占比 98.72% 实测）；换手率 ✅fund_portfolio_change_em；债基杠杆 ✅F10 zcfzb（1.008x 实测）/久期信用 ✗降级 config；A/C 份额 ✅fund_name_em+雪球费率表；规模历史 ✅F10 gmbd 季度序列（天然 point-in-time 防重述）。4.5 项纳入迭代六接入候选，统一天天基金 F10 三通道解析器（cyrjg/gmbd/zcfzb）
- 验证：四测试全绿（15/15+28/28+76/76+23/23）；强制端到端（patch 交易日）经理快照写入验证

### 迭代六·D：去「影子模式」+ 活指标卡片化 + 变量/不变量配色（2026-08-10）
- **文案「影子模式」→「观察」**（客户反馈看不懂底层术语）：13 处用户可见文案全改（产品状态标签「止盈信号观察中（第 N 天，仅记录不执行）」/「观察已进行 N 天（起始 …，共 6 个月）」/narrative/rules 摘要/llm 提示词与输出模板/推送继承）；代码层配置键 shadow_mode、API shadow_stats、trace.shadow 字段不动；test_rules 断言同步
- **活指标卡片化**（渲染层）：变量（动态数值）= 蓝色短卡 `.val`（浅蓝底蓝字、圆角 8px、tabular-nums），静态文案 = 默认色；涨跌红绿/温度条/徽章/诊断数字卡保留语义色
  - 主卡规则：「：」锚（≤14 字标签）或白名单行词（净值/近1年最大回撤/同类排名/费用/权益暴露/组合市值/上次反馈）→ 后接数值或 ≤8 字文字+数字 → 数值区截断于深度 0 的 ，；→⚠️（括号内千位逗号/日期跳过）→ 整段蓝卡；标签与卡同行 inline（不独占一行，占满自动换行）
  - 细卡（决策依据行）：PE分位 N%、权益目标/上限 = N%
  - pct 冲突跳过：「分位 N%」位置让给温度条（不重复）；「PE分位」细卡覆盖并跳过温度条
  - 天然排除：术语速查/人话/注/研判/系统说明（文字开头无数字）；warn/note 行不卡
  - 实测：Evan_Lei 44 卡 / NULL_Xue 44 卡 / 术语零误伤；「调仓后目标仓位」移出 note 行（活值卡化）
- **回归**：四测试 15/15+28/28+84/84+23/23；e2e 全量重生成；「影子模式」0 残留

### 迭代六·C：矛盾修复 + 详情页目录 + 推送重构（2026-08-10）
- **Bug 修复（数据源错位）**：「今日一句话」误报「无规则触发」根因 = main.py CRO 构造传了 `tp_actions`（仅今日新动作，repeat 被订单簿去重剔除）而持续信号在 `tp_ctx_map` → 改为合并 tp_ctx_map 构造 tp_sig_ctx（name/action/amount/streak_days）；narrative SIGNAL 分支 amount 为 None 时显示「持续观察中（第 N 天，影子模式，仅记录）」；Evan_Lei 实测一句话变为「止盈信号（观察期）…」+ 触发链 ✓
- **「今日一句话」hero 卡**（方案 A）：整卡 180° 蓝→白渐变浸入 + 蓝色柔和阴影，blockquote 透明无边框，无硬切（深色 rgba(10,132,255,.30)）
- **「｜」一律换行**（渲染层）：列表行按「｜」拆段成行（li-line），分位温度条插入所在段尾；段落/表格/blockquote 内换行；区间收益保留 chips 横条卡（用户批准）；md 源与推送保持紧凑
- **详情页目录结构**：sticky 毛玻璃胶囊目录条（当前章节 IntersectionObserver 高亮）+ 章节折叠手风琴（默认展开「今日一句话/理财产品跟踪/今日跟投指令」其余折叠；点击分组头或目录项展开并平滑滚动；scroll-margin-top 防遮挡；reduced-motion 兼容）
- **推送重构**：build_compact 仅两板块「理财产品跟踪 + 今日跟投指令」；空用户（无持仓无关注）→ 正文空白仅详情网址；超长 3700 字节保护改为裁剪产品板块细节行（保留规则信号行）；实测正常 2978B / 空用户 / 超长 1372B ✓
- **回归**：四测试 15/15+28/28+84/84+23/23；5 页面结构闭合；e2e 全量重生成

### 迭代六·B：网页 iOS 原生级重设计（2026-08-10）
- **设计规范**：Apple HIG（System Colors / Inset Grouped 卡片 / SF Pro+PingFang / 8pt 间距网格 / 深色模式跟随系统 / 毛玻璃导航条）；学习途径：Apple 官方文档需 JS 无法抓取，基于内化 HIG 规范 + 主流 iOS 财富 App 范式
- **html_render.py 全面重写**（签名 render(md, title, charts) 不变，main.py 零改动）：
  - 设计令牌双主题 CSS 变量（浅 #F2F2F7/#FFFFFF、深 #000/#1C1C1E、语义蓝红绿橙、0.5px 分隔线、双层柔和阴影）
  - sticky 毛玻璃导航条（backdrop-filter + rgba 兜底）+ 大标题 34px + 章节分组头
  - h2 → Inset Grouped 白色圆角卡片（16px 圆角），h1 前图表区独立卡片化
  - 列表行 iOS 设置页样式（0.5px 分隔线、44px+ 触控区）、表格去边框化（行分隔线+灰色表头）、blockquote → iOS 提醒卡（「今日一句话」渐变信息卡特型）
  - 入口页：链接行卡片化 + chevron
- **文字数据可视化（渲染层解析，不改 markdown）**：
  - 分位温度条：「分位 N%」→ 蓝-橙-红色阶条（hsl 210-0 映射）+ 数字标签（估值/利率/性价比 15 处）
  - 区间收益条：产品「近1周/1月/3月/6月/1年」→ 红涨绿跌横向条 chips
  - 组合诊断数字格：波动率/250日最大回撤/日度VaR95 → iOS 健康 App 风格数字卡（含人话解释）
  - 涨跌着色（🟢→绿 🔴→红）、规则信号徽章（✅/👀/⚠️ → 彩色 pill）、⚠️ 警示行红字、注释行弱化
- **Chart.js 升级**：估值条温度色阶圆角柱 / 饼图 iOS 色板 cutout 62% / 净值曲线渐变填充+基准虚线；**深色模式**：脚本读 getComputedStyle CSS 变量动态取色（深浅自动切换）
- 动效：淡入 + 卡片 stagger（prefers-reduced-motion 全关）；meta theme-color 双模式
- **修复**：⚠️ 双码点正则（[⚠️]→(✅|👀|⚠️) 交替）；饼图 cutout '62%' 撞 % 格式化（62%%）；产品行括号残缺（_PROD_ROW 简化）；收益条 ++ 重复加号
- **验证**：4 客户+入口页 HTML 结构闭合检查（Evan_Lei 3 图表/15 组/3 产品卡/3 诊断格/15 温度条/15 收益条/3 徽章/5 涨跌色）；四测试 15/15+28/28+84/84+23/23 全绿

### 迭代六：评分引擎新维度接入（2026-08-09，数据源审计落地 4.5 项）
- **F10 统一解析器**：`_f10_table`（FundArchivesDatas.aspx + Referer 头 + content 提取 + read_html），一解析器覆盖 3 维度（cyrjg/gmbd/zcfzb），实测 0.6-2.4s/次 + 进程内缓存
- **数据层 5 方法**（全部实测对照审计数值）：
  - holder_structure（cyrjg）：006195 机构 10.42% / 675123 机构 98.72%（机构定制债基识别）✓
  - scale_history（gmbd）：006195 35 期季度序列，最新 43.53 亿与雪球交叉验证 ✓（point-in-time 防重述；无效代码占位行已过滤）
  - bond_leverage（zcfzb&showtype=1）：675123 1.0079（与审计 1.008 一致；卖出回购正回购点已解析）✓
  - turnover（akshare fund_portfolio_change_em）：006195 2025年报 6.99 倍 / 675123 16.11（仅股票部分）；卖出表列名坑与当年空表 KeyError 已容错 ✓
  - ac_partner + partner_fees：006195→016858、675123→675121 配对 ✓；partner 结构化费率（管理/托管/销售服务/申购/短期赎回，小数口径）✓
- **评分层消费**（权重表 Σ=100 不变）：
  - 运作稳定性 15 重构为四因子：任期 5（三态）+ 规模 4（类型分叉曲线 + scale_hist 点内数据 + 规模突增/骤降监测 ×0.7）+ 持有人 3（inst_ratio≥90%→0.3 机构定制风险 / <10%→0.6 / 缺失 0.7）+ 换手 3（>3.0→0.4 漂移 / 缺失 0.7）
  - 尾部风险 15 债基杠杆修正：leverage>1.2 → ×0.7；1.1-1.2 → ×0.85（正回购放大尾部风险）
  - TCO 18 A/C 份额择优：partner_total=(mgmt+trustee+sales)×持有年数+申购+短期赎回，与本产品取低者（candidate_holding_years 默认 2 可配）
- **测试**：test_rules 76→84（G3 新增 8 条：稳定性四因子/杠杆修正/TCO 择优/降级兼容）；全量回归 15/15+28/28+84/84+23/23；评分全键消费 smoke 通过
- **数据源 49→58 项**（新增 9：持有人/规模/杠杆/换手/配对×N 产品）

### 云端运行排查（2026-08-09）
- Actions 记录（UTC）：08-07 cron success（10:31 UTC，报告+推送+Pages 全成功）；**08-06 cron failure（13:22 UTC）仅最后一步 Deploy to Pages 失败**（报告生成/推送/回写/归档全成功，08-07 已自动恢复）；**08-08/08-09 周末无 schedule 运行**——cron '35 7 * * 1-5' 仅工作日，周末不推是预期行为（非故障）
- 结论：08-07 报告用户已收到（验证过 AI 1-4 条）；08-06 仅网页部署失败不影响推送；"昨天没收到"若指 08-08 周六=正常不跑
- 待用户决策：是否改 cron 为每日（周末也推）？SERVERCHAN_KEY Secret 是否已配（08-06 失败通知是否收到）？

### main.py 拆分方案（P2，已存档待执行）
- 现状 1909 行；目标 ≤800 行。建议顺序（每次拆一个模块、拆完跑全部测试、只移代码不改逻辑）：
  1. `data_fetcher.py`：DataFetcher 类 + fetch_section/fetch_once/_mark_fetch/fetch_once 工具
  2. `push.py`：push_serverchan/push_wecom/post_with_retry
  3. `config_loader.py`：load_config/get_clients/deep_merge
  4. `run_client` 拆三段：_analyze_phase/_narrative_phase/_report_phase
- **注意**：拆分需同步更新 `.opencode/agents/` 中各子 agent 的职责描述（data-fetcher 等提示词引用 main.py 路径）；run_client 600 行拆分时先画数据流图再动

## 待办（下一步）🔜

- [x] 云端四客户版首跑验证（2026-08-07 完成）：微信四条推送（Harley/Echo 独立接收）+ Pages 客户入口页四链接 + **AI 综述板块与方向倾向句实测通过**（核对清单 1-4 全部符合：summary 含 ≥2 快照数值因果链/跨市场/新闻传导/方向倾向三选一 ≤180 字；产品解读风险/背离/传导 ≥2 项；不复读 CRO；compact ≤3700 字节；状态标签 ✅/👀/⚠️ 与"止盈持续中（第 N 天）"正常）
- [x] 技术治理记账项修复（3 项已清：pending_lines 死代码 / add_trading_days 回退 TypeError / portfolio_diagnostics products 参数；费率口径与 max_dd 剪裁已由 V3.1 覆盖）
- [ ] 向 NULL_Xue 发送 docs/CLIENT_ONBOARDING.md，索要 Server酱 SendKey；收到后配 per-client push 并更新 Secret
- [ ] 客户持仓信息到位后：填 holdings，按各自风险偏好设 target（出厂默认起步，非 Evan 调优值）
- [ ] 用户确认 GitHub Secret SERVERCHAN_KEY 已添加（Actions 失败通知用）
- [ ] 执行回执反馈机制使用：维护者手动 `state_store.record_feedback(client, date, status, note)` 记录执行情况（报告将显示上次反馈），可后续脚本化
- [ ] C 迭代观察：AI 行业推荐板块（宁缺毋滥纪律生效中，连续未输出）
- [ ] D 迭代：多平台结算参数校准（等用户提供且慢/支付宝实测数据）
- [ ] 知识库建档扩展：重点关注产品持续更新档案
- [ ] 影子模式期满后：信号命中率统计、止盈算法转正式
- [ ] **IC 回测样本扩充后重测**：ic_backtest.py 已可跑（8 产品样本小），关注池扩充或时间积累后重跑，重点复核费率 IC 方向（当前与预测相反，疑类型混杂所致）——复核后才考虑权重 ∝ ICIR 校准
- [ ] dynamic_tp_line 分红再投资台账：排期 2027-01 初专项（期满前 1 个月，留观察期）
- [ ] 经理任期快照冷启动积累：V3.1 起每日记录 manager 快照，随运行自然攒任期历史；关注池产品可人工补 `manager_since`
- [x] **state.json 云端持久化缺口**（迭代五已修：feedback/manager_snapshots 双文件 kb 回写通道 + 经理快照接线）
- [ ] V3.1 云端首跑核对（下个交易日 cron 自动生效）：BUY-NEW 候选评分（需权益缺口且无预警时才出现）、申购状态/成立门槛剔除日志、复权口径下的产品收益数字

## 关键技术点备忘

- 数据源 49 项全部免费：腾讯行情（指数主源）/akshare（指数备选、PE乐咕、债券、宏观扩展：美债10Y/SC原油/沪铜/CPI/PPI/PMI/社融）/上金所黄金（备选沪金主力AU0）/天天基金（净值、股票仓位穿透、行业HHI）/雪球（资料/费率/业绩）/财联社电报/央行LPR/外汇/两融/标普500
- 乐咕 PE 支持指数名：上证50/沪深300/上证380/创业板50/中证500/上证180/深证红利/深证100/中证1000/上证红利/中证100/中证800（创业板用"创业板50"代理，报告脚注透明标注）
- 规则 ID 体系（Gatekeeper 白名单同源）：TP-YIELD-1/2/3、TP-DD、LAD-CSI300-80/90/95、LAD-CSI500-75、LAD-CYB-90、MIN-MERGE、EP-CAP-10（动态阈值）、STORM-5、REB-EQ、REB-BOND、BUY-NEW；止损 SL-* 已全部删除
- 推送限制：Server酱约3800字节，build_compact 按核心板块优先裁剪；免费额度 5 条/天
- 云端无跨日磁盘，跨日持久唯一通道 = knowledge_base git 回写（traces/recommendations/state_history）
- 用户持仓（Evan_Lei）：006195 国金量化多因子股票A（233.55份）、014846 博时恒乐债券A（1407.30份）、003504 已清仓仅观察、现金0；在途：006195 赎回 116.78份 8-10 到账约¥351；平台=招商证券
- 多客户纪律：新客户参数用出厂默认（非 Evan 调优值，如 tier_gap 动态算法接管）；Harley_Lei/NULL_Xue/Echo_Wang 目标仓位待各自产品信息到位后设定
- 客户推送：全局 push 默认推服务方本人微信；客户提供 SendKey 后在其客户段加 push 覆盖（客户级优先），实现每客户独立微信接收
- 用户操作习惯：出入金手动执行；config 每次交易后需更新（Secret ADVISOR_CONFIG 为云端事实源）
