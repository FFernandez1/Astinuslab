from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

E2E_DIR = Path(__file__).parent.resolve()

# Operator-supplied Volatility tooling. Dropped from the inherited environment so
# this tier's readiness state is a property of the tier, not of the developer.
_VOL3_TOOL_VARS = frozenset(
    {
        "ASTINUSLAB_VOL3_ENABLED",
        "ASTINUSLAB_VOL3_VOLATILITY_BIN",
        "ASTINUSLAB_VOL3_DWARF2JSON_PATH",
        "ASTINUSLAB_VOL3_DWARF2JSON_SHA256",
    }
)
ROOT = E2E_DIR.parents[2]


def pytest_collection_modifyitems(config, items):
    args = [arg for arg in config.invocation_params.args if not arg.startswith("-")]
    explicit = any(E2E_DIR == (ROOT / arg).resolve() or E2E_DIR in (ROOT / arg).resolve().parents for arg in args)
    if explicit:
        return
    skip = pytest.mark.skip(reason="e2e tests run only when tests/python/e2e/ is requested explicitly")
    for item in items:
        if E2E_DIR in Path(str(item.fspath)).resolve().parents:
            item.add_marker(skip)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_catalogs(tmp_path: Path) -> dict[str, Path]:
    images_dir = tmp_path / "images"
    catalog_dir = tmp_path / "catalog"
    images_dir.mkdir()
    catalog_dir.mkdir()
    for name in ("ubuntu-22.04-test.qcow2", "fedora-40-test.qcow2", "kali-rolling-test.qcow2"):
        (images_dir / name).write_bytes(b"fake qcow2\n")

    targets = catalog_dir / "targets.yaml"
    targets.write_text(
        """
targets:
  - id: ubuntu-22-04-default
    enabled: true
    distro:
      slug: ubuntu
      version: "22.04"
      family: debian
    image:
      source_type: qcow2
      image_ref: ubuntu-22.04-test.qcow2
      default_username: analyst
    virtualization:
      os_variant: generic
  - id: fedora-40
    enabled: true
    distro:
      slug: fedora
      version: "40"
      family: redhat
    image:
      source_type: qcow2
      image_ref: fedora-40-test.qcow2
      default_username: analyst
    virtualization:
      os_variant: generic
""".lstrip(),
        encoding="utf-8",
    )
    workloads = catalog_dir / "workloads.yaml"
    workloads.write_text("version: 1\nworkloads: []\n", encoding="utf-8")
    attackers = catalog_dir / "attackers.yaml"
    attackers.write_text(
        """
attackers:
  - id: kali-rolling
    enabled: true
    display_name: Kali Rolling (test)
    image_ref: kali-rolling-test.qcow2
    username: kali
    os_variant: generic
""".lstrip(),
        encoding="utf-8",
    )
    return {
        "images_dir": images_dir,
        "targets_catalog_path": targets,
        "workloads_catalog_path": workloads,
        "attackers_catalog_path": attackers,
    }


