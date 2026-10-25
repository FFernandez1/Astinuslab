from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .identity import SymbolIdentity


@dataclass
class KernelMetadata:
    distro: str
    version: str
    kernel_release: str
    architecture: str = "x86_64"
    build_id: str | None = None


@dataclass
class FetchResult:
    success: bool
    isf_path: Path | None = None
    vmlinux_path: Path | None = None
    system_map_path: Path | None = None
    error: str | None = None
    unsupported: bool = False
    exit_code: int | None = None
    phase: str | None = None
    log_excerpt: str | None = None
    attempted_urls: list[str] = field(default_factory=list)
    source_kind: str = "elf"
    build_id_verified: bool = True


@dataclass
class ProviderAttempt:
    name: str
    skipped: bool = False
    skip_reason: str | None = None
    success: bool = False
    error: str | None = None
    urls: list[str] = field(default_factory=list)
    duration_ms: int = 0
    log_excerpt: str | None = None
    vmlinux_path: str | None = None


@dataclass
class IsfBuildOutcome:
    success: bool
    isf_path: Path | None
    identity: SymbolIdentity
    metadata: KernelMetadata
    attempts: list[ProviderAttempt] = field(default_factory=list)

    def to_event_dict(self) -> dict:
        return {
            "success": self.success,
            "distro": self.metadata.distro,
            "version": self.metadata.version,
            "kernel_release": self.metadata.kernel_release,
            "architecture": self.metadata.architecture,
            "build_id": self.metadata.build_id,
            "attempts": [attempt.__dict__ for attempt in self.attempts],
        }


class SymbolProvider(Protocol):
    def supports(self, metadata: KernelMetadata) -> bool: ...
    def package_name(self, metadata: KernelMetadata) -> str: ...
    def fetch_url(self, metadata: KernelMetadata) -> str | None: ...
    def fetch_vmlinux(self, metadata: KernelMetadata, work_dir: Path) -> FetchResult: ...
