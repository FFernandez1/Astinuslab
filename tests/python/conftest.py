from __future__ import annotations

import os
import shutil
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _seed_isolated_data_root() -> str:
    """Build the disposable data root the suite runs against.

    ``Settings`` hangs every mutable path off the data root and reads the
    operator's ``astinuslab.toml`` from it, so a checkout used for real work
    otherwise feeds three host-specific inputs into the suite: the operator's
    ``astinuslab.toml`` (a feature flag there changes which routes exist), the
    images this host happens to have downloaded (``load_targets`` appends a
    discovered entry per bootable file), and the live ``config/catalog/*.yaml``
    (which ``_preferred_catalog_path`` picks over the committed example even
    when the example is the configured path).

    Seeding a temporary root with the committed ``*.example.yaml`` catalogs and
    an empty image directory gives every machine the inputs CI already has,
    where none of those three exist. Only the examples are copied: leaving the
    live names absent is what makes the fallback deterministic.
    """
    root = Path(_TEST_DATA_ROOT.name)
    catalog_dir = root / "config" / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    for example in sorted((ROOT / "config" / "catalog").glob("*.example.yaml")):
        shutil.copyfile(example, catalog_dir / example.name)
    (root / "images" / "definitions").mkdir(parents=True, exist_ok=True)
    # `celery_result_backend` hangs off the data root, not off
    # ASTINUSLAB_STORAGE_DIR, so `<data_root>/storage/celery_results.db` has to
    # exist as a directory before any task result is written. SQLite will not
    # create the file under a missing parent; without this line every folder
    # collected before tests/python/lab_packages (which creates the directory
    # as a side effect) fails with "unable to open database file", so
    # `pytest tests/python/playground` broke while the whole-suite run passed.
    (root / "storage").mkdir(parents=True, exist_ok=True)
    return str(root)


# Isolate before any application module caches its Settings. Explicit overrides
# (e.g. real-host/E2E harnesses) still win: every variable below is a default.
_TEST_DATA_ROOT = TemporaryDirectory(prefix="astinuslab-test-data-")
os.environ.setdefault("ASTINUSLAB_DATA_DIR", _seed_isolated_data_root())

_TEST_DB_PATH = Path(mkdtemp(prefix="astinuslab-tests-")) / "tests.db"
os.environ.setdefault("ASTINUSLAB_DB_PATH", str(_TEST_DB_PATH))

# A temporary database alone leaves fake captures orphaned in the live vault.
_TEST_STORAGE = TemporaryDirectory(prefix="astinuslab-test-storage-")
os.environ.setdefault("ASTINUSLAB_STORAGE_DIR", _TEST_STORAGE.name)

from tests.python._support.helpers import autospec_setattr, configure_csrf_client


def _ensure_multipart_stub() -> None:
    try:
        import python_multipart  # noqa: F401

        return
    except ImportError:
        pass

    python_multipart = types.ModuleType("python_multipart")
    python_multipart.__version__ = "0.0.20"
    sys.modules.setdefault("python_multipart", python_multipart)

    multipart = types.ModuleType("multipart")
    multipart.__version__ = "0.0.20"
    sys.modules.setdefault("multipart", multipart)

    multipart_multipart = types.ModuleType("multipart.multipart")

    def _parse_options_header(value):
        if value is None:
            return (b"", {})
        if isinstance(value, str):
            value = value.encode("latin-1")
        if b"multipart/form-data" in value:
            return (b"multipart/form-data", {})
        if b"application/x-www-form-urlencoded" in value:
            return (b"application/x-www-form-urlencoded", {})
        return (b"", {})

    multipart_multipart.parse_options_header = _parse_options_header
    sys.modules.setdefault("multipart.multipart", multipart_multipart)


def _ensure_libvirt_stub() -> None:
    try:
        import libvirt  # noqa: F401

        return
    except ImportError:
        pass

    libvirt = types.ModuleType("libvirt")
    libvirt.open = lambda uri: MagicMock()
    libvirt.VIR_DUMP_MEMORY_ONLY = 1

    class libvirtError(Exception):
        pass

    libvirt.libvirtError = libvirtError
    sys.modules.setdefault("libvirt", libvirt)


