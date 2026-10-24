# 在 Windows PowerShell 中初始化本项目运行环境。
# 用法：powershell -ExecutionPolicy Bypass -File .\setup.ps1
[CmdletBinding()]
param(
    [switch]$ForceReinstall
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

function Find-Python {
    $candidates = @(
        (Get-Command py -ErrorAction SilentlyContinue),
        (Get-Command python -ErrorAction SilentlyContinue)
    )
    foreach ($candidate in $candidates) {
        if ($candidate) { return $candidate.Source }
    }
    throw '未找到 Python。请先安装 Python 3.10 或更高版本，并勾选 Add Python to PATH。'
}

$Python = Find-Python
$VenvPython = Join-Path $ProjectRoot 'venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host '正在创建项目虚拟环境 venv ...' -ForegroundColor Cyan
    if ((Split-Path -Leaf $Python) -eq 'py.exe') {
        & $Python -3 -m venv (Join-Path $ProjectRoot 'venv')
    } else {
        & $Python -m venv (Join-Path $ProjectRoot 'venv')
    }
}

Write-Host '正在升级 pip 并安装依赖 ...' -ForegroundColor Cyan
& $VenvPython -m pip install --upgrade pip
if ($ForceReinstall) {
    & $VenvPython -m pip install --upgrade --force-reinstall -r (Join-Path $ProjectRoot 'requirements.txt')
} else {
    & $VenvPython -m pip install --upgrade -r (Join-Path $ProjectRoot 'requirements.txt')
}

Write-Host '正在执行 Python 导入检查 ...' -ForegroundColor Cyan
& $VenvPython -c "import sys; sys.path.insert(0, 'py'); import config, captcha, followup_batch, shop_followup, shop_list_reader, task_runtime, youzan_bot; print('IMPORT_CHECK=PASS')"

$edgePaths = @(
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe"
)
$edgeInstalled = $edgePaths | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if ($edgeInstalled) {
    Write-Host "已检测到 Microsoft Edge：$edgeInstalled" -ForegroundColor Green
} else {
    Write-Warning '未检测到 Microsoft Edge。项目默认使用系统 Edge，请安装 Edge 后再启动。'
}

Write-Host ''
Write-Host '运行环境已准备完成。' -ForegroundColor Green
Write-Host '启动 GUI：      .\venv\Scripts\python.exe .\main.py'
Write-Host '或直接运行：    python .\main.py'
<<<<<<< HEAD
Write-Host '安全试运行：    .\venv\Scripts\python.exe .\main.py --fill-first3-dry'

=======
Write-Host '安全试运行：    .\venv\Scripts\python.exe .\main.py --fill-first3-dry'

>>>>>>> b94e3e2 (修复Python 3.14登录并持久化本地配置)
