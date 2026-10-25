import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "ops" / "host" / "prepare_target_image.sh"
# Prefer the real catalog when present; fall back to the committed example
# (the live targets.yaml is gitignored and absent in CI), mirroring catalog.py.
_CATALOG = REPO_ROOT / "config" / "catalog" / "targets.yaml"
CATALOG = _CATALOG if _CATALOG.exists() else REPO_ROOT / "config" / "catalog" / "targets.example.yaml"


def _resolve(arg: str) -> dict[str, str]:
    out = subprocess.run(
        [str(SCRIPT), arg, "--resolve-only"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result = {}
    for line in out.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


def _catalog_ids() -> list[str]:
    doc = yaml.safe_load(CATALOG.read_text(encoding="utf-8")) or {}
    return [t["id"] for t in doc.get("targets", []) if "id" in t]


def test_resolve_only_matches_catalog_image_ref():
    doc = yaml.safe_load(CATALOG.read_text(encoding="utf-8")) or {}
    targets = {t["id"]: t for t in doc.get("targets", [])}
    target_id = "ubuntu-24.04"
    assert target_id in targets, "fixture id missing from catalog"
    expected_ref = targets[target_id]["image"]["image_ref"]

    resolved = _resolve(target_id)
    assert resolved["image_ref"] == expected_ref
    assert resolved["path"].endswith(f"images/definitions/{expected_ref}")
    assert resolved["username"] == targets[target_id]["image"].get("default_username", "ubuntu")


@pytest.mark.parametrize("target_id", _catalog_ids())
def test_resolve_only_works_for_every_catalog_id(target_id):
    resolved = _resolve(target_id)
    assert resolved["image_ref"]
    assert resolved["path"].endswith(f"images/definitions/{resolved['image_ref']}")


def test_raw_filename_passthrough():
    resolved = _resolve("some-stock-image.qcow2")
    assert resolved["image_ref"] == "some-stock-image.qcow2"
    assert resolved["username"] == "ubuntu"
