<#
.SYNOPSIS
  The PC node's serving toggle (#145): turn local-model serving on or off, or show it. Calls the local gate's own
  switch endpoint (/cortados/local-switch), the same switch the provider's local_switch tool flips with node "pc".

.DESCRIPTION
  on        serving allowed again (it still waits until the owner is away: node_policy windows-idle)
  off       no new turns are admitted; a turn already running finishes
  status    the switch, whether the gate would serve now and why, and the owner state
  -FreeGpu  with off: also ask Strata on loopback to unload its model now, so a game gets the VRAM at once (Strata's
            POST /v1/unload; check its behaviour once on the PC, runbook step 3)
  -Pause    wait for a key before closing (the desktop shortcuts use it)

  Reads %USERPROFILE%\.config\cortados\pc-node\pc-node.json (written by install_pc_node.ps1) for the gate's address and
  the token file. The token is read into memory and sent as a header; it is never printed or put on a command line.
  PowerShell 5.1 safe, ASCII only.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true, Position = 0)][ValidateSet('on', 'off', 'status')][string]$Action,
  [switch]$FreeGpu,
  [switch]$Pause,
  [string]$Config = (Join-Path $env:USERPROFILE '.config\cortados\pc-node\pc-node.json')
)
$ErrorActionPreference = 'Stop'
$code = 0
try {
  $cfg = Get-Content -Raw -Encoding ASCII $Config | ConvertFrom-Json
  $token = (Get-Content -Raw -Encoding ASCII $cfg.token_file).Trim()
  $headers = @{ Authorization = "Bearer $token" }
  # No proxy: the gate is on the tailnet and Strata on loopback.
  [System.Net.WebRequest]::DefaultWebProxy = New-Object System.Net.WebProxy
  $url = $cfg.gate_url + '/cortados/local-switch'
  if ($Action -eq 'status') {
    $r = Invoke-RestMethod -Method Get -Uri $url -Headers $headers -TimeoutSec 10
  } else {
    $body = if ($Action -eq 'on') { '{"on": true}' } else { '{"on": false}' }
    $r = Invoke-RestMethod -Method Post -Uri $url -Headers $headers -Body $body -ContentType 'application/json' -TimeoutSec 10
  }
  $token = $null
  $headers = $null
  Write-Host ("switch: {0}  serving now: {1}  ({2})" -f $r.switch, $r.allowed, $r.reason)
  Write-Host ("owner: {0}  power: {1}  backend: {2}  loaded: {3}" -f $r.activity, $r.power, $r.backend, ($r.models_loaded -join ', '))
  if ($Action -eq 'off' -and $FreeGpu) {
    try {
      Invoke-RestMethod -Method Post -Uri ($cfg.strata_url + '/v1/unload') -Body '{}' -ContentType 'application/json' -TimeoutSec 30 | Out-Null
      Write-Host 'Strata asked to unload its model (GPU memory freed once it has).'
    } catch {
      Write-Host ('Strata unload failed: ' + $_.Exception.Message)
      $code = 1
    }
  }
} catch {
  Write-Host ('serving toggle failed: ' + $_.Exception.Message)
  $code = 1
}
if ($Pause) { Write-Host 'Press any key to close.'; [void][System.Console]::ReadKey($true) }
exit $code
