from __future__ import annotations

import lzma
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

from ...core.config import settings as _app_settings
from .base import FetchResult, KernelMetadata

# Test seam: tests monkeypatch this module attribute with a SimpleNamespace.
# Production code must resolve settings via _active_settings(), never read
# this name or _app_settings directly.
settings = None


def _active_settings():
    return settings if settings is not None else _app_settings


_DEFAULT_DEBUGINFOD_URLS = {
    "fedora": ["https://debuginfod.fedoraproject.org"],
    "arch": ["https://debuginfod.archlinux.org"],
    "archlinux": ["https://debuginfod.archlinux.org"],
    "opensuse": ["https://debuginfod.opensuse.org"],
    "opensuse-leap": ["https://debuginfod.opensuse.org"],
    "opensuse-tumbleweed": ["https://debuginfod.opensuse.org"],
}


SUPPORTED_DISTROS: frozenset[str] = frozenset(
    {
        "ubuntu",
        "debian",
        "kali",
        "parrot",
        "fedora",
        "rhel",
        "centos",
        "rocky",
        "almalinux",
        "ol",
        "opensuse",
        "opensuse-leap",
        "opensuse-tumbleweed",
        "sles",
        "suse",
        "arch",
        "archlinux",
        "manjaro",
    }
)


def is_supported(distro: str) -> bool:
    return distro.lower() in SUPPORTED_DISTROS


class ScriptFetchMixin:
    def _log_excerpt(self, text: str | None, *, max_lines: int = 8) -> str | None:
        if not text:
            return None
        lines = [line for line in text.splitlines() if line.strip()]
        return "\n".join(lines[-max_lines:]) if lines else None

    def fetch_vmlinux(self, metadata: KernelMetadata, work_dir: Path) -> FetchResult:
        active_settings = _active_settings()

        cmd = [
            "bash",
            str(active_settings.base_dir / "vol3" / "scripts" / "fetch_vmlinux.sh"),
            "--work-dir",
            str(work_dir),
            "--symbols-dir",
            str(active_settings.vol3_symbols_path),
            "--distro",
            metadata.distro,
            "--version",
            metadata.version,
            "--kernel-release",
            metadata.kernel_release,
            "--arch",
            metadata.architecture,
        ]
        env = os.environ.copy()
        env["SYMBOL_PACKAGE_NAME"] = self.package_name(metadata)
        if metadata.build_id:
            env["GUEST_BUILD_ID"] = metadata.build_id
        timeout_seconds = getattr(active_settings, "vol3_fetch_timeout_seconds", 1800)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                env=env,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return FetchResult(
                success=False,
                error=f"fetch_vmlinux.sh timed out after {timeout_seconds}s",
                phase="timeout",
                log_excerpt=f"fetch_vmlinux.sh timed out after {timeout_seconds}s",
            )
        output = result.stdout.decode("utf-8", errors="replace")
        error = result.stderr.decode("utf-8", errors="replace") or None
        excerpt = self._log_excerpt(error)

        if result.returncode == 2:
            return FetchResult(
                success=False,
                error=error,
                unsupported=True,
                exit_code=result.returncode,
                phase="fetch_vmlinux",
                log_excerpt=excerpt,
            )
        if result.returncode != 0:
            return FetchResult(
                success=False,
                error=error,
                exit_code=result.returncode,
                phase="fetch_vmlinux",
                log_excerpt=excerpt,
            )

        vmlinux_path: Path | None = None
        system_map_path: Path | None = None
        for line in output.splitlines():
            if line.startswith("vmlinux_path="):
                vmlinux_path = Path(line.split("=", 1)[1].strip())
            elif line.startswith("system_map_path="):
                system_map_path = Path(line.split("=", 1)[1].strip())

        if vmlinux_path is None:
            return FetchResult(
                success=False,
                error=error or "fetch_vmlinux.sh did not output vmlinux_path",
                exit_code=result.returncode,
                phase="parse_output",
                log_excerpt=excerpt,
            )
        return FetchResult(success=True, vmlinux_path=vmlinux_path, system_map_path=system_map_path)


