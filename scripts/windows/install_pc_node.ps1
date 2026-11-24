<#
.SYNOPSIS
  Install the PC node (#145, option A): Strata and the CortadOS local gate kept up across reboots and logins with nobody
  signed in, the owner-activity writer in the owner's session, the token file, the firewall rule and the desktop
  toggle. DRY RUN BY DEFAULT: without -Apply it only prints what it would do. Nothing here runs from the router's
  tests; the owner runs it on the PC (docs/runbooks/PC-RTX3060-ENROLLMENT.md section 12).

.DESCRIPTION
  Scheduled tasks (all run as the account that runs this script, an administrator: owner decision 2026-11-24):
    CortadOS-Strata    at startup, S4U (no stored password, runs with nobody signed in), highest privileges, priority
                       Normal (5): Strata documents that the default task priority (7) throttled its expert loads about
                       25 times. Runs the keepalive launcher for -StrataExe with -StrataArgs.
    CortadOS-Gate      at startup + 60 s (Tailscale must have its address first; the gate also refuses 0.0.0.0), S4U,
                       runs the keepalive launcher for the gate on -ListenIp, --no-studio-gate.
    CortadOS-Activity  at logon of this account, interactive, pythonw (no window): writes this session's activity file
                       every 5 s (pc-activity.s<session id>.json: the session's id, user and logon time, its full-screen
                       state and input idle time, which the gate in session 0 cannot read itself). Serving stops while a
                       full-screen app runs; a signed-in session of ANOTHER account has no writer and stops it too.
  Keepalive launchers restart their process 30 s after it exits, because Task Scheduler's restart-on-failure does not
  fire on a non-zero exit code. Logs go to %USERPROFILE%\.config\cortados\pc-node\logs.

  PowerShell 5.1 safe: ASCII only; every file it writes is written as ASCII. The token is never printed.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\install_pc_node.ps1 `
    -Python <py> -ListenIp <pc-ts-ip> -StrataExe <strata.exe> -StrataArgs '<the arguments verified in step 2>'
  (prints the plan; add -Apply to make the changes, -Remove to take them away)
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Python,
  [Parameter(Mandatory = $true)][string]$ListenIp,
  [string]$StrataExe = '',
  [string]$StrataArgs = '',
  [int]$Port = 11435,
  [string]$Repo = '',
  [string]$StrataUrl = 'http://127.0.0.1:8080',
  [switch]$Apply,
  [switch]$Remove
)
$ErrorActionPreference = 'Stop'

if (-not $Repo) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
$CfgDir = Join-Path $env:USERPROFILE '.config\cortados'
$NodeDir = Join-Path $CfgDir 'pc-node'
$LogDir = Join-Path $NodeDir 'logs'
$TokenFile = Join-Path $CfgDir 'local-gate-token'
$BackendCfg = Join-Path $CfgDir 'local-backend.json'
$NodeCfg = Join-Path $NodeDir 'pc-node.json'
$ActivityFile = Join-Path $CfgDir 'pc-activity.json'
$Pythonw = Join-Path (Split-Path $Python -Parent) 'pythonw.exe'
$User = "$env:USERDOMAIN\$env:USERNAME"
$Tasks = @('CortadOS-Strata', 'CortadOS-Gate', 'CortadOS-Activity')
$RuleName = 'CortadOS local gate (tailnet only)'
# Tailscale's address range (CGNAT 100.64.0.0/10): only tailnet peers may reach the gate's port.
$TailnetRange = '100.64.0.0/10'
$Ascii = New-Object System.Text.ASCIIEncoding

function Step([string]$What, [scriptblock]$Do) {
  if ($Apply) { Write-Host "[apply] $What"; & $Do } else { Write-Host "[dry-run] would: $What" }
}

function Write-Ascii([string]$Path, [string]$Text) {
  [System.IO.File]::WriteAllText($Path, $Text, $Ascii)
}

function Test-Ascii([string]$Text) {
  foreach ($c in $Text.ToCharArray()) { if ([int]$c -gt 126 -or [int]$c -lt 32) { return $false } }
  return $true
}

# Paths the launchers, pc-node.json and the task arguments carry. Write-Ascii replaces a non-ASCII character with '?',
# which would silently name another path (the gate would then not find its config or token), so a profile, repo, python
# or script folder with one is refused here, before any change, rather than written lossily (#145 review).
$PathChecks = @(
  @('%USERPROFILE% (the config, token and log folders derive from it)', $env:USERPROFILE),
  @('-Repo', $Repo),
  @('-Python', $Python),
  @("this script's folder (the desktop shortcuts point at it)", $PSScriptRoot)
)

if ($Remove) {
  foreach ($t in $Tasks) {
    Step "unregister scheduled task $t" { Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue }
  }
  Step "remove firewall rule '$RuleName'" { Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue }
  Write-Host 'The token file, configs and logs are left in place (remove them by hand if wanted).'
  exit 0
}

# ---- checks before anything changes
foreach ($pc in $PathChecks) {
  if (-not (Test-Ascii $pc[1])) {
    throw ("The path in " + $pc[0] + " contains non-ASCII characters. This installer writes its launchers and " +
      "pc-node.json as ASCII, so the path would be stored wrongly. Nothing was changed. Install from an account " +
      "and folders whose paths are ASCII only.")
  }
}
if (-not (Test-Path $Python)) { throw "Python not found at the path given with -Python" }
if (-not (Test-Path $Pythonw)) { throw "pythonw.exe not found next to the given python.exe" }
if (-not (Test-Path (Join-Path $Repo 'cortados_router\local_gate.py'))) { throw "-Repo is not a CortadOS-Router checkout" }
$parsed = $null
if (-not [System.Net.IPAddress]::TryParse($ListenIp, [ref]$parsed) -or $ListenIp -eq '0.0.0.0') {
  throw "-ListenIp must be the PC's Tailscale IPv4 address (never 0.0.0.0)"
}
if (-not $StrataExe) {
  Write-Host 'No -StrataExe: the CortadOS-Strata task is skipped (give the command verified in runbook step 2).'
} elseif (-not (Test-Path $StrataExe)) {
  throw '-StrataExe not found'
} elseif (-not (Test-Ascii ($StrataExe + $StrataArgs))) {
  throw '-StrataExe and -StrataArgs must be ASCII (the launcher file is written as ASCII)'
}

# ---- folders, token, configs
Step "create $NodeDir and $LogDir" {
  New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}
if (Test-Path $TokenFile) {
  Write-Host "token file exists: kept (never printed)"
} else {
  Step "create a new 64-hex gate token in $TokenFile (unique to this node, never printed)" {
    $bytes = New-Object byte[] 32
    $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
    $rng.GetBytes($bytes)
    $hex = -join ($bytes | ForEach-Object { $_.ToString('x2') })
    Write-Ascii $TokenFile $hex
    $hex = $null
  }
}
# The gate refuses a token file any other account can read (cortados_router/gate_platform.py token_file_problem):
# inheritance removed, read for this account, full for SYSTEM and Administrators.
Step "restrict the token file's ACL (icacls: this account read; SYSTEM, Administrators full)" {
  & icacls.exe $TokenFile /inheritance:r /grant:r "${User}:(R)" '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "icacls failed ($LASTEXITCODE)" }
}
if (Test-Path $BackendCfg) {
  Write-Host "backend config exists: kept ($BackendCfg)"
} else {
  Step "copy tables\local-backend.strata.example.json to $BackendCfg (edit the model there, not in code)" {
    Copy-Item (Join-Path $Repo 'tables\local-backend.strata.example.json') $BackendCfg
  }
}
$nodeJson = '{"gate_url": "http://' + $ListenIp + ':' + $Port + '", "token_file": "' + ($TokenFile -replace '\\', '/') +
  '", "strata_url": "' + $StrataUrl + '"}'
Step "write $NodeCfg (the toggle's settings: gate address, token path, Strata's loopback URL; no secret)" {
  Write-Ascii $NodeCfg $nodeJson
}

# ---- keepalive launchers
# Each launcher restarts its process 30 s after it exits (Task Scheduler's restart-on-failure does not fire on a
# non-zero exit code). Start-Process with redirected output, so no cmd.exe quoting is involved; the previous run's
# output is kept as <name>.out.log.prev / <name>.err.log.prev.
$LauncherTemplate = @'
# Written by install_pc_node.ps1: keeps one CortadOS PC-node process up (restart 30 s after it exits).
$ErrorActionPreference = 'Continue'
Set-Location __REPO__
$exe = __EXE__
$argLine = __ARGS__
$out = __OUT__
$err = __ERR__
$log = __LOG__
while ($true) {
  foreach ($f in @($out, $err)) { if (Test-Path $f) { Move-Item -Force $f ($f + '.prev') } }
  Add-Content -Encoding ASCII -Path $log -Value ((Get-Date -Format s) + ' start')
  try {
    $p = Start-Process -FilePath $exe -ArgumentList $argLine -RedirectStandardOutput $out -RedirectStandardError $err `
      -NoNewWindow -Wait -PassThru
    $rc = $p.ExitCode
  } catch {
    $rc = 'start failed: ' + $_.Exception.Message
  }
  Add-Content -Encoding ASCII -Path $log -Value ((Get-Date -Format s) + ' exit ' + $rc)
  Start-Sleep -Seconds 30
}
'@