_ensure_multipart_stub()
_ensure_libvirt_stub()


@pytest.fixture(scope="session", autouse=True)
def _initialize_test_database():
    from astinuslab.db import initialize_database

    initialize_database()


# Directory -> registered marker (see [tool.pytest.ini_options].markers in
# pyproject.toml). Folders not listed default to "unit". This auto-tags every
# application Python test by location so `-m api`, `-m host`, `-m "not e2e"`, etc. work
# without a pytestmark line in 157 files. Runs alongside the e2e skip hook.
_DIR_MARKERS = {
    "api": "api",
    "ui": "ui",
    "host": "host",
    "e2e": "e2e",
    "integration": "integration",
}
_TESTS_ROOT = Path(__file__).parent.resolve()
_TOOLING_ROOT = _TESTS_ROOT / "tooling"


def pytest_ignore_collect(collection_path, config):
    """Keep tooling out of app discovery unless its path was explicitly named.

    ``tests/python`` remains the app-test root used by bare pytest and the
    coverage job. Tooling lives below it for final ownership, so path recursion
    needs this boundary rather than ``norecursedirs``: the latter would also
    reject ``pytest tests/python/tooling``.
    """
    path = Path(collection_path).resolve()
    if path != _TOOLING_ROOT:
        return None

    explicit_paths = [
        (Path(argument) if Path(argument).is_absolute() else Path.cwd() / argument).resolve()
        for argument in config.invocation_params.args
        if not argument.startswith("-")
    ]
    return not any(argument == _TOOLING_ROOT or _TOOLING_ROOT in argument.parents for argument in explicit_paths)


def pytest_collection_modifyitems(config, items):
    for item in items:
        try:
            rel = Path(str(item.fspath)).resolve().relative_to(_TESTS_ROOT)
        except ValueError:
            continue
        top = rel.parts[0] if len(rel.parts) > 1 else ""
        item.add_marker(getattr(pytest.mark, _DIR_MARKERS.get(top, "unit")))


@pytest.fixture(autouse=True)
def _reset_catalog_caches():
    import astinuslab.catalog as catalog_mod

    catalog_mod.invalidate_catalog_caches()
    catalog_mod._supported_os_variants.cache_clear()
    yield
    catalog_mod.invalidate_catalog_caches()
    catalog_mod._supported_os_variants.cache_clear()


@pytest.fixture()
def in_memory_session_scope():
    """Returns a session_scope-compatible context manager backed by in-memory SQLite."""
    import astinuslab.db.models  # noqa: F401 — registers ORM classes with Base.metadata
    from astinuslab.db import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    @contextmanager
    def _scope():
        session = Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _scope


_TEST_TARGETS = [
    {
        "id": "ubuntu-22-04-default",
        "enabled": True,
        "distro": "ubuntu",
        "version": "22.04",
        "family": "debian",
        "image_available": True,
        "source_type": "qcow2",
        "supports_cloud_image": True,
        "supports_iso_autoinstall": False,
        "image_ref": "ubuntu-22.04-test.qcow2",
        "os_variant": "ubuntu22.04",
        "display_name": "Ubuntu 22.04 (test)",
    },
    {
        "id": "fedora-40",
        "enabled": True,
        "distro": "fedora",
        "version": "40",
        "family": "redhat",
        "image_available": True,
        "source_type": "qcow2",
        "supports_cloud_image": True,
        "supports_iso_autoinstall": False,
        "image_ref": "fedora-40-test.qcow2",
        "os_variant": "fedora40",
        "display_name": "Fedora 40 (test)",
    },
]

_TEST_SCENARIOS = [
    {
        "id": "immediate-capture",
        "enabled": True,
        "mode": "immediate",
        "description": "Test immediate",
        "attack_techniques": [],
    },
    {
        "id": "workload-sample-post-script",
        "enabled": True,
        "mode": "post_script",
        "description": "Test post_script",
        "attack_techniques": ["T1059"],
    },
    {
        "id": "lab-session",
        "enabled": True,
        "mode": "manual_trigger",
        "description": "Test manual_trigger",
        "attack_techniques": ["T1190"],
    },
]