class PrebuiltIsfProvider:
    name = "prebuilt_isf"

    def supports(self, metadata: KernelMetadata) -> bool:
        return bool(metadata.build_id)

    def package_name(self, metadata: KernelMetadata) -> str:
        return ""

    def fetch_url(self, metadata: KernelMetadata) -> str | None:
        urls = self._urls(metadata)
        return urls[0] if urls else None

    def _settings(self):
        return _active_settings()

    def _paths(self, metadata: KernelMetadata) -> list[str]:
        build_id = metadata.build_id or ""
        return [
            f"{metadata.architecture}/{metadata.kernel_release}/{build_id}.json.xz",
            f"{metadata.architecture}/{build_id}.json.xz",
        ]

    def _urls(self, metadata: KernelMetadata) -> list[str]:
        active_settings = self._settings()
        urls: list[str] = []
        for mirror in getattr(active_settings, "vol3_isf_mirror_urls", []):
            for path in self._paths(metadata):
                urls.append(urljoin(mirror.rstrip("/") + "/", path))
        return urls

    def _copy_capped(self, src, dst, max_bytes: int) -> int:
        total = 0
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                return total
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"response exceeds limit of {max_bytes} bytes")
            dst.write(chunk)

    def fetch_vmlinux(self, metadata: KernelMetadata, work_dir: Path) -> FetchResult:
        active_settings = self._settings()
        attempted_urls = self._urls(metadata)
        if not attempted_urls:
            return FetchResult(success=False, error="no prebuilt ISF mirrors configured", attempted_urls=[])
        for url in attempted_urls:
            if not (url.startswith("https://") or url.startswith("file://")):
                return FetchResult(
                    success=False,
                    error="prebuilt ISF mirrors must use https:// or file:// URLs",
                    attempted_urls=attempted_urls,
                )

        work_dir.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        compressed_cap = getattr(active_settings, "vol3_prebuilt_max_compressed_bytes", 64 * 1024 * 1024)
        decompressed_cap = getattr(active_settings, "vol3_prebuilt_max_decompressed_bytes", 1024 * 1024 * 1024)

        for url in attempted_urls:
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "AstinusLab/vol3"})  # noqa: S310  # scheme allowlisted to https://|file:// at line 198
                with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310  # scheme allowlisted to https://|file:// at line 198
                    length = response.headers.get("Content-Length")
                    if length and int(length) > compressed_cap:
                        raise ValueError(f"compressed response exceeds limit of {compressed_cap} bytes")
                    with tempfile.NamedTemporaryFile(dir=work_dir, suffix=".json.xz", delete=False) as compressed:
                        compressed_path = Path(compressed.name)
                        self._copy_capped(response, compressed, compressed_cap)
                try:
                    with lzma.open(compressed_path, "rb") as source:
                        with tempfile.NamedTemporaryFile(dir=work_dir, suffix=".json", delete=False) as decompressed:
                            isf_path = Path(decompressed.name)
                            self._copy_capped(source, decompressed, decompressed_cap)
                    return FetchResult(success=True, isf_path=isf_path, attempted_urls=attempted_urls)
                finally:
                    compressed_path.unlink(missing_ok=True)
            except (OSError, urllib.error.URLError, ValueError, lzma.LZMAError) as exc:
                errors.append(f"{url}: {exc}")
                for path in work_dir.glob("*.json"):
                    path.unlink(missing_ok=True)
        return FetchResult(success=False, error="; ".join(errors), attempted_urls=attempted_urls)


