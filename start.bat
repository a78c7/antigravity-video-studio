@echo off
chcp 65001 >nul
set "PYTHONUTF8=1"
setlocal
cd /d "%~dp0"

echo 正在启动「反重力视频工坊」本地制片台...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
set "EXIT_CODE=%errorlevel%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo 启动失败（退出码 %EXIT_CODE%），请查看上方报错信息。
    echo 常见原因：端口被其他程序占用、未安装 Python 3.12+、或网络超时（可设置镜像源后重试）。
    pause
    exit /b %EXIT_CODE%
)
endlocal
