"""Execute the guest context gate; Windows CIM/native calls are injected."""

import json
import os
import subprocess
from pathlib import Path

import pytest


def test_restriction_requires_completed_setup_and_disabled_ssh(tmp_path):
    pwsh = os.environ.get("ASTINUSLAB_TEST_PWSH")
    if not pwsh:
        pytest.skip("set ASTINUSLAB_TEST_PWSH for executable PowerShell checks")
    source = Path(__file__).parents[3] / "tools/dev/windows_candidate_trial/Restrict-CachedAnswer.ps1"
    assert source.exists(), "post-Setup context gate missing"
    script = tmp_path / "check.ps1"
    script.write_text(r"""param($Source)
$ErrorActionPreference='Stop'
. $Source
Add-Type @'
using System;
namespace AstinusAnswer {
 public static class Restriction {
  public static int Calls;
  public static object ApplyNative(string[] parents,Func<bool> ready) {
   Calls++;if(parents.Length!=3||!ready())throw new Exception("context");
   return new {Success=true,MutationAttempted=true,HeldVerified=true,ReopenedVerified=true,
    CloseFailures=0,Opened=8,Closed=8,Phase="complete",Failure="none"};
  }
 }
}
'@
function Get-SetupObservation {
 return @{image_state=$(if($script:case -ceq 'setup'){'IMAGE_STATE_UNDEPLOYABLE'}else{'IMAGE_STATE_COMPLETE'});
  system_setup_in_progress=0;oobe_in_progress=0;execution_is_system=($script:case -cne 'identity');
  boot_id=$(if($script:case -ceq 'boot'){'639000000000000001'}else{'639000000000000000'});uptime_seconds=100}
}
function Get-CimInstance {
 param($ClassName,$Filter,$ErrorAction)
 switch($ClassName) {
  'Win32_OperatingSystem' {return @{BuildNumber=$(if($script:case -ceq 'version'){'22621'}else{'20348'});ProductType=3}}
  'Win32_ComputerSystem' {return @{PartOfDomain=($script:case -ceq 'domain');DomainRole=2}}
  'Win32_Service' {
   if($Filter -ceq "Name='sshd'"){return @{State=$(if($script:case -ceq 'ssh'){'Running'}else{'Stopped'});StartMode='Disabled'}}
   if($Filter -ceq "Name='QEMU-GA'"){return @{State='Running';StartMode='Auto'}}
   throw 'unknown_service'
  }
  'Win32_Process' {if($script:case -ceq 'other'){return @{ProcessId=($PID+1)}};return @()}
  default {throw 'unknown_class'}
 }
}
$rows=@()
foreach($script:case in @('ok','setup','identity','boot','version','domain','ssh','other')) {
 [AstinusAnswer.Restriction]::Calls=0
 $r=Invoke-AstinusAnswerRestriction -ExpectedBoot '639000000000000000' -ParentAcls @('a','b','c')
 $rows+=@{scenario=$script:case;result=$r;calls=[AstinusAnswer.Restriction]::Calls}
}
$rows|ConvertTo-Json -Depth 6 -Compress
""")
    result = subprocess.run(
        [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script), str(source)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert len(rows) == 8
    for row in rows:
        assert row["result"]["success"] is (row["scenario"] == "ok"), row
        assert row["calls"] == int(row["scenario"] == "ok"), row
        if row["scenario"] != "ok":
            assert row["result"]["phase"] == "context", row
