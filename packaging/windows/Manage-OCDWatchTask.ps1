# Optional per-user observe-only task. This file does nothing until invoked with -Action.
# Example: .\Manage-OCDWatchTask.ps1 -Action Install -ConfigPath 'C:\Users\me\.config\ocd\config.json'
# Removal: .\Manage-OCDWatchTask.ps1 -Action Remove
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Install', 'Remove')]
    [string]$Action,

    [string]$ConfigPath,

    [string]$PythonExecutable = 'py.exe',

    [ValidateRange(1, 3600)]
    [int]$IntervalSeconds = 2
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$taskName = 'OCD-Watch-ObserveOnly'
$taskMarker = 'OCD official observe-only monitor; never runs apply or deletes files.'
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

if ($null -ne $existingTask -and $existingTask.Description -ne $taskMarker) {
    throw "A task named '$taskName' exists but is not this OCD task. Refusing to alter it."
}

if ($Action -eq 'Remove') {
    if ($null -eq $existingTask) {
        Write-Host "OCD observe-only task is already absent."
        exit 0
    }
    # Unregistering a running task does not guarantee its process has stopped.
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Removed per-user OCD observe-only task."
    exit 0
}

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    throw 'Install requires -ConfigPath pointing to an existing OCD JSON configuration.'
}

if ($ConfigPath -notmatch '^[A-Za-z]:\\') {
    throw 'ConfigPath must be a full local drive-letter path, not a relative or network location.'
}

$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath -ErrorAction Stop).ProviderPath
if (-not (Test-Path -LiteralPath $resolvedConfig -PathType Leaf)) {
    throw "ConfigPath is not an existing file: $resolvedConfig"
}

$pythonCommand = Get-Command -Name $PythonExecutable -CommandType Application -ErrorAction Stop
$pythonPath = $pythonCommand.Source
if ([string]::IsNullOrWhiteSpace($pythonPath)) {
    throw 'Could not resolve the requested Python executable.'
}

$arguments = '-m ocd --config "' + $resolvedConfig + '" watch --interval ' + $IntervalSeconds
if ($null -ne $existingTask) {
    # Never silently replace a task with changed configuration or executable.
    if ($existingTask.Actions.Count -eq 1 -and
        $existingTask.Actions[0].Execute -eq $pythonPath -and
        $existingTask.Actions[0].Arguments -eq $arguments) {
        Write-Host "OCD observe-only task is already installed with the same settings."
        exit 0
    }
    throw 'OCD task exists with different settings. Remove it explicitly before reinstalling.'
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskAction = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartCount 0

Register-ScheduledTask -TaskName $taskName -Description $taskMarker -Action $taskAction `
    -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
Write-Host "Registered user-level OCD observe-only task. It will run 'ocd watch' at logon."
Write-Host "No file moves, deletions, cloud uploads, or automatic apply are enabled."