def _is_host_real(request) -> bool:
    """True for the opt-in real-hypervisor tier.

    The hermetic autouse stubs below (fake catalog, no-op broker, fake key,
    fake virtiofsd) would neuter a real-host run: the tier must create real
    jobs against real images and dispatch real Celery tasks. Guard each stub
    so host_real tests get the production wiring.
    """
    return request.node.get_closest_marker("host_real") is not None


@pytest.fixture(autouse=True)
def _stub_catalog_in_services(monkeypatch, request):
    """Patch catalog loaders in services so tests are not gated on real image files.

    Targets at autouse so both client-based and direct-service-call tests work
    without image files on disk. Tests that import catalog functions directly
    (e.g. TestAttackerVMCatalog) are unaffected since we patch svc_mod only.
    """
    if _is_host_real(request):
        return
    import astinuslab.jobs.job_services as svc_mod

    autospec_setattr(monkeypatch, svc_mod, "load_targets", lambda: _TEST_TARGETS)
    autospec_setattr(monkeypatch, svc_mod, "load_scenarios", lambda: _TEST_SCENARIOS)


class _FakeResult:
    id = "fake-celery-id"


class _NoOpTask:
    def delay(self, *args, **kwargs):
        return _FakeResult()


_NOOP_TASK_MAP = {
    "run_immediate_job": _NoOpTask(),
    "run_post_script_job": _NoOpTask(),
    "run_manual_trigger_job": _NoOpTask(),
    "acquire_memory_dump": _NoOpTask(),
    "control_runtime": _NoOpTask(),
    "run_vol3_analysis": _NoOpTask(),
    "run_vol3_plugin": _NoOpTask(),
}


@pytest.fixture(autouse=True)
def _stub_task_map(monkeypatch, request):
    """Patch TASK_MAP in services with a no-op fake so tests never touch the broker.

    Tests that need to assert on dispatch behaviour (e.g. test_create_job_dispatches_celery_task)
    override this by calling monkeypatch.setattr(svc_mod, "TASK_MAP", ...) after fixture setup,
    which takes precedence because monkeypatch is applied in order.
    """
    if _is_host_real(request):
        return
    import astinuslab.jobs.job_services as svc_mod

    monkeypatch.setattr(svc_mod, "TASK_MAP", _NOOP_TASK_MAP)


@pytest.fixture(autouse=True)
def _stub_active_ssh_key(monkeypatch, tmp_path, request):
    """Most tests exercise job state, not keystore setup.

    JobRunDeps.default() reads keystore.get_active_key_paths at call time,
    so patching the source module covers the job-run flows too.
    """
    if _is_host_real(request):
        return
    import astinuslab.jobs.job_services as svc_mod
    import astinuslab.storage.keystore as keystore_mod

    key = tmp_path / "astinuslab-test-key"
    pub = tmp_path / "astinuslab-test-key.pub"
    key.write_text("test-key\n", encoding="utf-8")
    pub.write_text("ssh-ed25519 AAAATESTKEY astinuslab@test\n", encoding="utf-8")
    autospec_setattr(monkeypatch, keystore_mod, "get_active_key_paths", lambda: (key, pub))
    autospec_setattr(monkeypatch, svc_mod, "get_active_key_paths", lambda: (key, pub))


@pytest.fixture(autouse=True)
def _stub_virtiofsd(monkeypatch, request):
    """Unit tests should not start the host virtiofsd daemon."""
    if _is_host_real(request):
        return
    import astinuslab.virtualization.vm_support as vm_support_mod

    autospec_setattr(monkeypatch, vm_support_mod, "start_virtiofsd", lambda *a, **k: 12345)