def _server_env(
    tmp_path: Path,
    *,
    eager_tasks: bool = True,
    images_dir: Path | None = None,
    fake_orchestration: bool = False,
    volatility_bin: Path | None = None,
    vol3_mode: str = "",
) -> dict[str, str]:
    """Server environment for an isolated e2e backend.

    ``eager_tasks`` runs Celery work inline, which is what the Playground tests
    want: their VM and capture paths are fully faked, so a task can complete
    inside the request. It is wrong for Jobs: job orchestration is *not* faked
    by ASTINUSLAB_FAKE_LIBVIRT and reaches for privileged host commands
    (``sudo libvirt_storage_admin.sh install_base_image``), which would either
    fail noisily or, on a real libvirt host, actually provision a VM from a
    test. A Jobs server therefore only enqueues: the broker accepts the message,
    no worker consumes it, and the job stays admitted-but-not-started.
    """
    paths = _write_catalogs(tmp_path)
    storage_dir = tmp_path / "storage"
    inherited = {key: value for key, value in os.environ.items() if key not in _VOL3_TOOL_VARS}
    return {
        **inherited,
        # Volatility is enabled but has no operator-supplied binary, which is the
        # state a first-time operator meets. Pinned rather than inherited so a
        # developer who has `vol` configured in their shell does not silently get
        # a different tier than CI.
        "ASTINUSLAB_VOL3_ENABLED": "true",
        # Absent unless a tier asks for it, which keeps "no analysis tool is
        # configured" -- the state a first-time operator meets -- the default.
        "ASTINUSLAB_VOL3_VOLATILITY_BIN": str(volatility_bin) if volatility_bin else "",
        # Read only by fixtures/fake_vol3.py, which the plugin runner launches as
        # a subprocess and which therefore inherits the server's environment.
        # No product code reads either of them.
        "ASTINUSLAB_FAKE_VOL3_MODE": vol3_mode,
        "ASTINUSLAB_FAKE_VOL3_MARKER": str(tmp_path / "fake-vol3-invocations"),
        "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT))),
        "CELERY_TASK_ALWAYS_EAGER": "1" if eager_tasks else "0",
        "ASTINUSLAB_FAKE_LIBVIRT": "1",
        "ASTINUSLAB_FAKE_PLAYGROUND": "1",
        # Off by default. A Jobs tier that only tests admission wants a backend
        # that enqueues and never executes; only the tier that drives a running
        # job asks for the orchestration doubles.
        "ASTINUSLAB_FAKE_ORCHESTRATION": "1" if fake_orchestration else "",
        "ASTINUSLAB_AUTH_ENABLED": "false",
        "ASTINUSLAB_DB_PATH": str(storage_dir / "astinuslab-e2e.db"),
        "ASTINUSLAB_STORAGE_DIR": str(storage_dir),
        # Not derived from ASTINUSLAB_STORAGE_DIR: an ISF upload writes into the
        # symbol cache, and unpinned it lands in the repository's own vol3/.
        "ASTINUSLAB_VOL3_SYMBOLS_PATH": str(tmp_path / "vol3-symbols"),
        "ASTINUSLAB_PLAYGROUND_DISK_DIR": str(tmp_path / "playground-disks"),
        "ASTINUSLAB_PLAYGROUND_CAPTURE_DIR": str(tmp_path / "playground-captures"),
        "ASTINUSLAB_IMAGES_DIR": str(images_dir if images_dir is not None else paths["images_dir"]),
        "ASTINUSLAB_TARGETS_CATALOG_PATH": str(paths["targets_catalog_path"]),
        "ASTINUSLAB_WORKLOADS_CATALOG_PATH": str(paths["workloads_catalog_path"]),
        "ASTINUSLAB_ATTACKERS_CATALOG_PATH": str(paths["attackers_catalog_path"]),
        "ASTINUSLAB_CELERY_BROKER_URL": f"sqla+sqlite:///{storage_dir / 'celery-broker.db'}",
        "ASTINUSLAB_CELERY_RESULT_BACKEND": f"db+sqlite:///{storage_dir / 'celery-results.db'}",
        "ASTINUSLAB_CELERY_RECONCILIATION_ENABLED": "false",
        "ASTINUSLAB_PLAYGROUND_CAPTURE_STREAM_TIMEOUT_SECONDS": "10",
    }


def _wait_for_health(base_url: str, proc: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 20
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=0.5) as res:
                if res.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.1)
    stdout, _ = proc.communicate(timeout=2) if proc.poll() is not None else ("", None)
    raise RuntimeError(f"uvicorn did not become healthy: {last_error}\n{stdout}")


def _truncate_playground_tables(db_path: Path) -> None:
    if not db_path.exists():
        return
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        for table in (
            "acquisition_events",
            "acquisitions",
            "playground_topology_layout",
            "playground_sessions",
            "job_vm_nics",
            "job_vms",
            "job_networks",
            "host_actions",
            "job_events",
            "job_runtimes",
            "isf_artifacts",
            "jobs",
        ):
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(f'DELETE FROM "{table}"')
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")