function Q([string]$Text) { return "'" + $Text.Replace("'", "''") + "'" }

function Launcher([string]$Name, [string]$Exe, [string]$ArgLine) {
  $text = $LauncherTemplate.Replace('__REPO__', (Q $Repo)).Replace('__EXE__', (Q $Exe)).Replace('__ARGS__', (Q $ArgLine))
  $text = $text.Replace('__OUT__', (Q (Join-Path $LogDir "$Name.out.log"))).Replace('__ERR__', (Q (Join-Path $LogDir "$Name.err.log")))
  $text = $text.Replace('__LOG__', (Q (Join-Path $LogDir "$Name.launcher.log")))
  $path = Join-Path $NodeDir "run-$Name.ps1"
  Step "write launcher $path ($Exe $ArgLine)" { Write-Ascii $path ($text -replace "`r?`n", "`r`n") }
  return $path
}
$gateArgs = "-B -m cortados_router.local_gate --listen $ListenIp --port $Port --backend-config `"$BackendCfg`" " +
  "--token-file `"$TokenFile`" --no-studio-gate"
$gateLauncher = Launcher 'gate' $Python $gateArgs
if ($StrataExe) { $strataLauncher = Launcher 'strata' $StrataExe $StrataArgs }

# ---- scheduled tasks
function Register-Startup([string]$Name, [string]$Launcher, [string]$Delay) {
  $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Launcher`"" -WorkingDirectory $Repo
  $trigger = New-ScheduledTaskTrigger -AtStartup
  if ($Delay) { $trigger.Delay = $Delay }
  $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Highest
  # Priority 5 = Normal (module docstring); no time limit; start even on battery (a desktop reports AC anyway).
  $settings = New-ScheduledTaskSettingsSet -Priority 5 -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
  Step "register scheduled task $Name (at startup$(if ($Delay) { ' + ' + $Delay }), S4U, highest, priority Normal)" {
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
      -Force | Out-Null
  }
}
if ($StrataExe) { Register-Startup 'CortadOS-Strata' $strataLauncher '' }
Register-Startup 'CortadOS-Gate' $gateLauncher 'PT60S'

$actAction = New-ScheduledTaskAction -Execute $Pythonw `
  -Argument "-B -m cortados_router.node_policy activity --out `"$ActivityFile`"" -WorkingDirectory $Repo
$actTrigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$actPrincipal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$actSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Step "register scheduled task CortadOS-Activity (at logon of $User, interactive, pythonw)" {
  Register-ScheduledTask -TaskName 'CortadOS-Activity' -Action $actAction -Trigger $actTrigger -Principal $actPrincipal `
    -Settings $actSettings -Force | Out-Null
}

