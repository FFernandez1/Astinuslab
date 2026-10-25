from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from astinuslab.virtualization import windows_runtime

PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOnlyAstinusLabKey test"
FINGERPRINT = "SHA256:" + "A" * 43


@pytest.fixture(autouse=True)
def responsive_agent(monkeypatch):
    monkeypatch.setattr(windows_runtime, "probe_windows_agent", lambda *a, **k: True)


def _execute_bootstrap(tmp_path, *, failure="", attempts=1, hidden_host_keys=False, expected_boot_id=None):
    pwsh = os.environ.get("ASTINUSLAB_TEST_PWSH") or shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell required: set ASTINUSLAB_TEST_PWSH to a portable pwsh executable")
    ssh_root = tmp_path / "ProgramData" / "ssh"
    ssh_root.mkdir(parents=True)
    initial_fingerprint = FINGERPRINT if hidden_host_keys else "inherited-test-key"
    (ssh_root / "ssh_host_ed25519_key.pub").write_text(initial_fingerprint)
    binary_dir = tmp_path / "Windows" / "System32" / "OpenSSH"
    binary_dir.mkdir(parents=True)
    keygen = binary_dir / "ssh-keygen.exe"
    # This native-process double models exit status independently of stdout.
    # It never generates or reads a real private key or touches host SSH files.
    keygen.write_text(
        f"#!{sys.executable}\n"
        "import base64, hashlib, json, os, pathlib, sys\n"
        "root = pathlib.Path(os.environ['ASTINUS_TEST_ROOT']) / 'ProgramData' / 'ssh'\n"
        "state_file = pathlib.Path(os.environ['ASTINUS_TEST_STATE'])\n"
        "state = json.loads(state_file.read_text()) if state_file.exists() else {'generation': 0, 'fingerprint': os.environ['ASTINUS_TEST_INITIAL_FINGERPRINT']}\n"
        "failure = os.environ['ASTINUS_TEST_FAILURE']\n"
        "if sys.argv[1:] == ['-A']:\n"
        "    if (root / 'ssh_host_ed25519_key.pub').exists():\n"
        "        state_file.write_text(json.dumps(state))\n"
        "        sys.exit(0)\n"
        "    state['generation'] += 1\n"
        "    state['fingerprint'] = 'SHA256:' + base64.b64encode(hashlib.sha256(str(state['generation']).encode()).digest()).decode().rstrip('=')\n"
        "    (root / 'ssh_host_ed25519_key.pub').write_text(state['fingerprint'])\n"
        "    state_file.write_text(json.dumps(state))\n"
        "    sys.exit(1 if failure == 'keygen' else 0)\n"
        "if sys.argv[1] == '-lf':\n"
        "    print('malformed' if failure == 'fingerprint-invalid' else '256 ' + state['fingerprint'] + ' test (ED25519)')\n"
        "    sys.exit(1 if failure == 'fingerprint' else 0)\n"
        "sys.exit(99)\n"
    )
    keygen.chmod(0o700)
    script_path = tmp_path / "bootstrap.ps1"
    script_path.write_text(
        windows_runtime.build_windows_bootstrap_script(
            base64.b64encode(PUBLIC_KEY.encode()).decode(), "AL-123456789012", expected_boot_id=expected_boot_id
        )
    )
    harness = Path(__file__).parents[1] / "_support" / "windows_bootstrap_harness.ps1"
    completed = subprocess.run(
        [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(harness), str(script_path), str(attempts)],
        cwd=tmp_path,
        env={
            **os.environ,
            "ASTINUS_TEST_ROOT": str(tmp_path),
            "ASTINUS_TEST_STATE": str(tmp_path / "keygen-state.json"),
            "ASTINUS_TEST_FAILURE": failure,
            "ASTINUS_TEST_HIDDEN_HOST_KEYS": "1" if hidden_host_keys else "0",
            "ASTINUS_TEST_INITIAL_FINGERPRINT": initial_fingerprint,
            "WINDIR": str(tmp_path / "Windows"),
            "PATH": str(binary_dir) + os.pathsep + os.environ.get("PATH", ""),
        },
        text=True,
        capture_output=True,
        timeout=20,
        check=True,
    )
    return json.loads(completed.stdout)


def test_running_sshd_activates_regenerated_key_on_bootstrap_and_retry(tmp_path):
    report = _execute_bootstrap(tmp_path, attempts=2)
    assert all(result["success"] for result in report["results"])
    for result in report["results"]:
        assert result["payload"]["ssh_host_key_fingerprint"] == result["active_fingerprint"]
        assert result["service_status"] == "Running"
        assert result["payload"]["reboot_required"] is False
    assert report["results"][0]["active_fingerprint"] != report["results"][1]["active_fingerprint"]


