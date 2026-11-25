$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Set-Location -Path $PSScriptRoot

$PortRaw = if ($env:AGVS_PORT) { $env:AGVS_PORT.Trim() } else { "8765" }
[int]$PortNum = 0
if (-not [int]::TryParse($PortRaw, [ref]$PortNum) -or $PortNum -lt 1 -or $PortNum -gt 65535) {
    Write-Host "错误：无效端口 AGVS_PORT=$PortRaw（请填写 1–65535 之间的整数）。" -ForegroundColor Red
    exit 1
}

$Port = "$PortNum"
$Url = "http://127.0.0.1:$Port"
$HealthUrl = "$Url/api/health"

$Deps = @(
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "httpx[socks]>=0.28.0",
    "google-genai>=1.0.0",
    "openai>=1.60.0",
    "pydantic>=2.10.0",
    "python-multipart>=0.0.18",
    "watchdog>=6.0.0",
    "python-dotenv>=1.0.1",
    "pillow>=11.0.0"
)

function Test-LocalPortInUse {
    param([int]$TargetPort)
    $client = $null
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect("127.0.0.1", $TargetPort, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(500, $false)) {
            $client.EndConnect($iar)
            return $true
        }
        return $false
    } catch {
        return $false
    } finally {
        if ($client) {
            $client.Close()
        }
    }
}

function Test-AgvsHealthy {
    param([string]$TargetHealthUrl)
    $resp = $null
    $reader = $null
    try {
        $req = [System.Net.HttpWebRequest]::Create($TargetHealthUrl)
        $req.Method = "GET"
        $req.Timeout = 1500
        $req.ReadWriteTimeout = 1500
        # 显式清空代理，彻底绕过系统代理与 HTTP_PROXY（兼容 PowerShell 5.1 与 7+）
        $req.Proxy = $null
        $resp = $req.GetResponse()
        $reader = New-Object System.IO.StreamReader($resp.GetResponseStream(), [System.Text.Encoding]::UTF8)
        $body = $reader.ReadToEnd()
        $data = $body | ConvertFrom-Json
        return ($null -ne $data -and $data.app -eq "antigravity-video-studio")
    } catch {
        return $false
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($resp) { $resp.Close() }
    }
}

function Find-Python312 {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($ver in @("-3.13", "-3.12", "-3")) {
            try {
                & py $ver -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
                if ($LASTEXITCODE -eq 0) {
                    return @("py", $ver)
                }
            } catch {}
        }
    }
    foreach ($cmd in @("python3.13", "python3.12", "python", "python3")) {
        if (Get-Command $cmd -ErrorAction SilentlyContinue) {
            try {
                & $cmd -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
                if ($LASTEXITCODE -eq 0) {
                    return @($cmd)
                }
            } catch {}
        }
    }
    return $null
}

# 启动前先检查 127.0.0.1:$Port 是否已被占用，避免误弹其他软件网页
if (Test-LocalPortInUse -TargetPort $PortNum) {
    if (Test-AgvsHealthy -TargetHealthUrl $HealthUrl) {
        Write-Host "反重力视频工坊已在 $Url 运行中。" -ForegroundColor Green
        if ($env:AGVS_NO_BROWSER -ne "1") {
            Start-Process $Url
        }
        exit 0
    } else {
        Write-Host "错误：端口 $Port（127.0.0.1:$Port）已被其他程序占用，反重力视频工坊无法启动。" -ForegroundColor Red
        Write-Host "请先关闭占用该端口的程序，或改用其他端口启动，例如：" -ForegroundColor Yellow
        Write-Host '  $env:AGVS_PORT="8766"; .\start.ps1' -ForegroundColor Yellow
        exit 1
    }
}

$VenvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$VenvUvicorn = Join-Path $PSScriptRoot ".venv\Scripts\uvicorn.exe"

