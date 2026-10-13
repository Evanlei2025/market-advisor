# 公共市场简报

## 内容

`public_runner.py` 是长期运行入口，不读取客户持仓、成本、交易或客户产品。它覆盖国内宽基、申万一级行业、中证全债、黄金现货、十年国债收益率和 3 个月 Shibor。

- 工作日：采集市场快照，只有重要研究状态变化才准备提醒。
- 周六：发布周报，包含大类资产变化、行业观察、反证、失效条件和上一期观察回顾。
- 行业状态需要价格相对强度、成交参与、趋势和估值数据；资料不足时不会冒充机会。
- 行业指数观察不是基金收益，也不代表客户持仓收益或具体买卖点。

## GitHub Secrets

- `ADVISOR_CONFIG`：只用于公共简报收件渠道，格式参考 `public_delivery.example.json`。短期仍能在内存中兼容旧的完整配置 Secret 以完成迁移，但不会读取或写入其中的持仓数据；迁移完成后应替换为公共收件 JSON。
- `DEEPSEEK_API_KEY`：可选。仅用于周报编辑解读；没有它仍生成确定性周报。
- `PUBLIC_HEARTBEAT_URL`：无人值守上线前必须设置，由外部心跳服务提供；缺失时发布工作流直接失败。
- `SERVERCHAN_KEY`：工作流失败或调度延迟超宽限时通知维护者，可与收件渠道不同。

不要把任何 SendKey、Webhook 或 API key 写进仓库、问题单或聊天记录。

## 外部心跳

一次性在 https://healthchecks.io/ 创建免费 Check：Period 1 day、Grace 1 day，开启邮件提醒；将 Ping URL 设为 `PUBLIC_HEARTBEAT_URL`。`public-service-heartbeat` 工作流每天北京时间 22:17 检查市场快照和周报（休市日与最近交易日对齐），然后才给外部服务发送心跳。首次真实心跳后，外部服务会提醒后续漏报；新建 Check 尚未收到第一条心跳时不能当作监控已生效。

周报计划为北京时间周六 17:35，宽限 2 小时，19:35 起检查该周报。巡检按 Asia/Shanghai 的当前时间寻找最近一个已到截止时间的周六；进入周一、长假或新 ISO 年也不会免除这一要求。到期周必须存在，准备版本和部署版本必须一致；更晚的提前周报不能替代它。本版没有首次启动豁免，没有已部署的到期周报就不能报健康。

## 手动运行

在 Actions 选择 `public-market-brief`：

- 普通 Run workflow：采集工作日快照。
- `publish_weekly = true`：需要补发时发布最新完整交易日的周报。

Pages 只从 `public_site/` 构建，旧 `reports/` 不会被上传。站点先在临时目录生成完整文件集并校验 `manifest.json`，再整体替换输出目录，因此不会保留已删除的旧周报或告警页面。

## 状态与故障

公开状态保存在 `public_data/state.json`，使用 schema 3，包含市场快照、周报、公开观察、通知意图、脱敏发送状态和页面版本。状态文件有 schema migration 和字段一致性校验，使用原子替换，并拒绝损坏后静默重置。

`load` 只校验结构和保存的证据，不依赖当前 HTML/CSS。`prepared_pages` 是最近 prepare 的页面哈希，`deployed_pages` 是实际部署证据。模板升级后重新 prepare 和部署，送达记录及观察的首次发布时间、参考价、路径保持不变。部署会核对 manifest、文件内容和页面哈希。

schema 1/2 会在内存中迁移，下一次成功保存时写入 schema 3。schema 2 的历史哈希、unknown 和观察原样保留。schema 1 缺少可信页面版本，需重新准备和部署；原部署映射保存在 `legacy_deployed`，没有可信发布时间的原收益路径保存在观察的 `legacy_returns`，不参与新发布后的收益回顾。未知 schema 或损坏字段必须排查原文件，不能清空状态恢复。

`prepare` 只会记录待部署的页面和观察意图。Pages 部署动作成功后，工作流先执行 `python public_runner.py verify-live`：读取实际公开的 manifest，核对其与本次构建完全一致，再逐页核对所有文件的 SHA-256，最后重新核对 manifest，避免验证期间站点被替换。读取使用缓存规避参数、请求超时，并最多重试 6 次，重试间隔 10 秒。任何校验失败都会终止后续部署落账及通知。

