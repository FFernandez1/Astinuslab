"""Immutable contracts for portable lab definition and manifest documents."""

from __future__ import annotations

import ipaddress
import math
import unicodedata
from collections.abc import Mapping
from pathlib import PureWindowsPath
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_serializer, model_validator

SHA256_PATTERN = r"^[0-9a-f]{64}$"
PACKAGE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$"
VERSION_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$"
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_UNRESERVED = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
_SUB_DELIMITERS = frozenset("!$&'()*+,;=")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _json_values_are_finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(_json_values_are_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_json_values_are_finite(item) for item in value)
    return True


def normalize_legacy_definition_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Downgrade unprovable v1 lock claims without inventing missing facts.

    Early package definitions used ``clean_layer`` and a mutable catalog id as
    their entire image identity. They can still be displayed and exported as
    infrastructure, but must never retain a locked/reproducible claim.  The
    function deliberately does not guess a digest, boot layer, workload, or
    reachability mode; callers can surface the resulting unlocked definition
    with an actionable upgrade message.
    """
    normalized = dict(payload)
    if normalized.get("reproducibility") != "locked":
        return normalized
    raw_vms = normalized.get("vms")
    raw_networks = normalized.get("networks")
    stable_vms = isinstance(raw_vms, list) and all(
        isinstance(vm, Mapping)
        and vm.get("base_image") is not None
        and vm.get("boot_layer") is not None
        and vm.get("workload") is not None
        for vm in raw_vms
    )
    explicit_networks = isinstance(raw_networks, list) and all(
        isinstance(network, Mapping) and network.get("reachability") in {"isolated", "nat"} for network in raw_networks
    )
    if not stable_vms or not explicit_networks:
        normalized["reproducibility"] = "unlocked"
    return normalized


def _valid_manifest_payload_path(path: str) -> bool:
    if not path or "\\" in path or ":" in path or path.startswith("/") or PureWindowsPath(path).drive:
        return False
    if any(segment in {"", ".", ".."} for segment in path.split("/")):
        return False
    return not any(
        ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        or 0xD800 <= ord(character) <= 0xDFFF
        or unicodedata.category(character) in {"Cc", "Cf"}
        for character in path
    )


def _valid_http_host(host: str | None) -> bool:
    if host is None:
        return False
    unbracketed = host.removeprefix("[").removesuffix("]")
    try:
        ipaddress.ip_address(unbracketed)
        return True
    except ValueError:
        pass
    if unbracketed.endswith(".."):
        return False
    dns_host = unbracketed[:-1] if unbracketed.endswith(".") else unbracketed
    try:
        ascii_host = dns_host.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if not ascii_host or len(ascii_host) > 253:
        return False
    for label in ascii_host.split("."):
        if not label or len(label) > 63 or not label[0].isalnum() or not label[-1].isalnum():
            return False
        if any(not character.isalnum() and character != "-" for character in label):
            return False
    return True


def _valid_ipv6_zone_id(value: str) -> bool:
    return bool(value) and _valid_url_component(value, _UNRESERVED) and all(character.isascii() for character in value)


def _url_authority(source: str) -> str:
    scheme_separator = source.find("://")
    if scheme_separator == -1:
        return ""
    authority_start = scheme_separator + 3
    authority_end = min(
        (position for delimiter in "/?#" if (position := source.find(delimiter, authority_start)) != -1),
        default=len(source),
    )
    return source[authority_start:authority_end]


def _source_parseable_by_urlsplit(source: str, authority: str) -> str:
    host_start = authority.rfind("@") + 1
    if not authority.startswith("[", host_start):
        return source
    closing_bracket = authority.find("]", host_start + 1)
    if closing_bracket == -1:
        return source
    address, separator, _ = authority[host_start + 1 : closing_bracket].partition("%")
    if not separator:
        return source
    authority_start = source.find("://") + 3
    return source[: authority_start + host_start + 1] + address + source[authority_start + closing_bracket :]


def _valid_bracketed_ipv6_authority(authority: str) -> bool:
    if not authority.startswith("["):
        return True
    closing_bracket = authority.find("]")
    if (
        closing_bracket == -1
        or authority[closing_bracket + 1 :] not in {""}
        and not authority[closing_bracket + 1 :].startswith(":")
    ):
        return False
    literal = authority[1:closing_bracket]
    address, separator, encoded_zone = literal.partition("%")
    if separator and (not encoded_zone.startswith("25") or not _valid_ipv6_zone_id(encoded_zone[2:])):
        return False
    try:
        return isinstance(ipaddress.ip_address(address), ipaddress.IPv6Address)
    except ValueError:
        return False


def _valid_url_component(value: str, allowed_characters: frozenset[str]) -> bool:
    index = 0
    while index < len(value):
        character = value[index]
        if character == "%":
            if len(value) - index < 3 or any(digit not in _HEX_DIGITS for digit in value[index + 1 : index + 3]):
                return False
            index += 3
            continue
        if character not in allowed_characters and not character.isascii():
            index += 1
            continue
        if character not in allowed_characters:
            return False
        index += 1
    return True


def _valid_http_userinfo(userinfo: str) -> bool:
    username, separator, password = userinfo.partition(":")
    return (
        bool(username)
        and bool(separator)
        and bool(password)
        and _valid_url_component(username, _UNRESERVED | _SUB_DELIMITERS)
        and _valid_url_component(password, _UNRESERVED | _SUB_DELIMITERS | frozenset(":"))
    )


class HostResources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cpu_threads: int = Field(ge=1, le=256)
    memory_mb: int = Field(ge=512)
    free_disk_bytes: int = Field(ge=1)


class ResourceRecommendations(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    minimum: HostResources
    recommended: HostResources

    @model_validator(mode="after")
    def _recommended_meets_minimum(self) -> ResourceRecommendations:
        for dimension in ("cpu_threads", "memory_mb", "free_disk_bytes"):
            if getattr(self.recommended, dimension) < getattr(self.minimum, dimension):
                raise ValueError(f"recommended {dimension} must be at least minimum")
        return self


class AssetReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=1)
    media_type: str = Field(min_length=1, max_length=128)
    sources: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    embedded: bool = False
    license: str = Field(min_length=1, max_length=256)
    redistribution_confirmed: bool = False

    @model_validator(mode="after")
    def _validate_and_freeze_sources(self) -> AssetReference:
        for source in self.sources:
            if any(
                character.isspace()
                or ord(character) < 0x20
                or 0x7F <= ord(character) <= 0x9F
                or 0xD800 <= ord(character) <= 0xDFFF
                or unicodedata.category(character) in {"Cc", "Cf"}
                for character in source
            ):
                raise ValueError("asset source must use http or https")
            authority = _url_authority(source)
            try:
                parsed = urlsplit(_source_parseable_by_urlsplit(source, authority))
                host = parsed.hostname
                port = parsed.port
            except ValueError as exc:
                raise ValueError("asset source must use http or https") from exc
            host_authority = authority.rsplit("@", maxsplit=1)[-1]
            userinfo = authority.split("@", maxsplit=1)[0] if "@" in authority else ""
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.netloc
                or "\\" in source
                or parsed.netloc.count("@") > 1
                or "@" in parsed.netloc
                and not _valid_http_userinfo(userinfo)
                or host_authority.startswith("[")
                and not _valid_bracketed_ipv6_authority(host_authority)
                or not host_authority.startswith("[")
                and not _valid_http_host(host)
                or port is not None
                and not 1 <= port <= 65535
                or host_authority.endswith(":")
                or not _valid_url_component(parsed.path, _UNRESERVED | _SUB_DELIMITERS | frozenset(":@/"))
                or not _valid_url_component(parsed.query, _UNRESERVED | _SUB_DELIMITERS | frozenset(":@/?"))
                or not _valid_url_component(parsed.fragment, _UNRESERVED | _SUB_DELIMITERS | frozenset(":@/?"))
            ):
                raise ValueError("asset source must use http or https")
        object.__setattr__(self, "sources", tuple(self.sources))
        return self

    @field_serializer("sources", mode="wrap")
    def _serialize_sources(self, sources: tuple[str, ...], handler: Any) -> Any:
        return handler(sources)


class LabImageSpec(BaseModel):
    """Immutable catalog facts required to render one VM's base image.

    ``image_id`` is retained for compatibility with the Playground catalog, but
    a locked package is identified by the recorded bytes rather than whatever
    that catalog entry happens to point at on a recipient host.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    image_id: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=1)
    media_type: str = Field(min_length=1, max_length=128)
    architecture: Literal["x86_64"] = "x86_64"
    firmware: Literal["bios", "uefi"] = "bios"
    distro: str | None = Field(default=None, max_length=64)
    version: str | None = Field(default=None, max_length=64)
    username: str | None = Field(default=None, max_length=128)
    cloud_init: bool = False
    cloud_init_profile: str | None = Field(default=None, max_length=64)
    machine_type: str | None = Field(default=None, max_length=128)
    os_variant: str | None = Field(default=None, max_length=128)
    family: str | None = Field(default=None, max_length=64)
    image_format: str | None = Field(default=None, max_length=32)
    filesystem_type: str | None = Field(default=None, max_length=64)
    network_profile: str | None = Field(default=None, max_length=64)
    inject_network_config: bool | None = None


