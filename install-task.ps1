# Registers the per-user task that starts the lamp daemon at logon with no window. Safe to run again.
# It runs only while you are logged on and needs no password or elevation.
# Undo: Stop-ScheduledTask -TaskName 'moonside-lamp'; Unregister-ScheduledTask -TaskName 'moonside-lamp' -Confirm:$false
param(
  [string]$TaskName = 'moonside-lamp'
)
$ErrorActionPreference = 'Stop'
$pythonw = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$script = Join-Path $PSScriptRoot 'moonside_lamp.py'
$arguments = '-I -X utf8 "{0}" run --log-file' -f $script
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -Priority 7 -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
$task = Get-ScheduledTask -TaskName $TaskName
'{0}: {1}, at logon, runs {2} {3}' -f $task.TaskName, $task.State, $task.Actions[0].Execute, $task.Actions[0].Arguments
