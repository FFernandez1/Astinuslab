function Get-SetupObservation {
    $ErrorActionPreference='Stop'
    $base=$null;$state=$null;$setup=$null
    try {
        $base=[Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine,[Microsoft.Win32.RegistryView]::Registry64)
        $state=$base.OpenSubKey('SOFTWARE\Microsoft\Windows\CurrentVersion\Setup\State',$false)
        $setup=$base.OpenSubKey('SYSTEM\Setup',$false)
        if($null -eq $state -or $null -eq $setup){throw 'setup'}
        if($state.GetValueKind('ImageState') -ne [Microsoft.Win32.RegistryValueKind]::String -or
           $setup.GetValueKind('SystemSetupInProgress') -ne [Microsoft.Win32.RegistryValueKind]::DWord -or
           $setup.GetValueKind('OOBEInProgress') -ne [Microsoft.Win32.RegistryValueKind]::DWord){throw 'kind'}
        $image=[string]$state.GetValue('ImageState')
        if($image -cnotin @('IMAGE_STATE_COMPLETE','IMAGE_STATE_UNDEPLOYABLE','IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE','IMAGE_STATE_SPECIALIZE_RESEAL_TO_OOBE')){throw 'state'}
        $system=$setup.GetValue('SystemSetupInProgress');$oobe=$setup.GetValue('OOBEInProgress')
        if($system -notin @(0,1) -or $oobe -notin @(0,1)){throw 'flags'}
        $boot=(Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop).LastBootUpTime.ToUniversalTime()
        return [pscustomobject][ordered]@{image_state=$image;system_setup_in_progress=[int]$system;
            oobe_in_progress=[int]$oobe;execution_is_system=([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ceq 'S-1-5-18');
            boot_id=$boot.Ticks.ToString([Globalization.CultureInfo]::InvariantCulture);
            uptime_seconds=[long][Math]::Floor(([DateTime]::UtcNow-$boot).TotalSeconds)}
    } finally {
        if($null -ne $setup){$setup.Dispose()};if($null -ne $state){$state.Dispose()};if($null -ne $base){$base.Dispose()}
    }
}
