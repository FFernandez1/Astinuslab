from __future__ import annotations

import math
import warnings
from typing import cast, get_origin

import pytest
from pydantic import ValidationError

from astinuslab.lab_packages.canonical import canonical_bytes, canonical_sha256
from astinuslab.lab_packages.models import (
    AssetReference,
    HostResources,
    LabDefinition,
    LabExperience,
    LabImageSpec,
    LabManifest,
    LabNetwork,
    LabTopologyNode,
    LabVM,
    LabWorkloadSpec,
    ManifestPayload,
    ResourceRecommendations,
    normalize_legacy_definition_payload,
)

SHA256 = "a" * 64


@pytest.fixture
def asset_dict() -> dict:
    return {
        "sha256": SHA256,
        "size_bytes": 4096,
        "media_type": "application/x-qemu-disk",
        "sources": ["https://images.example.test/clean.qcow2"],
        "embedded": False,
        "license": "CC-BY-4.0",
        "redistribution_confirmed": True,
    }


@pytest.fixture
def locked_definition_dict(asset_dict) -> dict:
    return {
        "format_version": 1,
        "package_id": "org.example.cafe-lab",
        "version": "1.2.3-rc.1",
        "title": "Café lab",
        "description": "A deterministic lab",
        "creator": "Example Creator",
        "license": "CC-BY-4.0",
        "reproducibility": "locked",
        "resources": {
            "minimum": {"cpu_threads": 2, "memory_mb": 1024, "free_disk_bytes": 1_000_000},
            "recommended": {"cpu_threads": 4, "memory_mb": 2048, "free_disk_bytes": 2_000_000},
        },
        "networks": [
            {
                "id": "inside",
                "subnet_cidr": "10.42.1.0/24",
                "reachability": "isolated",
                "isolated": True,
                "guest_egress": False,
                "host_bridge": None,
            }
        ],
        "vms": [
            {
                "id": "target",
                "role": "target",
                "image_id": "ubuntu-24.04",
                "workload_id": None,
                "vcpus": 2,
                "memory_mb": 1024,
                "disk_gb": 12,
                "network_ids": ["inside"],
                "base_image": {
                    "image_id": "ubuntu-24.04",
                    "sha256": "b" * 64,
                    "size_bytes": 1024,
                    "media_type": "application/x-qemu-disk",
                    "distro": "ubuntu",
                    "version": "24.04",
                    "username": "ubuntu",
                    "cloud_init": True,
                },
                "boot_layer": asset_dict,
                "workload": {
                    "workload_baked": True,
                    "immutable_inputs": {"seed": "dvwa"},
                    "readiness_probes": [{"kind": "tcp", "port": 80}],
                },
            }
        ],
        "topology": [
            {"node_id": "inside", "node_type": "network", "x": 10.5, "y": 20.5},
            {"node_id": "target", "node_type": "vm", "x": 30.5, "y": 40.5},
        ],
        "experience": {
            "runbook_path": "experience/runbook.md",
            "objectives": ["Inspect the service"],
            "hints": [],
            "checkpoints": [{"id": "service", "answer": {"port": 8080, "ready": True}}],
        },
    }


