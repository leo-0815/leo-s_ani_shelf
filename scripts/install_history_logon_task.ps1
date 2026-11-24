[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = 'Stop'
$name = 'AniShelf History Menu'
$root = Split-Path -Parent $PSScriptRoot
$launcher = Get-ChildItem -LiteralPath $root -File -Filter '*.bat' |
    Where-Object { Select-String -LiteralPath $_.FullName -SimpleMatch 'python -m app.history_cli' -Quiet }
if (@($launcher).Count -ne 1) {
    throw 'Expected exactly one AniShelf history menu batch file.'
}
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = 'PT5M'
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" `
    -Argument ('/d /c ""{0}""' -f $launcher.FullName) -WorkingDirectory $root
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
if ($PSCmdlet.ShouldProcess($name, 'Open the history menu five minutes after this user signs in')) {
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description 'Open the AniShelf history menu; no automatic crawl or sync.' -Force
}