@pytest.fixture()
def client(in_memory_session_scope, monkeypatch):
    """TestClient with all session_scope references patched to use in-memory SQLite."""
    import astinuslab.app_settings as app_settings_mod
    import astinuslab.db as db_mod
    import astinuslab.forensics.api as volatility_api_mod
    import astinuslab.images.image_admin_support as ias_mod
    import astinuslab.ui.artifact_routes as ui_artifact_routes_mod
    import astinuslab.ui.image_routes as ui_image_routes_mod
    import astinuslab.ui.job_context as ui_job_context_mod
    import astinuslab.ui.job_mutations as ui_job_mutations_mod
    import astinuslab.ui.jobs_routes as ui_jobs_routes_mod
    import astinuslab.ui.logs_routes as ui_logs_routes_mod
    import astinuslab.ui.volatility_routes as ui_volatility_routes_mod

    scope = in_memory_session_scope
    monkeypatch.setattr(db_mod, "session_scope", scope)
    monkeypatch.setattr(app_settings_mod, "session_scope", scope)
    monkeypatch.setattr(ias_mod, "session_scope", scope)
    monkeypatch.setattr(volatility_api_mod, "session_scope", scope)
    for mod in (
        ui_artifact_routes_mod,
        ui_image_routes_mod,
        ui_job_context_mod,
        ui_job_mutations_mod,
        ui_jobs_routes_mod,
        ui_logs_routes_mod,
        ui_volatility_routes_mod,
    ):
        monkeypatch.setattr(mod, "session_scope", scope, raising=False)

    from astinuslab.api import get_session as _api_get_session
    from astinuslab.main import app

    def _override_get_session():
        with scope() as session:
            yield session

    app.dependency_overrides[_api_get_session] = _override_get_session
    try:
        with TestClient(app, raise_server_exceptions=True) as c:
            configure_csrf_client(c)
            yield c
    finally:
        app.dependency_overrides.pop(_api_get_session, None)


@pytest.fixture()
def created_job(client):
    """Creates a job via the API and returns the response JSON."""
    resp = client.post(
        "/api/v1/jobs",
        json={
            "target_id": "ubuntu-22-04-default",
            "scenario_id": "immediate-capture",
            "execution_mode": "immediate",
        },
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture()
def tmp_images_dir(tmp_path, monkeypatch):
    """Redirect settings.images_dir to a temporary directory for image-related tests."""
    import types

    import astinuslab.images.image_admin_support as ias_mod
    import astinuslab.ui.artifact_routes as ui_artifact_routes_mod
    import astinuslab.ui.image_routes as ui_image_routes_mod
    import astinuslab.ui.job_context as ui_job_context_mod
    import astinuslab.ui.jobs_routes as ui_jobs_routes_mod
    import astinuslab.ui.logs_routes as ui_logs_routes_mod
    import astinuslab.ui.volatility_routes as ui_volatility_routes_mod
    from astinuslab.core.config import Settings, settings

    fake_settings = types.SimpleNamespace(**{field: getattr(settings, field) for field in Settings.model_fields})
    fake_settings.images_dir = tmp_path

    monkeypatch.setattr(ias_mod, "settings", fake_settings)
    for mod in (
        ui_artifact_routes_mod,
        ui_image_routes_mod,
        ui_job_context_mod,
        ui_jobs_routes_mod,
        ui_logs_routes_mod,
        ui_volatility_routes_mod,
    ):
        monkeypatch.setattr(mod, "settings", fake_settings)
    return tmp_path


@pytest.fixture()
def available_host_capacity(monkeypatch):
    """Pin host capacity so admission never reads the machine running the tests.

    `read_host_capacity` measures real free RAM and disk unless fake libvirt is
    active. Any test that creates a Job and then asserts on its status or on a
    dispatch therefore passes or fails according to how busy the developer's
    laptop is: a host below the configured reserve defers the job to
    `waiting_for_capacity`, and nothing is dispatched. CI hid this by exporting
    ASTINUSLAB_FAKE_LIBVIRT for the whole suite, so the dependency only showed
    up on a plain local `pytest tests/python`.

    Tests that mean to exercise scarcity keep injecting their own leaner
    HostCapacity, which still wins: their monkeypatch is applied after this one.
    """
    from astinuslab.jobs import capacity_host
    from astinuslab.jobs.capacity import GIB, HostCapacity

    host = HostCapacity(65536, 60000, 1000 * GIB, 900 * GIB, {}, synthetic=True)
    monkeypatch.setattr(capacity_host, "read_host_capacity", lambda *a, **k: host)
    return host
