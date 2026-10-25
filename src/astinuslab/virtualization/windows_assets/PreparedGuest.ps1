# Definitions only; the host supplies the validated public nonce and current boot.
function Get-AstinusInitialization {
    $native = [UnattendedReceipt.Guard]::CollectNative()
    if (-not $native.Success -or $native.Opened -ne 5 -or $native.Closed -ne 5 -or
        $native.CloseFailures -ne 0 -or -not $native.Enabled) { throw 'receipt' }
    $account = @(Get-LocalUser -ErrorAction Stop | Where-Object { $_.SID.Value -match '-500$' })
    if ($account.Count -ne 1 -or $account[0].PrincipalSource -ne 'Local' -or
        -not $account[0].Enabled -or $null -eq $account[0].PasswordLastSet) { throw 'account' }
    $ticks = $account[0].PasswordLastSet.ToUniversalTime().Ticks.ToString([Globalization.CultureInfo]::InvariantCulture)
    $sid = $account[0].SID.AccountDomainSid.Value
    if ($sid -cnotmatch '^S-1-5-21-(0|[1-9][0-9]{0,9})-(0|[1-9][0-9]{0,9})-(0|[1-9][0-9]{0,9})$' -or
        $account[0].SID.Value -cne ($sid+'-500') -or $native.PasswordLastSetTicks -cne $ticks) { throw 'account_receipt' }
    return [ordered]@{computer_name=[Environment]::MachineName; machine_sid_prefix=$sid; password_last_set_ticks=$ticks}
}

function Invoke-AstinusPreparedGate {
    param([string]$ExpectedBoot, [string[]]$ParentAcls)
    $ErrorActionPreference = 'Stop'; $phase = 'context'
    try {
        Assert-AstinusAnswerContext -ExpectedBoot $ExpectedBoot
        $phase = 'initialization'; $before = Get-AstinusInitialization
        $phase = 'answer'; $answer = Invoke-AstinusAnswerRestriction -ExpectedBoot $ExpectedBoot -ParentAcls $ParentAcls
        if (-not $answer.success -or $answer.phase -cne 'complete') { throw 'answer' }
        $phase = 'initialization_after'; $after = Get-AstinusInitialization
        foreach ($key in @('computer_name','machine_sid_prefix','password_last_set_ticks')) {
            if ($after[$key] -cne $before[$key]) { throw 'identity_changed' }
        }
        $phase = 'context_after'; Assert-AstinusAnswerContext -ExpectedBoot $ExpectedBoot
        return [ordered]@{
            schema_version=1; scope='prepared_windows_guest'; success=$true; phase='complete'; boot_id=$ExpectedBoot;
            receipt_nonce=[UnattendedReceipt.Guard]::Nonce; computer_name=$after.computer_name;
            machine_sid_prefix=$after.machine_sid_prefix; answer_security=$answer.native.Leaf.Security;
            answer_descriptor_hash=$answer.native.Leaf.DescriptorHash; mutation_attempted=[bool]$answer.native.MutationAttempted;
            answer_handles_closed=$answer.native.Closed; receipt_handles_closed=10
        }
    } catch {
        # Never emit exception text, receipt contents or account credentials.
        return [ordered]@{schema_version=1; scope='prepared_windows_guest'; success=$false; phase=$phase}
    }
}