class LabReadinessProbe(BaseModel):
    """A bounded, declarative readiness probe for a locked workload."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["tcp", "http", "command"]
    port: int | None = Field(default=None, ge=1, le=65535)
    path: str | None = Field(default=None, min_length=1, max_length=512)
    command: str | None = Field(default=None, min_length=1, max_length=512)
    timeout_seconds: int = Field(default=30, ge=1, le=600)

    @model_validator(mode="after")
    def _validate_probe_shape(self) -> LabReadinessProbe:
        if self.kind in {"tcp", "http"} and self.port is None:
            raise ValueError(f"{self.kind} readiness probe requires a port")
        if self.kind == "http" and self.path is None:
            raise ValueError("http readiness probe requires a path")
        if self.kind == "command" and self.command is None:
            raise ValueError("command readiness probe requires a command")
        return self


# Checkpoints are intentionally represented as a small, declarative language.
# ``LabExperience.checkpoints`` remains a frozen mapping tuple for v1 reader
# compatibility (old packages used arbitrary JSON here), but new authors and
# verification code should pass these typed operations through
# ``validate_checkpoint``.  Keeping the compatibility boundary explicit lets a
# recipient display old packages as unlocked without accidentally granting an
# old checkpoint host access.
class GuestObservation(BaseModel):
    """Read one bounded value from a guest-scoped observation provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["observe"] = "observe"
    scope: Literal["guest"] = "guest"
    vm_id: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=512)
    timeout_seconds: int = Field(default=30, ge=1, le=600)


