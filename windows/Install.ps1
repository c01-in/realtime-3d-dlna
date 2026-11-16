[CmdletBinding()]
param(
    [Parameter(Mandatory)][string[]]$MediaPath,
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA 'Realtime3DDLNA'),
    [string]$ModelDir,
    [string]$PythonExe,
    [string[]]$ExtraPythonPath = @(),
    [string]$FfmpegDir,
    [string]$CudaHeaders,
    [ValidateRange(1,65535)][int]$Port = 8200,
    [string]$LanIp,
    [string]$Name = 'Realtime 3D DLNA',
    [string]$TaskName = 'Realtime 3D DLNA',
    [ValidateSet('keep-warm','release-idle')][string]$ResourceMode = 'keep-warm',
    [switch]$AcceptOvdaNonCommercial,
    [switch]$LanFirewall
)
$ErrorActionPreference = 'Stop'
$Source = Split-Path -Parent $PSScriptRoot
if (-not $AcceptOvdaNonCommercial) {
    throw 'Review https://huggingface.co/FriedFeid/oVDA/blob/main/LICENSE, then use -AcceptOvdaNonCommercial.'
}
function Invoke-Checked {
    param([string]$Program, [string[]]$Params)
    & $Program @Params
    if ($LASTEXITCODE -ne 0) { throw "$Program exited with code $LASTEXITCODE" }
}
$MediaPath = @($MediaPath | ForEach-Object {
    if (-not (Test-Path -LiteralPath $_ -PathType Container)) { throw "Media directory unavailable: $_" }
    (Resolve-Path -LiteralPath $_).ProviderPath
})
$DataDir = [IO.Path]::GetFullPath($DataDir)
if (-not $ModelDir) { $ModelDir = Join-Path $DataDir 'models' }
$ModelDir = [IO.Path]::GetFullPath($ModelDir)
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null

if ($FfmpegDir) {
    $FfmpegDir = (Resolve-Path -LiteralPath $FfmpegDir).ProviderPath
    $env:PATH = $FfmpegDir + [IO.Path]::PathSeparator + $env:PATH
}
foreach ($command in @('ffmpeg.exe', 'ffprobe.exe')) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Install an FFmpeg build with HEVC NVENC support, then set -FfmpegDir or add $command to PATH."
    }
}
if (-not $FfmpegDir) {
    $FfmpegDir = Split-Path -Parent (Get-Command ffmpeg.exe -CommandType Application).Source
}
if ($CudaHeaders) { $CudaHeaders = (Resolve-Path -LiteralPath $CudaHeaders).ProviderPath }
$ExtraPythonPath = @($ExtraPythonPath | ForEach-Object { (Resolve-Path -LiteralPath $_).ProviderPath })

if (-not $PythonExe) {
    $InstallTemp = Join-Path $DataDir 'install-temp'
    New-Item -ItemType Directory -Path $InstallTemp -Force | Out-Null
    $env:TEMP = $InstallTemp
    $env:TMP = $InstallTemp
    $Venv = Join-Path $DataDir 'venv'
    $PythonExe = Join-Path $Venv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        if (-not (Get-Command py.exe -ErrorAction SilentlyContinue)) {
            throw 'Install Python 3.12 with the Python launcher or supply -PythonExe.'
        }
        Invoke-Checked py.exe @('-3.12', '-m', 'venv', $Venv)
    }
    Invoke-Checked $PythonExe @('-m', 'pip', 'install', '--no-cache-dir', '--index-url',
        'https://download.pytorch.org/whl/cu128', 'torch==2.8.0')
    Invoke-Checked $PythonExe @('-m', 'pip', 'install', '--no-cache-dir', '-r', (Join-Path $Source 'requirements.txt'))
}
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).ProviderPath
$Pythonw = Join-Path (Split-Path -Parent $PythonExe) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $Pythonw -PathType Leaf)) { throw "Cannot find $Pythonw" }
Invoke-Checked $PythonExe @('-B', (Join-Path $Source 'assets.py'), '--data-dir', $DataDir,
    '--model-dir', $ModelDir, '--accept-ovda-noncommercial')

$ConfigPath = Join-Path $DataDir 'settings.json'
$Current = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Current -and $Current.State -eq 'Running') { Stop-ScheduledTask -TaskName $TaskName }
@{
    data_dir = $DataDir; model_dir = $ModelDir; media = $MediaPath; port = $Port
    name = $Name; lan_ip = $LanIp; ffmpeg_dir = $FfmpegDir; cuda_headers = $CudaHeaders
    python_path = $ExtraPythonPath; resource_mode = $ResourceMode.ToLowerInvariant()
} | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $ConfigPath -Encoding UTF8
if ($LanFirewall) {
    $HttpRule = "Realtime3DDLNA-HTTP-$Port"
    $CommonFirewall = @{Direction='Inbound'; Action='Allow'; Profile='Private'; RemoteAddress='LocalSubnet'}
    if (-not (Get-NetFirewallRule -Name $HttpRule -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule @CommonFirewall -Name $HttpRule -DisplayName "Realtime 3D DLNA HTTP ($Port)" -Protocol TCP -LocalPort $Port | Out-Null
    }
    if (-not (Get-NetFirewallRule -Name 'Realtime3DDLNA-SSDP' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule @CommonFirewall -Name 'Realtime3DDLNA-SSDP' -DisplayName 'Realtime 3D DLNA discovery' -Protocol UDP -LocalPort 1900 | Out-Null
    }
}
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Arguments = '-X utf8 -B "{0}" --config "{1}"' -f (Join-Path $Source 'serve.py'), $ConfigPath
$Action = New-ScheduledTaskAction -Execute $Pythonw -Argument $Arguments -WorkingDirectory $Source
$Principal = New-ScheduledTaskPrincipal -UserId $Identity -LogonType Interactive
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $Identity
$Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$Task = @{TaskName=$TaskName; Action=$Action; Trigger=$Trigger; Principal=$Principal; Settings=$Settings}
Register-ScheduledTask @Task -Description 'Realtime 2D to top-and-bottom 3D for DLNA' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
$Deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $Deadline) {
    try {
        $Ready = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
        if ($Ready.status -eq 'ready' -and $Ready.name -eq $Name -and $Ready.model -eq 'oVDA c16' -and
            $Ready.conversion.resource_mode -eq $ResourceMode) {
            Write-Host "Ready at http://localhost:$Port/ · log: $(Join-Path $DataDir 'server.log')"
            exit 0
        }
    } catch {}
    if ((Get-ScheduledTask -TaskName $TaskName).State -ne 'Running') {
        throw "Task stopped during startup. See $(Join-Path $DataDir 'server.log')."
    }
    Start-Sleep -Seconds 1
}
throw "Service is still starting. See $(Join-Path $DataDir 'server.log')."