@contextlib.contextmanager
def _running_server(
    tmp_path: Path,
    *,
    admin_token: str | None = None,
    eager_tasks: bool = True,
    max_concurrent_vms: int | None = None,
    images_dir: Path | None = None,
    fake_orchestration: bool = False,
    volatility_bin: Path | None = None,
    vol3_mode: str = "",
) -> Iterator[dict[str, object]]:
    env = _server_env(
        tmp_path,
        eager_tasks=eager_tasks,
        images_dir=images_dir,
        fake_orchestration=fake_orchestration,
        volatility_bin=volatility_bin,
        vol3_mode=vol3_mode,
    )
    if max_concurrent_vms is not None:
        env["ASTINUSLAB_MAX_CONCURRENT_VMS"] = str(max_concurrent_vms)
    if admin_token is not None:
        env["ASTINUSLAB_AUTH_ENABLED"] = "true"
        env["ASTINUSLAB_ADMIN_API_TOKEN_HASH"] = "sha256:" + hashlib.sha256(admin_token.encode()).hexdigest()
    db_path = Path(env["ASTINUSLAB_DB_PATH"])
    port = _free_port()
    # Origin validation derives the trusted loopback origins from the configured
    # API port, so the settings value must follow this fixture's dynamic port.
    env["ASTINUSLAB_API_PORT"] = str(port)
    base_url = f"http://127.0.0.1:{port}"

    subprocess.run(
        [sys.executable, "-c", "from astinuslab.db import main; raise SystemExit(main(['init']))"],
        cwd=ROOT,
        env=env,
        check=True,
    )
    uvicorn_bin = Path(sys.executable).with_name("uvicorn")
    command = [str(uvicorn_bin), "astinuslab.main:app", "--host", "127.0.0.1", "--port", str(port)]
    proc = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_health(base_url, proc)
        yield {"base_url": base_url, "db_path": db_path, "admin_token": admin_token}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture(scope="session")
def playground_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, object]]:
    with _running_server(tmp_path_factory.mktemp("playground-e2e")) as server:
        yield server


@pytest.fixture()
def authenticated_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, object]]:
    with _running_server(tmp_path_factory.mktemp("authenticated-e2e"), admin_token="browser-e2e-admin-token") as server:
        yield server


class CreateVmWizard:
    """Drives the Playground's create-VM modal.

    Six e2e modules open this wizard and they had each grown their own version
    of the same opening sequence, including the part that is easy to get wrong:
    the image grid arrives asynchronously and Next stays disabled until an image
    is selected, so a test that starts filling the form too early times out on a
    control whose tooltip blames the missing image.

    Deliberately granular. Tests that are *about* stepping through the wizard
    still step through it themselves -- hiding that behind a one-call `create()`
    would hide the thing under test.
    """

    def __init__(self, page: Page, base_url: str) -> None:
        self.page = page
        self.base_url = base_url
        self.modal = page.locator("#create-vm-modal")
        self.error = page.locator("#spawn-error")
        self.warning = page.locator("#create-vm-warning")
        self.submit_button = page.locator("#create-vm-submit")
        self.next_button = page.locator("#create-vm-next")

    def open(self, role: str = "target", *, label: str | None = None, goto: bool = True) -> CreateVmWizard:
        if goto:
            self.page.set_viewport_size({"width": 1600, "height": 900})
            self.page.goto(f"{self.base_url}/playground")
        self.page.locator(f'[data-open-create="{role}"]').click()
        expect(self.modal).to_be_visible()
        expect(self.page.locator(".pg-hud-images .pg-hud-image").first).to_be_visible()
        if label is not None:
            self.page.locator('#create-fields input[name="label"]').fill(label)
        return self

    def fill_label(self, label: str) -> CreateVmWizard:
        self.page.locator('#create-fields input[name="label"]').fill(label)
        return self

    def next(self, times: int = 1) -> CreateVmWizard:
        # By id, not by accessible name: the button reads "Next" only in English,
        # and this wizard is walked in four locales.
        for _ in range(times):
            self.next_button.click()
        return self

    def advance_to_submit(self) -> CreateVmWizard:
        """Walk to the step that can submit, failing loudly if it never appears."""
        for _ in range(4):
            if self.submit_button.is_visible():
                break
            expect(self.next_button).to_be_enabled()
            self.next_button.click()
        expect(self.submit_button).to_be_visible()
        return self

    def back_to_start(self, steps: int = 3) -> CreateVmWizard:
        for _ in range(steps):
            self.page.locator("#create-vm-back").click()
        return self

    def add_draft_network(self, name: str, cidr: str) -> CreateVmWizard:
        """Add a network from inside the wizard. It stays a draft until submit."""
        self.page.locator(".pg-hud-netadd").click()
        self.page.locator("#create-network-name").fill(name)
        self.page.locator("#create-network-cidr").fill(cidr)
        self.page.locator("#create-network-submit").click()
        return self

    def submit(self) -> None:
        self.submit_button.click()