公开页面校验通过后，工作流以 `python public_runner.py deployed --deployment-id <id>` 记录页面 manifest 版本；这一步才会给观察记录写入 `published_at` 和 `deployment_id`。reserve/notify 只接受已经记录且版本一致的周报或告警页面。`verify-live` 本身只读状态，不发送通知。

通知流转为 `不存在 -> reserved -> unknown -> accepted`。CLI 的 reserve、notify、reconcile 使用同一 Git 账本回调：原子保存公开状态，只提交该状态文件，并成功推送 `origin HEAD:master`。notify 只接受匹配 attempt 的 reserved，在同一次调用中先推送 unknown，成功返回后才联系渠道；渠道接受后再推送 accepted。该操作需有 Git 提交身份和推送权限，工作流已配置。代码中的注入回调用于离线测试。

发送前落账失败不会发送；发送超时、进程在消费 reservation 后退出、或 accepted 推送失败，都禁止自动重发。云端的 unknown 表示需核对渠道结果，不代表再次发送的权限。相同 attempt 不能重复消费。串行工作流和该账本机制避免自动重试不确定结果，不保证渠道端严格 exactly-once。

心跳会检查所有未完成的 weekly/alert 事件，包括尚未开始发送、reserved 和 unknown。需先在渠道后台确认结果，再人工处理。GitHub Actions 定时任务可能延迟或漏运行，所以外部心跳不可省略。

人工处理前先核对最新远端状态，在环境变量 `ADVISOR_DELIVERY_JSON` 中提供公共收件 Secret，并配置 Git 提交身份及推送权限。确认渠道没有收到后运行 `python public_runner.py reconcile --recipient <receiver-hash> --event <event-id> --status retry`；确认已经收到则使用 `--status accepted`。CLI 会推送处理结果；retry 后必须用新的 attempt 执行 reserve 和 notify，旧 attempt 被保留并拒绝复用。推送失败必须排查云端账本，不能仅凭本机 accepted 判定结束。不要在未确认时选择 retry。

漏掉的到期周报需要核对当期真实归档快照后补建和部署对应周报；生成今天所在周的周报不能替代漏发周。不得用今天的数据修改周次或 asof 冒充历史周报。页面版本不一致时先重新 prepare、核验 manifest，再执行真实部署及 deployed，最后恢复通知。

财联社新闻使用来源的发布日期与发布时间，合并为 Asia/Shanghai 的完整时间后按七天窗口筛选。空来源、缺字段和解析失败分别写入 quality；不会使用运行当天补日期，也不会由新闻标题直接触发判断。

## 当前验收

2026-10-11 的五项补修与上线准备已经提交并真实上线。首期 W41 的 prepare、Pages 部署、公开全部文件哈希核验、4 个订阅渠道接受及外部心跳均已验收；详细首次上线证据位于仓库外 `market-advisor-launch-review-20261011.md`。

