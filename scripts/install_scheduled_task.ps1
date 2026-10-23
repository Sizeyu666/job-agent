# 注册 Windows 开机自启任务：登录 Windows 后隐藏窗口启动 JobAgent
# 用法：右键「使用 PowerShell 运行」，或在项目根目录执行 powershell -ExecutionPolicy Bypass -File scripts\install_scheduled_task.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$startPs1 = Join-Path $PSScriptRoot "start.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startPs1`" -NoBrowser"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "JobAgent" -Action $action -Trigger $trigger -Settings $settings -Description "JobAgent 求职副驾驶本地平台（开机自启）" -Force
Write-Host "已注册开机自启任务 JobAgent。可在「任务计划程序」中查看或运行 uninstall_scheduled_task.ps1 卸载。" -ForegroundColor Green
