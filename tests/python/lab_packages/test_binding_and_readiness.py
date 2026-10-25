"""Boot-layer binding and readiness-probe contract.

Binding is the moment recipient-supplied bytes are about to become a running
guest's backing file, so it re-checks everything even though import already
verified a digest: the file is regular and unlinked, its bytes still hash to
the declared digest, and ``qemu-img`` reports a plain qcow2 with no external
backing or data-file chain pointing at the host. Readiness probes are bounded
and guest-scoped; there is no host shell hook to reach.
"""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from astinuslab.lab_packages import image_binding
from astinuslab.lab_packages.content_store import ContentStore, ContentStoreError
from astinuslab.lab_packages.experience_runner import ReadinessError, probe_ready
from astinuslab.lab_packages.image_binding import (
    ImageBindingError,
    immutable_boot_layer_path,
    validate_bound_immutable_layer,
)
from astinuslab.lab_packages.models import AssetReference, LabReadinessProbe, LabVM

pytestmark = pytest.mark.unit

LAYER = b"qcow2 layer bytes"
LAYER_DIGEST = hashlib.sha256(LAYER).hexdigest()


def _vm(*, boot_layer: AssetReference | None) -> LabVM:
    return LabVM(
        id="target",
        role="target",
        image_id="ubuntu-24.04",
        vcpus=2,
        memory_mb=2048,
        disk_gb=10,
        network_ids=("isolated",),
        boot_layer=boot_layer,
    )


def _reference() -> AssetReference:
    return AssetReference(
        sha256=LAYER_DIGEST,
        size_bytes=len(LAYER),
        media_type="application/x-qcow2",
        license="CC-BY-4.0",
    )


@pytest.fixture()
def store(tmp_path: Path) -> ContentStore:
    return ContentStore(tmp_path / "content", max_bytes=4096)


@pytest.fixture()
def qcow2_ok(monkeypatch):
    """Report a plain qcow2 with no external chain."""

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"format": "qcow2", "virtual-size": 1024}), stderr=""
        )

    monkeypatch.setattr(image_binding.subprocess, "run", fake_run)
    monkeypatch.setattr(image_binding, "_apply_qemu_acl", lambda _path: None)


# --- immutable_boot_layer_path -------------------------------------------------


def test_a_vm_without_a_boot_layer_cannot_be_bound(store):
    with pytest.raises(ImageBindingError, match="missing-clean-layer"):
        immutable_boot_layer_path(_vm(boot_layer=None), store)


def test_a_layer_absent_from_the_store_cannot_be_bound(store):
    with pytest.raises(ImageBindingError, match="missing-clean-layer"):
        immutable_boot_layer_path(_vm(boot_layer=_reference()), store)


def test_a_store_failure_is_reported_as_a_missing_layer(store, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise ContentStoreError("cache entry is a symlink")

    monkeypatch.setattr(store, "has", refuse)

    with pytest.raises(ImageBindingError, match="missing-clean-layer"):
        immutable_boot_layer_path(_vm(boot_layer=_reference()), store)


def test_a_cached_layer_binds_to_its_store_path(store, qcow2_ok):
    store.put(LAYER, sha256=LAYER_DIGEST, size_bytes=len(LAYER))

    bound = immutable_boot_layer_path(_vm(boot_layer=_reference()), store)

    assert bound == store.path_for(LAYER_DIGEST)


# --- validate_bound_immutable_layer --------------------------------------------


def _validate(path: Path, **overrides):
    kwargs: dict = {"sha256": LAYER_DIGEST, "size_bytes": len(LAYER), "virtual_size_limit": 1 << 40}
    kwargs.update(overrides)
    return validate_bound_immutable_layer(path, **kwargs)


def test_a_matching_layer_validates(tmp_path: Path, qcow2_ok):
    layer = tmp_path / "layer.qcow2"
    layer.write_bytes(LAYER)

    assert _validate(layer) == layer


@pytest.mark.parametrize("digest", ["", "a" * 63, "z" * 64, 12345, None])
def test_a_malformed_declared_digest_is_refused(tmp_path: Path, digest):
    layer = tmp_path / "layer.qcow2"
    layer.write_bytes(LAYER)

    with pytest.raises(ImageBindingError, match="unsafe-boot-layer"):
        _validate(layer, sha256=digest)


@pytest.mark.parametrize("size", [0, -1, True, 1.5, None])
def test_a_malformed_declared_size_is_refused(tmp_path: Path, size):
    layer = tmp_path / "layer.qcow2"
    layer.write_bytes(LAYER)

    with pytest.raises(ImageBindingError, match="unsafe-boot-layer"):
        _validate(layer, size_bytes=size)


def test_a_missing_layer_is_refused(tmp_path: Path):
    with pytest.raises(ImageBindingError, match="unsafe-boot-layer"):
        _validate(tmp_path / "absent.qcow2")


def test_a_symlinked_layer_is_refused(tmp_path: Path):
    (tmp_path / "real.qcow2").write_bytes(LAYER)
    link = tmp_path / "link.qcow2"
    link.symlink_to(tmp_path / "real.qcow2")

    with pytest.raises(ImageBindingError, match="unsafe-boot-layer"):
        _validate(link)


def test_a_layer_of_another_size_is_refused(tmp_path: Path):
    layer = tmp_path / "layer.qcow2"
    layer.write_bytes(LAYER + b"appended")

    with pytest.raises(ImageBindingError, match="unsafe-boot-layer"):
        _validate(layer)


def test_a_layer_whose_bytes_do_not_hash_to_its_digest_is_refused(tmp_path: Path):
    layer = tmp_path / "layer.qcow2"
    layer.write_bytes(b"x" * len(LAYER))

    with pytest.raises(ImageBindingError, match="unsafe-boot-layer"):
        _validate(layer)


def test_an_unreadable_qemu_img_probe_is_refused(tmp_path: Path, monkeypatch):
    layer = tmp_path / "layer.qcow2"
    layer.write_bytes(LAYER)

    def refuse(*_args, **_kwargs):
        raise OSError("qemu-img is not installed")

    monkeypatch.setattr(image_binding.subprocess, "run", refuse)

    with pytest.raises(ImageBindingError, match="unsafe-boot-layer"):
        _validate(layer)


def test_a_qemu_img_probe_that_times_out_is_refused(tmp_path: Path, monkeypatch):
    layer = tmp_path / "layer.qcow2"
    layer.write_bytes(LAYER)

    def refuse(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="qemu-img", timeout=10)

    monkeypatch.setattr(image_binding.subprocess, "run", refuse)

    with pytest.raises(ImageBindingError, match="unsafe-boot-layer"):
        _validate(layer)


@pytest.mark.parametrize(
    "info",
    [
        '{"format": "raw", "virtual-size": 1024}',
        '{"format": "qcow2"}',
        '{"format": "qcow2", "virtual-size": 0}',
        '{"format": "qcow2", "virtual-size": -1}',
        '{"format": "qcow2", "virtual-size": true}',
        '{"format": "qcow2", "virtual-size": "1024"}',
        "[]",
        "not json",
    ],
)
def test_a_layer_qemu_img_will_not_vouch_for_is_refused(tmp_path: Path, monkeypatch, info):
    layer = tmp_path / "layer.qcow2"
    layer.write_bytes(LAYER)

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=info, stderr="")

    monkeypatch.setattr(image_binding.subprocess, "run", fake_run)

    with pytest.raises(ImageBindingError, match="unsafe-boot-layer"):
        _validate(layer)