class GuestAssertion(BaseModel):
    """Assert a bounded guest observation equals an expected JSON value."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["assert"] = "assert"
    scope: Literal["guest"] = "guest"
    vm_id: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=512)
    expected: JsonValue
    timeout_seconds: int = Field(default=30, ge=1, le=600)

    @model_validator(mode="after")
    def _expected_is_finite(self) -> GuestAssertion:
        if not _json_values_are_finite(self.expected):
            raise ValueError("checkpoint expected value must be finite JSON")
        return self


GuestCheckpointOperation = GuestObservation | GuestAssertion


def validate_checkpoint(value: Mapping[str, Any]) -> GuestCheckpointOperation:
    """Validate one checkpoint without permitting host-side execution hooks.

    The compatibility form ``{"id": ..., "answer": ...}`` is accepted only
    as a guest assertion with no VM/path binding; it is still unsuitable for a
    locked definition and is therefore rejected by locked export/verification.
    This helper is the one place that turns the legacy JSON surface into the
    typed operation union.
    """

    if not isinstance(value, Mapping):
        raise ValueError("checkpoint must be an object")
    forbidden = {"host_command", "host_path", "shell", "filesystem", "exec", "command"}
    if forbidden.intersection(value):
        raise ValueError("checkpoints may only contain guest-scoped observations")
    payload = dict(value)
    if "operation" not in payload:
        # Old packages are read-only/unlocked compatibility data.  Do not
        # claim that an arbitrary answer is a portable guest assertion.
        raise ValueError("checkpoint is not a typed guest operation")
    operation = payload.get("operation")
    if operation == "observe":
        return GuestObservation.model_validate(payload)
    if operation == "assert":
        return GuestAssertion.model_validate(payload)
    raise ValueError("checkpoint operation must be observe or assert")


class LabWorkloadSpec(BaseModel):
    """Immutable workload inputs and the contract used to declare it ready."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    workload_baked: bool
    immutable_inputs: Mapping[str, JsonValue] = Field(default_factory=dict, max_length=64)
    readiness_probes: tuple[LabReadinessProbe, ...] = Field(default_factory=tuple, max_length=16)

    @model_validator(mode="after")
    def _freeze_workload_contract(self) -> LabWorkloadSpec:
        if len(self.immutable_inputs) > 64:
            raise ValueError("workload immutable inputs must contain at most 64 entries")
        if not _json_values_are_finite(self.immutable_inputs):
            raise ValueError("workload immutable inputs must be finite JSON values")
        object.__setattr__(self, "immutable_inputs", _freeze_json(self.immutable_inputs))
        object.__setattr__(self, "readiness_probes", tuple(self.readiness_probes))
        return self

    @field_serializer("immutable_inputs", mode="wrap")
    def _serialize_immutable_inputs(self, inputs: Mapping[str, Any], handler: Any) -> Any:
        return handler(_thaw_json(inputs))

    @field_serializer("readiness_probes", mode="wrap")
    def _serialize_readiness_probes(self, probes: tuple[LabReadinessProbe, ...], handler: Any) -> Any:
        return handler(probes)


