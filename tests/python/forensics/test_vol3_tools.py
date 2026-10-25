from __future__ import annotations

import hashlib
import importlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def _tools():
    return importlib.import_module("astinuslab.forensics.vol3_tools")


def _settings(**overrides):
    values = {
        "vol3_enabled": True,
        "vol3_volatility_bin": None,
        "vol3_dwarf2json_path": None,
        "vol3_dwarf2json_sha256": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _make_executable(path: Path, content: bytes = b"tool") -> Path:
    path.write_bytes(content)
    path.chmod(0o755)
    return path


def _successful_probe(*, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr=stderr)


def test_tool_status_is_immutable_and_hides_resolved_path(tmp_path):
    tools = _tools()
    resolved = tmp_path / "vol"
    status = tools.ToolStatus(
        ready=True,
        code="ready",
        detail="Volatility 3 is ready.",
        version="2.28.0",
        resolved_path=resolved,
    )

    with pytest.raises(AttributeError):
        status.ready = False

    assert status.public_dict() == {
        "ready": True,
        "code": "ready",
        "detail": "Volatility 3 is ready.",
        "version": "2.28.0",
        "untested": False,
    }
    assert str(resolved) not in repr(status)


def test_readiness_public_dict_only_exposes_safe_status_fields():
    tools = _tools()
    ready = tools.ToolStatus(True, "ready", "Ready.", version="2.28.0", resolved_path=Path("/secret/vol"))
    disabled = tools.ToolStatus(False, "feature_disabled", "Disabled.", resolved_path=Path("/secret/dwarf2json"))

    readiness = tools.Vol3Readiness(enabled=True, analysis=ready, isf_build=disabled)

    assert readiness.public_dict() == {
        "enabled": True,
        "analysis": ready.public_dict(),
        "isf_build": disabled.public_dict(),
    }
    assert "/secret" not in str(readiness.public_dict())


def test_volatility_absolute_executable_is_probed(monkeypatch, tmp_path):
    tools = _tools()
    binary = _make_executable(tmp_path / "vol")
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *args, **kwargs: _successful_probe(stdout="Volatility 3 Framework 2.28.0\n"),
    )

    status = tools.check_volatility_tool(_settings(vol3_volatility_bin=str(binary)))

    assert status.ready is True
    assert status.code == "ready"
    assert status.version == "2.28.0"
    assert status.resolved_path == binary


def test_volatility_command_name_resolves_with_shutil_which(monkeypatch, tmp_path):
    tools = _tools()
    binary = _make_executable(tmp_path / "vol")
    seen = []

    def fake_which(command):
        seen.append(command)
        return str(binary)

    monkeypatch.setattr(tools.shutil, "which", fake_which)
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *args, **kwargs: _successful_probe(stderr="Volatility 3 Framework 2.28.0\n"),
    )

    status = tools.check_volatility_tool(_settings(vol3_volatility_bin="vol"))

    assert status.ready is True
    assert status.resolved_path == binary
    assert seen == ["vol"]


def test_volatility_missing_configuration_has_stable_code(monkeypatch):
    tools = _tools()
    monkeypatch.setattr(tools.subprocess, "run", lambda *args, **kwargs: pytest.fail("probe must not run"))

    status = tools.check_volatility_tool(_settings(vol3_volatility_bin="  "))

    assert status.ready is False
    assert status.code == "volatility_not_configured"


def test_volatility_missing_path_has_stable_code(monkeypatch, tmp_path):
    tools = _tools()
    monkeypatch.setattr(tools.subprocess, "run", lambda *args, **kwargs: pytest.fail("probe must not run"))
    missing = tmp_path / "missing-vol"

    status = tools.check_volatility_tool(_settings(vol3_volatility_bin=str(missing)))

    assert status.ready is False
    assert status.code == "volatility_not_found"
    assert str(missing) not in status.detail