# ---- firewall: the gate's port from the tailnet only
Step "firewall rule '$RuleName': inbound TCP $Port on the Tailscale address, from $TailnetRange only" {
  Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
  New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Protocol TCP -LocalPort $Port -LocalAddress $ListenIp `
    -RemoteAddress $TailnetRange -Action Allow -Profile Any | Out-Null
}

# ---- desktop toggle
$toggle = Join-Path $PSScriptRoot 'cortados-serving.ps1'
$desktop = [Environment]::GetFolderPath('Desktop')
foreach ($s in @(@('CortadOS serving ON', 'on'), @('CortadOS serving OFF (game)', 'off -FreeGpu'))) {
  $lnk = Join-Path $desktop ($s[0] + '.lnk')
  Step "desktop shortcut '$($s[0])' -> cortados-serving.ps1 $($s[1])" {
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($lnk)
    $sc.TargetPath = 'powershell.exe'
    $sc.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$toggle`" $($s[1]) -Pause"
    $sc.WorkingDirectory = $Repo
    $sc.Save()
  }
}

Write-Host ''
Write-Host 'Not done by this script (owner, by hand; runbook step 0): powercfg /change standby-timeout-ac 0,'
Write-Host 'powercfg /change hibernate-timeout-ac 0, Tailscale "run unattended", Windows Update active hours.'
if (-not $Apply) { Write-Host 'Dry run: nothing was changed. Run again with -Apply to make these changes.' }
