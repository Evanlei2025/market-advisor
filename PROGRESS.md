# 迁移状态

项目目标已改为按需本地生成报告。旧任务 `alert:2026-10-14` 的恢复、10-17 W42 云端验收及外部调度器部署，均因需求变更取消，不代表已验收。

Windows 本机三个已确认属于本项目的计划任务已导出到仓库外备份并注销。项目旧启动工具已归档，不再作为日常入口。

云端 GitHub Actions、Healthchecks、Cloudflare Worker、Pages 发布及项目专用远端凭据未能在线核实；不能据此声称已停用。同步本地删除前，应由有账户权限的操作人先关闭服务、取消运行并确认状态。

2026-10-26 补验：发现本机已有 `D:\每周推送\market_advisor-validation\py312\python.exe`（Python 3.12.15），已建立独立 `.venv` 并安装 `requirements.txt`。本地 12 项测试和 `pip check` 通过；真实联网采集已生成 `reports/2026-10-26_115200_521646/`，行情截至 2026-10-23，5 个资产及 31 个行业同日对齐，`latest.html` 与当次报告一致。CPI/PPI 发布时点不可验证，故按质量规则排除。

GitHub 实查：远端 master 初始为 `8c736ea`，无活动运行。已关闭本仓库 Actions、禁用 4 个业务工作流、下线 Pages，删除本项目 5 项仓库 Secret；远端旧账本另行备份并保持原样。Healthchecks 仍未停用，Cloudflare Worker 及独立方糖凭据状态仍待账户确认；这些事项完成前不可写“整体迁移完成”。旧 `public_data/state.json` 原样归档；不读取、不重发、不伪造其 pending/unknown 完成状态。
