param(
    [string]$PythonPath = $env:CAMELIA_PYTHON,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$uiDirectory = Join-Path $projectRoot "camelia-ui"
$logDirectory = Join-Path ([System.IO.Path]::GetTempPath()) "camelia-launcher"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$apiOutputLog = Join-Path $logDirectory "api-$timestamp.log"
$apiErrorLog = Join-Path $logDirectory "api-$timestamp.error.log"
$uiOutputLog = Join-Path $logDirectory "ui-$timestamp.log"
$uiErrorLog = Join-Path $logDirectory "ui-$timestamp.error.log"
$apiProcess = $null
$uiProcess = $null
$launcherFailed = $false

function Test-TcpPort {
    param([int]$Port)

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connection = $client.ConnectAsync("127.0.0.1", $Port)
        return $connection.Wait(300) -and $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Test-CameliaApi {
    try {
        Invoke-WebRequest `
            -Uri "http://127.0.0.1:5000/api/status/launcher-probe" `
            -UseBasicParsing `
            -TimeoutSec 2 `
            -ErrorAction Stop | Out-Null
        return $false
    }
    catch {
        $statusCode = $null
        if ($_.Exception.Response) {
            $statusCode = [int]$_.Exception.Response.StatusCode
        }
        return $statusCode -eq 404 -and $_.ErrorDetails.Message -match "Session not found"
    }
}

function Wait-ForService {
    param(
        [int]$Port,
        [System.Diagnostics.Process]$Process,
        [int]$TimeoutSeconds = 60
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $Process.Refresh()
        if ($Process.HasExited) {
            return $false
        }
        if (Test-TcpPort -Port $Port) {
            return $true
        }
        Start-Sleep -Milliseconds 300
    }
    return $false
}

function Resolve-CameliaPython {
    param([string]$RequestedPath)

    $candidates = @()
    if ($RequestedPath) {
        $candidates += $RequestedPath
    }
    $candidates += @(
        (Join-Path $projectRoot ".venv\Scripts\python.exe"),
        (Join-Path $projectRoot "camelia_env\Scripts\python.exe"),
        "C:\ProgramData\miniconda3\envs\camelia_env\python.exe"
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        return $pythonCommand.Source
    }

    throw "Could not find Python. Set CAMELIA_PYTHON to the full path of the Camelia environment's python.exe."
}

function Stop-ProcessTree {
    param(
        [System.Diagnostics.Process]$Process,
        [int]$Port
    )

    if (-not $Process) {
        return
    }

    $Process.Refresh()
    if (-not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    }

    Start-Sleep -Milliseconds 300
    $listenerProcessIds = Get-NetTCPConnection `
        -LocalPort $Port `
        -State Listen `
        -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique

    foreach ($listenerProcessId in $listenerProcessIds) {
        if ($listenerProcessId -ne $PID) {
            Stop-Process -Id $listenerProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

try {
    Set-Location -LiteralPath $projectRoot

    $apiPortBusy = Test-TcpPort -Port 5000
    $uiPortBusy = Test-TcpPort -Port 3000
    if ($apiPortBusy -and -not (Test-CameliaApi)) {
        throw "Port 5000 is in use by something other than the Camelia API."
    }
    if ($uiPortBusy -and -not $apiPortBusy) {
        throw "Port 3000 is already in use. Close the existing program and run this launcher again."
    }
    if ($apiPortBusy -and $uiPortBusy) {
        Write-Host "Camelia already appears to be running." -ForegroundColor Green
        if (-not $NoBrowser) {
            Start-Process "http://localhost:3000"
        }
        return
    }
    $python = Resolve-CameliaPython -RequestedPath $PythonPath
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm) {
        throw "npm.cmd was not found. Install Node.js or add it to PATH."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $uiDirectory "node_modules"))) {
        throw "UI dependencies are missing. Run npm install once inside the camelia-ui directory."
    }

    & $python -c "import flask, PIL, psutil" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "The selected Python does not contain Camelia's dependencies: $python"
    }

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $env:PYTHONUNBUFFERED = "1"

    if ($apiPortBusy) {
        Write-Host "Camelia API is already running; reusing it."
    }
    else {
        Write-Host "Starting Camelia API..."
        $apiProcess = Start-Process `
            -FilePath $python `
            -ArgumentList @("-u", "api.py") `
            -WorkingDirectory $projectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $apiOutputLog `
            -RedirectStandardError $apiErrorLog `
            -PassThru

        if (-not (Wait-ForService -Port 5000 -Process $apiProcess)) {
            throw "The API did not start. Check $apiOutputLog and $apiErrorLog"
        }
    }

    Write-Host "Starting Camelia web UI..."
    $uiProcess = Start-Process `
        -FilePath $npm.Source `
        -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", "3000") `
        -WorkingDirectory $uiDirectory `
        -WindowStyle Hidden `
        -RedirectStandardOutput $uiOutputLog `
        -RedirectStandardError $uiErrorLog `
        -PassThru

    if (-not (Wait-ForService -Port 3000 -Process $uiProcess)) {
        throw "The web UI did not start. Check $uiOutputLog and $uiErrorLog"
    }

    Write-Host ""
    Write-Host "Camelia is ready at http://localhost:3000" -ForegroundColor Green
    Write-Host "Logs are stored in $logDirectory"
    Write-Host "Press Enter to stop both services."

    if (-not $NoBrowser) {
        Start-Process "http://localhost:3000"
    }

    while ($true) {
        if ($apiProcess) {
            $apiProcess.Refresh()
        }
        $uiProcess.Refresh()
        if ($apiProcess -and $apiProcess.HasExited) {
            throw "The API stopped unexpectedly. Check $apiOutputLog and $apiErrorLog"
        }
        if ($uiProcess.HasExited) {
            throw "The web UI stopped unexpectedly. Check $uiOutputLog and $uiErrorLog"
        }
        if ([Console]::KeyAvailable) {
            $key = [Console]::ReadKey($true)
            if ($key.Key -eq [ConsoleKey]::Enter) {
                break
            }
        }
        Start-Sleep -Milliseconds 500
    }
}
catch {
    $launcherFailed = $true
    Write-Host ""
    Write-Host "Launcher error: $($_.Exception.Message)" -ForegroundColor Red
}
finally {
    if ($uiProcess -or $apiProcess) {
        Write-Host "Stopping Camelia services..."
    }
    Stop-ProcessTree -Process $uiProcess -Port 3000
    Stop-ProcessTree -Process $apiProcess -Port 5000
}

if ($launcherFailed) {
    exit 1
}

Write-Host "Camelia stopped." -ForegroundColor Green
