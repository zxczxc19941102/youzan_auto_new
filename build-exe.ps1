param(
    [string]$UpxPath = '',
    [switch]$SkipSign,
    [switch]$SkipUpx,
    [ValidatePattern('^[A-Za-z0-9_-]+$')]
    [string]$BuildName = 'YouzanAuto'
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw '.venv interpreter not found. Run .\setup.ps1 first.'
}
if (-not $SkipUpx) {
    if ([string]::IsNullOrWhiteSpace($UpxPath)) {
        $upxCommand = Get-Command upx.exe -ErrorAction SilentlyContinue
        if ($upxCommand) { $UpxPath = $upxCommand.Source }
    }
    if (-not $UpxPath -or -not (Test-Path -LiteralPath $UpxPath)) {
        throw 'UPX not found. Pass -UpxPath <path>, or use -SkipUpx for the compatibility build.'
    }
    $UpxVersion = & $UpxPath --version | Select-Object -First 1
}
$PyInstaller = & $Python -c "import PyInstaller; print(PyInstaller.__version__)" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw 'PyInstaller is missing in .venv. Install requirements-build.txt in .venv.'
}
Write-Host "Building $BuildName with PyInstaller $PyInstaller; UPX enabled: $(-not $SkipUpx) $UpxVersion"
Push-Location $ProjectRoot
try {
    $BuildArgs = @(
        '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--noconsole',
        '--name', $BuildName,
        '--paths', (Join-Path $ProjectRoot 'py'),
        '--collect-all', 'playwright',
        '--add-data', ((Join-Path $ProjectRoot 'project_config.json') + ';.'),
        '--distpath', (Join-Path $ProjectRoot 'dist'),
        '--workpath', (Join-Path $ProjectRoot ('build\pyinstaller\' + $BuildName)),
        '--specpath', (Join-Path $ProjectRoot 'build\specs')
    )
    if ($SkipUpx) {
        $BuildArgs += '--noupx'
    }
    else {
        # Compress supported native modules, not the appended onefile executable.
        # Never force-pack a signed/CFG-enabled binary.
        $BuildArgs += @(
            '--upx-dir', (Split-Path -Parent $UpxPath),
            '--upx-exclude', 'python*.dll',
            '--upx-exclude', '_tkinter.pyd',
            '--upx-exclude', 'tcl*.dll',
            '--upx-exclude', 'tk*.dll',
            '--upx-exclude', 'node.exe'
        )
    }
    $BuildArgs += (Join-Path $ProjectRoot 'main.py')
    # Windows PowerShell 5 may classify native stderr (normal PyInstaller
    # INFO logs) as an error when callers redirect output. Use the exit code.
    $PreviousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Python @BuildArgs
        $BuildExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $PreviousErrorAction
    }
    if ($BuildExitCode -ne 0) { throw "PyInstaller failed with exit code $BuildExitCode" }
    $Exe = Join-Path $ProjectRoot ('dist\' + $BuildName + '.exe')
    if (-not (Test-Path -LiteralPath $Exe)) { throw 'Expected EXE was not produced.' }
    if (-not $SkipSign) {
        $KitsRoot = Join-Path ([Environment]::GetEnvironmentVariable('ProgramFiles(x86)')) 'Windows Kits\10\bin'
        $SignTool = Get-ChildItem -LiteralPath $KitsRoot -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
            Sort-Object FullName -Descending | Select-Object -First 1
        if (-not $SignTool) { throw 'Windows SDK signtool.exe (x64) was not found.' }
        $Subject = 'CN=YouzanAuto Local Self-Signed Code Signing'
        $Certificate = Get-ChildItem Cert:\CurrentUser\My |
            Where-Object { $_.Subject -eq $Subject -and $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date) } |
            Sort-Object NotAfter -Descending | Select-Object -First 1
        if (-not $Certificate) {
            $Certificate = New-SelfSignedCertificate -Type CodeSigningCert -Subject $Subject -CertStoreLocation Cert:\CurrentUser\My -HashAlgorithm SHA256 -KeyLength 3072 -NotAfter (Get-Date).AddYears(2)
        }
        & $SignTool.FullName sign /fd SHA256 /s My /sha1 $Certificate.Thumbprint $Exe
        if ($LASTEXITCODE -ne 0) { throw "Authenticode signing failed with exit code $LASTEXITCODE" }
        # Export only the public certificate, NEVER a private key or PFX.
        Export-Certificate -Cert $Certificate -FilePath (Join-Path $ProjectRoot 'dist\YouzanAuto-Signer.cer') -Force | Out-Null
        $Signature = Get-AuthenticodeSignature -LiteralPath $Exe
        if (-not $Signature.SignerCertificate -or $Signature.SignerCertificate.Thumbprint -ne $Certificate.Thumbprint) {
            throw 'The generated executable does not have the expected signature.'
        }
        Write-Host "Signature status on this PC: $($Signature.Status)"
        Write-Warning 'Self-signed certificate: other PCs do NOT automatically trust it. Do not disable security software.'
    }
    Write-Host "EXE: $Exe"
    Write-Host "SIZE: $((Get-Item -LiteralPath $Exe).Length) bytes"
    Write-Host "SHA256: $((Get-FileHash -LiteralPath $Exe -Algorithm SHA256).Hash)"
}
finally {
    Pop-Location
}
