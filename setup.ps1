[CmdletBinding()]
param(
    [switch]$ForceReinstall
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path)).TrimEnd('\', '/')
Set-Location -LiteralPath $ProjectRoot
$VenvRoot = Join-Path $ProjectRoot '.venv'
$VenvPython = Join-Path $VenvRoot 'Scripts\python.exe'

function Test-PythonExecutable {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$Arguments = @()
    )
    try {
        & $Executable @Arguments -c "import sys; assert sys.version_info >= (3, 10); print(sys.executable)" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Find-Python {
    # 优先复用项目自身的 venv。项目移动后，venv 仍可能完全可用；此时不应要求系统
    # 另外安装 Python，也不应误删可用环境。
    if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
        if (Test-PythonExecutable -Executable $VenvPython) {
            return @{ Path = $VenvPython; Args = @(); IsVenv = $true }
        }
        Write-Warning "现有 venv 无法启动：$VenvPython"
    }

    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher -and (Test-PythonExecutable -Executable $launcher.Source -Arguments @('-3'))) {
        return @{ Path = $launcher.Source; Args = @('-3'); IsVenv = $false }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python -and (Test-PythonExecutable -Executable $python.Source)) {
        return @{ Path = $python.Source; Args = @(); IsVenv = $false }
    }

    throw '未找到可运行的 Python 3.10+。现有 venv 也不可用，请先安装 Python 并确保 py 或 python 可用。'
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Operation
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation 失败，退出码：$LASTEXITCODE"
    }
}

function Remove-BrokenVenv {
    if (-not (Test-Path -LiteralPath $VenvRoot)) { return }

    $rootFull = [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\', '/')
    $venvFull = [System.IO.Path]::GetFullPath($VenvRoot).TrimEnd('\', '/')
    $venvParent = [System.IO.Path]::GetDirectoryName($venvFull).TrimEnd('\', '/')
    if (-not [string]::Equals($venvParent, $rootFull, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals([System.IO.Path]::GetFileName($venvFull), 'venv', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝删除非项目 venv 路径：$venvFull"
    }

    $item = Get-Item -LiteralPath $VenvRoot -Force
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw "venv 是链接/重解析点，为避免删除链接目标而停止：$VenvRoot"
    }
    Write-Host '检测到损坏或迁移过的 venv，正在重建项目虚拟环境 ...' -ForegroundColor Yellow
    Remove-Item -LiteralPath $VenvRoot -Recurse -Force
}

$Python = Find-Python
$venvIsValid = Test-PythonExecutable -Executable $VenvPython

if (-not $venvIsValid) {
    Remove-BrokenVenv
    Write-Host '正在创建项目虚拟环境 venv ...' -ForegroundColor Cyan
    Invoke-Checked -Executable $Python.Path -Arguments ($Python.Args + @('-m', 'venv', $VenvRoot)) -Operation '创建虚拟环境'
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "虚拟环境创建后仍找不到解释器：$VenvPython"
}

Write-Host '正在检查并安装依赖 ...' -ForegroundColor Cyan
$requirements = Join-Path $ProjectRoot 'requirements.txt'
if ($ForceReinstall) {
    Invoke-Checked -Executable $VenvPython -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '--upgrade', '--force-reinstall', '-r', $requirements) -Operation '安装项目依赖'
} else {
    # 不强制升级已安装的包，避免仅做本地初始化时因网络/镜像不可用而长时间阻塞。
    Invoke-Checked -Executable $VenvPython -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', $requirements) -Operation '安装项目依赖'
}

Write-Host '正在执行 Python 导入检查 ...' -ForegroundColor Cyan
$importCheck = "import sys; sys.path.insert(0, 'py'); import config, captcha, followup_batch, shop_followup, shop_list_reader, task_runtime, youzan_bot; print('IMPORT_CHECK=PASS')"
Invoke-Checked -Executable $VenvPython -Arguments @('-c', $importCheck) -Operation 'Python 导入检查'
Invoke-Checked -Executable $VenvPython -Arguments @('-m', 'pip', 'check') -Operation '依赖一致性检查'

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
Write-Host '启动 GUI：      .\.venv\Scripts\python.exe .\main.py'
Write-Host '或直接运行：    .\run.cmd'
Write-Host '安全试运行：    .\.venv\Scripts\python.exe .\main.py --fill-first3-dry'