@pytest.fixture()
def create_vm_wizard(page: Page):
    """Factory: the base URL differs per tier, the wizard does not.

    ``on`` overrides the tier's own page. Only the two-tab module needs it --
    it drives a second browser context against the same backend -- but the
    wizard is the same wizard, and the topology contract keeps shared test code
    here rather than letting one test module import another.
    """

    def _wizard(base_url: str, on: Page | None = None) -> CreateVmWizard:
        return CreateVmWizard(on or page, base_url)

    return _wizard


@pytest.fixture(scope="session")
def capped_playground_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, object]]:
    """A playground backend with room for exactly two VMs.

    `max_concurrent_vms` is unset by default, so the admission cap -- the point
    where a lab silently stops growing -- is unreachable on the normal tier.
    """
    with _running_server(tmp_path_factory.mktemp("capped-e2e"), max_concurrent_vms=2) as server:
        yield server


@pytest.fixture()
def capped_base_url(capped_playground_server: dict[str, object]) -> Iterator[str]:
    db_path = capped_playground_server["db_path"]
    assert isinstance(db_path, Path)
    _truncate_playground_tables(db_path)
    yield str(capped_playground_server["base_url"])
    _truncate_playground_tables(db_path)


@pytest.fixture()
def disposable_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, object]]:
    """A whole backend per test, for tests that destroy things.

    Deleting an image or a keystore key is a file-system effect that truncating
    tables will not undo, so these tests must not share a server with anything
    -- including each other.
    """
    with _running_server(tmp_path_factory.mktemp("disposable-e2e")) as server:
        yield server


@pytest.fixture()
def disposable_base_url(disposable_server: dict[str, object]) -> str:
    return str(disposable_server["base_url"])


@pytest.fixture()
def disposable_storage_dir(disposable_server: dict[str, object]) -> Path:
    """The storage root of the disposable server: the DB sits directly inside it."""
    db_path = disposable_server["db_path"]
    assert isinstance(db_path, Path)
    return db_path.parent


@pytest.fixture(scope="session")
def bare_install_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, object]]:
    """A backend with catalogs but no images on disk: what a fresh install is.

    The other tiers pre-write fake qcow2 files, so the pages that list them are
    never seen in the state an operator meets first.
    """
    root = tmp_path_factory.mktemp("bare-install-e2e")
    empty_images = root / "no-images"
    empty_images.mkdir()
    with _running_server(root, images_dir=empty_images) as server:
        yield server


@pytest.fixture()
def bare_install_base_url(bare_install_server: dict[str, object]) -> Iterator[str]:
    db_path = bare_install_server["db_path"]
    assert isinstance(db_path, Path)
    _truncate_playground_tables(db_path)
    yield str(bare_install_server["base_url"])
    _truncate_playground_tables(db_path)


@pytest.fixture(scope="session")
def jobs_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, object]]:
    """Own backend for the Jobs tier, deliberately without an executing worker."""
    with _running_server(tmp_path_factory.mktemp("jobs-e2e"), eager_tasks=False) as server:
        yield server


@pytest.fixture()
def jobs_base_url(jobs_server: dict[str, object]) -> Iterator[str]:
    db_path = jobs_server["db_path"]
    assert isinstance(db_path, Path)
    _truncate_playground_tables(db_path)
    yield str(jobs_server["base_url"])
    _truncate_playground_tables(db_path)


@pytest.fixture(scope="session")
def running_jobs_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, object]]:
    """A Jobs backend whose worker actually runs, without touching the host.

    `jobs_server` deliberately never executes, so the boundary it tests is
    admission. This one executes inline under ASTINUSLAB_FAKE_ORCHESTRATION,
    which replaces every binding that would reach libvirt or `sudo` with one
    that refuses -- so a job walks the real status graph and no VM is created.
    See src/astinuslab/orchestration/fake_runtime.py.
    """
    with _running_server(
        tmp_path_factory.mktemp("running-jobs-e2e"),
        eager_tasks=True,
        fake_orchestration=True,
    ) as server:
        yield server


@pytest.fixture()
def running_jobs_base_url(running_jobs_server: dict[str, object]) -> Iterator[str]:
    db_path = running_jobs_server["db_path"]
    assert isinstance(db_path, Path)
    _truncate_playground_tables(db_path)
    yield str(running_jobs_server["base_url"])
    _truncate_playground_tables(db_path)