def test_boot_changed_since_preparation_prevents_key_installation(tmp_path):
    report = _execute_bootstrap(tmp_path, expected_boot_id="123")
    assert report["results"][0]["success"] is False
    assert not (tmp_path / "ProgramData/ssh/administrators_authorized_keys").exists()
    assert not (tmp_path / "keygen-state.json").exists()


def test_bootstrap_rotates_hidden_system_host_keys_instead_of_pinning_inherited_key(tmp_path):
    report = _execute_bootstrap(tmp_path, hidden_host_keys=True)

    result = report["results"][0]
    assert result["success"] is True
    assert result["active_fingerprint"] != FINGERPRINT
    assert result["payload"]["ssh_host_key_fingerprint"] == result["active_fingerprint"]


@pytest.mark.parametrize(
    "failure", ["stop", "stop-stuck", "keygen", "fingerprint", "fingerprint-invalid", "start", "start-stuck"]
)
def test_bootstrap_does_not_report_success_after_host_key_activation_failure(tmp_path, failure):
    report = _execute_bootstrap(tmp_path, failure=failure)
    assert report["results"][0]["success"] is False
    if failure in {"stop", "stop-stuck"}:
        assert not (tmp_path / "keygen-state.json").exists()
        assert (tmp_path / "ProgramData" / "ssh" / "ssh_host_ed25519_key.pub").read_text() == "inherited-test-key"
    if failure in {"keygen", "fingerprint", "fingerprint-invalid"}:
        assert "start" not in report["events"]


@pytest.mark.parametrize("fingerprint", ["", "SHA256:test", "SHA256:" + "A" * 42, "SHA256:" + "A" * 43 + "\n"])
def test_bootstrap_rejects_invalid_host_key_fingerprint(monkeypatch, fingerprint):
    payload = {
        "computer_name": "AL-123456789012",
        "ssh_host_key_fingerprint": fingerprint,
        "reboot_required": False,
        "boot_id": "638930000000000000",
    }
    monkeypatch.setattr(
        windows_runtime, "run_guest_exec", lambda *a, **k: windows_runtime.GuestExecResult(0, json.dumps(payload), "")
    )
    with pytest.raises(RuntimeError, match="during result"):
        windows_runtime.bootstrap_windows_guest("qemu:///system", "test-vm", PUBLIC_KEY, "AL-123456789012")


@pytest.mark.parametrize("failure", ["keygen", "fingerprint", "start"])
def test_bootstrap_retry_recovers_from_partial_host_key_setup(tmp_path, failure):
    report = _execute_bootstrap(tmp_path, failure=failure, attempts=2)
    failed, recovered = report["results"]
    assert failed["success"] is False
    assert recovered["success"] is True
    assert recovered["active_fingerprint"] == recovered["payload"]["ssh_host_key_fingerprint"]
    assert recovered["service_status"] == "Running"


def test_bootstrap_script_is_key_only_and_applies_windows_ssh_invariants():
    script = windows_runtime.build_windows_bootstrap_script(
        base64.b64encode(PUBLIC_KEY.encode()).decode(), "ASTINUS-WIN-01"
    )

    assert "FromBase64String" in script
    assert "ProgramData\\ssh\\administrators_authorized_keys" in script
    assert "SetAccessRuleProtection($true, $false)" in script
    assert "NT AUTHORITY\\SYSTEM" in script
    assert "BUILTIN\\Administrators" in script
    assert "FullControl" in script
    assert "Set-Service -Name sshd -StartupType Automatic" in script
    assert "Remove-Item" in script
    assert "ConvertTo-Json" in script
    assert "BEGIN OPENSSH PRIVATE KEY" not in script
    assert PUBLIC_KEY not in script


def test_bootstrap_script_rejects_invalid_computer_name():
    with pytest.raises(ValueError, match="computer name"):
        windows_runtime.build_windows_bootstrap_script("a2V5", "name with spaces")


def test_bootstrap_guest_returns_json_result(monkeypatch):
    result_payload = {
        "computer_name": "ASTINUS-WIN-01",
        "ssh_host_key_fingerprint": FINGERPRINT,
        "reboot_required": False,
        "boot_id": "638930000000000000",
    }
    monkeypatch.setattr(
        windows_runtime,
        "run_guest_exec",
        lambda *args, **kwargs: windows_runtime.GuestExecResult(
            exitcode=0, stdout=json.dumps(result_payload), stderr=""
        ),
    )

    result = windows_runtime.bootstrap_windows_guest("qemu:///system", "winref", PUBLIC_KEY, "ASTINUS-WIN-01")

    assert result.computer_name == "ASTINUS-WIN-01"
    assert result.ssh_host_key_fingerprint == FINGERPRINT
    assert result.reboot_required is False
    assert result.boot_id == "638930000000000000"


