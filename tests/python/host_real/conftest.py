"""Opt-in real-host tier.

Skips unless ASTINUSLAB_REAL_HOST is enabled. An explicitly enabled tier fails
when libvirt or a real hypervisor connection is unavailable. This tier must
never run in the default suite or hermetic CI.

The tier gate above is about the *host*. The operator-supplied Volatility tools
have a second, deliberately different gate: :func:`operator_volatility` RAISES
instead of skipping (see its docstring).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import time
from urllib.parse import urlparse

import pytest

from tests.python._support.helpers import real_host_worker_command

pytestmark = pytest.mark.host_real


def _real_host_available() -> tuple[bool, str]:
    if os.getenv("ASTINUSLAB_REAL_HOST", "").lower() not in {"1", "true", "yes", "on"}:
        return False, "ASTINUSLAB_REAL_HOST not set"
    if os.getenv("ASTINUSLAB_FAKE_LIBVIRT", "").lower() in {"1", "true", "yes", "on"}:
        return False, "ASTINUSLAB_FAKE_LIBVIRT is set (fake backend)"
    try:
        import libvirt
    except ImportError:
        return False, "libvirt module not importable"
    conn = None
    try:
        conn = libvirt.open(os.getenv("ASTINUSLAB_LIBVIRT_URI", "qemu:///system"))
        if conn is None:
            return False, "libvirt.open returned no connection"
    except Exception as exc:  # noqa: BLE001 - any failure means no usable host
        return False, f"libvirt.open failed: {exc}"
    finally:
        if conn is not None:
            close = getattr(conn, "close", None)
            if callable(close):
                close()
    return True, ""


@pytest.fixture(scope="session", autouse=True)
def _require_real_host():
    ok, reason = _real_host_available()
    if not ok:
        if os.getenv("ASTINUSLAB_REAL_HOST", "").lower() in {"1", "true", "yes", "on"}:
            pytest.fail(f"real-host tier required but unavailable: {reason}")
        pytest.skip(f"real-host tier skipped: {reason}")


_VOLATILITY_NOT_CONFIGURED = """
OPERATOR-SUPPLIED VOLATILITY IS NOT CONFIGURED ON THIS HOST.

THIS IS A FAILURE, NOT A PASS. It is deliberately not a skip: a silently skipped
Volatility row is not evidence that Volatility works, and the nightly must stay
red until the runner is actually provisioned.

What the readiness probe reported:
{report}

Provision the self-hosted runner with operator-supplied Volatility 3 and
dwarf2json, installed OUTSIDE the AstinusLab tree, and point the backend at them
with these settings (environment overrides shown; the same keys exist under
[vol3] in astinuslab.toml):

  ASTINUSLAB_VOL3_ENABLED=true
  ASTINUSLAB_VOL3_VOLATILITY_BIN=<path to the operator's vol executable>
  ASTINUSLAB_VOL3_DWARF2JSON_PATH=<path to the operator's dwarf2json executable>
  ASTINUSLAB_VOL3_DWARF2JSON_SHA256=<64-hex SHA-256 of that exact file>

In .github/workflows/host-nightly.yml those three values come from the GitHub
Actions repository/environment variables HOST_RUNNER_VOL3_VOLATILITY_BIN,
HOST_RUNNER_VOL3_DWARF2JSON_PATH and HOST_RUNNER_VOL3_DWARF2JSON_SHA256.

AstinusLab never downloads, installs, vendors or imports either tool; obtaining
them is an operator action performed outside this tree. Provisioning steps:
docs/dev/host-nightly-runner.md and docs/user/VOLATILITY3.md.
""".strip()


@pytest.fixture(scope="session")
def operator_volatility(_require_real_host):
    """Operator-supplied Volatility 3 + dwarf2json, or a loud failure.

    Returns the ``Vol3Readiness`` probed once per session when BOTH capabilities
    are ready. When they are not, this raises instead of calling ``pytest.skip``.

    That asymmetry is the whole point of the row: the real-host tier is gated on
    a real hypervisor (``_require_real_host``, which does skip, and which gates
    every row in this directory equally), but a *missing tool configuration* is
    an unprovisioned runner, not an inapplicable test. Skipping it would report
    green for a nightly that proved nothing about Volatility. Do not "fix" a
    failure here by converting it to a skip, an xfail, or a conditional marker;
    fix it by provisioning the runner.
    """
    try:
        from astinuslab.core.config import settings
        from astinuslab.forensics.vol3_tools import get_vol3_readiness

        readiness = get_vol3_readiness(settings)
    except Exception as exc:  # noqa: BLE001 - an unloadable config is still an unprovisioned runner
        report = f"  the Volatility settings could not even be loaded: {exc!r}"
        raise RuntimeError(_VOLATILITY_NOT_CONFIGURED.format(report=report)) from exc

    if readiness.enabled and readiness.analysis.ready and readiness.isf_build.ready:
        return readiness

    report = "\n".join(
        [
            f"  vol3_enabled     = {readiness.enabled}",
            f"  analysis  ready={readiness.analysis.ready} code={readiness.analysis.code!r} "
            f"detail={readiness.analysis.detail!r}",
            f"  isf_build ready={readiness.isf_build.ready} code={readiness.isf_build.code!r} "
            f"detail={readiness.isf_build.detail!r}",
        ]
    )
    raise RuntimeError(_VOLATILITY_NOT_CONFIGURED.format(report=report))


def _broker_db_path() -> str | None:
    """Filesystem path of the sqla+sqlite Celery broker, or None if not sqlite."""
    from astinuslab.core.config import settings

    url = settings.celery_broker_url
    # sqla+sqlite:////abs/path or sqla+sqlite:///rel/path
    if "sqlite" not in url:
        return None
    return urlparse(url.split("+", 1)[-1]).path or None


def _real_host_worker_command() -> list[str]:
    return real_host_worker_command()


@pytest.fixture(scope="session", autouse=True)
def _real_host_worker(_require_real_host):
    """Run a real Celery worker for the tier so dispatched jobs actually execute.

    The hermetic suite fakes the broker (no-op TASK_MAP); host_real turns that
    off (see the marker guards in the parent conftest), so it needs a live
    worker consuming the queue. We drain any stale backlog first: a dev-box
    broker can hold weeks of queued teardowns that would starve the single
    worker (concurrency=1). The worker inherits this process's environment, so
    it shares the same DB and broker.
    """
    broker_path = _broker_db_path()
    if broker_path and os.path.exists(broker_path):
        conn = sqlite3.connect(broker_path)
        try:
            conn.execute("DELETE FROM kombu_message")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # fresh broker: table not created yet
        finally:
            conn.close()

    # Capture the worker's log so we can detect readiness from its "ready." line.
    # control.ping() is unreliable here: the sqla+sqlite broker's pidbox/control
    # broadcast does not round-trip promptly, so ping() can time out on a worker
    # that is in fact up and consuming the queue.
    log_file = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed in finally
        prefix="host-real-worker-", suffix=".log", delete=False, mode="w+"
    )
    proc = subprocess.Popen(
        _real_host_worker_command(),
        env=os.environ.copy(),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    deadline = time.monotonic() + 60
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log_file.seek(0)
            tail = log_file.read()[-2000:]
            raise RuntimeError(f"celery worker exited early (rc={proc.returncode}):\n{tail}")
        log_file.seek(0)
        if "ready." in log_file.read():
            ready = True
            break
        time.sleep(1)
    if not ready:
        proc.terminate()
        raise RuntimeError("celery worker did not log ready within 60s")

    try:
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        os.unlink(log_file.name)