def _activate_ssh_key(page: Page, base_url: str, name: str) -> str:
    """Generate and activate a keystore key through the real Security page.

    Creating a Job refuses with 400 ("No active SSH key is configured") until the
    operator has done this, so every Jobs test needs it first. Driving the real
    forms rather than seeding files keeps the precondition honest and gives the
    keystore page its only browser coverage.
    """
    page.goto(f"{base_url}/ssh-keys")
    if page.locator("td", has_text=name).count() == 0:
        page.locator('form[action="/ssh-keys/generate"] input[name="name"]').fill(name)
        page.locator('form[action="/ssh-keys/generate"] button[type="submit"]').click()
        expect(page).to_have_url(f"{base_url}/ssh-keys")
    row = page.locator("tr.admin-table__row", has_text=name)
    expect(row).to_have_count(1)
    # Target the form action, not the button's label: the label is translated
    # and this fixture must keep working in every locale.
    activate = row.locator('button[formaction="/ssh-keys/set-active"]')
    if activate.count():
        activate.first.click()
        row = page.locator("tr.admin-table__row", has_text=name)
    expect(row.locator(".wiz-badge--success")).to_be_visible()
    return name


@pytest.fixture()
def active_ssh_key(jobs_base_url: str, page: Page) -> str:
    return _activate_ssh_key(page, jobs_base_url, "e2e-jobs-key")


@pytest.fixture()
def running_jobs_ssh_key(running_jobs_base_url: str, page: Page) -> str:
    return _activate_ssh_key(page, running_jobs_base_url, "e2e-running-jobs-key")


@pytest.fixture()
def capped_jobs_base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """`running_jobs_base_url` with room for exactly one VM.

    The admission cap is unset by default, so "a second Job waits for a slot"
    -- the state the whole capacity subsystem exists to produce -- was
    unreachable from a browser. It needs both halves at once: a worker that
    really runs, so the first job holds a reservation, and a cap low enough for
    the second to be refused a slot.

    One server per test rather than one per session, unlike `running_jobs_*`:
    the point of these tests is a job left holding the only slot there is, and
    a session-scoped backend would hand that job to the next test.
    """
    with _running_server(
        tmp_path_factory.mktemp("capped-jobs-e2e"),
        eager_tasks=True,
        fake_orchestration=True,
        max_concurrent_vms=1,
    ) as server:
        yield str(server["base_url"])


@pytest.fixture()
def capped_jobs_ssh_key(capped_jobs_base_url: str, page: Page) -> str:
    return _activate_ssh_key(page, capped_jobs_base_url, "e2e-capped-jobs-key")


class JobWizard:
    """Drives /jobs/new far enough to have a running Job.

    The wizard skips the steps a scenario does not use, so the walk to the
    submit control cannot pin a step count -- it advances until the control
    appears. Two modules need a job they are not otherwise testing the creation
    of, and the topology contract keeps shared test code here.
    """

    def __init__(self, base_url: str, page: Page) -> None:
        self.base_url = base_url
        self.page = page

    def launch(self, mode: str) -> str:
        """Create a job of one execution mode and follow it. Returns its id."""
        self.page.set_viewport_size({"width": 1500, "height": 950})
        self.page.goto(f"{self.base_url}/jobs/new")
        self.page.locator('[name="target_id"]').first.check(force=True)
        self.page.locator(f'[name="scenario_id"][data-mode="{mode}"]').first.check(force=True)
        launch = self.page.locator("#wiz-launch-btn")
        for _ in range(5):
            if launch.is_visible():
                break
            expect(self.page.locator("#wiz-next-btn")).to_be_enabled()
            self.page.locator("#wiz-next-btn").click()
        expect(launch).to_be_visible()
        launch.click()
        self.page.wait_for_url(lambda url: "/jobs/" in url and not url.endswith("/new"), timeout=20_000)
        return str(self.page.url.rsplit("/", 1)[-1])

    def jobs(self) -> list[dict]:
        with urllib.request.urlopen(f"{self.base_url}/api/v1/jobs", timeout=10) as response:
            return list(json.loads(response.read().decode("utf-8")))

    def job(self, job_id: str) -> dict:
        matches = [row for row in self.jobs() if row["id"] == job_id]
        assert len(matches) == 1, f"expected exactly one job {job_id}, got {matches}"
        return matches[0]


@pytest.fixture()
def job_wizard(page: Page):
    """Factory: the base URL differs per tier, the wizard does not."""

    def _wizard(base_url: str, on: Page | None = None) -> JobWizard:
        return JobWizard(base_url, on or page)

    return _wizard