class FakeClock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _setup_payload(**changes):
    return {
        "image_state": "IMAGE_STATE_COMPLETE",
        "system_setup_in_progress": 0,
        "oobe_in_progress": 0,
        "execution_is_system": True,
        "boot_id": "638930000000000001",
        "uptime_seconds": 100,
        **changes,
    }


def test_setup_waits_for_stable_agent_across_automatic_setup_restart(monkeypatch):
    clock = FakeClock()
    executions = []
    monkeypatch.setattr(windows_runtime, "probe_windows_agent", lambda *a, **k: not 24 <= clock.now < 26)

    def execute(*args, **kwargs):
        executions.append(clock.now)
        return windows_runtime.GuestExecResult(0, json.dumps(_setup_payload(uptime_seconds=100 + int(clock.now))), "")

    monkeypatch.setattr(windows_runtime, "run_guest_exec", execute)
    windows_runtime.wait_for_windows_setup(
        "qemu:///system", "test-vm", cancel_requested=lambda: False, time_module=clock, max_wait_seconds=100
    )
    assert executions == [56, 58]


def test_incomplete_setup_rearms_stability_wait_without_retrying_exec(monkeypatch):
    clock = FakeClock()
    executions = []

    def execute(*args, **kwargs):
        executions.append(clock.now)
        observation = _setup_payload(uptime_seconds=100 + int(clock.now))
        if len(executions) == 1:
            observation.update(image_state="IMAGE_STATE_SPECIALIZE_RESEAL_TO_OOBE", system_setup_in_progress=1)
        return windows_runtime.GuestExecResult(0, json.dumps(observation), "")

    monkeypatch.setattr(windows_runtime, "run_guest_exec", execute)
    windows_runtime.wait_for_windows_setup(
        "qemu:///system", "test-vm", cancel_requested=lambda: False, time_module=clock, max_wait_seconds=100
    )
    assert executions == [30, 62, 64]


@pytest.mark.parametrize("cancel", [False, True])
def test_setup_stability_respects_shared_deadline_and_cancellation(monkeypatch, cancel):
    clock = FakeClock()
    monkeypatch.setattr(windows_runtime, "run_guest_exec", lambda *a, **k: pytest.fail("exec before stability"))
    with pytest.raises(RuntimeError, match="cancel_requested" if cancel else "Setup did not complete"):
        windows_runtime.wait_for_windows_setup(
            "qemu:///system",
            "test-vm",
            cancel_requested=lambda: cancel and clock.now >= 4,
            time_module=clock,
            max_wait_seconds=6,
        )
    assert clock.now == (4 if cancel else 6)


def _wait_setup(monkeypatch, responses, *, cancel=lambda: False, max_wait=10):
    # Payload/transport cases bypass the separately tested stability interval.
    assert hasattr(windows_runtime, "wait_for_windows_setup"), "setup completion gate missing"
    clock = FakeClock()
    answers = iter(responses)
    last = [responses[-1]]

    def execute(*args, **kwargs):
        assert kwargs["discard_stderr"] is True
        assert kwargs["timeout"] <= max_wait - clock.now
        value = next(answers, last[0])
        last[0] = value
        if isinstance(value, Exception):
            raise value
        return windows_runtime.GuestExecResult(0, value if isinstance(value, str) else json.dumps(value), "unused")

    monkeypatch.setattr(windows_runtime, "run_guest_exec", execute)
    windows_runtime.wait_for_windows_setup(
        "qemu:///system",
        "test-vm",
        cancel_requested=cancel,
        time_module=clock,
        max_wait_seconds=max_wait,
        poll_interval_seconds=2,
        ready_stability_seconds=0,
    )
    return clock.now


def test_setup_wait_requires_two_complete_same_boot_samples(monkeypatch):
    assert (
        _wait_setup(
            monkeypatch,
            [
                _setup_payload(image_state="IMAGE_STATE_UNDEPLOYABLE", oobe_in_progress=1),
                _setup_payload(),
                _setup_payload(boot_id="638930000000000002", uptime_seconds=5),
                _setup_payload(boot_id="638930000000000002", uptime_seconds=7),
            ],
        )
        == 6
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"image_state": "IMAGE_STATE_UNDEPLOYABLE"},
        {"system_setup_in_progress": 1},
        {"oobe_in_progress": 1},
        {},
    ],
)
def test_setup_pending_or_nonadvancing_samples_never_pass(monkeypatch, changes):
    with pytest.raises(RuntimeError, match="Setup did not complete"):
        _wait_setup(monkeypatch, [_setup_payload(**changes)], max_wait=4)