def _valid_model_cases() -> list[tuple[type, dict]]:
    asset = {
        "sha256": SHA256,
        "size_bytes": 1,
        "media_type": "application/octet-stream",
        "license": "MIT",
    }
    resources = {
        "minimum": {"cpu_threads": 1, "memory_mb": 512, "free_disk_bytes": 1},
        "recommended": {"cpu_threads": 1, "memory_mb": 512, "free_disk_bytes": 1},
    }
    definition = {
        "package_id": "org.example.lab",
        "version": "1.0.0",
        "title": "Lab",
        "creator": "Creator",
        "license": "MIT",
        "reproducibility": "locked",
        "resources": resources,
        "networks": [{"id": "net", "subnet_cidr": "10.1.0.0/24", "reachability": "isolated"}],
        "vms": [
            {
                "id": "vm",
                "role": "target",
                "image_id": "image",
                "vcpus": 1,
                "memory_mb": 512,
                "disk_gb": 1,
                "network_ids": ["net"],
                "base_image": {
                    "image_id": "image",
                    "sha256": "b" * 64,
                    "size_bytes": 1,
                    "media_type": "application/x-qemu-disk",
                },
                "boot_layer": asset,
                "workload": {"workload_baked": False},
            }
        ],
        "experience": {},
    }
    return [
        (HostResources, resources["minimum"]),
        (ResourceRecommendations, resources),
        (AssetReference, asset),
        (
            LabImageSpec,
            {
                "image_id": "image",
                "sha256": "b" * 64,
                "size_bytes": 1,
                "media_type": "application/x-qemu-disk",
            },
        ),
        (LabNetwork, {"id": "net", "subnet_cidr": "10.1.0.0/24"}),
        (LabTopologyNode, {"node_id": "vm", "node_type": "vm", "x": 0, "y": 0}),
        (
            LabVM,
            {
                "id": "vm",
                "role": "target",
                "image_id": "image",
                "vcpus": 1,
                "memory_mb": 512,
                "disk_gb": 1,
                "network_ids": ["net"],
            },
        ),
        (LabExperience, {}),
        (LabWorkloadSpec, {"workload_baked": False}),
        (LabDefinition, definition),
        (
            ManifestPayload,
            {"path": "lab.json", "sha256": SHA256, "size_bytes": 1, "media_type": "application/json"},
        ),
        (
            LabManifest,
            {
                "package_id": "org.example.lab",
                "version": "1.0.0",
                "payloads": [
                    {"path": "lab.json", "sha256": SHA256, "size_bytes": 1, "media_type": "application/json"},
                    {
                        "path": "experience/runbook.md",
                        "sha256": "b" * 64,
                        "size_bytes": 1,
                        "media_type": "text/markdown",
                    },
                ],
            },
        ),
    ]


@pytest.mark.parametrize(("model_type", "payload"), _valid_model_cases())
def test_all_package_model_families_reject_unknown_fields(model_type, payload):
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model_type.model_validate({**payload, "unknown": "value"})


@pytest.mark.parametrize("model_type", [AssetReference, ManifestPayload])
def test_sha256_rejects_uppercase_and_non_sha256_values(model_type):
    payload = {
        "sha256": "A" * 64,
        "size_bytes": 1,
        "media_type": "application/octet-stream",
    }
    if model_type is AssetReference:
        payload["license"] = "MIT"
    else:
        payload["path"] = "payload.bin"

    with pytest.raises(ValidationError, match="String should match pattern"):
        model_type.model_validate(payload)


@pytest.mark.parametrize(
    "source",
    [
        "file:///tmp/image",
        "ftp://example.test/image",
        "ssh://host/image",
        "http:// example.com/image",
        "https://./image",
        "http://:80/image",
        "https:///image",
        "https://@/image",
        "https://example..com/image",
        "https://example.com../image",
        "https://-invalid.example/image",
        "https://invalid-.example/image",
        "https://example.test:not-a-port/image",
        "https://example.test:70000/image",
        "https://[2001:db8::1/image",
        "https://[v1.foo]/a",
        "https://[fe80::1%GG]/a",
        "https://[fe80::1%eth0]/a",
        "https://[fe80::1%25]/a",
        "https://user@@example.test/image",
        "https://user@example.test/image",
        "https://:pass@example.test/image",
        "https://user:@example.test/image",
        "https://example.test/%",
        "https://example.test/%GG",
        "https://example.test/image?download=%",
        "https://example.test/image#section-%GG",
        "https://example.test/a\\b",
        " http://example.test/image ",
        "http://exa\tmple.com/image",
        "http://example.test/\nimage",
        "http://example.test/an image",
        "https://example.test/\ud800",
    ],
)
def test_asset_sources_allow_only_http_and_https(source):
    with pytest.raises(ValidationError, match="asset source must use http or https"):
        AssetReference(
            sha256=SHA256,
            size_bytes=1,
            media_type="application/octet-stream",
            sources=(source,),
            license="MIT",
        )


