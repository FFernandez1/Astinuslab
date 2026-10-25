from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_BASE = Path(__file__).resolve().parents[3]


def _configured_data_base() -> Path:
    """Root of every mutable path, which is not always where the code lives.

    An installed release keeps its source and virtualenv under
    ``releases/<version>-<id>/`` and is reached through the ``current`` symlink,
    while storage, config, images and the Volatility symbol cache stay at the
    stable install root so they survive a release switch. A development
    checkout has one directory for both, so the default is the code root.
    """
    configured = os.environ.get("ASTINUSLAB_DATA_DIR")
    return Path(configured) if configured else _BASE


_DATA_BASE = _configured_data_base()
_TOML_PATH = _DATA_BASE / "astinuslab.toml"

# Mutable paths hang off the data root, never off the directory the code was
# loaded from. load_settings() re-derives these whenever the data root is
# configured, so the two roots can differ at runtime.
_DATA_RELATIVE_DEFAULTS: dict[str, str] = {
    "storage_dir": "storage",
    "db_path": "storage/astinuslab.db",
    "images_dir": "images/definitions",
    "serial_log_dir": "storage/logs/serial",
    "playground_disk_dir": "storage/playground/disks",
    "playground_capture_dir": "storage/playground/captures",
    "targets_catalog_path": "config/catalog/targets.yaml",
    "scenarios_catalog_path": "config/catalog/scenarios.yaml",
    "workloads_catalog_path": "config/catalog/workloads.yaml",
    "attackers_catalog_path": "config/catalog/attackers.yaml",
    "network_nodes_catalog_path": "config/catalog/network_nodes.yaml",
    "vol3_symbols_path": "vol3/symbols",
}


def _celery_sqlite_url(data_base: Path, filename: str, *, backend: bool = False) -> str:
    scheme = "db+sqlite" if backend else "sqla+sqlite"
    return f"{scheme}:///{data_base}/storage/{filename}"


MAX_CONCURRENT_VMS_SOFT_LIMIT = 16

# Keep in sync with list/dict-typed Settings fields.
_COMPOSITE_FIELD_TYPES = {
    "browser_allowed_origins": "list",
    "vol3_isf_mirror_urls": "list",
    "vol3_debuginfod_urls": "list",
    "vol3_provider_timeouts": "dict",
}


