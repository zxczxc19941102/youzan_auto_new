param(
    [switch]$ShowMode,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$GitArgs
)

$ErrorActionPreference = 'Stop'
$proxyHost = '127.0.0.1'
$proxyPort = 10808
$proxy = "socks5h://${proxyHost}:${proxyPort}"

# 500ms 内检测本地代理端口是否可连接
$proxyOpen = $false
$client = New-Object System.Net.Sockets.TcpClient
try {
    $async = $client.BeginConnect($proxyHost, $proxyPort, $null, $null)
    if ($async.AsyncWaitHandle.WaitOne(500)) {
        $client.EndConnect($async)
        $proxyOpen = $client.Connected
    }
} catch {
    $proxyOpen = $false
} finally {
    $client.Close()
}

if ($ShowMode) {
    Write-Host ($(if ($proxyOpen) { 'MODE=PROXY' } else { 'MODE=DIRECT' }))
}

if (-not $GitArgs -or $GitArgs.Count -eq 0) {
    $GitArgs = @('push', '-u', 'origin', 'main')
}

if ($proxyOpen) {
    & git -c "http.proxy=$proxy" -c "https.proxy=$proxy" @GitArgs
} else {
    # 显式清空 -c，确保不会被全局/仓库旧代理配置干扰
    & git -c http.proxy= -c https.proxy= @GitArgs
}
exit $LASTEXITCODE