@pytest.mark.parametrize(
    "source",
    [
        "http://example.test/a",
        "https://example.test/a",
        "https://example.test./a",
        "https://user:pass@example.test/a%20b",
        "https://[fe80::1%25eth0]/a",
        "https://[fe80::1%25eth%41]/a",
        "https://example.test/café/東京?q=✓",
    ],
)
def test_asset_sources_accept_http_and_https(source):
    assert AssetReference(
        sha256=SHA256,
        size_bytes=1,
        media_type="application/octet-stream",
        sources=(source,),
        license="MIT",
    ).sources == (source,)


def test_asset_source_has_no_implicit_url_length_cap():
    source = "https://example.test/" + ("a" * 2100)
    asset = AssetReference(
        sha256=SHA256,
        size_bytes=1,
        media_type="application/octet-stream",
        sources=(source,),
        license="MIT",
    )
    assert asset.sources == (source,)


@pytest.mark.parametrize("source", ["https://example.test/a\u202eb", "https://example.test/a\u200bb"])
def test_asset_sources_reject_unicode_format_characters(source):
    with pytest.raises(ValidationError, match="asset source must use http or https"):
        AssetReference(
            sha256=SHA256,
            size_bytes=1,
            media_type="application/octet-stream",
            sources=(source,),
            license="MIT",
        )


@pytest.mark.parametrize(
    ("network", "message"),
    [
        ({"id": "n", "subnet_cidr": "not-a-cidr"}, "invalid subnet CIDR"),
        ({"id": "n", "subnet_cidr": "10.0.0.1/24"}, "subnet CIDR must be canonical"),
        ({"id": "n", "subnet_cidr": "10.0.0.0/31"}, "subnet must contain at least 4 addresses"),
        ({"id": "n", "subnet_cidr": "2001:db8::1/64"}, "subnet CIDR must be canonical"),
        ({"id": "n", "subnet_cidr": "2001:db8::/127"}, "subnet must contain at least 4 addresses"),
        (
            {
                "id": "n",
                "subnet_cidr": "10.0.0.0/24",
                "reachability": "isolated",
                "isolated": True,
                "guest_egress": True,
            },
            "isolated networks cannot enable guest egress",
        ),
        (
            {
                "id": "n",
                "subnet_cidr": "10.0.0.0/24",
                "reachability": "isolated",
                "isolated": True,
                "host_bridge": "br0",
            },
            "isolated networks cannot use a host bridge",
        ),
    ],
)
def test_network_cidr_and_semantic_rules(network, message):
    with pytest.raises(ValidationError, match=message):
        LabNetwork.model_validate(network)


def test_network_accepts_smallest_usable_ipv4_subnet():
    network = LabNetwork(id="n", subnet_cidr="10.0.0.0/30", reachability="nat", isolated=False, guest_egress=True)
    assert network.subnet_cidr == "10.0.0.0/30"


def test_network_accepts_canonical_usable_ipv6_subnet():
    network = LabNetwork(id="v6", subnet_cidr="2001:db8::/64", reachability="isolated")
    assert network.subnet_cidr == "2001:db8::/64"


@pytest.mark.parametrize("dimension", ["cpu_threads", "memory_mb", "free_disk_bytes"])
def test_recommended_resources_cannot_be_below_minimum(dimension):
    minimum = {"cpu_threads": 4, "memory_mb": 4096, "free_disk_bytes": 4000}
    recommended = dict(minimum)
    recommended[dimension] -= 1

    with pytest.raises(ValidationError, match=f"recommended {dimension} must be at least minimum"):
        ResourceRecommendations.model_validate({"minimum": minimum, "recommended": recommended})


def test_definition_rejects_duplicate_network_and_vm_ids(locked_definition_dict):
    duplicate_network = locked_definition_dict["networks"][0].copy()
    duplicate_vm = locked_definition_dict["vms"][0].copy()

    with pytest.raises(ValidationError, match="duplicate network id: inside"):
        LabDefinition.model_validate(
            {**locked_definition_dict, "networks": [*locked_definition_dict["networks"], duplicate_network]}
        )
    with pytest.raises(ValidationError, match="duplicate VM id: target"):
        LabDefinition.model_validate({**locked_definition_dict, "vms": [*locked_definition_dict["vms"], duplicate_vm]})