def normalize_browser_origin(value: str) -> str | None:
    if not value or any(character.isspace() for character in value) or "?" in value or "#" in value:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname or port == 0:
        return None
    if parsed.username is not None or parsed.password is not None or parsed.path:
        return None
    hostname = parsed.hostname.lower().rstrip(".")
    if not hostname or "\\" in hostname:
        return None
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{scheme}://{rendered_host}:{effective_port}"


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    base_dir: Path = _BASE
    data_dir: Path = _DATA_BASE

    # Orchestration
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # Match the separate MkDocs server started by astinuslab.sh --docs.
    docs_port: int = Field(default=9000, ge=1, le=65535)
    api_log_level: str = "info"
    # Mirror each service's records into storage/logs/<service>.log so the
    # in-app Logs page has something to read. Off by default: the development
    # launcher already redirects stdout to those same files, and two writers on
    # one file interleave. The systemd units turn it on, where stdout goes to
    # the journal instead.
    log_to_file: bool = False
    log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=0)
    log_backup_count: int = Field(default=5, ge=0)
    # "text" is the human format the Logs page renders. "json" emits one object
    # per record with the correlation fields named, for an aggregator or the
    # journal. An unrecognised value degrades to text rather than failing start.
    log_format: str = "text"
    libvirt_uri: str = "qemu:///system"
    libvirt_network: str = "default"
    # Product-level cap on simultaneously active AstinusLab VMs. None = no cap.
    max_concurrent_vms: int | None = None
    capacity_ram_headroom_mb: int = Field(default=2048, ge=0)
    capacity_storage_headroom_gb: int = Field(default=10, ge=0)
    capacity_vm_overhead_mb: int = Field(default=256, ge=0)

    # Authentication
    auth_enabled: bool = False
    admin_api_token_hash: str | None = None
    allow_unsafe_network_bind_without_auth: bool = False
    browser_allowed_origins: list[str] = Field(default_factory=list)

    # Storage
    storage_dir: Path = _DATA_BASE / "storage"
    db_path: Path = _DATA_BASE / "storage" / "astinuslab.db"
    images_dir: Path = _DATA_BASE / "images" / "definitions"
    serial_log_dir: Path = _DATA_BASE / "storage" / "logs" / "serial"
    playground_disk_dir: Path = _DATA_BASE / "storage" / "playground" / "disks"
    playground_capture_dir: Path = _DATA_BASE / "storage" / "playground" / "captures"
    libvirt_image_base_dir: Path = Path("/var/lib/libvirt/images/astinuslab")
    playground_default_vcpus: int = 2
    playground_default_memory_mb: int = 2048
    playground_capture_stream_timeout_seconds: int = 3600
    playground_capture_timeout_seconds: int = 900

    # Portable lab packages. The workflow is disabled by default and must be
    # explicitly enabled on a controlled local host.
    packages_enabled: bool = False
    packages_allow_source_network: bool = False
    packages_allow_guest_egress: bool = False
    packages_max_asset_bytes: int = Field(default=16 * 1024**3, gt=0)
    packages_max_archive_bytes: int = Field(default=40 * 1024**3, gt=0)
    packages_gc_grace_seconds: int = Field(default=24 * 60 * 60, ge=0)

    # Catalogs
    targets_catalog_path: Path = _DATA_BASE / "config" / "catalog" / "targets.yaml"
    scenarios_catalog_path: Path = _DATA_BASE / "config" / "catalog" / "scenarios.yaml"
    workloads_catalog_path: Path = _DATA_BASE / "config" / "catalog" / "workloads.yaml"
    attackers_catalog_path: Path = _DATA_BASE / "config" / "catalog" / "attackers.yaml"
    network_nodes_catalog_path: Path = _DATA_BASE / "config" / "catalog" / "network_nodes.yaml"

    # Vol3
    vol3_enabled: bool = True
    vol3_symbols_path: Path = _DATA_BASE / "vol3" / "symbols"
    vol3_dwarf2json_path: Path | None = None
    vol3_dwarf2json_sha256: str | None = None
    vol3_volatility_bin: str | None = None
    vol3_default_plugins: str = "linux.bash.Bash,linux.pslist.PsList,linux.pstree.PsTree,linux.pmap.Pmap"
    vol3_fetch_timeout_seconds: int = 1800
    vol3_isf_mirror_urls: list[str] = Field(default_factory=list)
    vol3_prebuilt_max_compressed_bytes: int = 64 * 1024 * 1024
    vol3_prebuilt_max_decompressed_bytes: int = 1024 * 1024 * 1024
    vol3_debuginfod_urls: list[str] = Field(default_factory=list)
    vol3_debuginfod_min_free_bytes: int = 2 * 1024 * 1024 * 1024
    vol3_provider_timeouts: dict[str, int] = Field(
        default_factory=lambda: {
            "prebuilt_isf": 60,
            "debuginfod": 900,
            "host_fetch": 1800,
            "live_guest": 1800,
        }
    )
    vol3_dwarf2json_timeout_seconds: int = 600
    vol3_allow_invasive_guest_symbol_install: bool = False
    vol3_plugin_run_max_rows: int = 50000

    # Image inspection
    image_inspection_enabled: bool = True
    image_inspection_timeout_seconds: int = 60
    image_inspection_virt_inspector_bin: str = "virt-inspector"

    # Task queue
    # LOAD-BEARING: sqla+sqlite broker plus one worker at concurrency 1 is a
    # deliberate single-host V1 invariant. Locks/reconciliation assume it; to
    # scale, replace the broker (Redis) first. See docs/architecture/components.md#scaling.
    celery_broker_url: str = _celery_sqlite_url(_DATA_BASE, "celery_broker.db")
    celery_result_backend: str = _celery_sqlite_url(_DATA_BASE, "celery_results.db", backend=True)
    celery_reconciliation_enabled: bool = True
    celery_reconciliation_interval_seconds: int = 60
    celery_reconciliation_grace_seconds: int = 120

    @field_validator("vol3_dwarf2json_sha256")
    @classmethod
    def validate_vol3_dwarf2json_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower().removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("vol3_dwarf2json_sha256 must be a 64-character SHA-256 value")
        return normalized

    @field_validator("browser_allowed_origins")
    @classmethod
    def validate_browser_allowed_origins(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            origin = normalize_browser_origin(value)
            if origin is None:
                raise ValueError(f"browser_allowed_origins contains invalid origin: {value!r}")
            if origin not in normalized:
                normalized.append(origin)
        return normalized

    @model_validator(mode="after")
    def validate_max_concurrent_vms(self) -> Settings:
        if self.max_concurrent_vms is not None and self.max_concurrent_vms < 1:
            raise ValueError("max_concurrent_vms must be an integer >= 1 (omit the key for no cap)")
        return self

    @model_validator(mode="after")
    def validate_prebuilt_mirrors(self) -> Settings:
        for url in self.vol3_isf_mirror_urls:
            if not (url.startswith("https://") or url.startswith("file://")):
                raise ValueError("vol3_isf_mirror_urls entries must use https:// or file://")
        return self

    @model_validator(mode="after")
    def validate_playground_paths_not_nested(self) -> Settings:
        # Captures must never live under playground_disk_dir (or vice versa):
        # the startup sweep iterates the disk dir and would shred the captures
        # tree as if it were a stale session.
        disk = self.playground_disk_dir.resolve()
        cap = self.playground_capture_dir.resolve()
        if disk == cap:
            raise ValueError("playground_disk_dir and playground_capture_dir must differ")
        if disk.is_relative_to(cap) or cap.is_relative_to(disk):
            raise ValueError(
                f"playground_disk_dir and playground_capture_dir must not be nested (got disk={disk}, captures={cap})"
            )
        return self

    @model_validator(mode="after")
    def validate_auth_network_bind(self) -> Settings:
        host = self.api_host.strip()
        host_is_loopback = host.lower() == "localhost"
        if not host_is_loopback:
            try:
                host_is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                host_is_loopback = False

        if self.auth_enabled and not self.admin_api_token_hash:
            raise ValueError("auth_enabled=true requires admin_api_token_hash")

        if self.admin_api_token_hash:
            token_hash = self.admin_api_token_hash.removeprefix("sha256:")
            if len(token_hash) != 64:
                raise ValueError("admin_api_token_hash must be sha256:<64-hex>")
            try:
                int(token_hash, 16)
            except ValueError as exc:
                raise ValueError("admin_api_token_hash must be sha256:<64-hex>") from exc

        if not host_is_loopback and not self.auth_enabled and not self.allow_unsafe_network_bind_without_auth:
            raise ValueError(
                "non-loopback api_host requires auth_enabled=true or allow_unsafe_network_bind_without_auth=true"
            )
        if not host_is_loopback:
            if not self.browser_allowed_origins:
                raise ValueError("non-loopback api_host requires explicit browser_allowed_origins")
            if any(not origin.startswith("https://") for origin in self.browser_allowed_origins):
                raise ValueError("non-loopback browser_allowed_origins entries must use HTTPS")
        return self

    @property
    def acl_allowed_storage_roots(self) -> tuple[Path, ...]:
        roots = [
            Path("/opt/astinuslab/storage").resolve(),
            self.storage_dir.resolve(),
            (self.data_dir / "storage").resolve(),
        ]
        # De-duplicate while preserving order
        unique_roots: list[Path] = []
        for r in roots:
            if r not in unique_roots:
                unique_roots.append(r)
        return tuple(unique_roots)


def _parse_composite_env(key: str, raw: str) -> list | dict:
    kind = _COMPOSITE_FIELD_TYPES[key]
    env_key = f"ASTINUSLAB_{key.upper()}"
    text_value = raw.strip()

    if text_value.startswith(("[", "{")):
        try:
            parsed = json.loads(text_value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{env_key} must be valid JSON ({kind}); got: {raw!r}") from exc

        if kind == "list" and not isinstance(parsed, list):
            raise ValueError(f"{env_key} must be a JSON list; got: {raw!r}")
        if kind == "dict" and not isinstance(parsed, dict):
            raise ValueError(f"{env_key} must be a JSON object; got: {raw!r}")
        return parsed

    if kind == "list":
        return [item.strip() for item in text_value.split(",") if item.strip()]

    raise ValueError(f"{env_key} must be a JSON object; got: {raw!r}")


_TOML_SECTIONS = {
    "orchestration",
    "storage",
    "catalogs",
    "vol3",
    "image_inspection",
    "celery",
    "auth",
    "packages",
}
_CATALOG_TOML_KEYS = {
    "targets",
    "scenarios",
    "workloads",
    "attackers",
    "network_nodes",
}


def _reject_unknown_toml_keys(raw_data: dict[str, Any]) -> None:
    unknown_sections = set(raw_data) - _TOML_SECTIONS
    if unknown_sections:
        keys = ", ".join(sorted(unknown_sections))
        raise ValueError(f"Unknown settings section(s): {keys}")

    catalogs = raw_data.get("catalogs")
    if isinstance(catalogs, dict):
        unknown_catalogs = set(catalogs) - _CATALOG_TOML_KEYS
        if unknown_catalogs:
            keys = ", ".join(sorted(unknown_catalogs))
            raise ValueError(f"Unknown [catalogs] key(s): {keys}")


def load_settings() -> Settings:
    data: dict[str, Any] = {}
    data_base = _configured_data_base()
    # The data root owns astinuslab.toml. Without an explicit data root the
    # module-level path applies, which keeps it patchable in tests.
    toml_path = data_base / "astinuslab.toml" if "ASTINUSLAB_DATA_DIR" in os.environ else _TOML_PATH
    if toml_path.exists():
        with open(toml_path, "rb") as f:
            raw_data = tomllib.load(f)
            _reject_unknown_toml_keys(raw_data)
            # Flatten the nested TOML structure into the flat Settings model
            if "orchestration" in raw_data:
                data.update(raw_data["orchestration"])
            if "storage" in raw_data:
                data.update(raw_data["storage"])
            if "catalogs" in raw_data:
                # Map to our internal catalog path names
                cat = raw_data["catalogs"]
                if "targets" in cat:
                    data["targets_catalog_path"] = cat["targets"]
                if "scenarios" in cat:
                    data["scenarios_catalog_path"] = cat["scenarios"]
                if "workloads" in cat:
                    data["workloads_catalog_path"] = cat["workloads"]
                if "attackers" in cat:
                    data["attackers_catalog_path"] = cat["attackers"]
                if "network_nodes" in cat:
                    data["network_nodes_catalog_path"] = cat["network_nodes"]
            if "vol3" in raw_data:
                for k, v in raw_data["vol3"].items():
                    data[f"vol3_{k}"] = v
            if "image_inspection" in raw_data:
                for k, v in raw_data["image_inspection"].items():
                    data[f"image_inspection_{k}"] = v
            if "celery" in raw_data:
                for k, v in raw_data["celery"].items():
                    data[f"celery_{k}"] = v
            if "auth" in raw_data:
                for k, v in raw_data["auth"].items():
                    if k == "enabled":
                        data["auth_enabled"] = v
                    else:
                        data[k] = v
            if "packages" in raw_data:
                for k, v in raw_data["packages"].items():
                    data[f"packages_{k}"] = v

    for key in Settings.model_fields:
        env_key = f"ASTINUSLAB_{key.upper()}"
        if env_key in os.environ:
            raw_value = os.environ[env_key]
            if key in _COMPOSITE_FIELD_TYPES:
                data[key] = _parse_composite_env(key, raw_value)
            else:
                data[key] = raw_value

    # Anything the operator did not pin follows the data root, so a release
    # switch never drags storage or config into the new release directory.
    data.setdefault("data_dir", data_base)
    for field, relative in _DATA_RELATIVE_DEFAULTS.items():
        if field not in data:
            data[field] = data_base.joinpath(*relative.split("/"))
    data.setdefault("celery_broker_url", _celery_sqlite_url(data_base, "celery_broker.db"))
    data.setdefault("celery_result_backend", _celery_sqlite_url(data_base, "celery_results.db", backend=True))

    # Resolve paths relative to the data root if they are strings and relative
    for key, value in data.items():
        if isinstance(value, str):
            # Support {base_dir} placeholder, which names mutable locations
            if "{base_dir}" in value:
                value = value.replace("{base_dir}", str(data_base))
                data[key] = value

            if key.endswith(("_path", "_dir")):
                if value.startswith("~/"):
                    data[key] = Path(value).expanduser()
                elif not value.startswith("/"):
                    data[key] = data_base / value
            elif key.endswith("_bin") and "/" in value:
                # `_bin` fields are typed as plain strings so a bare command name
                # can still resolve through PATH.  Keep the resolved value a string:
                # Pydantic does not coerce Path back to str and would reject it.
                if value.startswith("~/"):
                    data[key] = str(Path(value).expanduser())
                elif not value.startswith("/"):
                    data[key] = str(_BASE / value)

    loaded = Settings(**data)
    if loaded.max_concurrent_vms is not None and loaded.max_concurrent_vms > MAX_CONCURRENT_VMS_SOFT_LIMIT:
        logging.getLogger(__name__).warning(
            "max_concurrent_vms=%d exceeds the recommended maximum of %d; "
            "each Playground VM defaults to %d MB RAM - verify host memory before use",
            loaded.max_concurrent_vms,
            MAX_CONCURRENT_VMS_SOFT_LIMIT,
            loaded.playground_default_memory_mb,
        )
    return loaded


settings = load_settings()
