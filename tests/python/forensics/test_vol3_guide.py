from pathlib import Path
from types import SimpleNamespace

import pytest

from astinuslab.forensics.tool_metadata import DWARF2JSON_AMD64
from astinuslab.forensics.vol3_tools import ToolStatus, Vol3Readiness, preferred_tested_volatility_version


def test_external_tool_metadata_matches_audited_release() -> None:
    assert preferred_tested_volatility_version() == "2.28.0"
    assert DWARF2JSON_AMD64.version == "0.9.0"
    assert DWARF2JSON_AMD64.commit == "9f14607e0d339d463ea725fbd5c08aa7b7d40f75"
    assert DWARF2JSON_AMD64.sha256 == "e2e75ba5bc9c22bc38a48edffbb050456d089d54998d54f46cb580223d9fbc6b"
    assert DWARF2JSON_AMD64.download_url.startswith("https://github.com/volatilityfoundation/dwarf2json/")


def _readiness(analysis: ToolStatus, isf_build: ToolStatus) -> Vol3Readiness:
    return Vol3Readiness(enabled=True, analysis=analysis, isf_build=isf_build)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ToolStatus(True, "ready", "Ready.", version="2.28.0"), "READY"),
        (ToolStatus(True, "ready", "Ready.", version="2.29.0", untested=True), "READY (UNTESTED)"),
        (ToolStatus(False, "volatility_not_configured", "Configure it."), "UNCONFIGURED"),
        (ToolStatus(False, "dwarf2json_not_found", "Missing."), "MISSING"),
        (ToolStatus(False, "volatility_not_executable", "Permissions."), "NOT EXECUTABLE"),
        (ToolStatus(False, "volatility_version_unsupported", "Old."), "UNSUPPORTED"),
        (ToolStatus(False, "dwarf2json_hash_not_configured", "Pin it."), "HASH NOT CONFIGURED"),
        (ToolStatus(False, "dwarf2json_hash_mismatch", "Mismatch."), "HASH MISMATCH"),
        (ToolStatus(False, "volatility_probe_failed", "Probe failed."), "PROBE FAILED"),
    ],
)
def test_status_label_is_stable(status: ToolStatus, expected: str) -> None:
    from astinuslab.forensics.vol3_guide import status_label

    assert status_label(status) == expected


def test_release_guide_is_copy_paste_ready_and_read_only() -> None:
    from astinuslab.forensics.vol3_guide import GuideContext, render_guide

    context = GuideContext(
        base_dir=Path("/opt/astinuslab/releases/1.0.0-test"),
        data_dir=Path("/opt/astinuslab"),
        service_user="operator",
        split_users=False,
        api_url="http://127.0.0.1:8000",
        auth_enabled=True,
    )
    output = render_guide(
        context,
        _readiness(
            ToolStatus(False, "volatility_not_configured", "Configure it."),
            ToolStatus(False, "dwarf2json_not_configured", "Configure it."),
        ),
    )

    assert "This command is read-only" in output
    assert "Analysis: UNCONFIGURED" in output
    assert "ISF build: UNCONFIGURED" in output
    assert "volatility3==2.28.0" in output
    assert "sudo install -d -m 0755 /opt/volatility3-2.28.0" in output
    assert DWARF2JSON_AMD64.download_url in output
    assert f"echo '{DWARF2JSON_AMD64.sha256}  /opt/dwarf2json/0.9.0/dwarf2json-linux-amd64'" in output
    assert "[vol3]" in output
    assert 'volatility_bin = "/opt/volatility3-2.28.0/bin/vol"' in output
    assert "astinuslab-api@operator" in output
    assert "Authorization: Bearer $ASTINUSLAB_ADMIN_TOKEN" in output
    assert "Volatility → Readiness" in output


def test_development_guide_uses_manual_restart_and_no_auth_curl() -> None:
    from astinuslab.forensics.vol3_guide import GuideContext, render_guide

    output = render_guide(
        GuideContext(
            base_dir=Path("/src/astinuslab"),
            data_dir=Path("/src/astinuslab"),
            service_user="developer",
            split_users=False,
            api_url="http://127.0.0.1:8001",
            auth_enabled=False,
        ),
        None,
    )

    assert "Current status: configuration unavailable" in output
    assert "./astinuslab.sh restart" in output
    assert "systemctl restart" not in output
    assert "http://127.0.0.1:8001/api/v1/volatility/readiness" in output
    assert "Authorization: Bearer" not in output