@pytest.mark.parametrize(
    "changes",
    [
        {"oobe_in_progress": False},
        {"system_setup_in_progress": "0"},
        {"execution_is_system": False},
        {"image_state": "IMAGE_STATE_OTHER"},
        {"image_state": "IMAGE_STATE_GENERALIZE_RESEAL_TO_AUDIT"},
        {"boot_id": "0"},
        {"uptime_seconds": True},
        {"uptime_seconds": -1},
        {"unexpected": "SYNTHETIC_PRIVATE"},
    ],
)
def test_setup_invalid_observation_is_fixed_error_without_payload(monkeypatch, changes):
    with pytest.raises(RuntimeError, match="Setup observation invalid") as error:
        _wait_setup(monkeypatch, [_setup_payload(**changes)])
    assert "SYNTHETIC_PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "raw",
    [
        '{"image_state":"IMAGE_STATE_COMPLETE","image_state":"IMAGE_STATE_COMPLETE"}',
        '{"value":NaN}',
        "SYNTHETIC_PRIVATE",
        "x" * 4097,
    ],
)
def test_setup_malformed_or_duplicate_json_is_not_accepted(monkeypatch, raw):
    with pytest.raises(RuntimeError, match="Setup observation invalid") as error:
        _wait_setup(monkeypatch, [raw])
    assert "SYNTHETIC_PRIVATE" not in str(error.value)


def test_setup_agent_unavailability_is_bounded_and_cancellation_stops(monkeypatch):
    with pytest.raises(windows_runtime.GuestExecError, match="launch"):
        _wait_setup(monkeypatch, [windows_runtime.GuestExecError("test-vm", "launch")], max_wait=4)
    with pytest.raises(RuntimeError, match="cancel_requested"):
        _wait_setup(monkeypatch, [_setup_payload()], cancel=lambda: True)


def test_setup_late_complete_result_is_not_accepted(monkeypatch):
    assert hasattr(windows_runtime, "wait_for_windows_setup"), "setup completion gate missing"
    clock = FakeClock()

    def execute(*args, **kwargs):
        clock.now += 6
        return windows_runtime.GuestExecResult(0, json.dumps(_setup_payload(uptime_seconds=100 + int(clock.now))), "")

    monkeypatch.setattr(windows_runtime, "run_guest_exec", execute)
    with pytest.raises(RuntimeError, match="Setup did not complete"):
        windows_runtime.wait_for_windows_setup(
            "qemu:///system",
            "test-vm",
            cancel_requested=lambda: False,
            time_module=clock,
            max_wait_seconds=5,
            ready_stability_seconds=0,
        )


def _identity(name="AL-123456789012", boot_id="638930000000000001"):
    return windows_runtime.GuestExecResult(0, json.dumps({"computer_name": name, "boot_id": boot_id}), "")


def _bootstrap(reboot_required=True):
    from types import SimpleNamespace

    return SimpleNamespace(
        computer_name="AL-123456789012", reboot_required=reboot_required, boot_id="638930000000000000"
    )