FAKE_VOL3_BIN = E2E_DIR / "fixtures" / "fake_vol3.py"


class AnalysisWorkbench:
    """Drives a Playground capture to the point where a plugin run is possible.

    Four steps stand between an empty lab and a runnable dump, and none of them
    is what a Volatility test is usually about: spawn a target, capture its
    RAM, state the capture's OS context, and supply symbols for it. Two modules
    need all four -- what a completed run attributes its results to, and what
    the workbench says when a run fails -- so the sequence lives here rather
    than being copied, and the topology contract keeps shared test code in
    conftest rather than letting one test module import another.

    Granular for `CreateVmWizard`'s reason: a test that is *about* one of these
    steps still drives that step itself.
    """

    CSRF_TOKEN = "playground-e2e-csrf-token"
    # The context an operator states for a Playground capture. Arbitrary, but
    # the supplied ISF has to declare the same kernel release or the upload is
    # refused -- which is the product refusing to analyse bytes with symbols
    # that do not belong to them. Exposed so a test can assert the run carried
    # the operator's stated context rather than re-deriving one.
    KERNEL_RELEASE = "5.15.0-e2e-generic"

    def __init__(self, base_url: str, page: Page) -> None:
        self.base_url = base_url
        self.page = page

    def api(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Cookie": f"astinuslab_csrf={self.CSRF_TOKEN}", "X-CSRF-Token": self.CSRF_TOKEN}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
        return json.loads(payload) if payload else {}

    def spawn_target(self, label: str, network_id: str | None = None) -> str:
        """Create a VM through the API, making a network for it if needed."""
        if network_id is None:
            session = self.api(
                "POST",
                "/api/v1/playground/networks",
                {"name": "labnet", "subnet_cidr": "10.42.2.0/24", "isolated": True},
            )
            network_id = session["networks"][0]["id"]
        self.api(
            "POST",
            "/api/v1/playground/vms",
            {
                "label": label,
                "image_id": "ubuntu-22-04-default",
                "role": "target",
                "network_ids": [network_id],
                "disk_gb": 20,
            },
        )
        return str(network_id)

    def capture_ram(self, label: str) -> None:
        """Capture RAM from `label` through the Playground's own control."""
        self.page.set_viewport_size({"width": 1600, "height": 950})
        self.page.goto(f"{self.base_url}/playground")
        node = self.page.locator('.pg-v2-map-node[data-type="vm"]', has_text=label).first
        expect(node).to_have_attribute("data-state", "running", timeout=15_000)
        node.click()
        self.page.locator("#pg-detail-panel").get_by_role("button", name="Capture RAM").click()
        expect(self.page.locator("#stream-log")).to_contain_text("completed", timeout=60_000)

    def open(self) -> None:
        self.page.goto(f"{self.base_url}/volatility")

    def state_capture_context(self, note: str = "e2e verified context") -> None:
        """Fill the workbench's analysis-context form for the selected dump.

        A Playground capture arrives without a verified OS context, so the
        workbench refuses to run until the operator states one. This is that
        step, driven through the real form.
        """
        form = self.page.locator("#analysis-context-form")
        expect(form).to_be_visible()
        self.page.locator("#context-kernel").fill(self.KERNEL_RELEASE)
        self.page.locator("#context-architecture").fill("x86_64")
        self.page.locator("#context-note").fill(note)
        self.page.locator("#context-verified").check()
        self.page.locator("#context-submit").click()
        expect(form).to_be_hidden(timeout=15_000)

    def dump_rows(self) -> list[dict]:
        return list(self.api("GET", "/api/v1/volatility/dumps")["dumps"])

    def isf_bytes(self) -> bytes:
        """A minimal valid Volatility 3 Linux ISF for KERNEL_RELEASE.

        `metadata.linux` plus a non-empty `symbols` object is what the upload
        validates; the kernel release is what it matches against the stated
        capture context. Nothing reads the symbols themselves.
        """
        return json.dumps(
            {
                "metadata": {
                    "linux": {"kernel_release": self.KERNEL_RELEASE, "architecture": "x86_64"},
                    "producer": {"name": "astinuslab-e2e", "version": "1"},
                    "format": "6.2.0",
                },
                "symbols": {"init_task": {"address": 4096}},
                "base_types": {},
                "user_types": {},
                "enums": {},
            }
        ).encode("utf-8")

    def upload_isf(self, job_id: str) -> dict:
        """Supply symbols for the analysis job through the real operator-override route."""
        payload = self.isf_bytes()
        boundary = f"----astinuslab-e2e-{uuid.uuid4().hex}"
        body = (
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; filename="isf.json"\r\n'
                "Content-Type: application/json\r\n\r\n"
            ).encode()
            + payload
            + f"\r\n--{boundary}--\r\n".encode()
        )
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/jobs/{job_id}/isf/upload",
            data=body,
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Cookie": f"astinuslab_csrf={self.CSRF_TOKEN}",
                "X-CSRF-Token": self.CSRF_TOKEN,
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return dict(json.loads(response.read().decode("utf-8") or "{}"))

    def prepare(self, label: str = "target-A") -> dict:
        """All four steps. Returns the dump row the workbench is now showing."""
        self.spawn_target(label)
        self.capture_ram(label)
        self.open()
        self.state_capture_context()
        row = self.dump_rows()[0]
        self.upload_isf(str(row["job_id"]))
        return row


