# Executes the real generated script with Windows-only operations replaced.
# Files and the native keygen test double are confined to pytest's directory.
param([string]$BootstrapPath, [int]$Attempts = 1)
$ErrorActionPreference = 'Stop'
New-PSDrive -Name C -PSProvider FileSystem -Root $env:ASTINUS_TEST_ROOT | Out-Null
$env:COMPUTERNAME = 'AL-123456789012'
$global:serviceStatus = 'Running'
$global:activeFingerprint = 'inherited-test-key'
$global:events = [Collections.Generic.List[string]]::new()
function Get-CimInstance { [pscustomobject]@{LastBootUpTime = [datetime]'2026-09-10T01:00:00Z'} }
function New-Object {
    param([string]$TypeName, [object[]]$ArgumentList)
    $object = [pscustomobject]@{}
    if ($TypeName -eq 'System.Security.AccessControl.FileSecurity') {
        $object | Add-Member ScriptMethod SetAccessRuleProtection { param($a, $b) }
        $object | Add-Member ScriptMethod SetAccessRule { param($rule) }
    }
    return $object
}
function Set-Acl { param($LiteralPath, $AclObject) }
function Set-Service { param($Name, $StartupType) }
function Get-ChildItem {
    param($LiteralPath, $Filter, [switch]$File, [switch]$Force)
    # Model Windows Hidden/System selection, unavailable on the Linux test FS.
    # Keep actual enumeration/deletion confined to the existing test directory.
    if ($env:ASTINUS_TEST_HIDDEN_HOST_KEYS -eq '1' -and -not $Force) { return }
    Microsoft.PowerShell.Management\Get-ChildItem -LiteralPath $LiteralPath -Filter $Filter -File:$File -Force:$Force
}
function Get-Service { param($Name) [pscustomobject]@{Status = $global:serviceStatus} }
function Stop-Service {
    param($Name, $ErrorAction)
    $global:events.Add('stop')
    if ($env:ASTINUS_TEST_FAILURE -eq 'stop') { throw 'test stop failed' }
    if ($env:ASTINUS_TEST_FAILURE -ne 'stop-stuck') { $global:serviceStatus = 'Stopped' }
}
function Start-Service {
    param($Name, $ErrorAction)
    $global:events.Add('start')
    if ($env:ASTINUS_TEST_FAILURE -eq 'start') { throw 'test start failed' }
    if ($env:ASTINUS_TEST_FAILURE -eq 'start-stuck') { return }
    # Like Start-Service, starting an already-running service is a no-op.
    if ($global:serviceStatus -ne 'Running') {
        $global:activeFingerprint = (Get-Content $env:ASTINUS_TEST_STATE -Raw | ConvertFrom-Json).fingerprint
        $global:serviceStatus = 'Running'
    }
}
function Rename-Computer { throw 'unexpected rename in host-key test' }
function shutdown.exe { throw 'unexpected reboot in host-key test' }
$results = @()
for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
    try {
        $output = & $BootstrapPath
        $results += [pscustomobject]@{
            success = $true
            payload = ($output | ConvertFrom-Json)
            active_fingerprint = $global:activeFingerprint
            service_status = $global:serviceStatus
        }
    } catch {
        $results += [pscustomobject]@{
            success = $false
            service_status = $global:serviceStatus
        }
    }
    # Retry after a transient failure, retaining files and service state.
    $env:ASTINUS_TEST_FAILURE = 'none'
}
[pscustomobject]@{results = $results; events = @($global:events)} | ConvertTo-Json -Depth 5 -Compress