@pytest.mark.parametrize("stage", ["setup", "identity"])
@pytest.mark.parametrize("phase", ["launch", "poll", "timeout", "decode"])
def test_uncertain_guest_execution_stops_without_another_probe(monkeypatch, stage, phase):
    calls = []
    failure = windows_runtime.GuestExecError("test-vm", phase)

    def execute(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise failure
        if stage == "setup":
            return windows_runtime.GuestExecResult(0, json.dumps(_setup_payload(uptime_seconds=100 + len(calls))), "")
        return _identity()

    monkeypatch.setattr(windows_runtime, "run_guest_exec", execute)
    clock = FakeClock()
    with pytest.raises(windows_runtime.GuestExecError) as caught:
        if stage == "setup":
            windows_runtime.wait_for_windows_setup(
                "qemu:///system",
                "test-vm",
                cancel_requested=lambda: False,
                time_module=clock,
                max_wait_seconds=10,
                ready_stability_seconds=0,
            )
        else:
            windows_runtime.wait_for_windows_identity(
                "qemu:///system",
                "test-vm",
                _bootstrap(),
                cancel_requested=lambda: False,
                time_module=clock,
                max_wait_seconds=10,
            )
    assert caught.value is failure
    assert len(calls) == 1


def test_identity_wait_requires_changed_boot_and_applied_name(monkeypatch):
    clock = FakeClock()
    responses = iter(
        [
            _identity(boot_id="638930000000000000"),  # Desired name alone is insufficient.
            _identity(boot_id="638930000000000000"),  # Old boot is still responsive.
            _identity(name="OLD-NAME"),  # New boot alone is insufficient.
            _identity(),
        ]
    )

    def execute(*args, **kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(windows_runtime, "run_guest_exec", execute)
    windows_runtime.wait_for_windows_identity(
        "qemu:///system",
        "test-vm",
        _bootstrap(),
        cancel_requested=lambda: False,
        time_module=clock,
        max_wait_seconds=20,
        poll_interval_seconds=2,
    )
    assert clock.now == 6
    assert next(responses, None) is None


@pytest.mark.parametrize(
    "response",
    [
        _identity(boot_id="638930000000000000"),
        _identity(name="WRONG-NAME"),
        windows_runtime.GuestExecResult(0, '{"computer_name":"AL-123456789012"}', ""),
        windows_runtime.GuestExecResult(0, "not-json", ""),
        windows_runtime.GuestExecResult(1, "", "guest diagnostic must not be logged"),
    ],
)
def test_identity_wait_times_out_without_boot_and_name_proof(monkeypatch, response):
    clock = FakeClock()
    monkeypatch.setattr(windows_runtime, "run_guest_exec", lambda *a, **k: response)
    with pytest.raises(RuntimeError, match="Windows identity verification timed out") as exc:
        windows_runtime.wait_for_windows_identity(
            "qemu:///system",
            "test-vm",
            _bootstrap(),
            cancel_requested=lambda: False,
            time_module=clock,
            max_wait_seconds=5,
            poll_interval_seconds=2,
        )
    assert clock.now == 5
    assert "guest diagnostic" not in str(exc.value)


def test_identity_wait_without_reboot_still_checks_active_name(monkeypatch):
    clock = FakeClock()
    responses = iter([_identity(name="OLD-NAME"), _identity(boot_id="638930000000000000")])
    monkeypatch.setattr(windows_runtime, "run_guest_exec", lambda *a, **k: next(responses))
    windows_runtime.wait_for_windows_identity(
        "qemu:///system",
        "test-vm",
        _bootstrap(False),
        cancel_requested=lambda: False,
        time_module=clock,
        max_wait_seconds=10,
        poll_interval_seconds=2,
    )
    assert clock.now == 2


def test_identity_wait_honors_cancellation_before_guest_execution(monkeypatch):
    monkeypatch.setattr(windows_runtime, "run_guest_exec", lambda *a, **k: pytest.fail("guest query after cancel"))
    with pytest.raises(RuntimeError, match="cancel_requested"):
        windows_runtime.wait_for_windows_identity(
            "qemu:///system", "test-vm", _bootstrap(), cancel_requested=lambda: True, time_module=FakeClock()
        )


def test_identity_wait_does_not_accept_response_after_deadline(monkeypatch):
    clock = FakeClock()

    def delayed_response(*args, **kwargs):
        assert kwargs["timeout"] <= 5
        clock.now += 6
        return _identity()

    monkeypatch.setattr(windows_runtime, "run_guest_exec", delayed_response)
    with pytest.raises(RuntimeError, match="timed out"):
        windows_runtime.wait_for_windows_identity(
            "qemu:///system",
            "test-vm",
            _bootstrap(),
            cancel_requested=lambda: False,
            time_module=clock,
            max_wait_seconds=5,
        )


@pytest.mark.parametrize(
    "overrides", [{"boot_id": None}, {"boot_id": ""}, {"boot_id": []}, {"computer_name": "WRONG-NAME"}]
)
def test_bootstrap_rejects_missing_boot_identity_or_wrong_name(monkeypatch, overrides):
    payload = {
        "computer_name": "AL-123456789012",
        "ssh_host_key_fingerprint": FINGERPRINT,
        "reboot_required": True,
        "boot_id": "638930000000000000",
        **overrides,
    }
    monkeypatch.setattr(
        windows_runtime, "run_guest_exec", lambda *a, **k: windows_runtime.GuestExecResult(0, json.dumps(payload), "")
    )
    with pytest.raises(RuntimeError, match="during result"):
        windows_runtime.bootstrap_windows_guest("qemu:///system", "test-vm", PUBLIC_KEY, "AL-123456789012")