def test_definition_rejects_duplicate_and_missing_vm_network_references(locked_definition_dict):
    vm = dict(locked_definition_dict["vms"][0])
    vm["network_ids"] = ["inside", "inside"]
    with pytest.raises(ValidationError, match="VM target has duplicate network id: inside"):
        LabDefinition.model_validate({**locked_definition_dict, "vms": [vm]})

    vm["network_ids"] = ["missing"]
    with pytest.raises(ValidationError, match="VM target references unknown network: missing"):
        LabDefinition.model_validate({**locked_definition_dict, "vms": [vm]})


@pytest.mark.parametrize("subnet_cidr", ["10.42.1.0/24", "10.42.1.128/25"])
def test_definition_allows_overlapping_subnets_in_distinct_network_namespaces(locked_definition_dict, subnet_cidr):
    overlapping = {"id": "other-namespace", "subnet_cidr": subnet_cidr, "reachability": "isolated"}
    definition = LabDefinition.model_validate(
        {**locked_definition_dict, "networks": [*locked_definition_dict["networks"], overlapping]}
    )
    assert [network.id for network in definition.networks] == ["inside", "other-namespace"]


def test_definition_rejects_duplicate_missing_and_mistyped_topology_references(locked_definition_dict):
    duplicate = [*locked_definition_dict["topology"], locked_definition_dict["topology"][0]]
    with pytest.raises(ValidationError, match="duplicate topology node reference: inside"):
        LabDefinition.model_validate({**locked_definition_dict, "topology": duplicate})

    missing = [{"node_id": "missing", "node_type": "vm", "x": 0, "y": 0}]
    with pytest.raises(ValidationError, match="topology references unknown VM: missing"):
        LabDefinition.model_validate({**locked_definition_dict, "topology": missing})

    mistyped = [{"node_id": "inside", "node_type": "vm", "x": 0, "y": 0}]
    with pytest.raises(ValidationError, match="topology node inside has type vm but references a network"):
        LabDefinition.model_validate({**locked_definition_dict, "topology": mistyped})


def test_topology_node_type_disambiguates_vm_and_network_with_same_logical_id(locked_definition_dict):
    vm = dict(locked_definition_dict["vms"][0])
    vm["id"] = "inside"
    topology = [
        {"node_id": "inside", "node_type": "network", "x": 0, "y": 0},
        {"node_id": "inside", "node_type": "vm", "x": 1, "y": 1},
    ]

    definition = LabDefinition.model_validate({**locked_definition_dict, "vms": [vm], "topology": topology})
    assert [(node.node_type, node.node_id) for node in definition.topology] == [
        ("network", "inside"),
        ("vm", "inside"),
    ]


@pytest.mark.parametrize("coordinate", [math.inf, -math.inf, math.nan])
def test_topology_coordinates_must_be_finite(coordinate):
    with pytest.raises(ValidationError, match="topology coordinates must be finite"):
        LabTopologyNode(node_id="vm", node_type="vm", x=coordinate, y=0)


def test_locked_definitions_require_stable_image_boot_layer_and_workload_contract(locked_definition_dict):
    vm = dict(locked_definition_dict["vms"][0])
    vm.pop("base_image")
    with pytest.raises(ValidationError, match="locked definition requires base_image for VM: target"):
        LabDefinition.model_validate({**locked_definition_dict, "vms": [vm]})

    vm = dict(locked_definition_dict["vms"][0])
    vm.pop("boot_layer")
    with pytest.raises(ValidationError, match="locked definition requires boot_layer for VM: target"):
        LabDefinition.model_validate({**locked_definition_dict, "vms": [vm]})

    vm = dict(locked_definition_dict["vms"][0])
    vm.pop("workload")
    with pytest.raises(ValidationError, match="locked definition requires workload contract for VM: target"):
        LabDefinition.model_validate({**locked_definition_dict, "vms": [vm]})

    vm = dict(locked_definition_dict["vms"][0])
    vm["workload"] = {"workload_baked": False}
    with pytest.raises(ValidationError, match="locked definition requires a baked workload for VM: target"):
        LabDefinition.model_validate({**locked_definition_dict, "vms": [vm]})


