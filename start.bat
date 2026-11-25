@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo 正在启动「反重力视频工坊」本地制片台...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
set "EXIT_CODE=%errorlevel%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo 启动失败（退出码 %EXIT_CODE%），请查看上方报错信息（常见原因：端口被其他程序占用、未安装 Python 3.12+ 或依赖安装超时）。
    pause
    exit /b %EXIT_CODE%
)
endlocal
