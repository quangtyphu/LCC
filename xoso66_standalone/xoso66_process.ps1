# XOSO66 - kiem tra / tat process (khong dung LC79 main.py)
# Usage:
#   .\xoso66_process.ps1
#   .\xoso66_process.ps1 status
#   .\xoso66_process.ps1 stop-main
#   .\xoso66_process.ps1 stop-api

param(
    [Parameter(Position = 0)]
    [ValidateSet('status', 'stop-main', 'stop-api')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
$XosoRoot = (Resolve-Path $PSScriptRoot).Path

function Get-ListenPorts([int]$ProcId) {
    $ports = [System.Collections.Generic.HashSet[int]]::new()
    netstat -ano | Select-String 'LISTENING' | Select-String " $($ProcId)`$" | ForEach-Object {
        if ($_.Line -match ':(\d+)\s') { [void]$ports.Add([int]$matches[1]) }
    }
    return @($ports | Sort-Object)
}

function Test-Lc79Main([string]$Cmd, [int[]]$Ports) {
    if ($Cmd -match '\\lc79\\main\.py') { return $true }
    if ($Ports -contains 8080) { return $true }
    return $false
}

function Get-ProcessKind([string]$Cmd, [int[]]$Ports) {
    $cmdL = if ($Cmd) { $Cmd.ToLowerInvariant() } else { '' }

    if ($cmdL -match 'xoso66_api\.py') { return 'xoso66-api' }
    if ($cmdL -match 'xoso66_standalone[\\/]main\.py') { return 'xoso66-main' }
    if ($cmdL -match 'xoso66_standalone' -and $cmdL -match 'main\.py') { return 'xoso66-main' }

    if (Test-Lc79Main $Cmd $Ports) { return 'lc79-main' }
    if ($cmdL -match '\\allgame\\main\.py') { return 'allgame-main' }
    if ($cmdL -match '\\browser_isolate\\main\.py') { return 'browser-isolate' }
    if ($Ports -contains 8888) { return 'banking-api' }

    if ($cmdL -match '(^|\s)main\.py' -or $cmdL -match 'main\.py"?\s*$') {
        if ($Ports -contains 8080) { return 'lc79-main' }
        if (($Ports -contains 8799) -and ($Ports -notcontains 8080)) { return 'xoso66-main' }
    }

    if ($cmdL -match 'xoso66') { return 'xoso66-other' }
    return 'other'
}

function Get-PythonRows() {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        ForEach-Object {
            $procId = [int]$_.ProcessId
            $ports = Get-ListenPorts $procId
            $kind = Get-ProcessKind $_.CommandLine $ports
            [PSCustomObject]@{
                PID   = $procId
                Kind  = $kind
                Ports = if ($ports.Count) { ($ports -join ',') } else { '-' }
                Cmd   = $_.CommandLine
            }
        }
}

function Show-Status() {
    $rows = Get-PythonRows | Where-Object { $_.Kind -ne 'other' -or $_.Cmd -match 'main\.py|xoso66' }
    if (-not $rows) {
        Write-Host 'Khong thay python XOSO66 / LC79 main.' -ForegroundColor Yellow
        return
    }

    $labels = @{
        'xoso66-main'     = 'XOSO66 main.py (full automation)'
        'xoso66-api'      = 'XOSO66 API only (xoso66_api.py)'
        'lc79-main'       = 'LC79 main.py - KHONG TAT bang script nay'
        'allgame-main'    = 'AllGame main.py'
        'browser-isolate' = 'browser_isolate main.py'
        'banking-api'     = 'Banking (port 8888)'
        'xoso66-other'    = 'Python khac trong xoso66'
        'other'           = 'Khac'
    }

    Write-Host "Thu muc xoso66: $XosoRoot"
    Write-Host ''
    foreach ($r in $rows | Sort-Object Kind, PID) {
        $label = $labels[$r.Kind]
        if (-not $label) { $label = $r.Kind }
        $color = switch ($r.Kind) {
            'xoso66-main' { 'Red' }
            'xoso66-api'  { 'Cyan' }
            'lc79-main'   { 'Green' }
            default       { 'Gray' }
        }
        Write-Host ("[{0}] PID {1} ports {2}" -f $label, $r.PID, $r.Ports) -ForegroundColor $color
        if ($r.Cmd) {
            $short = if ($r.Cmd.Length -gt 160) { $r.Cmd.Substring(0, 160) + '...' } else { $r.Cmd }
            Write-Host "       $short"
        }
        Write-Host ''
    }

    $main = $rows | Where-Object Kind -eq 'xoso66-main'
    if ($main) {
        Write-Host '=> XOSO66 main.py DANG CHAY. Tat: .\xoso66_process.ps1 stop-main' -ForegroundColor Red
    } else {
        Write-Host '=> XOSO66 main.py KHONG chay (chi API/CMS la binh thuong).' -ForegroundColor Green
    }
}

function Stop-ByKind([string]$Kind, [string]$Prompt) {
    $targets = Get-PythonRows | Where-Object Kind -eq $Kind
    if (-not $targets) {
        Write-Host "Khong co process loai $Kind."
        return
    }
    foreach ($t in $targets) {
        Write-Host "$Prompt PID $($t.PID) ports $($t.Ports)"
        Stop-Process -Id $t.PID -Force -ErrorAction Stop
    }
}

switch ($Action) {
    'status' { Show-Status }
    'stop-main' {
        $targets = Get-PythonRows | Where-Object Kind -eq 'xoso66-main'
        if (-not $targets) {
            Write-Host 'Khong tim thay XOSO66 main.py - khong tat gi (LC79 an toan).'
            exit 0
        }
        $safe = $targets | Where-Object { $_.Ports -notcontains 8080 }
        $blocked = $targets | Where-Object { $_.Ports -contains 8080 }
        if ($blocked) {
            Write-Host 'Bo qua PID co port 8080 (LC79):' ($blocked.PID -join ', ')
        }
        if (-not $safe) {
            Write-Host 'Khong co PID xoso66-main an toan de tat.'
            exit 1
        }
        foreach ($t in $safe) {
            Write-Host "Dung XOSO66 main PID $($t.PID) ..."
            Stop-Process -Id $t.PID -Force -ErrorAction Stop
        }
        Write-Host 'Xong.'
    }
    'stop-api' {
        Stop-ByKind 'xoso66-api' 'Dung XOSO66 API'
        Write-Host 'CMS co the chay lai API qua POST /api/xoso66/ensure.'
    }
}