@pytest.fixture()
def analysis_workbench(page: Page):
    """Factory: the base URL differs per tier, the setup sequence does not."""

    def _workbench(base_url: str, on: Page | None = None) -> AnalysisWorkbench:
        return AnalysisWorkbench(base_url, on or page)

    return _workbench


@pytest.fixture()
def analysis_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, object]]:
    """A Playground backend with an analysis tool configured.

    Every other tier pins the Volatility toolchain absent, because "no tool is
    configured" is the state a first-time operator meets and the tier must not
    read the developer's shell. This one points [vol3].volatility_bin at
    fixtures/fake_vol3.py so a plugin run can complete. No product code changes
    for it: the whole real pipeline runs and only the framework that would read
    the memory image is absent. See that file for what keeps it honest.

    One server per test, like `disposable_server`. A run leaves state that
    truncating tables does not undo -- the uploaded ISF in the symbol cache, and
    the retained run output under storage/dumps -- so sharing one backend would
    let a test read a neighbour's evidence, which is the exact confusion these
    tests exist to rule out.
    """
    assert FAKE_VOL3_BIN.is_file(), FAKE_VOL3_BIN
    with _running_server(tmp_path_factory.mktemp("analysis-e2e"), volatility_bin=FAKE_VOL3_BIN) as server:
        yield server


@pytest.fixture()
def analysis_base_url(analysis_server: dict[str, object]) -> str:
    return str(analysis_server["base_url"])


@pytest.fixture()
def failing_analysis_base_url(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    """`analysis_base_url`, with the stand-in told to fail in a stated way.

    Parametrized indirectly with one of `fixtures/fake_vol3.py`'s modes:

        @pytest.mark.parametrize("failing_analysis_base_url", ["exit_nonzero"], indirect=True)

    The mode reaches the tool because the plugin runner launches it as a
    subprocess of the server, which this fixture starts with the variable set.
    The product neither reads it nor knows it exists -- the whole point of a
    stand-in is that the failure arrives through the same subprocess boundary a
    real Volatility would fail through.

    One server per test, for `analysis_server`'s reasons and one more: a failed
    run leaves retained stdout, stderr and a failure record on disk, and a test
    that read a neighbour's would be asserting against the wrong failure.
    """
    mode = str(request.param)
    assert FAKE_VOL3_BIN.is_file(), FAKE_VOL3_BIN
    with _running_server(
        tmp_path_factory.mktemp("failing-analysis-e2e"),
        volatility_bin=FAKE_VOL3_BIN,
        vol3_mode=mode,
    ) as server:
        yield str(server["base_url"])


@pytest.fixture()
def fake_vol3_bin() -> Path:
    """The stand-in the analysis tier runs, so a test can assert it was the one used.

    A fixture rather than an importable constant: the topology contract allows
    test modules to share code only through conftest fixtures and the four
    `tests.python._support` modules.
    """
    return FAKE_VOL3_BIN


@pytest.fixture()
def playground_base_url(playground_server: dict[str, object]) -> Iterator[str]:
    db_path = playground_server["db_path"]
    assert isinstance(db_path, Path)
    _truncate_playground_tables(db_path)
    yield str(playground_server["base_url"])
    _truncate_playground_tables(db_path)
