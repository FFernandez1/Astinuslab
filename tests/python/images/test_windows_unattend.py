from __future__ import annotations

import hashlib
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from astinuslab.virtualization import windows_unattend as target

NONCE = "0123456789abcdef0123456789abcdef"
NS = {"u": "urn:schemas-microsoft-com:unattend"}


def build(**overrides):
    options: dict[str, Any] = dict(
        nonce=NONCE,
        locale="en-US",
        input_locale="0409:00000409",
        terms_accepted=True,
        allow_guest_password_initialization=True,
    )
    options.update(overrides)
    return target.build_unattended_setup(**options)


def xml_text(root: ET.Element, path: str) -> str:
    element = root.find(path, NS)
    assert element is not None, path
    assert element.text is not None, path
    return element.text


def test_rendered_configuration_binds_code_and_supported_passes_without_secrets():
    result = build()
    root = ET.fromstring(result.answer_file)
    settings = root.findall("u:settings", NS)
    assert [x.attrib["pass"] for x in settings] == ["specialize", "oobeSystem"]
    command = xml_text(root, ".//u:RunSynchronousCommand/u:Path")
    assert len(command) <= 259
    assert hashlib.sha256(result.initializer).hexdigest().upper() in command
    assert result.volume_label in command
    assert xml_text(root, ".//u:WillReboot") == "Never"
    assert xml_text(root, ".//u:HideLocalAccountScreen") == "true"
    assert xml_text(root, ".//u:HideEULAPage") == "true"
    assert xml_text(root, ".//u:InputLocale") == "0409:00000409"
    forbidden = {
        "AdministratorPassword",
        "Password",
        "Credentials",
        "AutoLogon",
        "UserAccounts",
        "ProductKey",
        "SkipMachineOOBE",
        "SkipUserOOBE",
    }
    assert not {el.tag.split("}")[-1] for el in root.iter()} & forbidden


@pytest.mark.parametrize(
    "override",
    [
        {"terms_accepted": False},
        {"allow_guest_password_initialization": False},
        {"terms_accepted": 1},
        {"allow_guest_password_initialization": "yes"},
        {"nonce": "x" * 32},
        {"nonce": "a" * 31},
        {"nonce": "A" * 32},
        {"nonce": "../x"},
        {"locale": "en-US';exit 0;#"},
        {"locale": "unknown"},
        {"input_locale": "0409:00000409;"},
    ],
)
def test_invalid_or_unapproved_setup_is_rejected(override):
    with pytest.raises(ValueError):
        build(**override)


def test_each_attempt_changes_the_code_digest_and_medium_label():
    a = build()
    b = build(nonce="fedcba9876543210fedcba9876543210")
    assert a.volume_label != b.volume_label
    assert a.initializer != b.initializer
    assert a.answer_file != b.answer_file