@pytest.mark.parametrize("kind", ["directory", "nonexecutable"])
def test_volatility_unusable_path_is_not_executable(monkeypatch, tmp_path, kind):
    tools = _tools()
    path = tmp_path / "vol"
    if kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"tool")
        path.chmod(0o644)
    monkeypatch.setattr(tools.subprocess, "run", lambda *args, **kwargs: pytest.fail("probe must not run"))

    status = tools.check_volatility_tool(_settings(vol3_volatility_bin=str(path)))

    assert status.ready is False
    assert status.code == "volatility_not_executable"


def test_volatility_probe_uses_exact_argv_and_options(monkeypatch, tmp_path):
    tools = _tools()
    binary = _make_executable(tmp_path / "vol")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _successful_probe(stdout="Volatility 3 Framework 2.28.0")

    monkeypatch.setattr(tools.subprocess, "run", fake_run)

    status = tools.check_volatility_tool(_settings(vol3_volatility_bin=str(binary)))

    assert status.ready is True
    assert calls == [
        (
            [binary, "-q", "frameworkinfo.FrameworkInfo"],
            {
                "check": False,
                "capture_output": True,
                "text": True,
                "timeout": 60,
            },
        )
    ]


def test_volatility_probe_accepts_shorter_deep_readiness_timeout(monkeypatch, tmp_path):
    tools = _tools()
    binary = _make_executable(tmp_path / "vol")
    observed: list[float] = []

    def run(*args, **kwargs):
        observed.append(kwargs["timeout"])
        return _successful_probe(stdout="Volatility 3 Framework 2.28.0")

    monkeypatch.setattr(tools.subprocess, "run", run)

    status = tools.check_volatility_tool(
        _settings(vol3_volatility_bin=str(binary)),
        timeout_seconds=3.0,
    )

    assert status.ready is True
    assert observed == [3.0]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_volatility_probe_parses_supported_banner_from_either_stream(monkeypatch, tmp_path, stream):
    tools = _tools()
    binary = _make_executable(tmp_path / "vol")
    output = {"stdout": "", "stderr": ""}
    output[stream] = "noise\nVolatility 3 Framework 2.28.0\n"
    monkeypatch.setattr(tools.subprocess, "run", lambda *args, **kwargs: _successful_probe(**output))

    status = tools.check_volatility_tool(_settings(vol3_volatility_bin=str(binary)))

    assert status.ready is True
    assert status.version == "2.28.0"


def _probe_version(tools, monkeypatch, tmp_path, version: str):
    """Run the readiness probe against a stub reporting exactly `version`."""
    binary = _make_executable(tmp_path / "vol")
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *args, **kwargs: _successful_probe(stdout=f"Volatility 3 Framework {version}"),
    )
    return binary, tools.check_volatility_tool(_settings(vol3_volatility_bin=str(binary)))


@pytest.mark.parametrize(
    "version",
    [
        # A plain older release, and a version whose *string* sorts above the floor
        # while its real ordering sorts below it.  The second is the whole reason
        # this gate uses PEP 440 ordering rather than `<` on str.
        "2.27.0",
        "2.9.0",
        # PEP 440 puts a pre-release below its own final release, so a release
        # candidate *of the floor* is under the floor and is refused.
        "2.28.0rc1",
    ],
)
def test_volatility_version_below_the_floor_is_refused(monkeypatch, tmp_path, version):
    tools = _tools()

    _binary, status = _probe_version(tools, monkeypatch, tmp_path, version)

    assert status.ready is False
    assert status.code == "volatility_version_unsupported"
    assert status.version == version
    assert status.untested is False
    assert status.resolved_path is None
    assert tools.MINIMUM_VOLATILITY_VERSION in status.detail


def test_volatility_string_comparison_would_clear_a_version_below_the_floor():
    """Pins the bug the PEP 440 gate exists to prevent.

    `"2.9.0" >= "2.28.0"` is True as strings and False as versions.  If the gate
    ever regresses to string comparison this test is what says so, independently
    of the probe plumbing.
    """
    tools = _tools()

    assert "2.9.0" >= tools.MINIMUM_VOLATILITY_VERSION
    assert tools.Version("2.9.0") < tools.Version(tools.MINIMUM_VOLATILITY_VERSION)


