"""Tests for catalog YAML loading."""

from __future__ import annotations

import io
import os
import stat
import zipfile
from types import SimpleNamespace

import pytest
import yaml

from astinuslab.catalog import load_scenarios, load_targets
from tests.python._support.helpers import autospec_setattr


def _write_workloads_catalog(path, workload_ids: list[str]) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "workloads": [{"id": workload_id, "display_name": workload_id} for workload_id in workload_ids],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _bump_mtime(path) -> None:
    stat = os.stat(path)
    new_ns = stat.st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(new_ns, new_ns))


class TestLoadTargets:
    def test_returns_list(self):
        targets = load_targets()
        assert isinstance(targets, list)
        assert len(targets) > 0

    def test_each_target_has_required_fields(self):
        for t in load_targets():
            assert "id" in t
            assert "distro" in t
            assert "version" in t
            assert "enabled" in t

    def test_known_target_present(self):
        ids = {t["id"] for t in load_targets()}
        assert "ubuntu-22-04-default" in ids

    def test_enabled_flag_is_bool(self):
        for t in load_targets():
            assert isinstance(t["enabled"], bool)

    def test_missing_image_disables_curated_target(self):
        fedora = next(t for t in load_targets() if t["id"] == "fedora-40")
        assert fedora["image_available"] is False
        assert fedora["enabled"] is False

    def test_scan_infers_fedora_43_metadata(self):
        from astinuslab.catalog import _infer_from_filename

        meta = _infer_from_filename("Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2")
        assert meta["distro"] == "fedora"
        assert meta["version"] == "43"
        assert meta["default_username"] == "fedora"
        assert meta["os_variant"] in {"fedora-unknown", "generic"}

    def test_scan_infers_non_debian_non_redhat_family_as_other(self):
        from astinuslab.catalog import _infer_from_filename

        meta = _infer_from_filename("arch-linux-cloudimg.qcow2")

        assert meta["distro"] == "arch"
        assert meta["family"] == "arch"
        assert meta["default_username"] == "arch"

    def test_scan_images_dir_ignores_non_auto_bootable_extensions(self, tmp_path, monkeypatch):
        import astinuslab.catalog as catalog

        images_dir = tmp_path / "images"
        images_dir.mkdir()
        (images_dir / "debian-12.qcow2").write_bytes(b"")
        (images_dir / "debian-12.raw").write_bytes(b"")

        autospec_setattr(monkeypatch, catalog, "_supported_os_variants", lambda: {"generic"})

        entries = catalog._scan_images_dir(images_dir)

        assert [entry["image_ref"] for entry in entries] == ["debian-12.qcow2"]

    def test_invalidate_catalog_caches_clears_all_catalog_loader_caches(self, tmp_path, monkeypatch):
        import astinuslab.catalog as catalog

        images_dir = tmp_path / "images"
        images_dir.mkdir()
        targets_yaml = tmp_path / "targets.yaml"
        targets_yaml.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "targets": [
                        {
                            "id": "target-one",
                            "enabled": True,
                            "distro": {"slug": "ubuntu", "version": "22.04", "family": "debian"},
                            "image": {
                                "source_type": "qcow2",
                                "image_ref": "target.qcow2",
                                "default_username": "ubuntu",
                            },
                            "virtualization": {"os_variant": "generic"},
                        }
                    ],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        network_nodes_yaml = tmp_path / "network_nodes.yaml"
        network_nodes_yaml.write_text(
            yaml.safe_dump(
                {"version": 1, "network_nodes": [{"id": "router", "image_ref": "router.qcow2"}]},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        workloads_yaml = tmp_path / "workloads.yaml"
        _write_workloads_catalog(workloads_yaml, ["workload-one"])
        attackers_yaml = tmp_path / "attackers.yaml"
        attackers_yaml.write_text(
            yaml.safe_dump(
                {"version": 1, "attackers": [{"id": "kali", "image_ref": "kali.qcow2"}]},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        scenarios_yaml = tmp_path / "scenarios.yaml"
        scenarios_yaml.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "scenarios": [
                        {
                            "id": "manual",
                            "enabled": True,
                            "mode": "manual_trigger",
                            "description": "Manual",
                            "script": {"enabled": False},
                        }
                    ],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            catalog,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                targets_catalog_path=targets_yaml,
                network_nodes_catalog_path=network_nodes_yaml,
                workloads_catalog_path=workloads_yaml,
                attackers_catalog_path=attackers_yaml,
                scenarios_catalog_path=scenarios_yaml,
                images_dir=images_dir,
            ),
        )
        autospec_setattr(monkeypatch, catalog, "_supported_os_variants", lambda: {"generic"})

        catalog.invalidate_catalog_caches()
        catalog.load_targets()
        catalog.load_network_nodes()
        catalog.load_workloads()
        catalog.load_attackers()
        catalog.load_scenarios()

        private_caches = [
            catalog._load_targets_cached,
            catalog._load_network_nodes_cached,
            catalog._load_workloads_cached,
            catalog._load_attackers_cached,
            catalog._load_scenarios_cached,
        ]
        assert [cache.cache_info().currsize for cache in private_caches] == [1, 1, 1, 1, 1]

        catalog.invalidate_catalog_caches()

        assert [cache.cache_info().currsize for cache in private_caches] == [0, 0, 0, 0, 0]

    def test_invalidate_catalog_caches_reloads_distro_profiles_after_base_dir_change(self, tmp_path, monkeypatch):
        import astinuslab.catalog as catalog

        first_base = tmp_path / "first"
        second_base = tmp_path / "second"
        for base_dir, distro in ((first_base, "firstos"), (second_base, "secondos")):
            profile_dir = base_dir / "config" / "catalog"
            profile_dir.mkdir(parents=True)
            (profile_dir / "profiles.yaml").write_text(
                yaml.safe_dump(
                    {
                        "profiles": [
                            {
                                "id": distro,
                                "rules": [{"pattern": r"^sample\.qcow2$"}],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

        settings = SimpleNamespace(base_dir=first_base, data_dir=first_base)
        monkeypatch.setattr(catalog, "settings", settings)
        autospec_setattr(monkeypatch, catalog, "_supported_os_variants", lambda: {"generic"})

        catalog.invalidate_catalog_caches()
        assert catalog._infer_from_filename("sample.qcow2")["distro"] == "firstos"

        settings.base_dir = second_base

        settings.data_dir = second_base
        catalog.invalidate_catalog_caches()

        assert catalog._infer_from_filename("sample.qcow2")["distro"] == "secondos"

    def test_resolve_os_variant_uses_supported_value(self, monkeypatch):
        import astinuslab.catalog as catalog

        catalog._supported_os_variants.cache_clear()
        autospec_setattr(monkeypatch, catalog, "_supported_os_variants", lambda: {"ubuntu24.04", "generic"})

        assert catalog._resolve_os_variant("ubuntu24.04", distro="ubuntu") == "ubuntu24.04"

    def test_resolve_os_variant_falls_back_to_distro_unknown(self, monkeypatch):
        import astinuslab.catalog as catalog

        catalog._supported_os_variants.cache_clear()
        autospec_setattr(
            monkeypatch, catalog, "_supported_os_variants", lambda: {"fedora42", "fedora-unknown", "generic"}
        )

        assert catalog._resolve_os_variant("fedora43", distro="fedora") == "fedora-unknown"

    def test_supported_os_variants_times_out_to_generic(self, tmp_path, monkeypatch):
        import subprocess
        from types import SimpleNamespace

        import astinuslab.catalog as catalog

        def fake_run(cmd, **kwargs):
            assert cmd == ["virt-install", "--osinfo", "list"]
            assert kwargs["timeout"] == catalog.CATALOG_OSINFO_TIMEOUT_SECONDS
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

        catalog._supported_os_variants.cache_clear()
        monkeypatch.setattr(
            catalog,
            "settings",
            SimpleNamespace(base_dir=tmp_path, data_dir=tmp_path, storage_dir=tmp_path / "storage"),
        )
        monkeypatch.setattr(catalog.subprocess, "run", fake_run)

        assert catalog._supported_os_variants() == {"generic"}

        catalog._supported_os_variants.cache_clear()

    def test_supported_os_variants_survive_an_unwritable_shared_cache(self, tmp_path, monkeypatch):
        """Under split identities storage/tmp belongs to the runner.

        The osinfo cache is an optimisation, so the API identity must still get
        the real variant list instead of a PermissionError that fails a spawn
        request with HTTP 500.
        """
        from types import SimpleNamespace

        import astinuslab.catalog as catalog

        shared = tmp_path / "storage/tmp"
        shared.mkdir(parents=True)
        shared.chmod(0o500)
        observed: list[str] = []

        def fake_run(cmd, **kwargs):
            assert cmd == ["virt-install", "--osinfo", "list"]
            cache_home = kwargs["env"]["XDG_CACHE_HOME"]
            observed.append(cache_home)
            assert os.access(cache_home, os.W_OK), "the fallback cache must be writable"
            return SimpleNamespace(stdout="ubuntu24.04\nfedora42, fedora\n")

        catalog._supported_os_variants.cache_clear()
        monkeypatch.setattr(
            catalog,
            "settings",
            SimpleNamespace(base_dir=tmp_path, data_dir=tmp_path, storage_dir=tmp_path / "storage"),
        )
        monkeypatch.setattr(catalog.subprocess, "run", fake_run)
        try:
            assert catalog._supported_os_variants() == {"ubuntu24.04", "fedora42", "generic"}
        finally:
            catalog._supported_os_variants.cache_clear()
            shared.chmod(0o700)

        assert not observed[0].startswith(str(shared)), "an unwritable shared cache must not be used"

    def test_curated_target_can_define_cloud_init_compatibility(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        import astinuslab.catalog as catalog

        catalog_yaml = tmp_path / "targets.yaml"
        catalog_yaml.write_text(
            """
version: 1
targets:
  - id: ubuntu-20-04-legacy
    enabled: true
    distro:
      slug: ubuntu
      version: "20.04"
      family: debian
    image:
      source_type: img
      image_ref: ubuntu-20.04-server-cloudimg-amd64-disk-kvm.img
      default_username: ubuntu
      cloud_init_profile: ubuntu
      inject_network_config: false
    virtualization:
      os_variant: ubuntu20.04
      network_profile: default
""".strip(),
            encoding="utf-8",
        )

        images_dir = tmp_path / "images"
        images_dir.mkdir()
        (images_dir / "ubuntu-20.04-server-cloudimg-amd64-disk-kvm.img").write_text("", encoding="utf-8")

        monkeypatch.setattr(
            catalog,
            "settings",
            SimpleNamespace(
                base_dir=tmp_path,
                data_dir=tmp_path,
                storage_dir=tmp_path / "storage",
                targets_catalog_path=catalog_yaml,
                images_dir=images_dir,
            ),
        )
        autospec_setattr(monkeypatch, catalog, "_supported_os_variants", lambda: {"ubuntu20.04", "generic"})

        catalog.invalidate_catalog_caches()
        targets = catalog.load_targets()
        target = next(t for t in targets if t["id"] == "ubuntu-20-04-legacy")
        assert target["cloud_init_profile"] == "ubuntu"
        assert target["inject_network_config"] is False

    def test_curated_attacker_can_enable_cloud_init(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        import astinuslab.catalog as catalog

        attackers_yaml = tmp_path / "attackers.yaml"
        attackers_yaml.write_text(
            """
version: 1
attackers:
  - id: kali
    display_name: Kali
    image_ref: kali.qcow2
    username: kali
    os_variant: generic
    cloud_init: true
    enabled: true
""".strip(),
            encoding="utf-8",
        )
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        (images_dir / "kali.qcow2").write_text("", encoding="utf-8")

        monkeypatch.setattr(
            catalog,
            "settings",
            SimpleNamespace(
                attackers_catalog_path=attackers_yaml,
                images_dir=images_dir,
            ),
        )

        catalog.invalidate_catalog_caches()
        attackers = catalog.load_attackers()
        attacker = next(a for a in attackers if a["id"] == "kali")
        assert attacker["cloud_init"] is True
        assert catalog.list_playground_images_for_role("attacker")[0]["cloud_init"] is True
        catalog.invalidate_catalog_caches()


class TestCatalogCacheCoherence:
    def test_workloads_reloads_when_catalog_stamp_changes(self, tmp_path, monkeypatch):
        import astinuslab.catalog as catalog

        workloads_yaml = tmp_path / "workloads.yaml"
        _write_workloads_catalog(workloads_yaml, ["first"])
        monkeypatch.setattr(catalog, "settings", SimpleNamespace(workloads_catalog_path=workloads_yaml))

        catalog.invalidate_catalog_caches()
        first = catalog.load_workloads()
        assert [item["id"] for item in first] == ["first"]

        _write_workloads_catalog(workloads_yaml, ["first", "second"])
        _bump_mtime(workloads_yaml)

        second = catalog.load_workloads()
        assert [item["id"] for item in second] == ["first", "second"]

    def test_workloads_cache_hit_returns_same_object_when_catalog_unchanged(self, tmp_path, monkeypatch):
        import astinuslab.catalog as catalog

        workloads_yaml = tmp_path / "workloads.yaml"
        _write_workloads_catalog(workloads_yaml, ["cached"])
        monkeypatch.setattr(catalog, "settings", SimpleNamespace(workloads_catalog_path=workloads_yaml))

        catalog.invalidate_catalog_caches()
        first = catalog.load_workloads()
        second = catalog.load_workloads()

        assert second is first
        assert [item["id"] for item in second] == ["cached"]

    def test_deleted_network_nodes_catalog_reloads_to_missing_file_behavior(self, tmp_path, monkeypatch):
        import astinuslab.catalog as catalog

        images_dir = tmp_path / "images"
        images_dir.mkdir()
        network_nodes_yaml = tmp_path / "network_nodes.yaml"
        network_nodes_yaml.write_text(
            yaml.safe_dump(
                {"version": 1, "network_nodes": [{"id": "router", "image_ref": "router.qcow2"}]},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            catalog,
            "settings",
            SimpleNamespace(network_nodes_catalog_path=network_nodes_yaml, images_dir=images_dir),
        )

        catalog.invalidate_catalog_caches()
        assert [item["id"] for item in catalog.load_network_nodes()] == ["router"]

        network_nodes_yaml.unlink()

        assert catalog.load_network_nodes() == []


class TestLoadScenarios:
    def test_returns_list(self):
        scenarios = load_scenarios()
        assert isinstance(scenarios, list)
        assert len(scenarios) > 0

    def test_each_scenario_has_required_fields(self):
        for s in load_scenarios():
            assert "id" in s
            assert "mode" in s
            assert "description" in s
            assert "enabled" in s

    def test_known_scenario_present(self):
        ids = {s["id"] for s in load_scenarios()}
        assert "immediate-capture" in ids

    def test_modes_are_valid(self):
        valid_modes = {"immediate", "post_script", "manual_trigger"}
        for s in load_scenarios():
            assert s["mode"] in valid_modes, f"Unknown mode: {s['mode']}"

    def test_attack_ids_are_normalized(self, tmp_path, monkeypatch):
        import astinuslab.catalog as catalog

        scenarios_yaml = tmp_path / "scenarios.yaml"
        scenarios_yaml.write_text(
            """
version: 1
scenarios:
  - id: lab-session
    enabled: true
    mode: manual_trigger
    description: Lab
    attack: [T1190, T1059.004]
    script:
      enabled: false
""".strip(),
            encoding="utf-8",
        )
        monkeypatch.setattr(catalog, "settings", SimpleNamespace(scenarios_catalog_path=scenarios_yaml))

        catalog.invalidate_catalog_caches()
        try:
            scenarios = catalog.load_scenarios()
        finally:
            catalog.invalidate_catalog_caches()

        assert scenarios[0]["attack_techniques"] == ["T1190", "T1059.004"]

    def test_invalid_attack_ids_are_skipped_with_warning(self, tmp_path, monkeypatch, caplog):
        import astinuslab.catalog as catalog

        scenarios_yaml = tmp_path / "scenarios.yaml"
        scenarios_yaml.write_text(
            """
version: 1
scenarios:
  - id: lab-session
    enabled: true
    mode: manual_trigger
    description: Lab
    attack: [t1190, bogus, 1234]
    script:
      enabled: false
""".strip(),
            encoding="utf-8",
        )
        monkeypatch.setattr(catalog, "settings", SimpleNamespace(scenarios_catalog_path=scenarios_yaml))

        catalog.invalidate_catalog_caches()
        try:
            with caplog.at_level("WARNING"):
                scenarios = catalog.load_scenarios()
        finally:
            catalog.invalidate_catalog_caches()

        assert scenarios[0]["attack_techniques"] == ["T1190"]
        assert "ignoring invalid ATT&CK technique id" in caplog.text

    def test_absent_attack_field_returns_empty_list(self, tmp_path, monkeypatch):
        import astinuslab.catalog as catalog

        scenarios_yaml = tmp_path / "scenarios.yaml"
        scenarios_yaml.write_text(
            """
version: 1
scenarios:
  - id: immediate-capture
    enabled: true
    mode: immediate
    description: Capture
    script:
      enabled: false
""".strip(),
            encoding="utf-8",
        )
        monkeypatch.setattr(catalog, "settings", SimpleNamespace(scenarios_catalog_path=scenarios_yaml))

        catalog.invalidate_catalog_caches()
        try:
            scenarios = catalog.load_scenarios()
        finally:
            catalog.invalidate_catalog_caches()

        assert scenarios[0]["attack_techniques"] == []


class TestScenarioPackages:
    def test_build_scenario_package_writes_metadata_recipe_and_safe_scripts(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        import astinuslab.catalog as catalog

        script = tmp_path / "guest" / "workloads" / "dvwa" / "install.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\necho dvwa\n", encoding="utf-8")
        monkeypatch.setattr(
            catalog,
            "settings",
            SimpleNamespace(base_dir=tmp_path, data_dir=tmp_path, storage_dir=tmp_path / "storage"),
        )

        package = catalog.build_scenario_package(
            {"schema_version": 1, "name": "dvwa-lab", "networks": [], "vms": [], "topology": []},
            metadata={"name": "DVWA Lab", "author": "tester"},
            script_paths=["guest/workloads/dvwa/install.sh"],
        )

        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            assert sorted(archive.namelist()) == [
                "guest/workloads/dvwa/install.sh",
                "metadata.yaml",
                "scenario.yaml",
            ]
            metadata = yaml.safe_load(archive.read("metadata.yaml"))
            recipe = yaml.safe_load(archive.read("scenario.yaml"))
            assert metadata["name"] == "DVWA Lab"
            assert recipe["name"] == "dvwa-lab"
            assert archive.read("guest/workloads/dvwa/install.sh").decode() == "#!/bin/sh\necho dvwa\n"

    def test_read_scenario_package_rejects_missing_recipe(self):
        from astinuslab.catalog import ScenarioPackageError, read_scenario_package

        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("metadata.yaml", "name: broken\n")

        with pytest.raises(ScenarioPackageError, match="scenario.yaml"):
            read_scenario_package(payload.getvalue())

    def test_read_scenario_package_rejects_unsafe_zip_path(self):
        from astinuslab.catalog import ScenarioPackageError, read_scenario_package

        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("scenario.yaml", "schema_version: 1\nname: lab\n")
            archive.writestr("../evil.sh", "oops\n")

        with pytest.raises(ScenarioPackageError, match="unsafe"):
            read_scenario_package(payload.getvalue())

    @pytest.mark.parametrize(
        "member_name",
        ["/tmp/evil.sh", "C:/evil.sh", "scripts/../evil.sh", "scripts//evil.sh", r"scripts\..\evil.sh"],
    )
    def test_read_scenario_package_rejects_absolute_windows_and_collapsed_paths(self, member_name):
        from astinuslab.catalog import ScenarioPackageError, read_scenario_package

        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("scenario.yaml", "schema_version: 1\nname: lab\n")
            archive.writestr(member_name, "oops\n")

        with pytest.raises(ScenarioPackageError, match="unsafe"):
            read_scenario_package(payload.getvalue())

    def test_read_scenario_package_rejects_corrupt_member_payload(self):
        from astinuslab.catalog import ScenarioPackageError, read_scenario_package

        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("scenario.yaml", "schema_version: 1\nname: lab\n")
        raw = bytearray(payload.getvalue())
        raw[raw.index(b"schema_version")] = ord("X")

        with pytest.raises(ScenarioPackageError, match="invalid scenario package zip"):
            read_scenario_package(bytes(raw))

    def test_read_scenario_package_rejects_casefold_duplicate_members(self):
        from astinuslab.catalog import ScenarioPackageError, read_scenario_package

        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("scenario.yaml", "schema_version: 1\nname: lab\n")
            archive.writestr("metadata.yaml", "name: lab\n")
            archive.writestr("README.txt", "one\n")
            archive.writestr("readme.txt", "two\n")

        with pytest.raises(ScenarioPackageError, match="duplicate"):
            read_scenario_package(payload.getvalue())

    def test_read_scenario_package_rejects_special_member_type(self):
        from astinuslab.catalog import ScenarioPackageError, read_scenario_package

        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("scenario.yaml", "schema_version: 1\nname: lab\n")
            info = zipfile.ZipInfo("metadata.yaml")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "name: lab\n")

        with pytest.raises(ScenarioPackageError, match="unsupported"):
            read_scenario_package(payload.getvalue())

    def test_read_scenario_package_rejects_oversized_upload(self, monkeypatch):
        import astinuslab.catalog as catalog

        monkeypatch.setattr(catalog, "PACKAGE_MAX_UPLOAD_BYTES", 10)

        with pytest.raises(catalog.ScenarioPackageError, match="too large"):
            catalog.read_scenario_package(b"x" * 11)

    def test_apply_package_resource_overrides_updates_vms_by_label(self):
        from astinuslab.catalog import apply_scenario_package_resource_overrides

        recipe = {
            "schema_version": 1,
            "name": "lab",
            "vms": [
                {"label": "web", "image_id": "img", "networks": ["lab"], "vcpus": 1, "memory_mb": 512},
                {"label": "attacker", "image_id": "kali", "networks": ["lab"]},
            ],
        }

        updated = apply_scenario_package_resource_overrides(
            recipe,
            {"web": {"vcpus": 4, "memory_mb": 8192, "disk_gb": 80}},
        )

        assert updated["vms"][0]["vcpus"] == 4
        assert updated["vms"][0]["memory_mb"] == 8192
        assert updated["vms"][0]["disk_gb"] == 80
        assert "vcpus" not in updated["vms"][1]


class TestWorkloadCompatibility:
    def test_built_in_workload_declares_compatibility(self):
        from astinuslab.catalog import workload_by_id

        workload = workload_by_id("wordpress-6.4")
        assert workload is not None
        include = workload["compatibility"]["include"]
        assert len(include) == 1
        assert include[0]["families"] == ["debian"]
        assert include[0]["distros"] == []
        assert include[0]["versions"] == []

    def test_version_range_selector_blocks_too_new_target(self):
        from astinuslab.catalog import _parse_compatibility, workload_compatibility_for_target

        workload = {
            "id": "range-test",
            "compatibility": _parse_compatibility(
                {"include": [{"distro": "ubuntu", "version_min": "20.04", "version_max": "22.04"}]}
            ),
        }
        result = workload_compatibility_for_target(
            workload,
            {"id": "ubuntu-24", "family": "debian", "distro": "ubuntu", "version": "24.04", "disk_gb": 20},
        )
        assert result["compatibility_status"] == "incompatible"
        reason = result["compatibility_reason"]
        assert reason is not None
        assert "not in the workload compatibility matrix" in reason

    def test_stack_missing_component_is_reported(self):
        from astinuslab.catalog import _parse_compatibility, workload_compatibility_for_target

        workload = {
            "id": "bad-stack",
            "is_stack": True,
            "components": ["missing"],
            "compatibility": _parse_compatibility({"include": [{"family": "debian"}]}),
        }
        result = workload_compatibility_for_target(
            workload,
            {"id": "ubuntu-22", "family": "debian", "distro": "ubuntu", "version": "22.04"},
            workload_map={},
        )
        assert result["compatibility_status"] == "incompatible"
        reason = result["compatibility_reason"]
        assert reason is not None
        assert "Missing component 'missing'" in reason

    def test_workload_is_compatible_with_debian_family_target(self):
        from astinuslab.catalog import workload_by_id, workload_compatibility_for_target

        workload = workload_by_id("wordpress-6.4")
        assert workload is not None
        result = workload_compatibility_for_target(
            workload,
            {"id": "ubuntu-22-04-default", "family": "debian", "distro": "ubuntu", "version": "22.04"},
        )
        assert result["compatibility_status"] == "compatible"

    def test_workload_is_incompatible_with_redhat_family_target(self):
        from astinuslab.catalog import workload_by_id, workload_compatibility_for_target

        workload = workload_by_id("wordpress-6.4")
        assert workload is not None
        result = workload_compatibility_for_target(
            workload,
            {"id": "fedora-40", "family": "redhat", "distro": "fedora", "version": "40"},
        )
        assert result["compatibility_status"] == "incompatible"

    def test_stack_inherits_component_compatibility(self):
        from astinuslab.catalog import workload_by_id, workload_compatibility_for_target

        workload = workload_by_id("full-php-stack")
        assert workload is not None
        result = workload_compatibility_for_target(
            workload,
            {"id": "ubuntu-22-04-default", "family": "debian", "distro": "ubuntu", "version": "22.04"},
        )
        assert result["compatibility_status"] == "compatible"


class TestCatalogValidation:
    def test_example_catalog_uses_loaded_wordpress_workload_id(self):
        from astinuslab.catalog import load_workloads

        with open("config/catalog/workloads.example.yaml", encoding="utf-8") as handle:
            example = yaml.safe_load(handle)

        loaded_wordpress_ids = {
            item["id"] for item in load_workloads() if item["id"].startswith("wordpress-") and item.get("enabled", True)
        }
        example_wordpress_ids = {
            item["id"]
            for item in example["workloads"]
            if item["id"].startswith("wordpress-") and item.get("enabled", True)
        }

        assert example_wordpress_ids == loaded_wordpress_ids

    def test_every_enabled_workload_install_script_exists(self):
        from astinuslab.catalog import load_workloads
        from astinuslab.core.config import settings

        for workload in load_workloads():
            if workload.get("is_stack"):
                continue
            script = workload.get("install_script")
            assert script, f"{workload['id']} missing install_script"
            assert (settings.base_dir / script).exists(), f"{workload['id']} install_script missing: {script}"

    def test_every_stack_component_exists(self):
        from astinuslab.catalog import load_workloads

        workloads = {item["id"]: item for item in load_workloads()}
        for workload in workloads.values():
            for component_id in workload.get("components", []):
                assert component_id in workloads, f"{workload['id']} references missing component {component_id}"

    def test_every_enabled_workload_has_declared_compatibility(self):
        from astinuslab.catalog import load_workloads

        for workload in load_workloads():
            compatibility = workload.get("compatibility") or {}
            assert compatibility.get("include") or compatibility.get("exclude"), (
                f"{workload['id']} lacks compatibility rules"
            )

    def test_validate_workload_catalog_reports_missing_component(self):
        from astinuslab.catalog import validate_workload_catalog

        errors = validate_workload_catalog(
            [
                {
                    "id": "stack",
                    "is_stack": True,
                    "components": ["missing"],
                    "compatibility": {"include": [{"families": ["debian"]}], "exclude": []},
                }
            ]
        )
        assert any("missing component missing" in e for e in errors)