def run_harness(tmp_path, scenario):
    pwsh = os.environ.get("ASTINUSLAB_TEST_PWSH")
    if not pwsh or not Path(pwsh).is_file():
        pytest.skip("set ASTINUSLAB_TEST_PWSH for isolated PowerShell behavior tests")
    script = tmp_path / "initializer.ps1"
    script.write_bytes(build().initializer)
    harness = Path(__file__).parents[1] / "_support" / "windows_unattend_harness.ps1"
    completed = subprocess.run(
        [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(harness), str(script), scenario],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_initializer_consumes_attempt_before_setting_an_opaque_credential(tmp_path):
    result = run_harness(tmp_path, "success")
    assert result["result"]["success"] is True
    assert result["result"]["phase"] == "complete"
    assert result["events"] == ["context", "claim", "set", "verify", "receipt"]
    assert result["secure_length"] == 64
    assert result["secure_readonly"] is True
    assert result["secret_disposed"] is True


@pytest.mark.parametrize(
    ("scenario", "events", "phase"),
    [
        ("context", ["context"], "context"),
        ("claim", ["context", "claim"], "claim"),
        ("set", ["context", "claim", "set"], "credential"),
        ("verify", ["context", "claim", "set", "verify"], "verify"),
        ("receipt", ["context", "claim", "set", "verify", "receipt"], "receipt"),
    ],
)
def test_initializer_failures_never_publish_success_or_error_details(tmp_path, scenario, events, phase):
    report = run_harness(tmp_path, scenario)
    assert report["result"] == {"schema_version": 1, "nonce": NONCE, "success": False, "phase": phase}
    assert report["events"] == events
    assert "synthetic-sensitive-diagnostic" not in json.dumps(report)


def test_disabled_account_is_not_enabled_by_initialization(tmp_path):
    report = run_harness(tmp_path, "disabled")
    assert report["result"]["success"] is True
    assert report["account_enabled"] is False


def test_reentry_does_not_reset_password_after_a_consumed_attempt(tmp_path):
    report = run_harness(tmp_path, "repeat")
    assert report["result"]["success"] is False
    assert report["result"]["phase"] == "claim"
    assert report["events"].count("set") == 1


@pytest.mark.parametrize("scenario", ["valid", "tampered", "missing", "duplicate", "wrong-label"])
def test_actual_launcher_only_executes_matching_code_from_one_matching_medium(tmp_path, monkeypatch, scenario):
    pwsh = os.environ.get("ASTINUSLAB_TEST_PWSH")
    if not pwsh or not Path(pwsh).is_file():
        pytest.skip("set ASTINUSLAB_TEST_PWSH")
    # Replace only the payload: never execute the actual Windows entrypoint.
    monkeypatch.setattr(target, "_INITIALIZER", "[IO.File]::WriteAllText('executed.txt','fixture')\nexit 0\n")
    result = build()
    (tmp_path / "s.ps1").write_bytes(result.initializer + (b"# changed\n" if scenario == "tampered" else b""))
    command = xml_text(ET.fromstring(result.answer_file), ".//u:RunSynchronousCommand/u:Path")
    prefix = 'powershell.exe -nop -noni -c "'
    assert command.startswith(prefix) and command.endswith('"')
    command_body = command[len(prefix) : -1]
    volume = f"[pscustomobject]@{{FileSystemLabel='{result.volume_label}';DriveLetter='Z'}}"
    volumes = {
        "valid": volume,
        "tampered": volume,
        "missing": "",
        "duplicate": volume + ";" + volume,
        "wrong-label": "[pscustomobject]@{FileSystemLabel='OTHER';DriveLetter='Z'}",
    }[scenario]
    harness = tmp_path / "launch.ps1"
    harness.write_text(
        "$ErrorActionPreference='Stop'\n"
        "New-PSDrive -Name Z -PSProvider FileSystem -Root $PWD | Out-Null\n"
        f"function Get-Volume {{ {volumes} }}\n" + command_body
    )
    done = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(harness)], cwd=tmp_path, capture_output=True, timeout=10
    )
    assert (done.returncode == 0) is (scenario == "valid")
    assert (tmp_path / "executed.txt").exists() is (scenario == "valid")


def receipt():
    return {
        "schema_version": 1,
        "nonce": NONCE,
        "stage": "specialize",
        "initialized": True,
        "enabled": True,
        "password_last_set_ticks": "639028224000000000",
    }


def test_initializer_receipt_requires_exact_attempt_and_matching_post_setup_account_metadata():
    target.validate_initialization_receipt(
        json.dumps(receipt()),
        nonce=NONCE,
        account_enabled=True,
        password_last_set_ticks="639028224000000000",
    )


@pytest.mark.parametrize(
    "change",
    [
        {"nonce": "f" * 32},
        {"initialized": 1},
        {"schema_version": True},
        {"stage": "oobeSystem"},
        {"enabled": False},
        {"extra": "forbidden"},
        {"password_last_set_ticks": "639028224000000001"},
        {"password_last_set_ticks": "0"},
    ],
)
def test_wrong_or_later_changed_initialization_receipt_is_rejected(change):
    with pytest.raises(ValueError):
        target.validate_initialization_receipt(
            json.dumps({**receipt(), **change}),
            nonce=NONCE,
            account_enabled=True,
            password_last_set_ticks="639028224000000000",
        )


@pytest.mark.parametrize("raw", ["{}", "null", '{"nonce":1,"nonce":2}', "[]", "x" * 1025])
def test_malformed_or_unbounded_receipt_is_rejected(raw):
    with pytest.raises(ValueError):
        target.validate_initialization_receipt(
            raw, nonce=NONCE, account_enabled=True, password_last_set_ticks="639028224000000000"
        )
