# 公共市场机会与风险简报

工作日采集国内宽基、申万一级行业、中证全债、黄金现货与利率数据；重要状态变化才提醒，周末发布一次总结。报告不读取客户持仓、成本或交易，不提供个性化买卖指令。

## 运行方式

```powershell
python public_runner.py prepare
python -m unittest -q test_public.py
```

本地与云端使用 Python 3.12，按 `requirements.txt` 安装依赖（pandas `<3`）。公共数据采集由 `public_sources.py` 独立提供。生产工作流仅从 `public_data/state.json` 恢复公开研究状态，并从新版 `ADVISOR_CONFIG` Secret 读取微信收件渠道；Pages 部署后先核验实际公开 manifest 和所有页面哈希，再落账与通知。历史客户模块暂保留，待实际工作日与周六运行观察完成后再移除。

公开状态使用 schema 3，兼容 schema 1/2 迁移。通知发送前先将 unknown 保存、提交并成功推送账本；不确定结果需人工核对，不能重复使用同一 attempt。心跳检查最近一个已到周六 19:35 截止时间的周报，`PUBLIC_HEARTBEAT_URL` 是无人值守上线必需配置。

2026-10-11 补修尚未提交，仅完成本地离线验证；未部署或发送真实通知。最终提交需包含新增的 `public_sources.py`。

空状态默认在上海时间周六 19:35 后或周日首发，首期周次必须与最近到期周一致。工作日首次启动需先完成独立启动规则，当前版本尚不支持。实际上线顺序与配置检查见交接文档。

## 内容

- 首页和最新周报：`https://Evanlei2025.github.io/market-advisor/`
- 5/20/60 交易日的指数表现与行业相对强度、成交参与、估值可用性和风险。
- 上次研究观察回顾使用发布后的下一交易日收盘作为参考，非真实基金或客户收益。
- 资料不足时保留上次成功发布的网页，通知维护者而不是虚构机会。

配置、故障恢复和客户数据迁移见 [公共服务交接文档](docs/PUBLIC_SERVICE.md)。样例收件格式见 [public_delivery.example.json](public_delivery.example.json)。
