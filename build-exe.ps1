param(
    [string]$UpxPath = '',
    [switch]$SkipSign
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw ".venv interpreter not found. Run .\setup.ps1 first."
}
if ([string]::IsNullOrWhiteSpace($UpxPath)) {
    $upxCommand = Get-Command upx.exe -ErrorAction SilentlyContinue
    if ($upxCommand) { $UpxPath = $upxCommand.Source }
}
if (-not $UpxPath -or -not (Test-Path -LiteralPath $UpxPath)) {
    throw 'UPX not found. Install UPX and add upx.exe to PATH, or pass -UpxPath <path>.'
}
$PyInstaller = & $Python -c "import PyInstaller; print(PyInstaller.__version__)" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw 'PyInstaller is missing in .venv. Run .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt.'
}
$UpxVersion = & $UpxPath --version | Select-Object -First 1
Write-Host "Building with PyInstaller $PyInstaller and $UpxVersion"
Push-Location $ProjectRoot
try {
    & $Python -m PyInstaller --noconfirm --clean --onefile --noconsole `
        --name YouzanAuto `
        --paths (Join-Path $ProjectRoot 'py') `
        --collect-all playwright `
        --upx-dir (Split-Path -Parent $UpxPath) `
        --distpath (Join-Path $ProjectRoot 'dist') `
        --workpath (Join-Path $ProjectRoot 'build\pyinstaller') `
        (Join-Path $ProjectRoot 'main.py')
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
    Copy-Item -LiteralPath (Join-Path $ProjectRoot 'project_config.json') `
        -Destination (Join-Path $ProjectRoot 'dist\project_config.json') -Force
    $Exe = Join-Path $ProjectRoot 'dist\YouzanAuto.exe'
    if (-not (Test-Path -LiteralPath $Exe)) { throw 'Expected EXE was not produced.' }
    & $UpxPath --best --lzma --force $Exe
    if ($LASTEXITCODE -ne 0) { throw "UPX compression of the final EXE failed with exit code $LASTEXITCODE" }
    Write-Host 'UPX compression applied to the final EXE and bundled native binaries.'
    if (-not $SkipSign) {
        $SignTool = Get-ChildItem 'C:\Program Files (x86)\Windows Kits\10\bin' `
            -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
            Sort-Object FullName -Descending | Select-Object -First 1
        if (-not $SignTool) { throw 'Windows SDK signtool.exe (x64) was not found.' }
        $Subject = 'CN=YouzanAuto Local Self-Signed Code Signing'
        $Certificate = Get-ChildItem Cert:\CurrentUser\My |
            Where-Object { $_.Subject -eq $Subject -and $_.HasPrivateKey } |
            Sort-Object NotAfter -Descending | Select-Object -First 1
        if (-not $Certificate) {
            $Certificate = New-SelfSignedCertificate -Type CodeSigningCert `
                -Subject $Subject -CertStoreLocation Cert:\CurrentUser\My `
                -HashAlgorithm SHA256 -KeyLength 3072 -NotAfter (Get-Date).AddYears(2)
        }
        & $SignTool.FullName sign /fd SHA256 /s My /sha1 $Certificate.Thumbprint $Exe
        if ($LASTEXITCODE -ne 0) { throw "Authenticode signing failed with exit code $LASTEXITCODE" }
        Write-Host 'Signed with a locally generated self-signed certificate.'
        Write-Warning 'This signature is not publicly trusted and does not remove SmartScreen warnings on other PCs.'
    }
    $Hash = (Get-FileHash -LiteralPath $Exe -Algorithm SHA256).Hash
    Write-Host "EXE: $Exe"
    Write-Host "SHA256: $Hash"
    if (-not $SkipSign) {
        $Signature = Get-AuthenticodeSignature -LiteralPath $Exe
        Write-Host "Signature status: $($Signature.Status)"
        Write-Host "Signer: $($Signature.SignerCertificate.Subject)"
    }
}
finally {
    Pop-Location
}
