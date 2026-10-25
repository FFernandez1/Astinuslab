from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

import tests.python._support.helpers as test_support


def _os_release(*, distro_id: str | None = "ubuntu", version_id: str | None = "24.04") -> str:
    lines = ['PRETTY_NAME="Ubuntu 24.04.3 LTS"']
    if distro_id is not None:
        lines.append(f'ID="{distro_id}"')
    if version_id is not None:
        lines.append(f'VERSION_ID="{version_id}"')
    return "\n".join(lines) + "\n"


@pytest.fixture
def run_guard(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    def invoke(
        *,
        machine: str = "x86_64",
        os_release: str = _os_release(),
        kvm_exists: bool = True,
        kvm_accessible: bool = True,
    ) -> None:
        monkeypatch.setattr(test_support.platform, "machine", lambda: machine)
        monkeypatch.setattr(test_support.Path, "read_text", lambda _path, *, encoding: os_release)
        monkeypatch.setattr(test_support.Path, "exists", lambda path: path == Path("/dev/kvm") and kvm_exists)
        monkeypatch.setattr(
            test_support.os,
            "access",
            lambda path, mode: path == Path("/dev/kvm") and mode == os.R_OK | os.W_OK and kvm_accessible,
        )
        test_support.assert_fresh_ubuntu_kvm()

    return invoke


@pytest.mark.parametrize("machine", ["x86_64", "AMD64"])
def test_guard_accepts_exact_ubuntu_2404_on_supported_arch_aliases(
    run_guard: Callable[..., None], machine: str
) -> None:
    run_guard(machine=machine)


@pytest.mark.parametrize(
    "version_id",
    ["24", "24.00", "24.10", "24.04.1", "26.04", "not-a-version", None],
)
def test_guard_rejects_every_version_other_than_exact_2404(
    run_guard: Callable[..., None], version_id: str | None
) -> None:
    with pytest.raises(AssertionError, match=r"Ubuntu 24\.04 LTS"):
        run_guard(os_release=_os_release(version_id=version_id))


@pytest.mark.parametrize(
    "os_release",
    [
        _os_release(distro_id="debian"),
        'ID="linuxmint"\nID_LIKE="ubuntu debian"\nVERSION_ID="24.04"\n',
        'ID_LIKE="ubuntu"\nVERSION_ID="24.04"\n',
    ],
)
def test_guard_rejects_non_ubuntu_and_id_like_impostors(run_guard: Callable[..., None], os_release: str) -> None:
    with pytest.raises(AssertionError, match=r"Ubuntu 24\.04 LTS"):
        run_guard(os_release=os_release)


def test_guard_preserves_os_release_read_error_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(test_support.platform, "machine", lambda: "x86_64")

    def fail_read(_path: Path, *, encoding: str) -> str:
        raise OSError("unreadable")

    monkeypatch.setattr(test_support.Path, "read_text", fail_read)
    with pytest.raises(AssertionError, match=r"Ubuntu 24\.04 LTS") as caught:
        test_support.assert_fresh_ubuntu_kvm()
    assert isinstance(caught.value.__cause__, OSError)


def test_guard_rejects_architecture_before_reading_os_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(test_support.platform, "machine", lambda: "aarch64")

    def unexpected_read(_path: Path, *, encoding: str) -> str:
        raise AssertionError("os-release must not be read for an unsupported architecture")

    monkeypatch.setattr(test_support.Path, "read_text", unexpected_read)
    with pytest.raises(AssertionError, match=r"Ubuntu 24\.04 LTS.*x86_64"):
        test_support.assert_fresh_ubuntu_kvm()


@pytest.mark.parametrize(
    ("kvm_exists", "kvm_accessible"),
    [(False, False), (True, False)],
)
def test_guard_preserves_readable_writable_kvm_requirement(
    run_guard: Callable[..., None], kvm_exists: bool, kvm_accessible: bool
) -> None:
    with pytest.raises(AssertionError, match=r"Ubuntu 24\.04 LTS.*KVM"):
        run_guard(kvm_exists=kvm_exists, kvm_accessible=kvm_accessible)
