"""Tests for the symbol provider abstraction."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astinuslab.forensics.symbols.base import FetchResult, KernelMetadata, SymbolProvider
from astinuslab.forensics.symbols.providers import (
    SUPPORTED_DISTROS,
    ArchProvider,
    DebianProvider,
    DebuginfodProvider,
    FedoraProvider,
    KaliProvider,
    OpenSUSEProvider,
    PrebuiltIsfProvider,
    RHELFamilyProvider,
    UbuntuProvider,
    get_provider,
    is_supported,
)
from tests.python._support.helpers import autospec_setattr


def _meta(distro: str, version: str = "22.04", kernel: str = "5.15.0-101-generic") -> KernelMetadata:
    return KernelMetadata(distro=distro, version=version, kernel_release=kernel)


class TestProviderSelection:
    def test_ubuntu_selects_ubuntu_provider(self):
        p = get_provider(_meta("ubuntu"))
        assert isinstance(p, UbuntuProvider)

    def test_debian_selects_debian_provider(self):
        p = get_provider(_meta("debian", "12"))
        assert isinstance(p, DebianProvider)

    def test_kali_selects_kali_provider(self):
        p = get_provider(_meta("kali", "2024.1"))
        assert isinstance(p, KaliProvider)

    def test_parrot_selects_kali_provider(self):
        p = get_provider(_meta("parrot", "6.0"))
        assert isinstance(p, KaliProvider)

    def test_fedora_selects_fedora_provider(self):
        p = get_provider(_meta("fedora", "39", "6.7.5-200.fc39.x86_64"))
        assert isinstance(p, FedoraProvider)

    def test_rhel_selects_rhel_family_provider(self):
        p = get_provider(_meta("rhel", "9.3", "5.14.0-362.8.1.el9_3.x86_64"))
        assert isinstance(p, RHELFamilyProvider)

    def test_rocky_selects_rhel_family_provider(self):
        p = get_provider(_meta("rocky", "9.3"))
        assert isinstance(p, RHELFamilyProvider)

    def test_almalinux_selects_rhel_family_provider(self):
        p = get_provider(_meta("almalinux", "9.3"))
        assert isinstance(p, RHELFamilyProvider)

    def test_centos_selects_rhel_family_provider(self):
        p = get_provider(_meta("centos", "8"))
        assert isinstance(p, RHELFamilyProvider)

    def test_opensuse_selects_opensuse_provider(self):
        p = get_provider(_meta("opensuse-leap", "15.5"))
        assert isinstance(p, OpenSUSEProvider)

    def test_arch_selects_arch_provider(self):
        p = get_provider(_meta("arch"))
        assert isinstance(p, ArchProvider)

    def test_unsupported_distro_returns_unsupported(self):
        from astinuslab.forensics.symbols.providers import UnsupportedProvider

        p = get_provider(_meta("gentoo"))
        assert isinstance(p, UnsupportedProvider)


class TestPackageNames:
    def test_ubuntu_package_name(self):
        p = UbuntuProvider()
        name = p.package_name(_meta("ubuntu", "22.04", "5.15.0-101-generic"))
        assert "5.15.0-101-generic" in name
        assert "dbgsym" in name

    def test_debian_package_name(self):
        p = DebianProvider()
        name = p.package_name(_meta("debian", "12", "6.1.0-18-amd64"))
        assert "6.1.0-18-amd64" in name
        assert "dbgsym" in name

    def test_debian_does_not_contain_host_uname(self):
        """Debian package name must derive from metadata, not host uname."""
        import subprocess

        p = DebianProvider()
        # Get host uname -r
        try:
            host_kernel = subprocess.check_output(["uname", "-r"], text=True).strip()
        except Exception:
            host_kernel = "host-kernel-string"
        name = p.package_name(_meta("debian", "12", "6.1.0-18-amd64"))
        assert host_kernel not in name

    def test_fedora_package_name(self):
        p = FedoraProvider()
        name = p.package_name(_meta("fedora", "39", "6.7.5-200.fc39.x86_64"))
        assert "kernel-debuginfo" in name
        assert "6.7.5" in name

    def test_rhel_family_package_name(self):
        p = RHELFamilyProvider()
        name = p.package_name(_meta("rhel", "9.3", "5.14.0-362.8.1.el9_3.x86_64"))
        assert "kernel-debuginfo" in name

    def test_unsupported_distro_gives_clear_result(self):
        from astinuslab.forensics.symbols.providers import UnsupportedProvider

        p = UnsupportedProvider()
        assert p.supports(_meta("gentoo")) is True
        assert p.package_name(_meta("gentoo")) == ""


class TestSupportedDistros:
    def test_is_supported_ubuntu(self):
        assert is_supported("ubuntu") is True

    def test_is_supported_debian(self):
        assert is_supported("debian") is True

    def test_is_not_supported_gentoo(self):
        assert is_supported("gentoo") is False

    def test_supported_distros_constant_has_expected_members(self):
        expected = {"ubuntu", "debian", "fedora", "rhel", "rocky", "almalinux", "centos"}
        assert expected.issubset(SUPPORTED_DISTROS)


class TestProviderFetchIntegration:
    def test_symbol_provider_protocol_includes_fetch_vmlinux(self):
        assert "fetch_vmlinux" in SymbolProvider.__dict__

    @pytest.mark.parametrize(
        "provider",
        [
            UbuntuProvider(),
            KaliProvider(),
            DebianProvider(),
            FedoraProvider(),
            RHELFamilyProvider(),
            OpenSUSEProvider(),
            ArchProvider(),
        ],
    )
    def test_concrete_providers_expose_fetch_vmlinux(self, provider):
        assert callable(provider.fetch_vmlinux)

    def test_vol3_fetch_path_uses_selected_symbol_provider(self, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        provider = Mock()
        provider.fetch_vmlinux.return_value = FetchResult(
            success=True,
            vmlinux_path=tmp_path / "vmlinux",
            system_map_path=tmp_path / "System.map",
        )
        autospec_setattr(monkeypatch, v3, "get_provider", lambda metadata: provider)
        monkeypatch.setattr(v3.subprocess, "run", Mock(side_effect=AssertionError("bypassed symbols provider")))

        vmlinux_path, system_map_path = v3._run_fetch_vmlinux(
            tmp_path / "fetch.log",
            tmp_path / "work",
            distro="kali",
            version="2024.1",
            kernel_release="6.6.9-amd64",
        )

        provider.fetch_vmlinux.assert_called_once()
        metadata, work_dir = provider.fetch_vmlinux.call_args.args
        assert metadata == KernelMetadata(
            distro="kali",
            version="2024.1",
            kernel_release="6.6.9-amd64",
            architecture="x86_64",
            build_id=None,
        )
        assert work_dir == tmp_path / "work"
        assert vmlinux_path == str(tmp_path / "vmlinux")
        assert system_map_path == str(tmp_path / "System.map")

    def test_vol3_fetch_path_logs_structured_provider_failure(self, monkeypatch, tmp_path):
        import astinuslab.forensics.vol3_support as v3

        provider = Mock()
        provider.fetch_vmlinux.return_value = FetchResult(
            success=False,
            error="full stderr\nERROR: download interrupted\n",
            exit_code=3,
            phase="fetch_vmlinux",
            log_excerpt="ERROR: download interrupted",
        )
        autospec_setattr(monkeypatch, v3, "get_provider", lambda metadata: provider)

        log_path = tmp_path / "fetch.log"
        vmlinux_path, system_map_path = v3._run_fetch_vmlinux(
            log_path,
            tmp_path / "work",
            distro="ubuntu",
            version="24.04",
            kernel_release="6.8.0-101-generic",
        )

        assert vmlinux_path is None
        assert system_map_path is None
        log = log_path.read_text(encoding="utf-8")
        assert "phase=fetch_vmlinux" in log
        assert "exit_code=3" in log
        assert "ERROR: download interrupted" in log

    def test_script_provider_returns_structured_error_details(self, monkeypatch, tmp_path):
        import astinuslab.forensics.symbols.providers as providers

        monkeypatch.setattr(
            providers,
            "settings",
            SimpleNamespace(base_dir=tmp_path, data_dir=tmp_path, vol3_symbols_path=tmp_path / "symbols"),
            raising=False,
        )

        def fake_run(*_args, **_kwargs):
            return SimpleNamespace(
                returncode=3,
                stdout=b"",
                stderr=b"line one\nline two\nERROR: package unavailable\n",
            )

        monkeypatch.setattr(providers.subprocess, "run", fake_run)

        result = UbuntuProvider().fetch_vmlinux(_meta("ubuntu"), tmp_path / "work")

        assert result.success is False
        assert result.exit_code == 3
        assert result.phase == "fetch_vmlinux"
        assert result.log_excerpt is not None
        assert "ERROR: package unavailable" in result.log_excerpt

    def test_script_provider_uses_configured_fetch_timeout(self, monkeypatch, tmp_path):
        import astinuslab.forensics.symbols.providers as providers

        calls = []
        monkeypatch.setattr(
            providers,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                vol3_symbols_path=tmp_path / "symbols",
                vol3_fetch_timeout_seconds=1800,
            ),
            raising=False,
        )

        def fake_run(*_args, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                returncode=0,
                stdout=f"vmlinux_path={tmp_path / 'vmlinux'}\n".encode(),
                stderr=b"",
            )

        monkeypatch.setattr(providers.subprocess, "run", fake_run)

        result = UbuntuProvider().fetch_vmlinux(_meta("ubuntu"), tmp_path / "work")

        assert result.success is True
        assert calls[0]["timeout"] == 1800

    def test_script_provider_converts_fetch_timeout_to_result(self, monkeypatch, tmp_path):
        import astinuslab.forensics.symbols.providers as providers

        monkeypatch.setattr(
            providers,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                vol3_symbols_path=tmp_path / "symbols",
                vol3_fetch_timeout_seconds=12,
            ),
            raising=False,
        )

        def fake_run(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd=["fetch_vmlinux.sh"], timeout=12)

        monkeypatch.setattr(providers.subprocess, "run", fake_run)

        result = UbuntuProvider().fetch_vmlinux(_meta("ubuntu"), tmp_path / "work")

        assert result.success is False
        assert result.exit_code is None
        assert result.phase == "timeout"
        assert result.error is not None
        assert "timed out after 12s" in result.error

    def test_debuginfod_success_requires_output_file(self, monkeypatch, tmp_path):
        import astinuslab.forensics.symbols.providers as providers

        monkeypatch.setattr(
            providers,
            "settings",
            SimpleNamespace(
                vol3_debuginfod_min_free_bytes=1,
                vol3_debuginfod_urls=["https://debuginfod.example/"],
                vol3_provider_timeouts={"debuginfod": 900},
            ),
            raising=False,
        )
        autospec_setattr(monkeypatch, providers.shutil, "disk_usage", lambda _path: SimpleNamespace(free=10))
        autospec_setattr(
            monkeypatch,
            providers.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
        )

        result = DebuginfodProvider().fetch_vmlinux(
            KernelMetadata("ubuntu", "24.04", "6.8.0-101-generic", build_id="abc"),
            tmp_path / "work",
        )

        assert result.success is False
        assert "no vmlinux file" in (result.error or "")

    def test_debuginfod_passes_output_flag_and_returns_dest(self, monkeypatch, tmp_path):
        import astinuslab.forensics.symbols.providers as providers

        monkeypatch.setattr(
            providers,
            "settings",
            SimpleNamespace(
                vol3_debuginfod_min_free_bytes=1,
                vol3_debuginfod_urls=["https://debuginfod.example/"],
                vol3_provider_timeouts={"debuginfod": 900},
            ),
            raising=False,
        )
        autospec_setattr(monkeypatch, providers.shutil, "disk_usage", lambda _path: SimpleNamespace(free=10))
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            dest = Path(cmd[cmd.index("-o") + 1])
            dest.write_bytes(b"\x7fELF")
            return SimpleNamespace(returncode=0, stdout=b"some progress line\n", stderr=b"")

        monkeypatch.setattr(providers.subprocess, "run", fake_run)

        result = DebuginfodProvider().fetch_vmlinux(
            KernelMetadata("ubuntu", "24.04", "6.8.0-101-generic", build_id="abc"),
            tmp_path / "work",
        )

        assert result.success is True
        assert calls[0][:3] == ["debuginfod-find", "debuginfo", "abc"]
        assert "-o" in calls[0]
        assert result.vmlinux_path == tmp_path / "work" / "vmlinux-debuginfod"
        assert result.vmlinux_path is not None
        assert result.vmlinux_path.read_bytes() == b"\x7fELF"


class TestPrebuiltIsfProvider:
    def test_prebuilt_rejects_http_mirror(self, monkeypatch, tmp_path):
        import astinuslab.forensics.symbols.providers as providers

        monkeypatch.setattr(
            providers,
            "settings",
            SimpleNamespace(
                vol3_isf_mirror_urls=["http://insecure.example/"],
                vol3_prebuilt_max_compressed_bytes=64 * 1024 * 1024,
                vol3_prebuilt_max_decompressed_bytes=1024 * 1024 * 1024,
            ),
            raising=False,
        )

        result = PrebuiltIsfProvider().fetch_vmlinux(
            KernelMetadata("ubuntu", "24.04", "6.8.0-101-generic", build_id="abc"),
            tmp_path,
        )

        assert result.success is False
        assert result.error is not None
        assert "https" in result.error.lower()

    def test_prebuilt_rejects_oversized_response(self, monkeypatch, tmp_path):
        import astinuslab.forensics.symbols.providers as providers

        class Response:
            headers = {"Content-Length": "10"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        monkeypatch.setattr(
            providers,
            "settings",
            SimpleNamespace(
                vol3_isf_mirror_urls=["https://symbols.example/"],
                vol3_prebuilt_max_compressed_bytes=1,
                vol3_prebuilt_max_decompressed_bytes=1024 * 1024 * 1024,
            ),
            raising=False,
        )
        autospec_setattr(monkeypatch, providers.urllib.request, "urlopen", lambda *args, **kwargs: Response())

        result = PrebuiltIsfProvider().fetch_vmlinux(
            KernelMetadata("ubuntu", "24.04", "6.8.0-101-generic", build_id="abc"),
            tmp_path,
        )

        assert result.success is False
        assert "exceeds" in (result.error or "").lower()
        assert not list(tmp_path.glob("*.json"))


class TestDebuginfodProvider:
    def test_debuginfod_rejects_insufficient_free_space(self, monkeypatch, tmp_path):
        import astinuslab.forensics.symbols.providers as providers

        monkeypatch.setattr(
            providers,
            "settings",
            SimpleNamespace(
                vol3_debuginfod_min_free_bytes=100,
                vol3_debuginfod_urls=["https://debuginfod.example/"],
                vol3_provider_timeouts={"debuginfod": 900},
            ),
            raising=False,
        )
        autospec_setattr(monkeypatch, providers.shutil, "disk_usage", lambda _path: SimpleNamespace(free=99))

        result = DebuginfodProvider().fetch_vmlinux(
            KernelMetadata("ubuntu", "24.04", "6.8.0-101-generic", build_id="abc"),
            tmp_path,
        )

        assert result.success is False
        assert result.phase == "precheck"
        assert "insufficient disk space" in (result.error or "")

    def test_debuginfod_empty_urls_without_system_default_skips(self, monkeypatch, tmp_path):
        import astinuslab.forensics.symbols.providers as providers

        monkeypatch.delenv("DEBUGINFOD_URLS", raising=False)
        monkeypatch.setattr(
            providers,
            "settings",
            SimpleNamespace(
                vol3_debuginfod_min_free_bytes=1,
                vol3_debuginfod_urls=[],
                vol3_provider_timeouts={"debuginfod": 900},
            ),
            raising=False,
        )
        autospec_setattr(monkeypatch, providers.shutil, "disk_usage", lambda _path: SimpleNamespace(free=10))
        monkeypatch.setattr(providers.subprocess, "run", Mock(side_effect=AssertionError("should skip")))

        result = DebuginfodProvider().fetch_vmlinux(
            KernelMetadata("ubuntu", "24.04", "6.8.0-101-generic", build_id="abc"),
            tmp_path,
        )

        assert result.success is False
        assert "no debuginfod urls configured" in (result.error or "").lower()

    def test_debuginfod_uses_provider_timeout_and_stderr_tail(self, monkeypatch, tmp_path):
        import astinuslab.forensics.symbols.providers as providers

        calls = []
        monkeypatch.setattr(
            providers,
            "settings",
            SimpleNamespace(
                vol3_debuginfod_min_free_bytes=1,
                vol3_debuginfod_urls=["https://debuginfod.example/"],
                vol3_provider_timeouts={"debuginfod": 7},
            ),
            raising=False,
        )
        autospec_setattr(monkeypatch, providers.shutil, "disk_usage", lambda _path: SimpleNamespace(free=10))

        def fake_run(*_args, **kwargs):
            calls.append(kwargs)
            out = tmp_path / "vmlinux-debuginfod"
            out.write_bytes(b"elf")
            return SimpleNamespace(
                returncode=0,
                stdout=(str(out) + "\n").encode("utf-8"),
                stderr=b"one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\n",
            )

        monkeypatch.setattr(providers.subprocess, "run", fake_run)

        result = DebuginfodProvider().fetch_vmlinux(
            KernelMetadata("ubuntu", "24.04", "6.8.0-101-generic", build_id="abc"),
            tmp_path,
        )

        assert result.success is True
        assert result.vmlinux_path == tmp_path / "vmlinux-debuginfod"
        assert calls[0]["timeout"] == 7
        assert calls[0]["env"]["DEBUGINFOD_URLS"] == "https://debuginfod.example/"
        assert "one" not in (result.log_excerpt or "")
        assert "nine" in (result.log_excerpt or "")


class TestFetchVmlinuxScriptScope:
    def _script(self) -> str:
        return (Path(__file__).resolve().parents[3] / "vol3" / "scripts" / "fetch_vmlinux.sh").read_text(
            encoding="utf-8"
        )

    def test_script_dispatches_all_supported_symbol_distros(self):
        script = self._script()

        for distro in ["kali", "parrot", "opensuse*", "arch*"]:
            assert distro in script

    def test_script_is_x86_64_only(self):
        script = self._script()

        assert '[[ "$ARCH" == "x86_64" ]]' in script
        for non_x86 in ["aarch64", "arm64", "armv7", "armhf", "i*86", "i386"]:
            assert non_x86 not in script

    def test_guest_build_id_injected_into_env_when_set(self, monkeypatch, tmp_path):
        """ScriptFetchMixin must inject GUEST_BUILD_ID when metadata.build_id is present."""
        import astinuslab.forensics.symbols.providers as providers

        monkeypatch.setattr(
            providers,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                vol3_symbols_path=tmp_path / "symbols",
                vol3_fetch_timeout_seconds=60,
            ),
            raising=False,
        )

        captured_env = {}

        def fake_run(*_args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return SimpleNamespace(
                returncode=0,
                stdout=f"vmlinux_path={tmp_path / 'vmlinux'}\n".encode(),
                stderr=b"",
            )

        monkeypatch.setattr(providers.subprocess, "run", fake_run)

        meta = KernelMetadata(
            distro="arch",
            version="",
            kernel_release="6.6.1-arch1",
            build_id="deadbeefcafe1234",
        )
        ArchProvider().fetch_vmlinux(meta, tmp_path / "work")

        assert captured_env.get("GUEST_BUILD_ID") == "deadbeefcafe1234"

    def test_guest_build_id_not_injected_when_absent(self, monkeypatch, tmp_path):
        """GUEST_BUILD_ID must not appear in env when metadata.build_id is None."""
        import astinuslab.forensics.symbols.providers as providers

        monkeypatch.setattr(
            providers,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                vol3_symbols_path=tmp_path / "symbols",
                vol3_fetch_timeout_seconds=60,
            ),
            raising=False,
        )

        captured_env = {}

        def fake_run(*_args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return SimpleNamespace(
                returncode=0,
                stdout=f"vmlinux_path={tmp_path / 'vmlinux'}\n".encode(),
                stderr=b"",
            )

        monkeypatch.setattr(providers.subprocess, "run", fake_run)

        meta = KernelMetadata(distro="ubuntu", version="22.04", kernel_release="5.15.0-101-generic")
        UbuntuProvider().fetch_vmlinux(meta, tmp_path / "work")

        assert "GUEST_BUILD_ID" not in captured_env

    def test_kali_and_parrot_dispatch_to_fetch_kali(self):
        """fetch_vmlinux.sh must have a kali | parrot dispatch case."""
        script = self._script()
        assert "kali | parrot" in script
        assert "_fetch_kali" in script

    def test_fetch_kali_function_present(self):
        """_fetch_kali must exist in fetch_vmlinux.sh."""
        script = self._script()
        assert "_fetch_kali()" in script
        assert "http.kali.org" in script
        assert "archive.parrotsec.org" in script