2026-10-13 调度维修的 Python 3.12 公共测试 62/62、依赖检查、23 个 Python 文件语法检查和工作流 actionlint 均通过，云端 CI [38332387343](https://github.com/Evanlei2025/market-advisor/actions/runs/38332387343) 通过。

10-12 历史普通日补跑 [38332494667](https://github.com/Evanlei2025/market-advisor/actions/runs/38332494667) 成功；快照现为 10-09、10-12，3 条观察参考点为 10-12。W41 首次发布时间、deployment_id、页面哈希和 delivered 时间均保持不变；订阅发送步骤跳过，pending=0、unknown=0，公开 manifest/全部页面哈希独立复核通过。生产心跳恢复 [38333092559](https://github.com/Evanlei2025/market-advisor/actions/runs/38333092559) 成功。

独立 Check 的故障与恢复状态演练 [38333150321](https://github.com/Evanlei2025/market-advisor/actions/runs/38333150321) 成功，外部观测到 down→up 后暂停测试 Check；生产周期/宽限仍为 86400 秒。用户确认尚未收到故障和恢复邮件，因此邮件告警实际收件验收未通过，不能把 Check 状态恢复当作邮件送达证据。需核查 Email 集成的验证、启用状态、故障/恢复通知选项、通知错误与邮箱垃圾邮件，再用独立测试 Check 复验。10-13、10-17 后续任务单独记录。

维修备份分支 `backup/pre-schedule-repair-20261013` 和仓库外 `market-advisor-schedule-backup-20261013/state.json` 保留；旧模块与归档继续保留，不能因离线验收通过而删除。

## 首次启动规则

默认采用周末首发：上海时间周六 19:35 之后或周日完成第一次 prepare、真实部署及通知。首期周次必须与 `public_health.due_week` 一致；行情截至真实交易日历确认的最近完整交易日，不能修改 date/asof 冒充过去周报。当前空账本不能在工作日直接首发，否则本周首期无法满足心跳对上周的要求。

本服务已于 2026-10-11 周日完成首发。定时周六 17:35 适用于已经启动的服务；将来新空状态首发应在上海时间周六 19:35 后或周日手动运行。必须工作日首发时，先独立实现首次成功发布起始时间和首发/周六正式版规则，并验收后再上线，不降低漏报检查。

目标仓库 `Evanlei2025/market-advisor`，分支 `master`，公开地址 `https://Evanlei2025.github.io/market-advisor/`。上线前需要确认 Pages 使用 GitHub Actions，公共收件 Secret `ADVISOR_CONFIG`、心跳 `PUBLIC_HEARTBEAT_URL` 和维护者失败告警 `SERVERCHAN_KEY` 已配置且可用；`DEEPSEEK_API_KEY` 可选。只记录配置存在与否，不能输出密钥值。

首次上线备份 `market_advisor-prelaunch-backup-20261011` 与明确清单继续保留；`public_sources.py` 已纳入上线提交。

## 定时延迟与历史补跑

2026-10-13 修复：`public_schedule.py` 在任务开始时冻结逻辑日期，`public_runner.py` 与 `public_health.py` 使用同一上下文。普通日 schedule 在上海时间 19:35 前按前一日处理，19:35 后按当日处理；周六 schedule 无论延迟到周日或周一都采用最近周六并强制周报。心跳采用相同市场截止规则，周次门禁继续使用实际时间下最近到期的周六周报。

`github.event.schedule` 区分普通日和周六任务。GitHub 不提供原定触发日期，因此日志中的延迟是根据最近适用时段推算；账本交易日连续性和已完成行情日期用于阻止静默跳过多日任务。缺失超过一个交易日时必须显式逐日补跑，不能将最新行情伪装为遗漏日期。

手动 `workflow_dispatch` 支持 `logical_date`、`backfill` 和 `publish_weekly`。历史普通日恢复填写 `YYYY-MM-DD`、`backfill=true`、`publish_weekly=false`：只建立快照与观察点，保留周报首次发布时间，抑制新的历史变化告警并跳过订阅发送步骤。恢复仍须按正常 Pages 部署、公开核验和部署落账顺序运行。已经比目标日期新的账本拒绝逆序插入。

实际启动时间、推算调度时段和延迟秒数写入 Actions 日志与摘要。超过 2 小时延迟时单独通知维护者；失败告警含 workflow、run ID/链接、失败阶段、逻辑日期和当前账本行情日。失败告警只使用维护者渠道。

独立故障演练使用 `public-repair-drill` 和 `PUBLIC_DRILL_HEARTBEAT_URL`。演练只在内存副本删除快照，通过独立 Healthchecks Check 的 Email 集成测试故障和恢复；不会落账、部署或调用生产订阅渠道。生产 Check 的 1 天周期和宽限保持不变，测试 Check 在外部观测到 down→up 后暂停，以避免后续测试噪声。Email 集成需单独验证并启用故障及恢复通知；账号登录、API 查询到 Email 渠道或 Ping 请求被接受均不能证明邮件已送达。

10 月 12 日普通日历史补跑已经完成，10 月 13 日普通日必须等到 19:35 后；10 月 17 日周六应产生 W42，正常行情截至 10 月 16 日。真实未来任务未完成前不标记验收通过。

## 客户数据迁移

新版运行不依赖客户数据。切换完成后，本地客户配置、旧报告、旧产品档案和旧状态文件可以清空；Git 历史按约定保留。旧 Pages 内容在新版工作流成功部署后才会被替换。
