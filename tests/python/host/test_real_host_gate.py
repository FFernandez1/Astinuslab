from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_CONFTEST_PATH = Path(__file__).resolve().parents[1] / "host_real" / "conftest.py"
_spec = importlib.util.spec_from_file_location("host_real_conftest", _CONFTEST_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load conftest from {_CONFTEST_PATH}")
host_real_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(host_real_conftest)


@pytest.fixture(autouse=True)
def _isolated_host_uri(monkeypatch):
    monkeypatch.delenv("ASTINUSLAB_LIBVIRT_URI", raising=False)


def _real_host_available():
    return host_real_conftest._real_host_available()


def _run_require_real_host(*, allow_skip: bool = False):
    fn = getattr(host_real_conftest._require_real_host, "__wrapped__", host_real_conftest._require_real_host)
    try:
        return fn()
    except pytest.skip.Exception as exc:
        if not allow_skip:
            raise AssertionError("Explicitly enabled real-host gate must not skip") from exc
        raise


@pytest.mark.parametrize(
    "env_val",
    [None, "", "0", "false", "no", "off", "disable", "FALSE", "No"],
)
def test_real_host_gate_opt_out_skips(monkeypatch, env_val):
    if env_val is None:
        monkeypatch.delenv("ASTINUSLAB_REAL_HOST", raising=False)
    else:
        monkeypatch.setenv("ASTINUSLAB_REAL_HOST", env_val)

    ok, reason = _real_host_available()
    assert ok is False
    assert reason == "ASTINUSLAB_REAL_HOST not set"

    with pytest.raises(pytest.skip.Exception, match="real-host tier skipped: ASTINUSLAB_REAL_HOST not set"):
        _run_require_real_host(allow_skip=True)


@pytest.mark.parametrize("truthy", ["1", "true", "yes", "on", "True", "YES", "ON"])
def test_real_host_gate_enabled_with_fake_libvirt_fails(monkeypatch, truthy):
    monkeypatch.setenv("ASTINUSLAB_REAL_HOST", truthy)
    monkeypatch.setenv("ASTINUSLAB_FAKE_LIBVIRT", "1")

    ok, reason = _real_host_available()
    assert ok is False
    assert reason == "ASTINUSLAB_FAKE_LIBVIRT is set (fake backend)"

    with pytest.raises(
        pytest.fail.Exception,
        match=r"real-host tier required but unavailable: ASTINUSLAB_FAKE_LIBVIRT is set \(fake backend\)",
    ):
        _run_require_real_host()


def test_real_host_gate_enabled_with_unimportable_libvirt_fails(monkeypatch):
    monkeypatch.setenv("ASTINUSLAB_REAL_HOST", "1")
    monkeypatch.delenv("ASTINUSLAB_FAKE_LIBVIRT", raising=False)
    monkeypatch.setitem(sys.modules, "libvirt", None)

    ok, reason = _real_host_available()
    assert ok is False
    assert reason == "libvirt module not importable"

    with pytest.raises(
        pytest.fail.Exception,
        match="real-host tier required but unavailable: libvirt module not importable",
    ):
        _run_require_real_host()


def test_real_host_gate_enabled_with_failed_connection_fails(monkeypatch):
    monkeypatch.setenv("ASTINUSLAB_REAL_HOST", "1")
    monkeypatch.delenv("ASTINUSLAB_FAKE_LIBVIRT", raising=False)

    stub_libvirt = types.SimpleNamespace()
    stub_libvirt.open = MagicMock(side_effect=RuntimeError("connection refused"))
    monkeypatch.setitem(sys.modules, "libvirt", stub_libvirt)

    ok, reason = _real_host_available()
    assert ok is False
    assert reason == "libvirt.open failed: connection refused"
    stub_libvirt.open.assert_called_once_with("qemu:///system")

    with pytest.raises(
        pytest.fail.Exception,
        match=r"real-host tier required but unavailable: libvirt\.open failed: connection refused",
    ):
        _run_require_real_host()


def test_real_host_gate_enabled_with_none_connection_fails(monkeypatch):
    monkeypatch.setenv("ASTINUSLAB_REAL_HOST", "1")
    monkeypatch.delenv("ASTINUSLAB_FAKE_LIBVIRT", raising=False)

    stub_libvirt = types.SimpleNamespace()
    stub_libvirt.open = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "libvirt", stub_libvirt)

    ok, reason = _real_host_available()
    assert ok is False
    assert reason == "libvirt.open returned no connection"
    stub_libvirt.open.assert_called_once_with("qemu:///system")

    with pytest.raises(
        pytest.fail.Exception,
        match="real-host tier required but unavailable: libvirt.open returned no connection",
    ):
        _run_require_real_host()


def test_real_host_gate_usable_connection_proceeds_and_closes(monkeypatch):
    monkeypatch.setenv("ASTINUSLAB_REAL_HOST", "1")
    monkeypatch.delenv("ASTINUSLAB_FAKE_LIBVIRT", raising=False)

    mock_conn = MagicMock()
    stub_libvirt = types.SimpleNamespace()
    stub_libvirt.open = MagicMock(return_value=mock_conn)
    monkeypatch.setitem(sys.modules, "libvirt", stub_libvirt)

    ok, reason = _real_host_available()
    assert ok is True
    assert reason == ""
    stub_libvirt.open.assert_called_once_with("qemu:///system")
    mock_conn.close.assert_called_once()

    # Fixture proceeds cleanly without skip or fail
    _run_require_real_host()
    assert mock_conn.close.call_count == 2


def test_real_host_gate_usable_connection_honors_custom_uri(monkeypatch):
    monkeypatch.setenv("ASTINUSLAB_REAL_HOST", "1")
    monkeypatch.delenv("ASTINUSLAB_FAKE_LIBVIRT", raising=False)
    monkeypatch.setenv("ASTINUSLAB_LIBVIRT_URI", "qemu:///session")

    mock_conn = MagicMock()
    stub_libvirt = types.SimpleNamespace()
    stub_libvirt.open = MagicMock(return_value=mock_conn)
    monkeypatch.setitem(sys.modules, "libvirt", stub_libvirt)

    ok, reason = _real_host_available()
    assert ok is True
    assert reason == ""
    stub_libvirt.open.assert_called_once_with("qemu:///session")
    mock_conn.close.assert_called_once()

    _run_require_real_host()
    assert mock_conn.close.call_count == 2


def test_real_host_gate_connection_close_failure_raises(monkeypatch):
    monkeypatch.setenv("ASTINUSLAB_REAL_HOST", "1")
    monkeypatch.delenv("ASTINUSLAB_FAKE_LIBVIRT", raising=False)

    mock_conn = MagicMock()
    mock_conn.close.side_effect = RuntimeError("close failure")
    stub_libvirt = types.SimpleNamespace()
    stub_libvirt.open = MagicMock(return_value=mock_conn)
    monkeypatch.setitem(sys.modules, "libvirt", stub_libvirt)

    with pytest.raises(RuntimeError, match="close failure"):
        _real_host_available()
    mock_conn.close.assert_called_once()
