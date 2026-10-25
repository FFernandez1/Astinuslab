from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError


@pytest.fixture(scope="module", autouse=True)
def _clear_astinuslab_env_for_module() -> Iterator[None]:
    original_env = {name: value for name, value in os.environ.items() if name.startswith("ASTINUSLAB_")}
    for name in original_env:
        os.environ.pop(name, None)

    try:
        yield
    finally:
        for name in list(os.environ):
            if name.startswith("ASTINUSLAB_"):
                os.environ.pop(name, None)
        os.environ.update(original_env)


def _config_module():
    from astinuslab.core import config

    return config


def test_max_concurrent_vms_defaults_to_none() -> None:
    from astinuslab.core.config import Settings

    assert Settings().max_concurrent_vms is None


def test_volatility_bin_defaults_to_none() -> None:
    from astinuslab.core.config import Settings

    assert Settings().vol3_volatility_bin is None


def test_dwarf2json_path_defaults_to_none() -> None:
    from astinuslab.core.config import Settings

    assert Settings().vol3_dwarf2json_path is None


def test_dwarf2json_sha256_defaults_to_none() -> None:
    from astinuslab.core.config import Settings

    assert Settings().vol3_dwarf2json_sha256 is None


def test_max_concurrent_vms_parsed_from_orchestration_section(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config_module()
    toml_path = tmp_path / "astinuslab.toml"
    toml_path.write_text(
        "[orchestration]\nmax_concurrent_vms = 4\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_TOML_PATH", toml_path)

    settings = config.load_settings()

    assert settings.max_concurrent_vms == 4


def test_max_concurrent_vms_rejects_zero() -> None:
    from astinuslab.core.config import Settings

    with pytest.raises(ValueError, match="max_concurrent_vms"):
        Settings(max_concurrent_vms=0)


def test_max_concurrent_vms_rejects_non_integer() -> None:
    from astinuslab.core.config import Settings

    with pytest.raises(ValidationError):
        Settings.model_validate({"max_concurrent_vms": "four"})


def test_packages_settings_are_disabled_by_default() -> None:
    from astinuslab.core.config import Settings

    settings = Settings()

    assert settings.packages_enabled is False
    assert settings.packages_allow_source_network is False
    assert settings.packages_allow_guest_egress is False
    assert settings.packages_max_asset_bytes == 16 * 1024**3
    assert settings.packages_max_archive_bytes == 40 * 1024**3
    assert settings.packages_gc_grace_seconds == 24 * 60 * 60


def test_packages_settings_are_flattened_and_environment_overrides_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config_module()
    toml_path = tmp_path / "astinuslab.toml"
    toml_path.write_text(
        "[packages]\n"
        "enabled = true\n"
        "allow_source_network = true\n"
        "allow_guest_egress = true\n"
        "max_asset_bytes = 123\n"
        "max_archive_bytes = 456\n"
        "gc_grace_seconds = 789\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_TOML_PATH", toml_path)
    monkeypatch.setenv("ASTINUSLAB_PACKAGES_ENABLED", "false")
    monkeypatch.setenv("ASTINUSLAB_PACKAGES_MAX_ARCHIVE_BYTES", "654")

    settings = config.load_settings()

    assert settings.packages_enabled is False
    assert settings.packages_allow_source_network is True
    assert settings.packages_allow_guest_egress is True
    assert settings.packages_max_asset_bytes == 123
    assert settings.packages_max_archive_bytes == 654
    assert settings.packages_gc_grace_seconds == 789


def test_load_settings_rejects_unknown_packages_toml_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config_module()
    toml_path = tmp_path / "astinuslab.toml"
    toml_path.write_text("[packages]\ntypo_key = true\n", encoding="utf-8")
    monkeypatch.setattr(config, "_TOML_PATH", toml_path)

    with pytest.raises(ValidationError) as exc_info:
        config.load_settings()

    assert "packages_typo_key" in str(exc_info.value)


def test_disabled_packages_expose_neither_api_routes_nor_export_control(client) -> None:
    from astinuslab.main import app

    assert not any(getattr(route, "path", "").startswith("/api/v1/lab-packages") for route in app.routes)

    response = client.get("/playground")

    assert response.status_code == 200
    assert b"btn-export-package" not in response.content


def test_settings_rejects_unknown_direct_key() -> None:
    from astinuslab.core.config import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings.model_validate({"vol3_dwarf2json": "x"})

    message = str(exc_info.value)
    assert "vol3_dwarf2json" in message
    assert "Extra inputs are not permitted" in message


def test_load_settings_rejects_unknown_vol3_toml_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config_module()
    toml_path = tmp_path / "astinuslab.toml"
    toml_path.write_text("[vol3]\ntypo_key = 1\n", encoding="utf-8")
    monkeypatch.setattr(config, "_TOML_PATH", toml_path)

    with pytest.raises(ValidationError) as exc_info:
        config.load_settings()

    message = str(exc_info.value)
    assert "vol3_typo_key" in message or "typo_key" in message
    assert "Extra inputs are not permitted" in message


def test_load_settings_rejects_unknown_catalog_toml_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config_module()
    toml_path = tmp_path / "astinuslab.toml"
    toml_path.write_text('[catalogs]\ntypo_catalog = "x.yaml"\n', encoding="utf-8")
    monkeypatch.setattr(config, "_TOML_PATH", toml_path)

    with pytest.raises(ValueError, match=r"\[catalogs\].*typo_catalog"):
        config.load_settings()


def test_load_settings_rejects_unknown_toml_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config_module()
    toml_path = tmp_path / "astinuslab.toml"
    toml_path.write_text("[unknown_section]\nfoo = 1\n", encoding="utf-8")
    monkeypatch.setattr(config, "_TOML_PATH", toml_path)

    with pytest.raises(ValueError, match="unknown_section"):
        config.load_settings()


def test_load_settings_preserves_absolute_external_tool_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config_module()
    toml_path = tmp_path / "astinuslab.toml"
    toml_path.write_text(
        ('[vol3]\ndwarf2json_path = "/opt/dwarf2json/bin/dwarf2json"\nvolatility_bin = "/opt/volatility3/bin/vol"\n'),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_TOML_PATH", toml_path)

    settings = config.load_settings()

    assert settings.vol3_dwarf2json_path == Path("/opt/dwarf2json/bin/dwarf2json")
    assert settings.vol3_volatility_bin == "/opt/volatility3/bin/vol"


def test_load_settings_accepts_home_relative_volatility_bin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`~/` is the natural thing to type for a locally installed tool.

    The loader rewrites any `_bin` value containing a slash, and the field is a
    plain string, so handing Pydantic a Path object fails configuration loading
    outright instead of resolving the tool.
    """
    config = _config_module()
    toml_path = tmp_path / "astinuslab.toml"
    toml_path.write_text('[vol3]\nvolatility_bin = "~/tools/volatility3/bin/vol"\n', encoding="utf-8")
    monkeypatch.setattr(config, "_TOML_PATH", toml_path)

    settings = config.load_settings()

    assert settings.vol3_volatility_bin == str(Path("~/tools/volatility3/bin/vol").expanduser())
    assert isinstance(settings.vol3_volatility_bin, str)


def test_load_settings_resolves_relative_volatility_bin_from_base_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config_module()
    toml_path = tmp_path / "astinuslab.toml"
    toml_path.write_text('[vol3]\nvolatility_bin = "tools/vol"\n', encoding="utf-8")
    monkeypatch.setattr(config, "_TOML_PATH", toml_path)

    settings = config.load_settings()

    assert settings.vol3_volatility_bin == str(config._BASE / "tools/vol")
    assert isinstance(settings.vol3_volatility_bin, str)


def test_load_settings_leaves_bare_command_name_volatility_bin_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare command name must stay resolvable through PATH, not become a path."""
    config = _config_module()
    toml_path = tmp_path / "astinuslab.toml"
    toml_path.write_text('[vol3]\nvolatility_bin = "vol"\n', encoding="utf-8")
    monkeypatch.setattr(config, "_TOML_PATH", toml_path)

    assert config.load_settings().vol3_volatility_bin == "vol"


def test_load_settings_resolves_relative_dwarf2json_path_from_base_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config_module()
    toml_path = tmp_path / "astinuslab.toml"
    toml_path.write_text(
        '[vol3]\ndwarf2json_path = "tools/dwarf2json"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_TOML_PATH", toml_path)

    settings = config.load_settings()

    assert settings.vol3_dwarf2json_path == Path(config._BASE) / "tools" / "dwarf2json"