def test_locked_boot_layer_may_be_a_baked_derivative_of_the_base_image(locked_definition_dict):
    definition = LabDefinition.model_validate(locked_definition_dict)
    vm = definition.vms[0]
    assert vm.base_image is not None and vm.boot_layer is not None
    assert vm.base_image.sha256 != vm.boot_layer.sha256
    assert vm.effective_boot_layer == vm.boot_layer


def test_locked_vm_requires_base_render_identity_and_a_single_boot_layer(locked_definition_dict):
    vm = dict(locked_definition_dict["vms"][0])
    vm["architecture"] = "x86_64"
    vm["firmware"] = "uefi"
    with pytest.raises(ValidationError, match="base image firmware must match VM firmware"):
        LabDefinition.model_validate({**locked_definition_dict, "vms": [vm]})

    vm = dict(locked_definition_dict["vms"][0])
    vm["clean_layer"] = dict(vm["boot_layer"])
    with pytest.raises(ValidationError, match="cannot specify both boot_layer and clean_layer"):
        LabDefinition.model_validate({**locked_definition_dict, "vms": [vm]})


def test_workload_immutable_inputs_are_bounded():
    with pytest.raises(ValidationError, match="at most 64"):
        LabWorkloadSpec(workload_baked=True, immutable_inputs={str(index): index for index in range(65)})


def test_network_requires_explicit_supported_reachability_for_locked_definitions(locked_definition_dict):
    network = dict(locked_definition_dict["networks"][0])
    network["reachability"] = "nat"
    network["guest_egress"] = False
    with pytest.raises(ValidationError, match="NAT networks must enable guest egress"):
        LabDefinition.model_validate({**locked_definition_dict, "networks": [network]})

    network = dict(locked_definition_dict["networks"][0])
    network["host_bridge"] = "br0"
    network["isolated"] = False
    network["reachability"] = "nat"
    network["guest_egress"] = True
    with pytest.raises(ValidationError, match="locked definition does not support host bridge"):
        LabDefinition.model_validate({**locked_definition_dict, "networks": [network]})


def test_legacy_unprovable_locked_definition_is_normalized_to_unlocked(locked_definition_dict):
    legacy = dict(locked_definition_dict)
    vm = dict(legacy["vms"][0])
    vm.pop("base_image")
    vm.pop("boot_layer")
    vm.pop("workload")
    vm["clean_layer"] = vm.get("boot_layer", {"sha256": SHA256, "size_bytes": 1, "media_type": "a", "license": "MIT"})
    legacy["vms"] = [vm]
    legacy["networks"] = [{"id": "inside", "subnet_cidr": "10.42.1.0/24"}]

    normalized = normalize_legacy_definition_payload(legacy)

    assert normalized["reproducibility"] == "unlocked"
    assert LabDefinition.model_validate(normalized).reproducibility == "unlocked"


def test_unlocked_definitions_may_omit_clean_layers(locked_definition_dict):
    vm = dict(locked_definition_dict["vms"][0])
    vm["clean_layer"] = None
    definition = LabDefinition.model_validate({**locked_definition_dict, "reproducibility": "unlocked", "vms": [vm]})
    assert definition.vms[0].clean_layer is None


def test_manifest_payload_paths_must_be_unique():
    payload = {"path": "lab.json", "sha256": SHA256, "size_bytes": 1, "media_type": "application/json"}
    with pytest.raises(ValidationError, match="duplicate manifest payload path: lab.json"):
        LabManifest.model_validate(
            {"package_id": "org.example.lab", "version": "1.0.0", "payloads": [payload, payload]}
        )