if (-not (Test-Path $VenvPython) -or -not (Test-Path $VenvUvicorn)) {
    Write-Host "正在初始化 Python 3.12+ 虚拟环境并安装依赖..." -ForegroundColor Cyan
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        $UvVenvOk = $false
        try {
            & uv venv --python 3.12 .venv
            if ($LASTEXITCODE -eq 0 -and (Test-Path $VenvPython)) {
                $UvVenvOk = $true
            }
        } catch {
            $UvVenvOk = $false
        }
        if (-not $UvVenvOk) {
            Write-Host "提示：uv venv --python 3.12 未成功，尝试使用默认 Python 创建虚拟环境..." -ForegroundColor Yellow
            & uv venv .venv
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) {
                Write-Host "错误：uv 创建 Python 虚拟环境失败（退出码 $LASTEXITCODE）。" -ForegroundColor Red
                Write-Host "排查建议：请确认网络可达或先安装 Python 3.12+（winget install Python.Python.3.12），删除残损的 .venv 目录后重试。" -ForegroundColor Yellow
                exit 1
            }
        }
        & uv pip install --python $VenvPython @Deps
        if ($LASTEXITCODE -ne 0) {
            Write-Host "错误：uv 安装项目依赖失败（退出码 $LASTEXITCODE）。" -ForegroundColor Red
            Write-Host "排查建议：" -ForegroundColor Yellow
            Write-Host "  1. 检查网络连接，或排查系统代理是否拦截 PyPI 访问；" -ForegroundColor Yellow
            Write-Host "  2. 国内网络建议设置镜像源后重试：" -ForegroundColor Yellow
            Write-Host '     $env:UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"; .\start.ps1' -ForegroundColor Yellow
            exit 1
        }
    } else {
        $PyCmd = Find-Python312
        if (-not $PyCmd) {
            Write-Host "错误：未检测到 Python 3.12 及以上版本。请先安装 Python 3.12+（winget install Python.Python.3.12）或安装 uv 后重试。" -ForegroundColor Red
            exit 1
        }
        if ($PyCmd.Length -gt 1) {
            & $PyCmd[0] $PyCmd[1] -m venv .venv
        } else {
            & $PyCmd[0] -m venv .venv
        }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) {
            Write-Host "错误：创建 Python 虚拟环境 .venv 失败（退出码 $LASTEXITCODE）。" -ForegroundColor Red
            Write-Host "排查建议：请检查当前目录写入权限，或删除残损的 .venv 目录后重试。" -ForegroundColor Yellow
            exit 1
        }
        & $VenvPython -m pip install --upgrade pip
        if ($LASTEXITCODE -ne 0) {
            Write-Host "提示：升级 pip 失败（退出码 $LASTEXITCODE），将尝试继续安装项目依赖..." -ForegroundColor Yellow
        }
        & $VenvPython -m pip install @Deps
        if ($LASTEXITCODE -ne 0) {
            Write-Host "错误：pip 安装项目依赖失败（退出码 $LASTEXITCODE）。" -ForegroundColor Red
            Write-Host "排查建议：" -ForegroundColor Yellow
            Write-Host "  1. 检查网络连接，或排查系统代理是否拦截 PyPI 访问；" -ForegroundColor Yellow
            Write-Host "  2. 国内网络建议设置镜像源后重试：" -ForegroundColor Yellow
            Write-Host '     $env:PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"; .\start.ps1' -ForegroundColor Yellow
            exit 1
        }
    }

    if (-not (Test-Path $VenvPython) -or -not (Test-Path $VenvUvicorn)) {
        Write-Host "错误：依赖安装后仍未找到 .venv\Scripts\uvicorn.exe，请删除 .venv 目录后重新运行 start.bat / start.ps1。" -ForegroundColor Red
        exit 1
    }
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "提示：未检测到 ffmpeg，本地视频剪辑、混音与音画体检功能需安装 ffmpeg。" -ForegroundColor Yellow
    Write-Host "  推荐安装：winget install Gyan.FFmpeg（安装后重开终端即可生效）" -ForegroundColor Yellow
}

Write-Host "启动「反重力视频工坊」本地制片台：$Url" -ForegroundColor Green
Write-Host "按 Ctrl+C 可随时停止服务（单进程轻量模式，数据仅保存在本地 data\ 目录）"

# 后台轮询 /api/health（绕过代理），仅在服务真正就绪时才打开浏览器；若主进程提前退出则立即终止
if ($env:AGVS_NO_BROWSER -ne "1") {
    $ParentPid = $PID
    $WatcherCmd = @"
`$healthUrl = '$HealthUrl'
`$openUrl = '$Url'
`$parentPid = $ParentPid
for (`$i = 0; `$i -lt 30; `$i++) {
    if (-not (Get-Process -Id `$parentPid -ErrorAction SilentlyContinue)) { break }
    `$resp = `$null
    `$reader = `$null
    try {
        `$req = [System.Net.HttpWebRequest]::Create(`$healthUrl)
        `$req.Method = 'GET'
        `$req.Timeout = 1500
        `$req.ReadWriteTimeout = 1500
        `$req.Proxy = `$null
        `$resp = `$req.GetResponse()
        `$reader = New-Object System.IO.StreamReader(`$resp.GetResponseStream(), [System.Text.Encoding]::UTF8)
        `$data = (`$reader.ReadToEnd()) | ConvertFrom-Json
        if (`$null -ne `$data -and `$data.app -eq 'antigravity-video-studio') {
            Start-Process `$openUrl
            break
        }
    } catch {} finally {
        if (`$reader) { `$reader.Dispose() }
        if (`$resp) { `$resp.Close() }
    }
    Start-Sleep -Milliseconds 500
}
"@
    $EncodedWatcher = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($WatcherCmd))
    Start-Process -WindowStyle Hidden powershell -ArgumentList "-NoProfile", "-EncodedCommand", $EncodedWatcher
}

& $VenvPython -m uvicorn backend.main:app --host 127.0.0.1 --port $Port
if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
