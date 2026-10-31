# Bat request grabred tu APP tren Android emulator bang mitmproxy.
#   cd C:\Users\Quang\Documents\LC79\xoso66_standalone
#   .\sniff_app_grabred_mitm.ps1
#
# 1) Mo emulator, adb devices thay device
# 2) Chay script
# 3) Cai CA lan dau (Settings -> Security) neu can
# 4) Mo APP -> Phong chat -> bam nhan li xi 1 lan
# 5) Ctrl+C; log trong red_packet_logs\

$ErrorActionPreference = "Stop"

$Serial = $env:ANDROID_SERIAL
if (-not $Serial) {
    $devs = @(adb devices | Select-String "device$" | ForEach-Object { ($_ -split "\s+")[0] })
    if ($devs.Count -eq 0) { throw "Khong thay emulator. Chay: adb devices" }
    $Serial = $devs[0]
}
Write-Host ">>> device=$Serial"

$OutDir = Join-Path $PSScriptRoot "red_packet_logs"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$Stamp = [int64]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
$FlowPath = Join-Path $OutDir "app_grabred_mitm_$Stamp.flow"
$TxtPath = Join-Path $OutDir "app_grabred_mitm_$Stamp.txt"
$AddonPath = Join-Path $PSScriptRoot "mitm_grabred_addon.py"
$MitmBin = "C:\Program Files\mitmproxy\bin\mitmdump.exe"
$CaPem = Join-Path $env:USERPROFILE ".mitmproxy\mitmproxy-ca-cert.pem"

if (-not (Test-Path $MitmBin)) { throw "Thieu mitmdump: $MitmBin" }
if (-not (Test-Path $AddonPath)) { throw "Thieu addon: $AddonPath" }

# Emulator classic: host = 10.0.2.2. Neu khong ra net: $env:MITM_HOST = IP LAN may
$HostForEmu = if ($env:MITM_HOST) { $env:MITM_HOST } else { "10.0.2.2" }
$Port = if ($env:MITM_PORT) { [int]$env:MITM_PORT } else { 8975 }

Write-Host ">>> mitm listen 0.0.0.0:$Port  (emu proxy = ${HostForEmu}:$Port)"
Write-Host ">>> flow=$FlowPath"
Write-Host ">>> txt =$TxtPath"

if (Test-Path $CaPem) {
    $TmpCer = Join-Path $env:TEMP "mitmproxy-ca.cer"
    Copy-Item $CaPem $TmpCer -Force
    adb -s $Serial push $TmpCer /sdcard/Download/mitmproxy-ca.cer
    Write-Host ">>> Da push CA -> /sdcard/Download/mitmproxy-ca.cer"
    Write-Host ">>> Lan dau: Settings -> Security -> Install a certificate -> CA certificate"
}

adb -s $Serial shell settings put global http_proxy "${HostForEmu}:${Port}"
Write-Host ">>> http_proxy=${HostForEmu}:${Port}"
Write-Host ""
Write-Host "=== MO APP -> Phong chat -> BAM NHAN LI XI 1 lan ==="
Write-Host "=== Ctrl+C de dung ==="
Write-Host ""

$env:MITM_GRABRED_OUT = $TxtPath
try {
    & $MitmBin -p $Port --set block_global=false -w $FlowPath -s $AddonPath
}
finally {
    Write-Host ">>> Clear proxy tren emulator..."
    adb -s $Serial shell settings delete global http_proxy 2>$null | Out-Null
    Write-Host ">>> Xong. Doc: $TxtPath"
    if (Test-Path $TxtPath) {
        Get-Content $TxtPath -Tail 80
    }
    else {
        Write-Host ">>> Chua co file txt - APP co the SSL pin / chua cai CA / khong goi grabred."
    }
}