def test_a_layer_larger_than_the_declared_ceiling_is_refused(tmp_path: Path, monkeypatch):
    layer = tmp_path / "layer.qcow2"
    layer.write_bytes(LAYER)

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"format": "qcow2", "virtual-size": 4096}), stderr=""
        )

    monkeypatch.setattr(image_binding.subprocess, "run", fake_run)

    with pytest.raises(ImageBindingError, match="unsafe-boot-layer"):
        _validate(layer, virtual_size_limit=1024)


# --- readiness probes ----------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "tcp"},
        {"kind": "http", "path": "/health"},
        {"kind": "http", "port": 8080},
    ],
)
def test_the_model_refuses_an_incomplete_probe(fields):
    """A package cannot declare a probe that names no endpoint."""

    with pytest.raises(ValueError):
        LabReadinessProbe(timeout_seconds=1, **fields)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"kind": "tcp", "port": None}, "lacks a port"),
        ({"kind": "http", "port": None, "path": "/health"}, "lacks a port or path"),
        ({"kind": "http", "port": 8080, "path": None}, "lacks a port or path"),
    ],
)
def test_the_runner_re_checks_the_probe_it_is_handed(fields, message):
    """Defence in depth: the runner does not trust the model's validation."""

    probe = LabReadinessProbe.model_construct(timeout_seconds=1, **fields)

    with pytest.raises(ReadinessError, match=message):
        probe_ready("127.0.0.1", probe)


def test_an_unsupported_probe_kind_is_refused():
    probe = LabReadinessProbe.model_construct(kind="icmp", port=1, path="/x", timeout_seconds=1)

    with pytest.raises(ReadinessError, match="unsupported readiness probe"):
        probe_ready("127.0.0.1", probe)


def _serving(status: int):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's interface
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"")

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_an_http_probe_accepts_a_successful_response():
    server = _serving(200)
    try:
        probe_ready(
            "127.0.0.1",
            LabReadinessProbe(kind="http", port=server.server_address[1], path="/health", timeout_seconds=5),
        )
    finally:
        server.shutdown()


def test_an_http_probe_rejects_a_server_error():
    server = _serving(500)
    try:
        with pytest.raises((ReadinessError, OSError)):
            probe_ready(
                "127.0.0.1",
                LabReadinessProbe(kind="http", port=server.server_address[1], path="/health", timeout_seconds=5),
            )
    finally:
        server.shutdown()


def test_a_tcp_probe_succeeds_against_a_listening_port():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    accepted = threading.Thread(target=lambda: listener.accept(), daemon=True)
    accepted.start()
    try:
        probe_ready("127.0.0.1", LabReadinessProbe(kind="tcp", port=port, timeout_seconds=5))
    finally:
        listener.close()


def test_a_tcp_probe_fails_against_a_closed_port():
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()

    with pytest.raises(OSError):
        probe_ready("127.0.0.1", LabReadinessProbe(kind="tcp", port=port, timeout_seconds=1))