@pytest.mark.parametrize("version", ["2.28.0"])
def test_volatility_tested_version_is_ready_without_the_untested_flag(monkeypatch, tmp_path, version):
    tools = _tools()
    assert version in tools.TESTED_VOLATILITY_VERSIONS

    binary, status = _probe_version(tools, monkeypatch, tmp_path, version)

    assert status.ready is True
    assert status.code == "ready"
    assert status.version == version
    assert status.untested is False
    assert status.detail == "Volatility 3 is ready."
    assert status.resolved_path == binary


@pytest.mark.parametrize(
    "version",
    [
        # A later release upstream has not shipped to us yet.
        "2.29.0",
        "3.0.0",
        # Above the floor, so a pre-release of a *later* version is cleared with
        # the caveat rather than refused.
        "2.29.0rc1",
        # Post- and local-version suffixes on the floor itself: PEP 440 orders both
        # above 2.28.0, and neither string is in the tested set.
        "2.28.0.post1",
        "2.28.0+local",
    ],
)
def test_volatility_version_above_the_floor_but_untested_runs_flagged(monkeypatch, tmp_path, version):
    tools = _tools()
    assert version not in tools.TESTED_VOLATILITY_VERSIONS

    binary, status = _probe_version(tools, monkeypatch, tmp_path, version)

    assert status.ready is True
    assert status.code == "ready"
    assert status.version == version
    assert status.untested is True
    assert status.resolved_path == binary
    # The caveat must name the version the operator actually has, and say it is untested.
    assert version in status.detail
    assert "tested" in status.detail
    assert status.public_dict()["untested"] is True


def test_untested_and_refused_details_match_what_the_api_docs_quote(monkeypatch, tmp_path):
    """docs/api/endpoints.md quotes these `detail` strings verbatim in its samples.

    Byte-for-byte, in all four locales.  Reword the code without reworking the docs
    and this is the test that notices.
    """
    tools = _tools()

    _b1, untested = _probe_version(tools, monkeypatch, tmp_path, "2.29.0")
    _b2, refused = _probe_version(tools, monkeypatch, tmp_path, "2.27.0")

    assert untested.detail == (
        "Volatility 2.29.0 is newer than the versions tested with this release; "
        "analysis will run, but results have not been validated against this version."
    )
    assert refused.detail == "Volatility version 2.27.0 is unsupported; install version 2.28.0 or newer."


def test_untested_version_still_yields_an_executable_to_analysis(monkeypatch, tmp_path):
    """`untested` is a banner, not a block: execution-time verification must pass."""
    tools = _tools()
    binary, status = _probe_version(tools, monkeypatch, tmp_path, "2.29.0")
    assert status.untested is True

    verified = tools.require_verified_volatility(_settings(vol3_volatility_bin=str(binary)))

    assert verified.path == binary
    assert verified.version == "2.29.0"


@pytest.mark.parametrize("version", ["2.28.0-weird", "2.28.0_foo", "2.28.0.x"])
def test_volatility_unparseable_version_is_refused(monkeypatch, tmp_path, version):
    """A version that cannot be compared against the floor cannot be cleared by it."""
    tools = _tools()
    with pytest.raises(tools.InvalidVersion):
        tools.Version(version)

    _binary, status = _probe_version(tools, monkeypatch, tmp_path, version)

    assert status.ready is False
    assert status.code == "volatility_version_unsupported"
    assert status.version == version
    assert status.untested is False
    assert status.resolved_path is None
    assert tools.MINIMUM_VOLATILITY_VERSION in status.detail