@pytest.mark.parametrize("path", ["manifest.json", "lab.json", "experience/runbook.md", "assets/clean.qcow2"])
def test_manifest_payload_paths_accept_canonical_relative_posix_members(path):
    payload = ManifestPayload(path=path, sha256=SHA256, size_bytes=1, media_type="application/octet-stream")
    assert payload.path == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/manifest.json",
        "//host/share.json",
        "C:/manifest.json",
        "C:manifest.json",
        "assets/clean.qcow2:payload",
        r"assets\\clean.qcow2",
        ".",
        "..",
        "assets/./clean.qcow2",
        "assets/../clean.qcow2",
        "assets//clean.qcow2",
        "assets/",
        "assets/clean\x00.qcow2",
        "assets/clean\u202e.qcow2",
        "assets/clean\ud800.qcow2",
    ],
)
def test_manifest_payload_paths_reject_noncanonical_or_unsafe_members(path):
    with pytest.raises(ValidationError, match="invalid manifest payload path"):
        ManifestPayload(path=path, sha256=SHA256, size_bytes=1, media_type="application/octet-stream")


def test_manifest_rejects_paths_that_collide_after_unicode_normalization():
    payloads = [
        {"path": "assets/café.qcow2", "sha256": SHA256, "size_bytes": 1, "media_type": "application/octet-stream"},
        {
            "path": "assets/cafe\u0301.qcow2",
            "sha256": SHA256,
            "size_bytes": 1,
            "media_type": "application/octet-stream",
        },
    ]
    with pytest.raises(ValidationError, match="duplicate manifest payload path: assets/café.qcow2"):
        LabManifest.model_validate({"package_id": "org.example.lab", "version": "1.0.0", "payloads": payloads})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("package_id", ""),
        ("package_id", "a"),
        ("package_id", "invalid/value"),
        ("version", ""),
        ("version", "1.0"),
        ("version", "v1.0.0"),
        ("version", "1.0.0+build"),
    ],
)
def test_manifest_identity_validation_matches_lab_definition(locked_definition_dict, field, value):
    manifest_payload = {
        "package_id": "org.example.lab",
        "version": "1.0.0",
        "payloads": [
            {"path": "manifest.json", "sha256": SHA256, "size_bytes": 1, "media_type": "application/json"},
            {"path": "lab.json", "sha256": SHA256, "size_bytes": 1, "media_type": "application/json"},
        ],
    }
    with pytest.raises(ValidationError) as definition_error:
        LabDefinition.model_validate({**locked_definition_dict, field: value})
    with pytest.raises(ValidationError) as manifest_error:
        LabManifest.model_validate({**manifest_payload, field: value})

    assert (
        definition_error.value.errors(include_url=False)[0]["type"]
        == manifest_error.value.errors(include_url=False)[0]["type"]
    )


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_experience_rejects_nonfinite_nested_checkpoint_values(value):
    with pytest.raises(ValidationError, match="checkpoint JSON values must be finite"):
        LabExperience.model_validate({"checkpoints": [{"id": "service", "nested": {"values": [value]}}]})


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (AssetReference, "sources"),
        (LabVM, "network_ids"),
        (LabExperience, "objectives"),
        (LabExperience, "hints"),
        (LabExperience, "checkpoints"),
        (LabDefinition, "networks"),
        (LabDefinition, "vms"),
        (LabDefinition, "topology"),
        (LabManifest, "required_capabilities"),
        (LabManifest, "payloads"),
    ],
)
def test_immutable_collection_fields_are_annotated_as_tuples(model, field):
    assert get_origin(model.model_fields[field].annotation) is tuple


def test_immutable_collection_fields_accept_and_serialize_json_arrays(locked_definition_dict, asset_dict):
    definition = LabDefinition.model_validate(locked_definition_dict)
    manifest = LabManifest.model_validate(
        {
            "package_id": "org.example.lab",
            "version": "1.0.0",
            "required_capabilities": ["kvm"],
            "payloads": [
                {"path": "lab.json", "sha256": SHA256, "size_bytes": 1, "media_type": "application/json"},
                {
                    "path": "manifest.json",
                    "sha256": SHA256,
                    "size_bytes": 1,
                    "media_type": "application/json",
                },
            ],
        }
    )
    asset = AssetReference.model_validate(asset_dict)

    assert isinstance(asset.model_dump(mode="json")["sources"], list)
    assert isinstance(definition.vms[0].model_dump(mode="json")["network_ids"], list)
    assert isinstance(definition.experience.model_dump(mode="json")["objectives"], list)
    assert isinstance(definition.experience.model_dump(mode="json")["hints"], list)
    assert isinstance(definition.experience.model_dump(mode="json")["checkpoints"], list)
    assert isinstance(definition.model_dump(mode="json")["networks"], list)
    assert isinstance(definition.model_dump(mode="json")["vms"], list)
    assert isinstance(definition.model_dump(mode="json")["topology"], list)
    assert isinstance(manifest.model_dump(mode="json")["required_capabilities"], list)
    assert isinstance(manifest.model_dump(mode="json")["payloads"], list)


