# 注册每日收盘报告计划任务（工作日 15:35 运行，避开收盘瞬时拥堵）
$ErrorActionPreference = "Stop"
$base = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command python).Source

$action = New-ScheduledTaskAction -Execute $py -Argument "`"$base\main.py`"" -WorkingDirectory $base
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 15:35
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName "MarketAdvisorDaily" -Action $action -Trigger $trigger -Settings $settings -Description "每日收盘后生成投顾报告并推送微信" -Force
Write-Host "已注册计划任务 MarketAdvisorDaily（工作日 15:35）。"
Write-Host "查看/手动运行:"
Write-Host "  schtasks /Run /TN MarketAdvisorDaily"
Write-Host "  Get-ScheduledTask -TaskName MarketAdvisorDaily | Select-Object -ExpandProperty State"
Write-Host "删除:"
Write-Host "  Unregister-ScheduledTask -TaskName MarketAdvisorDaily"
