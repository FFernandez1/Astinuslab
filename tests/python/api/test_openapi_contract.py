from __future__ import annotations

import pytest


@pytest.fixture()
def openapi_schema(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    return response.json()


def test_openapi_exposes_core_v1_workflow_paths(openapi_schema):
    paths = openapi_schema["paths"]

    expected_methods = {
        "/api/v1/jobs": {"get", "post"},
        "/api/v1/playground/sessions/current-or-null": {"get"},
        "/api/v1/playground/vms": {"post"},
        "/api/v1/playground/networks": {"post"},
        "/api/v1/playground/networks/suggest-cidr": {"get"},
        "/api/v1/playground/recipes/import": {"post"},
        "/api/v1/scenario-packages/playground/current": {"get"},
        "/api/v1/scenario-packages/import": {"post"},
        "/api/v1/artifacts/{artifact_id}/download": {"get"},
        "/api/v1/artifacts/{artifact_id}/manifest": {"get"},
        "/api/v1/artifacts/{artifact_id}/sha256": {"get"},
        "/api/v1/volatility/readiness": {"get"},
    }

    for path, methods in expected_methods.items():
        assert path in paths
        assert methods.issubset(paths[path])


def test_openapi_describes_volatility_readiness_payload(openapi_schema):
    operation = openapi_schema["paths"]["/api/v1/volatility/readiness"]["get"]
    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_schema == {"$ref": "#/components/schemas/Vol3ReadinessResponse"}

    schemas = openapi_schema["components"]["schemas"]
    readiness = schemas["Vol3ReadinessResponse"]
    assert set(readiness["properties"]) == {"enabled", "analysis", "isf_build"}
    assert set(readiness["required"]) == {"enabled", "analysis", "isf_build"}
    assert readiness["properties"]["enabled"]["type"] == "boolean"
    assert readiness["properties"]["analysis"] == {"$ref": "#/components/schemas/Vol3ToolReadinessResponse"}
    assert readiness["properties"]["isf_build"] == {"$ref": "#/components/schemas/Vol3ToolReadinessResponse"}

    tool = schemas["Vol3ToolReadinessResponse"]
    assert set(tool["properties"]) == {"ready", "code", "detail", "version", "untested"}
    assert set(tool["required"]) == {"ready", "code", "detail", "version", "untested"}
    assert tool["properties"]["ready"]["type"] == "boolean"
    assert tool["properties"]["code"]["type"] == "string"
    assert tool["properties"]["detail"]["type"] == "string"
    assert {item["type"] for item in tool["properties"]["version"]["anyOf"]} == {"string", "null"}


def test_openapi_playground_mocked_ui_fields_stay_in_schema(openapi_schema):
    schemas = openapi_schema["components"]["schemas"]

    session = schemas["PlaygroundSessionOut"]["properties"]
    assert {"job_id", "status", "vms", "networks"}.issubset(session)

    vm = schemas["PlaygroundVMOut"]["properties"]
    assert {
        "id",
        "job_id",
        "label",
        "image_id",
        "state",
        "role",
        "services",
        "nics",
        "can_capture_memory",
        "can_capture_disk",
        "can_destroy",
        "can_start",
        "can_stop",
        "can_reboot",
        "action_block_reason",
    }.issubset(vm)

    network = schemas["PlaygroundNetworkOut"]["properties"]
    assert {
        "id",
        "job_id",
        "name",
        "subnet_cidr",
        "isolated",
        "state",
        "attached_vm_count",
        "can_delete",
        "action_block_reason",
    }.issubset(network)


def test_openapi_playground_create_payloads_keep_ui_contract(openapi_schema):
    schemas = openapi_schema["components"]["schemas"]

    create_vm = schemas["PlaygroundCreateIfNeededSpawnVMRequest"]["properties"]
    assert {
        "label",
        "image_id",
        "vcpus",
        "memory_mb",
        "disk_gb",
        "role",
        "workload_id",
        "network_ids",
        "new_networks",
    }.issubset(create_vm)

    recipe_vm = schemas["PlaygroundRecipeVM"]["properties"]
    assert {"vcpus", "memory_mb", "disk_gb"}.issubset(recipe_vm)

    create_network = schemas["PlaygroundCreateNetworkRequest"]["properties"]
    assert {"name", "subnet_cidr", "isolated"}.issubset(create_network)

    cidr = schemas["PlaygroundCidrSuggestionOut"]["properties"]
    assert "subnet_cidr" in cidr


def test_openapi_artifact_server_paths_are_removed_from_schema(openapi_schema):
    artifact = openapi_schema["components"]["schemas"]["ArtifactOut"]["properties"]

    assert {
        "id",
        "job_id",
        "acquisition_id",
        "producer",
        "kind",
        "filename",
        "sha256",
        "size_bytes",
    }.issubset(artifact)
    assert "path" not in artifact
    assert "manifest_path" not in artifact
    assert "sha256_sidecar_path" not in artifact