def test_unparseable_version_is_logged_without_unbounded_output(monkeypatch, tmp_path, caplog):
    tools = _tools()

    with caplog.at_level("WARNING", logger=tools.__name__):
        _binary, status = _probe_version(tools, monkeypatch, tmp_path, "2.28.0_foo")

    assert status.code == "volatility_version_unsupported"
    assert "2.28.0_foo" in caplog.text
    assert len(caplog.text) < 3072


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "malformed", "oserror", "unicode"])
def test_volatility_probe_failures_have_one_stable_public_code(monkeypatch, tmp_path, failure):
    tools = _tools()
    binary = _make_executable(tmp_path / "vol")
    raw_secret = "/private/tool Volatility 3 Framework secret-output deadbeef"

    def fake_run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd=[str(binary)], timeout=60, output=raw_secret)
        if failure == "oserror":
            raise OSError(raw_secret)
        if failure == "unicode":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, raw_secret)
        if failure == "nonzero":
            return subprocess.CompletedProcess([], 7, stdout=raw_secret, stderr=raw_secret)
        return _successful_probe(stdout=raw_secret)

    monkeypatch.setattr(tools.subprocess, "run", fake_run)

    status = tools.check_volatility_tool(_settings(vol3_volatility_bin=str(binary)))

    assert status.ready is False
    assert status.code == "volatility_probe_failed"
    assert raw_secret not in str(status.public_dict())
    assert str(binary) not in str(status.public_dict())


def test_volatility_probe_rejects_oversized_version_token(monkeypatch, tmp_path):
    tools = _tools()
    binary = _make_executable(tmp_path / "vol")
    version = "2.28.0." + ("x" * 4096)
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *args, **kwargs: _successful_probe(stdout=f"Volatility 3 Framework {version}"),
    )

    status = tools.check_volatility_tool(_settings(vol3_volatility_bin=str(binary)))

    assert status.ready is False
    assert status.code == "volatility_probe_failed"
    assert version not in str(status.public_dict())


def test_volatility_probe_rejects_oversized_numeric_version(monkeypatch, tmp_path):
    tools = _tools()
    binary = _make_executable(tmp_path / "vol")
    version = ("9" * 4096) + ".28.0"
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *args, **kwargs: _successful_probe(stdout=f"Volatility 3 Framework {version}"),
    )

    status = tools.check_volatility_tool(_settings(vol3_volatility_bin=str(binary)))

    assert status.ready is False
    assert status.code == "volatility_probe_failed"
    assert version not in str(status.public_dict())


def test_volatility_probe_logs_bounded_diagnostics(monkeypatch, tmp_path, caplog):
    tools = _tools()
    binary = _make_executable(tmp_path / "vol")
    raw_output = ("x" * 4096) + "UNBOUNDED_TAIL"
    monkeypatch.setattr(
        tools.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 7, stdout=raw_output, stderr=""),
    )

    with caplog.at_level("WARNING", logger=tools.__name__):
        status = tools.check_volatility_tool(_settings(vol3_volatility_bin=str(binary)))

    assert status.code == "volatility_probe_failed"
    assert "UNBOUNDED_TAIL" not in caplog.text
    assert len(caplog.text) < 3072


def test_disabled_feature_performs_no_resolution_probe_or_hashing(monkeypatch):
    tools = _tools()
    monkeypatch.setattr(tools.shutil, "which", lambda *args, **kwargs: pytest.fail("resolution must not run"))
    monkeypatch.setattr(tools.subprocess, "run", lambda *args, **kwargs: pytest.fail("probe must not run"))
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("hashing must not run"))

    readiness = tools.get_vol3_readiness(
        _settings(
            vol3_enabled=False,
            vol3_volatility_bin="vol",
            vol3_dwarf2json_path=Path("dwarf2json"),
            vol3_dwarf2json_sha256="a" * 64,
        )
    )

    assert readiness.enabled is False
    assert readiness.analysis.code == "feature_disabled"
    assert readiness.isf_build.code == "feature_disabled"


def test_dwarf2json_matching_configured_hash_is_accepted(monkeypatch, tmp_path):
    tools = _tools()
    content = b"trusted dwarf2json"
    binary = _make_executable(tmp_path / "dwarf2json", content)
    digest = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("read_bytes must not be used"))

    status = tools.check_dwarf2json_tool(
        _settings(
            vol3_dwarf2json_path=binary,
            vol3_dwarf2json_sha256=f" SHA256:{digest.upper()} ",
        )
    )

    assert status.ready is True
    assert status.code == "ready"
    assert status.resolved_path == binary


