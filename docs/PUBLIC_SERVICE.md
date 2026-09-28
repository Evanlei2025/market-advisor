# 公共市场简报

## 内容

`public_runner.py` 是长期运行入口，不读取客户持仓、成本、交易或客户产品。它覆盖国内宽基、申万一级行业、中证全债、黄金现货、十年国债收益率和 3 个月 Shibor。

- 工作日：采集市场快照，只有重要研究状态变化才准备提醒。
- 周六：发布周报，包含大类资产变化、行业观察、反证、失效条件和上一期观察回顾。
- 行业状态需要价格相对强度、成交参与、趋势和估值数据；资料不足时不会冒充机会。
- 行业指数观察不是基金收益，也不代表客户持仓收益或具体买卖点。

## GitHub Secrets

- `ADVISOR_CONFIG`：新版只用于公共简报收件渠道，格式参考 `public_delivery.example.json`。当前版本也能在内存中兼容旧的完整配置 Secret 以完成迁移，但不会读取或写入其中的持仓数据；迁移完成后建议替换为公共收件 JSON。
- `DEEPSEEK_API_KEY`：可选。仅用于周报编辑解读；没有它仍生成确定性周报。
- `PUBLIC_HEARTBEAT_URL`：建议设置，由外部免费心跳服务提供。
- `SERVERCHAN_KEY`：仅工作流失败时通知维护者，可与收件渠道不同。

不要把任何 SendKey、Webhook 或 API key 写进仓库、问题单或聊天记录。

## 外部心跳

一次性在 https://healthchecks.io/ 创建免费 Check：Period 1 day、Grace 1 day，开启邮件提醒；将 Ping URL 设为 `PUBLIC_HEARTBEAT_URL`。`public-service-heartbeat` 工作流每天北京时间 22:17 检查市场快照和周报（休市日与最近交易日对齐），然后才给外部服务发送心跳。工作流从未启动时，外部服务会提醒；内容仍需参考 Actions 与 Pages。

## 手动运行

在 Actions 选择 `public-market-brief`：

- 普通 Run workflow：采集工作日快照。
- `publish_weekly = true`：需要补发时发布最新完整交易日的周报。

Pages 只从 `public_site/` 构建，旧 `reports/` 不会被上传。

## 状态与故障

公开状态保存在 `public_data/state.json`，包含市场快照、周报、公开观察、通知意图和脱敏发送状态。文件使用原子替换，并拒绝损坏后静默重置。

网络发送超时后，状态会保留为 `unknown`，不会自动盲目重发。需要先在渠道后台确认结果，再人工处理状态。GitHub Actions 定时任务可能延迟或漏运行，所以外部心跳不可省略。

确认渠道没有收到后，可以在本地取得公开状态文件和收件 Secret 后运行 `python public_runner.py reconcile --recipient <receiver-hash> --event <event-id> --status retry`；确认已经收到则使用 `--status accepted`。操作后必须将状态更新到云端，不能只改本地。不要在未确认时选择 retry。

## 客户数据迁移

新版运行不依赖客户数据。切换完成后，本地客户配置、旧报告、旧产品档案和旧状态文件可以清空；Git 历史按约定保留。旧 Pages 内容在新版工作流成功部署后才会被替换。
