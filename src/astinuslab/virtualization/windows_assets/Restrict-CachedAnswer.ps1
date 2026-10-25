# Definitions only. A reviewed host operation supplies pinned native code,
# Get-SetupObservation, the measured parent ACLs and this boot's identity.
function Assert-AstinusAnswerContext {
    param([string]$ExpectedBoot)
    $ErrorActionPreference = 'Stop'
    if ($ExpectedBoot -cnotmatch '^[1-9][0-9]{0,18}$' -or
        [decimal]$ExpectedBoot -gt [DateTime]::MaxValue.Ticks -or
        -not [Environment]::Is64BitProcess) { throw 'arguments' }
    $s = Get-SetupObservation
    if ($s.image_state -cne 'IMAGE_STATE_COMPLETE' -or $s.system_setup_in_progress -ne 0 -or
        $s.oobe_in_progress -ne 0 -or $s.execution_is_system -ne $true -or
        $s.boot_id -cne $ExpectedBoot) { throw 'setup' }
    $os = @(Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop)
    $computer = @(Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop)
    if ($os.Count -ne 1 -or $os[0].BuildNumber -cne '20348' -or $os[0].ProductType -ne 3 -or
        $computer.Count -ne 1 -or $computer[0].PartOfDomain -ne $false -or
        $computer[0].DomainRole -ne 2) { throw 'server' }
    $ssh = @(Get-CimInstance -ClassName Win32_Service -Filter "Name='sshd'" -ErrorAction Stop)
    $qga = @(Get-CimInstance -ClassName Win32_Service -Filter "Name='QEMU-GA'" -ErrorAction Stop)
    if ($ssh.Count -ne 1 -or $ssh[0].State -cne 'Stopped' -or $ssh[0].StartMode -cne 'Disabled' -or
        $qga.Count -ne 1 -or $qga[0].State -cne 'Running' -or $qga[0].StartMode -cne 'Auto') { throw 'services' }
    $others = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe' OR Name='powershell_ise.exe'" -ErrorAction Stop |
        Where-Object { [int]$_.ProcessId -ne $PID })
    if ($others.Count -ne 0) { throw 'other_probe' }
}

function Invoke-AstinusAnswerRestriction {
    param([string]$ExpectedBoot, [string[]]$ParentAcls)
    $ErrorActionPreference = 'Stop'
    $phase = 'context'; $native = $null; $success = $false
    try {
        if ($ParentAcls.Count -ne 3) { throw 'parents' }
        Assert-AstinusAnswerContext -ExpectedBoot $ExpectedBoot
        $ready = [Func[bool]] { Assert-AstinusAnswerContext -ExpectedBoot $ExpectedBoot; return $true }
        $phase = 'native'
        $native = [AstinusAnswer.Restriction]::ApplyNative($ParentAcls, $ready)
        if (-not $native.Success -or -not $native.HeldVerified -or -not $native.ReopenedVerified -or
            $native.CloseFailures -ne 0 -or $native.Opened -ne $native.Closed) { throw 'native' }
        $phase = 'context_after'
        Assert-AstinusAnswerContext -ExpectedBoot $ExpectedBoot
        $phase = 'complete'; $success = $true
    } catch {}
    return [ordered]@{ schema_version = 1; scope = 'post_setup_answer_restriction'; success = $success;
        phase = $phase; boot_id = $ExpectedBoot; native = $native }
}
