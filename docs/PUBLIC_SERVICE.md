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
- `SERVERCHAN_KEY`：仅工作流失败时通知维护者，可与收件渠道不同。

不要把任何 SendKey、Webhook 或 API key 写进仓库、问题单或聊天记录。

## 外部心跳

一次性在 https://healthchecks.io/ 创建免费 Check：Period 1 day、Grace 1 day，开启邮件提醒；将 Ping URL 设为 `PUBLIC_HEARTBEAT_URL`。`public-service-heartbeat` 工作流每天北京时间 22:17 检查市场快照和周报（休市日与最近交易日对齐），然后才给外部服务发送心跳。工作流从未启动时，外部服务会提醒；内容仍需参考 Actions 与 Pages。

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

## 本地修复验收

2026-10-11 的五项补修仅进行本地离线验收。工作区清理与补修仍未提交，新增 `public_sources.py` 必须包含在最终提交清单。生产公开 state 未迁移、未覆盖；未推送、部署或发送真实通知。仓库外工作区备份：`D:\每周推送\market_advisor-fix-backup-20261011`，16 个复制文件已经逐一校验 SHA-256。

本地验收命令全部退出 0：Python 3.12 公共测试 42/42（原有 19 项加 23 项补修用例）、规则 85/85、止盈 15/15、门禁 23/23、`pip check`、20 个根目录 Python 文件内存编译和 `git diff --check`。新增用例先复现五项错误，再验证模板升级、schema 1/2 迁移保留、manifest 篡改、发送前后 Git 推送失败、崩溃、人工核对、多收件人、周六截止/跨年/长假、真实格式新闻和 Bash 查询失败。普通日与新一期周六 prepare 均使用临时目录、fake collector/fetcher 与 fake 日历；没有访问真实渠道或远程账本。

生产 `public_data/state.json` 的 SHA-256 与备份一致，并与 HEAD 无差异。项目中没有新增字节码、日志、报告或测试站点残留。`master` 仍是 `37c0695`，本地记录的 `origin/master` 仍是 `afcd6af`；领先 1 个提交仍指此前迁移提交，不包括工作区补修。

真实采集、Pages 公开 URL 核验、渠道通知及一个真实工作日和周六的连续观察仍属于后续上线验收，离线测试不能代替它们。

## 首次启动与待上线检查

默认采用周末首发：上海时间周六 19:35 之后或周日完成第一次 prepare、真实部署及通知。首期周次必须与 `public_health.due_week` 一致；行情截至真实交易日历确认的最近完整交易日，不能修改 date/asof 冒充过去周报。当前空账本不能在工作日直接首发，否则本周首期无法满足心跳对上周的要求。

2026-10-11 周日是符合条件的窗口，但仍须先获得明确上线指令、确认云端配置及完成真实链路核验。如果错过该窗口，下一默认窗口为 2026-10-17 周六 19:35 后至 10-18 周日（Asia/Shanghai）。定时周六 17:35 适用于已经启动的服务；空状态首发应在上述窗口手动运行。必须工作日首发时，先独立实现首次成功发布起始时间和首发/周六正式版规则，并验收后再上线，不降低旧服务的漏报检查。

目标仓库 `Evanlei2025/market-advisor`，分支 `master`，公开地址 `https://Evanlei2025.github.io/market-advisor/`。上线前需要确认 Pages 使用 GitHub Actions，公共收件 Secret `ADVISOR_CONFIG`、心跳 `PUBLIC_HEARTBEAT_URL` 和维护者失败告警 `SERVERCHAN_KEY` 已配置且可用；`DEEPSEEK_API_KEY` 可选。只记录配置存在与否，不能输出密钥值。

本轮仅补充公开页面门禁与周末首发测试。工作区有独立备份 `D:\每周推送\market_advisor-prelaunch-backup-20261011`（11 个文件已核对 SHA-256）。远端只读查询连接失败，GitHub 当前提交、Pages 配置与 Secrets 尚未确认。明确提交清单和上线验收表位于项目外的 `D:\每周推送\market-advisor-launch-review-20261011.md`；没有明确上线指令时不提交、推送或真实发送。

上线准备验收：公共测试 48/48（五项补修的 42 项加本轮 6 项），Python 3.12 `pip check`、20 个文件内存编译通过。按明确清单暂存 19 个文件，包含 `public_sources.py`，排除了生产 state、真实配置和仓库外归档。首次将新增模块纳入暂存检查时，修正了原有文件末尾多余空行；暂存差异检查通过。未新增提交，生产公开状态与本轮备份及 HEAD 一致。

## 客户数据迁移

新版运行不依赖客户数据。切换完成后，本地客户配置、旧报告、旧产品档案和旧状态文件可以清空；Git 历史按约定保留。旧 Pages 内容在新版工作流成功部署后才会被替换。
