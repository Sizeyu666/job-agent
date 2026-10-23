# JobAgent 启动脚本：双击运行，或在终端执行 powershell -ExecutionPolicy Bypass -File scripts\start.ps1
# 加 -NoBrowser 则不自动打开浏览器（开机自启时使用）
param([switch]$NoBrowser)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "未找到虚拟环境，请先在项目根目录运行: uv venv --python 3.11 .venv && uv pip install -r requirements.txt" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

# 端口从 config/profile.yaml 读取，避免脚本里硬编码
$port = (& $python -c "from core.config import load_profile; print(load_profile().web.port)").Trim()
$url = "http://127.0.0.1:$port"
Write-Host "启动 JobAgent：$url" -ForegroundColor Green

if (-not $NoBrowser) {
    # 服务启动需要两三秒，延迟后再打开浏览器，避免看到连接失败页
    Start-Process powershell -WindowStyle Hidden -ArgumentList @(
        "-NoProfile", "-Command", "Start-Sleep -Seconds 3; Start-Process '$url'"
    )
}

& $python main.py serve
