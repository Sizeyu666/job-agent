# 卸载 JobAgent 开机自启任务
Unregister-ScheduledTask -TaskName "JobAgent" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "已卸载 JobAgent 开机自启任务。" -ForegroundColor Green