class DebuginfodProvider:
    name = "debuginfod"

    def supports(self, metadata: KernelMetadata) -> bool:
        return bool(metadata.build_id)

    def package_name(self, metadata: KernelMetadata) -> str:
        return ""

    def fetch_url(self, metadata: KernelMetadata) -> str | None:
        return None

    def _settings(self):
        return _active_settings()

    def _log_excerpt(self, text: str | bytes | None, *, max_lines: int = 8) -> str | None:
        if text is None:
            return None
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        lines = [line for line in text.splitlines() if line.strip()]
        return "\n".join(lines[-max_lines:]) if lines else None

    def fetch_vmlinux(self, metadata: KernelMetadata, work_dir: Path) -> FetchResult:
        active_settings = self._settings()
        min_free = getattr(active_settings, "vol3_debuginfod_min_free_bytes", 2 * 1024 * 1024 * 1024)
        if shutil.disk_usage(work_dir).free < min_free:
            return FetchResult(
                success=False,
                error="insufficient disk space",
                phase="precheck",
            )

        configured_urls = list(getattr(active_settings, "vol3_debuginfod_urls", []))
        default_urls = _DEFAULT_DEBUGINFOD_URLS.get(metadata.distro.lower(), [])
        merged_urls: list[str] = []
        for url in configured_urls + default_urls:
            if url not in merged_urls:
                merged_urls.append(url)
        env = os.environ.copy()
        if merged_urls:
            env["DEBUGINFOD_URLS"] = " ".join(merged_urls)
        elif "DEBUGINFOD_URLS" not in env:
            return FetchResult(success=False, error="no debuginfod URLs configured", phase="precheck")

        timeouts = getattr(active_settings, "vol3_provider_timeouts", {})
        timeout_seconds = timeouts.get("debuginfod", 900)
        # Mirror fetch_vmlinux.sh's _debuginfod_try: write to a known destination
        # and verify it exists, instead of scraping the path from stdout.
        dest_path = work_dir / "vmlinux-debuginfod"
        work_dir.mkdir(parents=True, exist_ok=True)
        cmd = ["debuginfod-find", "debuginfo", metadata.build_id or "", "-o", str(dest_path)]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                env=env,
                timeout=timeout_seconds,
            )
        except FileNotFoundError:
            return FetchResult(
                success=False,
                error="debuginfod-find not found",
                phase="precheck",
            )
        except subprocess.TimeoutExpired:
            return FetchResult(
                success=False,
                error=f"debuginfod-find timed out after {timeout_seconds}s",
                phase="timeout",
                log_excerpt=f"debuginfod-find timed out after {timeout_seconds}s",
            )
        excerpt = self._log_excerpt(result.stderr)
        if result.returncode != 0:
            error = result.stderr.decode("utf-8", errors="replace") or "debuginfod-find failed"
            return FetchResult(
                success=False,
                error=error,
                exit_code=result.returncode,
                phase="debuginfod-find",
                log_excerpt=excerpt,
            )

        if not dest_path.is_file() or dest_path.stat().st_size == 0:
            return FetchResult(
                success=False,
                error="debuginfod-find reported success but produced no vmlinux file",
                exit_code=result.returncode,
                phase="debuginfod-find",
                log_excerpt=excerpt,
            )
        return FetchResult(success=True, vmlinux_path=dest_path, log_excerpt=excerpt)


class UbuntuProvider(ScriptFetchMixin):
    def supports(self, metadata: KernelMetadata) -> bool:
        return metadata.distro in {"ubuntu"}

    def package_name(self, metadata: KernelMetadata) -> str:
        return f"linux-image-unsigned-{metadata.kernel_release}-dbgsym"

    def fetch_url(self, metadata: KernelMetadata) -> str | None:
        return None


class KaliProvider(ScriptFetchMixin):
    """Handles Kali Linux and Parrot OS, which ship custom kernels not present
    in the Debian snapshot archive."""

    def supports(self, metadata: KernelMetadata) -> bool:
        return metadata.distro in {"kali", "parrot"}

    def package_name(self, metadata: KernelMetadata) -> str:
        return f"linux-image-{metadata.kernel_release}-dbgsym"

    def fetch_url(self, metadata: KernelMetadata) -> str | None:
        return None