def test_dwarf2json_hash_reads_bounded_chunks(monkeypatch, tmp_path):
    tools = _tools()
    content = b"x" * (2 * 1024 * 1024 + 19)
    binary = _make_executable(tmp_path / "dwarf2json", content)
    digest = hashlib.sha256(content).hexdigest()
    original_open = Path.open
    read_sizes = []

    class TrackedReader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size=-1):
            read_sizes.append(size)
            assert 0 < size <= 1024 * 1024
            return self.handle.read(size)

    def tracked_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if path == binary:
            return TrackedReader(handle)
        return handle

    monkeypatch.setattr(Path, "open", tracked_open)

    status = tools.check_dwarf2json_tool(_settings(vol3_dwarf2json_path=binary, vol3_dwarf2json_sha256=digest))

    assert status.ready is True
    assert len(read_sizes) >= 4
    assert all(size > 0 for size in read_sizes)


def test_dwarf2json_missing_hash_is_distinct_from_missing_path(tmp_path):
    tools = _tools()
    binary = _make_executable(tmp_path / "dwarf2json")

    missing_hash = tools.check_dwarf2json_tool(_settings(vol3_dwarf2json_path=binary))
    missing_path = tools.check_dwarf2json_tool(_settings(vol3_dwarf2json_sha256=hashlib.sha256(b"tool").hexdigest()))

    assert missing_hash.code == "dwarf2json_hash_not_configured"
    assert missing_path.code == "dwarf2json_not_configured"


def test_dwarf2json_missing_file_has_stable_code(tmp_path):
    tools = _tools()
    missing = tmp_path / "dwarf2json"

    status = tools.check_dwarf2json_tool(_settings(vol3_dwarf2json_path=missing, vol3_dwarf2json_sha256="a" * 64))

    assert status.code == "dwarf2json_not_found"
    assert str(missing) not in status.detail


@pytest.mark.parametrize("kind", ["directory", "nonexecutable"])
def test_dwarf2json_unusable_path_is_not_executable(tmp_path, kind):
    tools = _tools()
    path = tmp_path / "dwarf2json"
    if kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"tool")
        path.chmod(0o644)

    status = tools.check_dwarf2json_tool(_settings(vol3_dwarf2json_path=path, vol3_dwarf2json_sha256="a" * 64))

    assert status.code == "dwarf2json_not_executable"


def test_dwarf2json_unreadable_file_keeps_its_code_with_a_detail_that_covers_both_causes(monkeypatch, tmp_path):
    """A read failure while hashing is not proof the file is non-executable.

    The executable bit is checked and passed before the hash is taken, so an EIO or
    EACCES here means unreadable, not non-executable.  `dwarf2json_not_executable`
    is documented and audited so it stays exactly as-is; only the human-readable
    detail is narrowed to what the failure actually supports.
    """
    tools = _tools()
    binary = _make_executable(tmp_path / "dwarf2json")

    def unreadable(_path):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(tools, "_hash_file", unreadable)

    status = tools.check_dwarf2json_tool(_settings(vol3_dwarf2json_path=binary, vol3_dwarf2json_sha256="a" * 64))

    assert status.ready is False
    assert status.code == "dwarf2json_not_executable"
    assert status.detail == "The configured dwarf2json path is not a usable executable file: it could not be read."
    assert str(binary) not in str(status.public_dict())


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"vol3_dwarf2json_path": None}, "dwarf2json_not_configured"),
        ({"vol3_dwarf2json_sha256": None}, "dwarf2json_hash_not_configured"),
    ],
    ids=("path", "hash"),
)
def test_dwarf2json_configuration_details_scope_the_loss_to_debug_symbol_builds(tmp_path, overrides, code):
    """The readiness detail is served verbatim on GET /api/v1/volatility/readiness.

    local_cache and prebuilt still deliver a Linux ISF with no dwarf2json at all, so
    "enable Linux ISF builds" overstated the loss and contradicted the UI copy.
    """
    tools = _tools()
    configured = {
        "vol3_dwarf2json_path": _make_executable(tmp_path / "dwarf2json"),
        "vol3_dwarf2json_sha256": "a" * 64,
        **overrides,
    }

    status = tools.check_dwarf2json_tool(_settings(**configured))

    assert status.code == code
    assert "to enable Linux ISF builds" not in status.detail
    assert "build a Linux ISF from kernel debug symbols" in status.detail
    assert "Prebuilt symbol sources and manual ISF upload remain available." in status.detail


