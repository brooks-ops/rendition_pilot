$ErrorActionPreference = 'Stop'
# Run in a normal PowerShell window as the Windows user who can see T:.
$python = (Get-Command python -ErrorAction Stop).Source
& $python -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10 or newer required"'
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.10 or newer first.' }
$destination = Join-Path $env:LOCALAPPDATA 'RenditionPilot\ReturnedMail'
New-Item -ItemType Directory -Force $destination | Out-Null
$script = Join-Path $destination 'watcher.py'
Copy-Item (Join-Path $PSScriptRoot 'watcher.py') $script -Force
& $python $script setup
if ($LASTEXITCODE -ne 0) { throw 'Sign-in failed; task was not installed.' }
$action = New-ScheduledTaskAction -Execute $python -Argument "`"$script`" run" -WorkingDirectory $destination
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName 'Rendition Pilot Returned Mail' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host 'Installed. Watches while this Windows user is signed in; T: must be connected.'
Write-Host "Check status: python `"$script`" status"