class LabNetwork(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    subnet_cidr: str
    # None is a pre-remediation representation. It remains readable only for
    # unlocked definitions; locked definitions must say what reachability is.
    reachability: Literal["isolated", "nat"] | None = None
    isolated: bool = True
    guest_egress: bool = False
    host_bridge: str | None = None

    @model_validator(mode="after")
    def _validate_network_semantics(self) -> LabNetwork:
        try:
            network = ipaddress.ip_network(self.subnet_cidr, strict=False)
        except ValueError as exc:
            raise ValueError(f"invalid subnet CIDR: {exc}") from exc
        if self.subnet_cidr != network.with_prefixlen:
            raise ValueError("subnet CIDR must be canonical")
        if network.num_addresses < 4:
            raise ValueError("subnet must contain at least 4 addresses")
        if self.isolated and self.host_bridge is not None:
            raise ValueError("isolated networks cannot use a host bridge")
        if self.reachability == "isolated" and (not self.isolated or self.guest_egress):
            raise ValueError("isolated networks cannot enable guest egress")
        if self.reachability == "nat" and (self.isolated or not self.guest_egress):
            raise ValueError("NAT networks must enable guest egress and cannot be isolated")
        if self.reachability is None and self.isolated and self.guest_egress:
            raise ValueError("isolated networks cannot enable guest egress")
        return self


class LabTopologyNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    node_type: Literal["vm", "network"]
    x: float
    y: float

    @model_validator(mode="after")
    def _coordinates_are_finite(self) -> LabTopologyNode:
        if not math.isfinite(self.x) or not math.isfinite(self.y):
            raise ValueError("topology coordinates must be finite")
        return self


class LabVM(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    role: Literal["target", "attacker", "network"]
    image_id: str
    architecture: Literal["x86_64"] = "x86_64"
    firmware: Literal["bios", "uefi"] = "bios"
    workload_id: str | None = None
    vcpus: int = Field(ge=1, le=64)
    memory_mb: int = Field(ge=128, le=131072)
    disk_gb: int = Field(ge=1, le=2048)
    network_ids: tuple[str, ...] = Field(min_length=1)
    base_image: LabImageSpec | None = None
    boot_layer: AssetReference | None = None
    workload: LabWorkloadSpec | None = None
    # Kept solely for v1 reader compatibility. New definitions use boot_layer.
    clean_layer: AssetReference | None = None

    @model_validator(mode="after")
    def _unique_and_frozen_network_ids(self) -> LabVM:
        seen: set[str] = set()
        for network_id in self.network_ids:
            if network_id in seen:
                raise ValueError(f"VM {self.id} has duplicate network id: {network_id}")
            seen.add(network_id)
        object.__setattr__(self, "network_ids", tuple(self.network_ids))
        if self.base_image is not None and self.base_image.image_id != self.image_id:
            raise ValueError(f"VM {self.id} base image id must match image_id")
        if self.base_image is not None and self.base_image.architecture != self.architecture:
            raise ValueError(f"VM {self.id} base image architecture must match VM architecture")
        if self.base_image is not None and self.base_image.firmware != self.firmware:
            raise ValueError(f"VM {self.id} base image firmware must match VM firmware")
        if self.boot_layer is not None and self.clean_layer is not None:
            raise ValueError(f"VM {self.id} cannot specify both boot_layer and clean_layer")
        return self

    @property
    def effective_boot_layer(self) -> AssetReference | None:
        """Return the v2 boot layer, falling back only for normalized v1 data."""
        return self.boot_layer or self.clean_layer

    @field_serializer("network_ids", mode="wrap")
    def _serialize_network_ids(self, network_ids: tuple[str, ...], handler: Any) -> Any:
        return handler(network_ids)


class LabExperience(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    runbook_path: Literal["experience/runbook.md"] = "experience/runbook.md"
    objectives: tuple[str, ...] = Field(default_factory=tuple)
    hints: tuple[str, ...] = Field(default_factory=tuple)
    checkpoints: tuple[Mapping[str, JsonValue], ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _freeze_collections(self) -> LabExperience:
        if not _json_values_are_finite(self.checkpoints):
            raise ValueError("checkpoint JSON values must be finite")
        object.__setattr__(self, "objectives", tuple(self.objectives))
        object.__setattr__(self, "hints", tuple(self.hints))
        object.__setattr__(self, "checkpoints", tuple(_freeze_json(item) for item in self.checkpoints))
        return self

    @field_serializer("objectives", "hints", mode="wrap")
    def _serialize_string_lists(self, values: tuple[str, ...], handler: Any) -> Any:
        return handler(values)

    @field_serializer("checkpoints", mode="wrap")
    def _serialize_checkpoints(self, checkpoints: tuple[Mapping[str, Any], ...], handler: Any) -> Any:
        return handler(tuple(_thaw_json(checkpoint) for checkpoint in checkpoints))


class LabDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: Literal[1] = 1
    package_id: str = Field(pattern=PACKAGE_ID_PATTERN)
    version: str = Field(pattern=VERSION_PATTERN)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    creator: str = Field(min_length=1, max_length=160)
    license: str = Field(min_length=1, max_length=256)
    reproducibility: Literal["locked", "unlocked"]
    resources: ResourceRecommendations
    networks: tuple[LabNetwork, ...] = Field(min_length=1)
    vms: tuple[LabVM, ...] = Field(min_length=1)
    topology: tuple[LabTopologyNode, ...] = Field(default_factory=tuple)
    experience: LabExperience

    @model_validator(mode="after")
    def _validate_definition(self) -> LabDefinition:
        network_by_id: dict[str, LabNetwork] = {}
        for network in self.networks:
            if network.id in network_by_id:
                raise ValueError(f"duplicate network id: {network.id}")
            network_by_id[network.id] = network

        vm_by_id: dict[str, LabVM] = {}
        for vm in self.vms:
            if vm.id in vm_by_id:
                raise ValueError(f"duplicate VM id: {vm.id}")
            vm_by_id[vm.id] = vm
            for network_id in vm.network_ids:
                if network_id not in network_by_id:
                    raise ValueError(f"VM {vm.id} references unknown network: {network_id}")
            if self.reproducibility == "locked":
                if vm.base_image is None:
                    raise ValueError(f"locked definition requires base_image for VM: {vm.id}")
                if vm.boot_layer is None:
                    raise ValueError(f"locked definition requires boot_layer for VM: {vm.id}")
                if vm.workload is None:
                    raise ValueError(f"locked definition requires workload contract for VM: {vm.id}")
                if not vm.workload.workload_baked:
                    raise ValueError(f"locked definition requires a baked workload for VM: {vm.id}")

        if self.reproducibility == "locked":
            for network in self.networks:
                if network.reachability is None:
                    raise ValueError(f"locked definition requires reachability for network: {network.id}")
                if network.host_bridge is not None:
                    raise ValueError(f"locked definition does not support host bridge: {network.id}")

        topology_references: set[tuple[str, str]] = set()
        for node in self.topology:
            reference = (node.node_type, node.node_id)
            if reference in topology_references:
                raise ValueError(f"duplicate topology node reference: {node.node_id}")
            topology_references.add(reference)
            if node.node_type == "vm":
                if node.node_id in network_by_id and node.node_id not in vm_by_id:
                    raise ValueError(f"topology node {node.node_id} has type vm but references a network")
                if node.node_id not in vm_by_id:
                    raise ValueError(f"topology references unknown VM: {node.node_id}")
            else:
                if node.node_id in vm_by_id and node.node_id not in network_by_id:
                    raise ValueError(f"topology node {node.node_id} has type network but references a VM")
                if node.node_id not in network_by_id:
                    raise ValueError(f"topology references unknown network: {node.node_id}")

        object.__setattr__(self, "networks", tuple(self.networks))
        object.__setattr__(self, "vms", tuple(self.vms))
        object.__setattr__(self, "topology", tuple(self.topology))
        return self

    @field_serializer("networks", "vms", "topology", mode="wrap")
    def _serialize_model_lists(self, values: tuple[BaseModel, ...], handler: Any) -> Any:
        return handler(values)


class ManifestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=1)
    media_type: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _validate_path(self) -> ManifestPayload:
        if not _valid_manifest_payload_path(self.path):
            raise ValueError("invalid manifest payload path")
        return self


class LabManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format_version: Literal[1] = 1
    package_id: str = Field(pattern=PACKAGE_ID_PATTERN)
    version: str = Field(pattern=VERSION_PATTERN)
    required_capabilities: tuple[str, ...] = Field(default_factory=tuple)
    payloads: tuple[ManifestPayload, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _unique_paths_and_frozen_collections(self) -> LabManifest:
        paths: set[str] = set()
        for payload in self.payloads:
            normalized_path = unicodedata.normalize("NFC", payload.path)
            if normalized_path in paths:
                raise ValueError(f"duplicate manifest payload path: {normalized_path}")
            paths.add(normalized_path)
        object.__setattr__(self, "required_capabilities", tuple(self.required_capabilities))
        object.__setattr__(self, "payloads", tuple(self.payloads))
        return self

    @field_serializer("required_capabilities", "payloads", mode="wrap")
    def _serialize_lists(self, values: tuple[Any, ...], handler: Any) -> Any:
        return handler(values)
