# 本地市场简报

需要时运行一次，读取公开市场数据并在本机生成 HTML、Markdown 和研究快照，然后退出。运行不需要 GitHub、Cloudflare、Healthchecks、消息渠道或 AI 凭据。

## 首次设置

安装 64 位 Python 3.12，然后在项目目录运行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

本机若 `py -3.12` 提示未注册 Python，可直接使用现有的 3.12 路径创建环境：

```powershell
& 'D:\每周推送\market_advisor-validation\py312\python.exe' -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 生成报告

双击 `生成周报.cmd`，或运行：

```powershell
.\.venv\Scripts\python.exe local_report.py
```

每次成功运行会创建 `reports/<生成时间>/report.html`、`report.md` 和 `research.json`；`reports/latest.html` 指向最近一次成功报告。HTML 可离线打开。历史目录不会被覆盖。

## 日期与失败处理

行情以真实交易日历确认的最近已完成交易日为准。上海时间 19:35 前不采用当天行情，之后才尝试采用当天已完成行情。周中报告展示截至当日的周度视角，不代表整周收盘结果。正文同时标注行情日期和实际生成时间。

关键行情、交易日历或最低覆盖缺失时，运行会返回错误，不生成新报告，也不覆盖上次成功的 `latest.html`。可选利率、黄金、宏观或新闻数据缺失时，会在报告中说明。

本地离线测试：

```powershell
.\.venv\Scripts\python.exe -m unittest -q
```

旧云端发布、通知和遗留客户程序已从本地运行入口退休。旧状态账本保存在项目外归档中，不会读取、改写或补发。云端账户当前状态仍需账户持有人单独确认，删除本地工作流不代表云端服务已停用。