def test_split_release_guide_uses_explicit_units() -> None:
    from astinuslab.forensics.vol3_guide import GuideContext, render_guide

    output = render_guide(
        GuideContext(
            base_dir=Path("/opt/astinuslab/releases/1.0.0-test"),
            data_dir=Path("/opt/astinuslab"),
            service_user="operator",
            split_users=True,
            api_url="http://127.0.0.1:8000",
            auth_enabled=True,
        ),
        None,
    )

    assert "systemctl restart astinuslab-api astinuslab-runner astinuslab-analysis" in output
    assert "astinuslab-api@operator" not in output


def test_cli_renders_injected_readiness_without_running_tools(monkeypatch, capsys) -> None:
    from astinuslab.forensics import vol3_guide

    expected = _readiness(
        ToolStatus(True, "ready", "Analysis ready.", version="2.28.0"),
        ToolStatus(False, "dwarf2json_hash_mismatch", "Hash differs."),
    )
    monkeypatch.setattr(vol3_guide, "load_settings", lambda: SimpleNamespace(auth_enabled=False))
    monkeypatch.setattr(vol3_guide, "get_vol3_readiness", lambda _settings: expected)

    result = vol3_guide.main(
        [
            "--base-dir",
            "/src/astinuslab",
            "--data-dir",
            "/src/astinuslab",
            "--service-user",
            "developer",
            "--api-url",
            "http://127.0.0.1:8001",
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "Analysis: READY" in output
    assert "ISF build: HASH MISMATCH" in output
    assert "Authorization: Bearer" not in output


def test_cli_falls_back_when_configuration_is_unreadable(monkeypatch, capsys) -> None:
    from astinuslab.forensics import vol3_guide

    def fail_to_load():
        raise ValueError("invalid configuration")

    monkeypatch.setattr(vol3_guide, "load_settings", fail_to_load)

    result = vol3_guide.main(
        [
            "--base-dir",
            "/opt/astinuslab/current",
            "--data-dir",
            "/opt/astinuslab",
            "--service-user",
            "operator",
            "--api-url",
            "http://127.0.0.1:8000",
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "Current status: configuration unavailable" in output
    assert "Authorization: Bearer $ASTINUSLAB_ADMIN_TOKEN" in output


def test_fresh_process_guide_recovers_from_malformed_config(tmp_path):
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[3]
    (tmp_path / "astinuslab.toml").write_text("[vol3]\nenabled=true\n[vol3]\nenabled=true\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ASTINUSLAB_")}
    env.update(
        ASTINUSLAB_DATA_DIR=str(tmp_path),
        ASTINUSLAB_DB_PATH=str(tmp_path / "unused.db"),
        ASTINUSLAB_FAKE_LIBVIRT="1",
        ASTINUSLAB_FAKE_PLAYGROUND="1",
        PYTHONPATH=str(root / "src") + ":" + str(root),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "astinuslab.forensics.vol3_guide",
            "--base-dir",
            str(root),
            "--data-dir",
            str(tmp_path),
            "--service-user",
            "operator",
            "--api-url",
            "http://127.0.0.1:8000",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "configuration unavailable" in result.stdout
    assert "existing [vol3]" in result.stdout
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "unused.db").exists()


def test_guide_edit_on_seeded_toml_and_printed_validation(tmp_path):
    import re
    import shlex
    import subprocess
    import sys
    import tomllib

    from astinuslab.forensics.vol3_guide import GuideContext, render_guide

    root = Path(__file__).resolve().parents[3]
    base = tmp_path / "release"
    (base / ".venv/bin").mkdir(parents=True)
    (base / ".venv/bin/python").symlink_to(sys.executable)
    context = GuideContext(base, tmp_path, "operator", False, "http://127.0.0.1:8000", False)
    output = render_guide(context, None)
    assert "Edit the existing [vol3]" in output
    assert "Do not append a second [vol3]" in output
    block = output.split("  [vol3]\n", 1)[1].split("\n\n", 1)[0]
    replacements = tomllib.loads("[vol3]\n" + block)["vol3"]
    source = (root / "astinuslab.toml.example").read_text()
    section = re.search(r"(?ms)^\[vol3\]\n.*?(?=^\[|\Z)", source)
    assert section is not None
    edited = section.group()
    for key, value in replacements.items():
        literal = "true" if value is True else '"' + value + '"'
        edited, count = re.subn(rf"(?m)^(?:#\s*)?{key}\s*=.*$", f"{key} = {literal}", edited)
        assert count == 1, key
    config = tmp_path / "astinuslab.toml"
    config.write_text(source[: section.start()] + edited + source[section.end() :])
    assert tomllib.loads(config.read_text())["vol3"].items() >= replacements.items()
    validation = next(line.strip() for line in output.splitlines() if "tomllib" in line)
    result = subprocess.run(shlex.split(validation), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "TOML valid" in result.stdout
    config.write_text(config.read_text() + "\n[vol3]\n")
    result = subprocess.run(shlex.split(validation), capture_output=True, text=True)
    assert result.returncode != 0