def test_dwarf2json_hash_mismatch_has_safe_detail(tmp_path):
    tools = _tools()
    content = b"actual content"
    binary = _make_executable(tmp_path / "dwarf2json", content)
    expected = hashlib.sha256(b"expected content").hexdigest()
    actual = hashlib.sha256(content).hexdigest()

    status = tools.check_dwarf2json_tool(_settings(vol3_dwarf2json_path=binary, vol3_dwarf2json_sha256=expected))

    assert status.ready is False
    assert status.code == "dwarf2json_hash_mismatch"
    assert expected not in status.detail
    assert actual not in status.detail
    assert str(binary) not in status.detail


def test_dwarf2json_rehashes_when_path_size_and_mtime_are_unchanged(tmp_path):
    tools = _tools()
    original = b"aaaa"
    replacement = b"bbbb"
    binary = _make_executable(tmp_path / "dwarf2json", original)
    digest = hashlib.sha256(original).hexdigest()
    configured = _settings(vol3_dwarf2json_path=binary, vol3_dwarf2json_sha256=digest)

    first = tools.check_dwarf2json_tool(configured)
    original_stat = binary.stat()
    binary.write_bytes(replacement)
    binary.chmod(0o755)
    os.utime(binary, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert binary.stat().st_size == original_stat.st_size
    assert binary.stat().st_mtime_ns == original_stat.st_mtime_ns

    second = tools.check_dwarf2json_tool(configured)

    assert first.ready is True
    assert second.code == "dwarf2json_hash_mismatch"


def test_dwarf2json_never_reads_or_trusts_adjacent_sidecar(monkeypatch, tmp_path):
    tools = _tools()
    content = b"trusted configured content"
    binary = _make_executable(tmp_path / "dwarf2json", content)
    digest = hashlib.sha256(content).hexdigest()
    sidecar = binary.with_suffix(binary.suffix + ".sha256")
    sidecar.write_text(f"{'0' * 64}  {binary.name}\n", encoding="utf-8")
    monkeypatch.setattr(Path, "read_text", lambda self, *args, **kwargs: pytest.fail("sidecar must not be read"))

    status = tools.check_dwarf2json_tool(_settings(vol3_dwarf2json_path=binary, vol3_dwarf2json_sha256=digest))

    assert status.ready is True


def test_require_dwarf2json_checks_fresh_and_raises_typed_safe_error(monkeypatch, tmp_path):
    tools = _tools()
    binary = tmp_path / "dwarf2json"
    statuses = [
        tools.ToolStatus(True, "ready", "Ready.", resolved_path=binary),
        tools.ToolStatus(
            False,
            "dwarf2json_hash_mismatch",
            "The configured dwarf2json file does not match its SHA-256 pin.",
        ),
    ]
    calls = []

    def fake_check(settings_obj):
        calls.append(settings_obj)
        return statuses.pop(0)

    configured = _settings()
    monkeypatch.setattr(tools, "check_dwarf2json_tool", fake_check)

    assert tools.require_dwarf2json_tool(configured) == binary
    with pytest.raises(tools.ToolDependencyError) as exc:
        tools.require_dwarf2json_tool(configured)

    assert len(calls) == 2
    assert exc.value.code == "dwarf2json_hash_mismatch"
    assert exc.value.detail == ("The configured dwarf2json file does not match its SHA-256 pin.")
