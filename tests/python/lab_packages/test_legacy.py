from __future__ import annotations

import io
import zipfile

from astinuslab.lab_packages.legacy import definition_from_scenario_package


def test_legacy_scenario_package_maps_to_unlocked_definition():
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "scenario.yaml",
            "schema_version: 1\nname: demo\nnetworks:\n- name: lab\n  subnet_cidr: 10.42.1.0/24\nvms:\n- label: target\n  role: target\n  image_id: ubuntu\n  networks: [lab]\ntopology: []\n",
        )
        archive.writestr("metadata.yaml", "name: Demo\n")

    definition = definition_from_scenario_package(
        payload.getvalue(),
        package_id="demo-lab",
        version="1.0.0",
        creator="tester",
        license="CC-BY-4.0",
    )

    assert definition.reproducibility == "unlocked"
    assert all(vm.clean_layer is None for vm in definition.vms)
    assert "infrastructure only" in definition.experience.objectives[0].lower()