def test_validated_collections_and_nested_json_are_immutable(locked_definition_dict):
    definition = LabDefinition.model_validate(locked_definition_dict)

    with pytest.raises(AttributeError):
        definition.vms.append(definition.vms[0])  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        definition.vms[0].network_ids.append("other")  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        cast(dict[str, object], definition.experience.checkpoints[0]["answer"])["port"] = 80


def test_builtin_base_descriptors_cannot_bypass_collection_immutability(locked_definition_dict):
    definition = LabDefinition.model_validate(locked_definition_dict)

    with pytest.raises(TypeError):
        list.append(cast(list[LabVM], definition.vms), definition.vms[0])
    with pytest.raises(TypeError):
        dict.__setitem__(cast(dict[str, object], definition.experience.checkpoints[0]), "mutated", True)


def test_model_validation_detaches_nested_input_state(locked_definition_dict):
    definition = LabDefinition.model_validate(locked_definition_dict)
    before = canonical_bytes(definition)
    locked_definition_dict["vms"][0]["network_ids"].append("mutated")
    locked_definition_dict["experience"]["checkpoints"][0]["answer"]["port"] = 80
    assert canonical_bytes(definition) == before


def test_canonical_serialization_is_compact_utf8_sorted_and_excludes_none(locked_definition_dict):
    definition = LabDefinition.model_validate(locked_definition_dict)
    serialized = canonical_bytes(definition)

    assert b"Caf\xc3\xa9" in serialized
    assert b'"description":"A deterministic lab"' in serialized
    assert b'"workload_id"' not in serialized
    assert b'"host_bridge"' not in serialized
    assert b": " not in serialized and b", " not in serialized
    assert serialized.index(b'"answer"') < serialized.index(b'"id"')


def test_canonical_serialization_keeps_semantically_ordered_lists(locked_definition_dict):
    locked_definition_dict["experience"]["objectives"] = ["second", "first"]
    definition = LabDefinition.model_validate(locked_definition_dict)
    assert b'"objectives":["second","first"]' in canonical_bytes(definition)


def test_canonical_serialization_emits_no_serializer_warnings(locked_definition_dict):
    definition = LabDefinition.model_validate(locked_definition_dict)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        canonical_bytes(definition)


def test_canonical_serialization_rejects_nan_as_final_defense():
    invalid = LabTopologyNode.model_construct(node_id="vm", node_type="vm", x=math.nan, y=0.0)
    with pytest.raises(ValueError, match="Out of range float values are not JSON compliant"):
        canonical_bytes(invalid)


def test_canonical_digest_ignores_input_mapping_order(locked_definition_dict):
    left = LabDefinition.model_validate(locked_definition_dict)
    right_data = dict(reversed(list(locked_definition_dict.items())))
    right = LabDefinition.model_validate(right_data)

    assert canonical_bytes(left) == canonical_bytes(right)
    assert canonical_sha256(left) == canonical_sha256(right)


def test_canonical_sha256_matches_fixed_vector():
    resources = HostResources(cpu_threads=2, memory_mb=512, free_disk_bytes=3)
    assert canonical_bytes(resources) == b'{"cpu_threads":2,"free_disk_bytes":3,"memory_mb":512}'
    assert canonical_sha256(resources) == "c7e783f6c5b1eb639b52a6e474f160ec1039dfa70c7bf67e1dae2bfcd8713d18"
