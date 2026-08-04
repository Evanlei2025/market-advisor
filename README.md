# 每日投顾报告程序

本地定时运行的规则化投顾报告：每天收盘后抓取市场数据，按固定规则给出操作建议，推送到微信。**只出建议，不自动交易；出入金均由你手动执行。**

## 两种运行方式

- **方式 A：GitHub Actions 云托管（推荐，不依赖你的电脑）**：见下方"云部署"
- **方式 B：本地计划任务**：适合想保留本地存档的情况，见"本地部署"

## 云部署（GitHub Actions）

1. 把本仓库 fork 或 push 到你的 GitHub 账号（公开/私有均可）
2. 进入仓库 → Settings → **Secrets and variables → Actions** → New repository secret：
   - Name：`ADVISOR_CONFIG`
   - Value：你的完整 `config.json` 内容（参考 `config.example.json` 结构，填入真实持仓金额和 webhook）
3. 确认 Workflow 已启用（Actions 页面可见 `daily-report`），或手动触发一次验证：Actions → daily-report → **Run workflow**
4. 之后每个交易日 **北京时间 15:35** 自动生成报告并推送到你的微信（UTC 07:35，周一到周五）
5. 报告存档在每次运行的 Artifacts 中（保留 90 天），微信推送内为完整建议

> 注意：公开仓库下，持仓金额与 webhook 务必只放在 Secret 里，不要提交到 `config.json`（该文件已被 .gitignore 排除）。

## 本地部署

## 文件说明

| 文件 | 说明 |
|---|---|
| `main.py` | 主程序：采集 → 决策 → 报告 → 推送 |
| `config.json` | 你的配置：目标仓位、持仓、推送渠道 |
| `register_task.ps1` | 注册 Windows 计划任务（工作日 15:35 运行） |
| `reports/` | 每日报告存档（Markdown） |
| `logs/run.log` | 运行日志 |

## 首次配置

1. 安装依赖：`pip install -r requirements.txt`
2. 编辑 `config.json`：
   - `holdings`：按你的实际持仓填 `amount`（金额）。**每次手动交易后请更新对应金额**，这是组合检查的基础。
   - 场内 ETF 填 `code`（如 `"510300"`），场外基金填 `fund_code`（如 `"003358"`），程序会自动显示最新净值与涨跌。
   - `target`：目标配置比例（默认 权益45% / 债券45% / 黄金10%，含现金并入债券）
   - `rebalance_band`：偏离阈值（默认 5 个百分点，低于阈值不触发再平衡）
   - `push`：选择推送渠道

3. 推送渠道二选一（或先不配，报告会存到本地 `reports/`）：
   - **企业微信机器人**（推荐，免费）：企业微信 → 群 → 添加机器人 → 复制 webhook，填入 `wecom_webhook`，`channel` 设为 `"wecom"`
   - **Server酱**：https://sct.ftqq.com 注册获取 SendKey，填入 `serverchan_key`，`channel` 设为 `"serverchan"`

4. 注册定时任务：右键以管理员身份运行 PowerShell，执行
   `powershell -ExecutionPolicy Bypass -File register_task.ps1`
   然后保持电脑在工作日 15:35 处于开机状态。

## 常用命令

```powershell
python main.py --push-off          # 试运行，只生成报告不推送
python main.py                     # 正常运行（按 config 推送）
schtasks /Run /TN MarketAdvisorDaily   # 手动触发计划任务
Get-ScheduledTask -TaskName MarketAdvisorDaily   # 查看任务状态
Unregister-ScheduledTask -TaskName MarketAdvisorDaily   # 删除任务
```

## 决策规则（透明公开，改 `main.py` 即可调整）

- **权益**：按指数 PE 十年分位（<30% 加1分 / 30-50% 加0.5 / 50-70% 0 / 70-85% 减0.5 / >85% 减1）+ 价格 vs 120日均线（±0.5）+ 20日动量（|涨跌|>3% ±0.5），各指数取平均 → 增配/略增/持有/略减/减配
- **债券**：10年期国债收益率十年分位，<30% 债价偏贵略减，>70% 债价便宜可增
- **黄金**：价格 vs 120日均线，趋势跟随（卫星仓，控制在目标比例内）
- **再平衡**：实际比例偏离目标 ≥ 5pp 才触发，金额四舍五入到百元，买入建议分批 2-3 周执行

## 数据源（已实测连通并设为长期源）

| 数据 | 来源 | 用途 |
|---|---|---|
| 指数/ETF K线+实时价 | 腾讯行情 | 市场速览、趋势判断、持仓实时表现 |
| 指数 PE 历史（10年） | 乐咕乐股 | 估值温度、权益信号 |
| 全市场 PE | 乐咕乐股 | 估值温度补充 |
| 中美国债收益率 | 英为财情 | 债市研判、利率分位 |
| 上海金 Au99.99 | 上金所官方 | 黄金趋势 |
| 标普500（隔夜） | 新浪财经 | 外盘情绪参考 |
| 财联社电报 | 财联社 | 每日市场要闻 |
| LPR（1Y/5Y） | 央行（AkShare） | 政策利率环境 |
| 美元/人民币 | 外汇牌价 | 汇率参考 |
| 融资融券余额（沪市） | 上交所 | 杠杆情绪 |
| 场外基金净值 | 天天基金 | 持仓中场外基金的净值/涨跌 |

全部免费、无需注册、自动重试；任一源失效不影响整体运行。

## 已评估但未采用的数据源（结论）

- **东财接口（akshare 系列 em）**：数据最全，但高频率请求会被断连限流（实测），仅保留低频调用（场外基金净值）
- **新浪 A 股指数 K线**：偶发超时，仅用于美股指数（该接口稳定）
- **yfinance / Yahoo**：国内连通性差；若需美股行情优先用新浪美股源
- **SEC EDGAR / Hacker News**：连通正常，但面向美股/科技，对当前 A 股为主的配置无增量，留作扩展
- **Econdb / Finnhub / AllTick / TwelveData / Marketstack / Alpha Vantage / Polygon / Tushare / iFinD / Wind / Choice / 聚宽 / 米筐**：需注册或付费，免费额度对"每日一次"场景过剩；未来若加美股实时行情，首选 Finnhub 免费层（60次/分）
- **AI Agent / RAG / 向量库 / MCP 架构**：当前是规则引擎，透明可审计、无需 Key，保持简单优先；未来若加"新闻→情绪→调仓"AI 层，推荐路径：财联社电报（已有）→ DeepSeek 摘要 → 微信推送，暂不需要向量库

## 免责声明

本程序按固定规则自动生成参考信息，不构成投资建议。投资有风险，决策需自行判断。