class DebianProvider(ScriptFetchMixin):
    def supports(self, metadata: KernelMetadata) -> bool:
        return metadata.distro in {"debian"}

    def package_name(self, metadata: KernelMetadata) -> str:
        return f"linux-image-{metadata.kernel_release}-dbgsym"

    def fetch_url(self, metadata: KernelMetadata) -> str | None:
        return None


class FedoraProvider(ScriptFetchMixin):
    def supports(self, metadata: KernelMetadata) -> bool:
        return metadata.distro == "fedora"

    def package_name(self, metadata: KernelMetadata) -> str:
        rpm_version = metadata.kernel_release.split("-")[0]
        rpm_release = metadata.kernel_release[len(rpm_version) + 1 :].removesuffix(".x86_64")
        return f"kernel-debuginfo-{rpm_version}-{rpm_release}.x86_64.rpm"

    def fetch_url(self, metadata: KernelMetadata) -> str | None:
        package_name = self.package_name(metadata)
        major = metadata.version.split(".")[0]
        return (
            f"https://dl.fedoraproject.org/pub/fedora/linux/releases/{major}"
            f"/Everything/x86_64/debug/tree/Packages/k/{package_name}"
        )


class RHELFamilyProvider(ScriptFetchMixin):
    def supports(self, metadata: KernelMetadata) -> bool:
        return metadata.distro in {"rhel", "centos", "rocky", "almalinux", "ol"}

    def package_name(self, metadata: KernelMetadata) -> str:
        rpm_version = metadata.kernel_release.split("-")[0]
        rpm_release = metadata.kernel_release[len(rpm_version) + 1 :].removesuffix(".x86_64")
        return f"kernel-debuginfo-{rpm_version}-{rpm_release}.x86_64.rpm"

    def fetch_url(self, metadata: KernelMetadata) -> str | None:
        package_name = self.package_name(metadata)
        distro = metadata.distro
        if distro == "rocky":
            return (
                f"https://dl.rockylinux.org/pub/rocky/{metadata.version}"
                f"/BaseOS/x86_64/debug/tree/Packages/k/{package_name}"
            )
        if distro == "almalinux":
            return (
                f"https://repo.almalinux.org/almalinux/{metadata.version}"
                f"/BaseOS/x86_64/debug/tree/Packages/k/{package_name}"
            )
        return None


class OpenSUSEProvider(ScriptFetchMixin):
    def supports(self, metadata: KernelMetadata) -> bool:
        return metadata.distro in {"opensuse", "opensuse-leap", "opensuse-tumbleweed", "sles", "suse"}

    def package_name(self, metadata: KernelMetadata) -> str:
        return f"kernel-default-debuginfo-{metadata.kernel_release}.x86_64.rpm"

    def fetch_url(self, metadata: KernelMetadata) -> str | None:
        return None


class ArchProvider(ScriptFetchMixin):
    def supports(self, metadata: KernelMetadata) -> bool:
        return metadata.distro in {"arch", "archlinux", "manjaro"}

    def package_name(self, metadata: KernelMetadata) -> str:
        return "linux-debug"

    def fetch_url(self, metadata: KernelMetadata) -> str | None:
        return None


class UnsupportedProvider:
    def supports(self, metadata: KernelMetadata) -> bool:
        return True

    def package_name(self, metadata: KernelMetadata) -> str:
        return ""

    def fetch_url(self, metadata: KernelMetadata) -> str | None:
        return None

    def fetch_vmlinux(self, metadata: KernelMetadata, work_dir: Path) -> FetchResult:
        return FetchResult(success=False, unsupported=True, error=f"Unsupported distro: {metadata.distro}")


_PROVIDERS = [
    UbuntuProvider(),
    KaliProvider(),
    DebianProvider(),
    FedoraProvider(),
    RHELFamilyProvider(),
    OpenSUSEProvider(),
    ArchProvider(),
    UnsupportedProvider(),
]


def get_provider(metadata: KernelMetadata):
    for provider in _PROVIDERS:
        if provider.supports(metadata):
            return provider
    # Should never reach here — UnsupportedProvider is a catch-all
    return UnsupportedProvider()
