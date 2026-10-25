import json
import os
import subprocess

import pytest

from astinuslab.images.windows_bundle import parse_windows_bundle
from tests.python._support.windows_images import windows_bundle_declaration


def success():
    return dict(
        schema_version=1,
        scope="prepared_windows_guest",
        success=True,
        phase="complete",
        boot_id="123",
        receipt_nonce="db3396d32fa84103b4f7b29c27179dbb",
        computer_name="WIN-EXAMPLE",
        machine_sid_prefix="S-1-5-21-1-2-3",
        answer_security="37892|S-1-5-18|S-1-5-32-544|0,0,2032127,S-1-5-18;0,0,2032127,S-1-5-32-544;",
        answer_descriptor_hash="a" * 64,
        mutation_attempted=False,
        answer_handles_closed=8,
        receipt_handles_closed=10,
    )


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"boot_id": "124"},
        {"success": False},
        {"receipt_nonce": "0" * 32},
        {"answer_handles_closed": 7},
        {"receipt_handles_closed": True},
        {"answer_security": "unknown"},
        {"password": "synthetic"},
        {"machine_sid_prefix": "S-1-5-21-4294967296-2-3"},
    ],
)
def test_prepared_gate_validates_evidence_before_it_can_enable_bootstrap(monkeypatch, change):
    from astinuslab.virtualization import windows_prepared as module
    from astinuslab.virtualization.qga_exec import GuestExecResult

    observation = success()
    observation.update(change)
    calls = []

    def execute(*a, **k):
        calls.append(a)
        assert k["discard_stderr"] is True
        return GuestExecResult(0, json.dumps(observation), "")

    monkeypatch.setattr(module, "run_guest_exec", execute)
    if change:
        with pytest.raises(RuntimeError, match="prepared guest"):
            module.prepare_windows_guest(
                "qemu:///system", "vm", parse_windows_bundle(windows_bundle_declaration()), "123"
            )
    else:
        proof = module.prepare_windows_guest(
            "qemu:///system", "vm", parse_windows_bundle(windows_bundle_declaration()), "123"
        )
        assert proof.computer_name == "WIN-EXAMPLE"
        assert proof.machine_sid_prefix == "S-1-5-21-1-2-3"
    assert len(calls) == 1


def test_prepared_payload_compiles_and_transport_reconstructs_exact_script(tmp_path):
    from astinuslab.virtualization import windows_prepared as module

    pwsh = os.environ.get("ASTINUSLAB_TEST_PWSH")
    if not pwsh:
        pytest.skip("ASTINUSLAB_TEST_PWSH required")
    bundle = parse_windows_bundle(windows_bundle_declaration())
    script = module.build_prepared_guest_script(bundle, "123")
    # Compile only; native entry points are not callable on Linux.
    source = tmp_path / "native.cs"
    source.write_text(module.native_source(bundle))
    payload = tmp_path / "payload.ps1"
    payload.write_text(script)
    harness = tmp_path / "check.ps1"
    harness.write_text(
        "param($Source,$Payload)\n$ErrorActionPreference='Stop';Add-Type -Path $Source;"
        "$t=$null;$e=$null;[Management.Automation.Language.Parser]::ParseFile($Payload,[ref]$t,[ref]$e)|Out-Null;"
        "if($e.Count){throw 'parse'};'COMPILED'"
    )
    result = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(harness), str(source), str(payload)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "COMPILED"
    wrapper = module.script_command("[Console]::Write('transport-ok')")
    assert len(module.script_command(script)) + 256 < 32767
    result = subprocess.run([pwsh, "-NoProfile", "-Command", wrapper], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "transport-ok"
    declarations_only = script.rsplit("\nInvoke-AstinusPreparedGate", 1)[0] + "\n[Console]::Write('full-transport-ok')"
    result = subprocess.run(
        [pwsh, "-NoProfile", "-Command", module.script_command(declarations_only)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "full-transport-ok"


@pytest.mark.parametrize(
    "failure", ["none", "context", "initialization", "answer", "identity_changed", "context_after"]
)
def test_executable_gate_orders_receipt_restriction_and_continuity(tmp_path, failure):
    from astinuslab.virtualization import windows_prepared as module

    pwsh = os.environ.get("ASTINUSLAB_TEST_PWSH")
    if not pwsh:
        pytest.skip("ASTINUSLAB_TEST_PWSH required")
    harness = tmp_path / "gate.ps1"
    harness.write_text(r"""param($Source,$Failure)
$ErrorActionPreference='Stop'
Add-Type 'namespace UnattendedReceipt {public class Guard {public const string Nonce="db3396d32fa84103b4f7b29c27179dbb";}}'
. $Source
$script:Events=[Collections.Generic.List[string]]::new();$script:Reads=0;$script:Contexts=0
function Assert-AstinusAnswerContext {
 param($ExpectedBoot)
 if($ExpectedBoot -cne '123'){throw 'boot'}
 $script:Contexts++;$script:Events.Add('context')
 if(($Failure -eq 'context' -and $script:Contexts -eq 1) -or ($Failure -eq 'context_after' -and $script:Contexts -eq 2)){throw 'context'}
}
function Get-AstinusInitialization {
 $script:Reads++;$script:Events.Add('receipt')
 if($Failure -eq 'initialization'){throw 'receipt'}
 return @{computer_name='WIN-TEST';machine_sid_prefix='S-1-5-21-1-2-3';password_last_set_ticks=$(if($Failure -eq 'identity_changed' -and $script:Reads -eq 2){'124'}else{'123'})}
}
function Invoke-AstinusAnswerRestriction {
 param($ExpectedBoot,$ParentAcls)
 if($ExpectedBoot -cne '123' -or $ParentAcls.Count -ne 3){throw 'binding'}
 $script:Events.Add('answer')
 return @{success=($Failure -ne 'answer');phase='complete';native=@{Leaf=@{Security='protected';DescriptorHash='hash'};MutationAttempted=$true;Closed=8}}
}
$result=Invoke-AstinusPreparedGate -ExpectedBoot '123' -ParentAcls @('p0','p1','p2')
@{result=$result;events=@($script:Events)}|ConvertTo-Json -Depth 5 -Compress
""")
    completed = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(harness), str(module._ASSETS.joinpath("PreparedGuest.ps1")), failure],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    row = json.loads(completed.stdout)
    assert row["result"]["success"] is (failure == "none")
    events = {
        "none": ["context", "receipt", "answer", "receipt", "context"],
        "context": ["context"],
        "initialization": ["context", "receipt"],
        "answer": ["context", "receipt", "answer"],
        "identity_changed": ["context", "receipt", "answer", "receipt"],
        "context_after": ["context", "receipt", "answer", "receipt", "context"],
    }
    assert row["events"] == events[failure]
