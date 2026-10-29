param(
    [Parameter(Mandatory = $true)][string]$ServerUrl,
    [Parameter(Mandatory = $true)][string]$AgentToken
)

$ErrorActionPreference = "Stop"
$ServerUrl = $ServerUrl.TrimEnd("/")
if ($ServerUrl -notmatch '^https?://') { throw "La URL del servidor no es válida." }
if ($AgentToken -match '[\r\n\"]') { throw "El token del agente no es válido." }

$pythonCommand = Get-Command py -ErrorAction SilentlyContinue
$pythonArgs = @("-3")
if (-not $pythonCommand) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    $pythonArgs = @()
}
if (-not $pythonCommand) {
    throw "Falta Python 3. Instálalo desde https://www.python.org/downloads/windows/ y activa 'Add Python to PATH'."
}

$installDir = Join-Path $env:LOCALAPPDATA "EscritorioRemotoAgent"
$bundle = Join-Path $installDir "device-agent.pyz"
$runner = Join-Path $installDir "run-agent.cmd"
$startup = [Environment]::GetFolderPath("Startup")
$launcher = Join-Path $startup "Escritorio Remoto Agent.vbs"
New-Item -ItemType Directory -Force -Path $installDir | Out-Null

# Detiene instalaciones anteriores para evitar varios agentes simultáneos y
# permitir sustituir el .pyz incluso cuando Windows lo tenía abierto.
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and $_.CommandLine.Contains($installDir) -and
    ($_.CommandLine.Contains("device-agent") -or $_.CommandLine.Contains("run-agent.cmd"))
} | ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate -ErrorAction SilentlyContinue | Out-Null }
Start-Sleep -Milliseconds 800

$pendingBundle = "$bundle.new"
Invoke-WebRequest -UseBasicParsing "$ServerUrl/api/device-agent/download/bundle" -OutFile $pendingBundle
Move-Item -Force $pendingBundle $bundle

$pythonPrefix = if ($pythonArgs.Count) { '"' + $pythonCommand.Source + '" -3' } else { '"' + $pythonCommand.Source + '"' }
$runnerContent = @"
@echo off
set PYTHONUNBUFFERED=1
:restart
$pythonPrefix "$bundle" --server "$ServerUrl" --token "$AgentToken"
timeout /t 5 /nobreak >nul
goto restart
"@
Set-Content -Path $runner -Value $runnerContent -Encoding ASCII

$escapedRunner = $runner.Replace('"', '""')
$vbs = 'CreateObject("Wscript.Shell").Run Chr(34) & "' + $escapedRunner + '" & Chr(34), 0, False'
Set-Content -Path $launcher -Value $vbs -Encoding ASCII
Start-Process -FilePath "wscript.exe" -ArgumentList ('"' + $launcher + '"')

Write-Host "Agente FTP de Windows instalado y activo."
Write-Host "Se iniciará automáticamente con la sesión de Windows."
