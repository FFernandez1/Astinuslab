from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from astinuslab.core import config
from astinuslab.core.config import Settings, load_settings


def _env_key(field_name: str) -> str:
    return f"ASTINUSLAB_{field_name.upper()}"


@pytest.fixture(autouse=True)
def clear_astinuslab_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for field_name in Settings.model_fields:
        monkeypatch.delenv(_env_key(field_name), raising=False)


def test_env_overrides_max_concurrent_vms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTINUSLAB_MAX_CONCURRENT_VMS", "3")

    assert load_settings().max_concurrent_vms == 3


def test_env_invalid_max_concurrent_vms_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTINUSLAB_MAX_CONCURRENT_VMS", "0")

    with pytest.raises(ValueError, match="max_concurrent_vms"):
        load_settings()


def test_external_tool_env_variables_override_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toml_path = tmp_path / "astinuslab.toml"
    toml_path.write_text(
        (
            "[vol3]\n"
            'dwarf2json_path = "/opt/toml/dwarf2json"\n'
            f'dwarf2json_sha256 = "{"a" * 64}"\n'
            'volatility_bin = "/opt/toml/vol"\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_TOML_PATH", toml_path)
    monkeypatch.setenv("ASTINUSLAB_VOL3_DWARF2JSON_PATH", "/opt/env/dwarf2json")
    monkeypatch.setenv("ASTINUSLAB_VOL3_DWARF2JSON_SHA256", "b" * 64)
    monkeypatch.setenv("ASTINUSLAB_VOL3_VOLATILITY_BIN", "/opt/env/vol")

    settings = load_settings()

    assert settings.vol3_dwarf2json_path == Path("/opt/env/dwarf2json")
    assert settings.vol3_dwarf2json_sha256 == "b" * 64
    assert settings.vol3_volatility_bin == "/opt/env/vol"


def test_dwarf2json_sha256_normalizes_uppercase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTINUSLAB_VOL3_DWARF2JSON_SHA256", "A" * 64)

    assert load_settings().vol3_dwarf2json_sha256 == "a" * 64


def test_dwarf2json_sha256_normalizes_sha256_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTINUSLAB_VOL3_DWARF2JSON_SHA256", "sha256:" + "B" * 64)

    assert load_settings().vol3_dwarf2json_sha256 == "b" * 64


@pytest.mark.parametrize(
    "invalid_hash",
    [
        "",
        "a" * 63,
        "a" * 65,
        "a" * 31 + " " + "a" * 32,
        "g" * 64,
    ],
    ids=["empty", "short", "long", "internal-whitespace", "non-hex"],
)
def test_invalid_dwarf2json_sha256_fails_configuration_loading(
    invalid_hash: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTINUSLAB_VOL3_DWARF2JSON_SHA256", invalid_hash)

    with pytest.raises(ValidationError) as exc_info:
        load_settings()

    error = exc_info.value.errors(include_url=False)[0]
    assert error["loc"] == ("vol3_dwarf2json_sha256",)
    assert error["msg"] == "Value error, vol3_dwarf2json_sha256 must be a 64-character SHA-256 value"


def test_isf_mirror_single_string_env_override_becomes_one_item_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTINUSLAB_VOL3_ISF_MIRROR_URLS", "https://a.example/x")

    settings = load_settings()

    assert settings.vol3_isf_mirror_urls == ["https://a.example/x"]


def test_isf_mirror_comma_separated_env_override_strips_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ASTINUSLAB_VOL3_ISF_MIRROR_URLS",
        " https://a.example/x , https://b.example/y ",
    )

    settings = load_settings()

    assert settings.vol3_isf_mirror_urls == [
        "https://a.example/x",
        "https://b.example/y",
    ]


def test_isf_mirror_json_list_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ASTINUSLAB_VOL3_ISF_MIRROR_URLS",
        '["https://a.example/x"]',
    )

    settings = load_settings()

    assert settings.vol3_isf_mirror_urls == ["https://a.example/x"]


def test_isf_mirror_empty_string_env_override_becomes_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTINUSLAB_VOL3_ISF_MIRROR_URLS", "")

    settings = load_settings()

    assert settings.vol3_isf_mirror_urls == []


def test_debuginfod_single_string_env_override_becomes_one_item_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ASTINUSLAB_VOL3_DEBUGINFOD_URLS",
        "https://debuginfod.example",
    )

    settings = load_settings()

    assert settings.vol3_debuginfod_urls == ["https://debuginfod.example"]


def test_debuginfod_comma_separated_env_override_strips_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ASTINUSLAB_VOL3_DEBUGINFOD_URLS",
        " https://debuginfod-a.example , https://debuginfod-b.example ",
    )

    settings = load_settings()

    assert settings.vol3_debuginfod_urls == [
        "https://debuginfod-a.example",
        "https://debuginfod-b.example",
    ]


def test_debuginfod_empty_string_env_override_becomes_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTINUSLAB_VOL3_DEBUGINFOD_URLS", "")

    settings = load_settings()

    assert settings.vol3_debuginfod_urls == []


def test_provider_timeouts_json_object_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTINUSLAB_VOL3_PROVIDER_TIMEOUTS", '{"debuginfod": 600}')

    settings = load_settings()

    assert settings.vol3_provider_timeouts["debuginfod"] == 600


def test_provider_timeouts_non_json_env_override_names_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTINUSLAB_VOL3_PROVIDER_TIMEOUTS", "not-json")

    with pytest.raises(
        ValueError,
        match="ASTINUSLAB_VOL3_PROVIDER_TIMEOUTS.*JSON object",
    ):
        load_settings()


def test_provider_timeouts_json_non_object_env_override_names_expected_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTINUSLAB_VOL3_PROVIDER_TIMEOUTS", "[]")

    with pytest.raises(
        ValueError,
        match="ASTINUSLAB_VOL3_PROVIDER_TIMEOUTS.*JSON object",
    ):
        load_settings()


def test_isf_mirror_env_override_still_runs_url_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ASTINUSLAB_VOL3_ISF_MIRROR_URLS",
        "http://insecure.example",
    )

    with pytest.raises(ValueError, match="https:// or file://"):
        load_settings()


def _round_trip_env_case(field_name: str, annotation: object) -> tuple[str, object]:
    special_values = {
        "max_concurrent_vms": (
            "5",
            5,
        ),
        "admin_api_token_hash": (
            "sha256:" + "a" * 64,
            "sha256:" + "a" * 64,
        ),
        "api_host": ("127.0.0.1", "127.0.0.1"),
        "browser_allowed_origins": (
            '["https://astinuslab.example"]',
            ["https://astinuslab.example:443"],
        ),
        "celery_broker_url": (
            "sqla+sqlite:////tmp/astinuslab-config-smoke/broker.db",
            "sqla+sqlite:////tmp/astinuslab-config-smoke/broker.db",
        ),
        "celery_result_backend": (
            "db+sqlite:////tmp/astinuslab-config-smoke/results.db",
            "db+sqlite:////tmp/astinuslab-config-smoke/results.db",
        ),
        "playground_disk_dir": (
            "/tmp/astinuslab-config-smoke/disks",
            Path("/tmp/astinuslab-config-smoke/disks"),
        ),
        "playground_capture_dir": (
            "/tmp/astinuslab-config-smoke/captures",
            Path("/tmp/astinuslab-config-smoke/captures"),
        ),
        "vol3_isf_mirror_urls": (
            '["https://mirror.example/isf"]',
            ["https://mirror.example/isf"],
        ),
        "vol3_debuginfod_urls": (
            '["https://debuginfod.example"]',
            ["https://debuginfod.example"],
        ),
        "vol3_provider_timeouts": (
            '{"prebuilt_isf": 60, "debuginfod": 900}',
            {"prebuilt_isf": 60, "debuginfod": 900},
        ),
        "vol3_dwarf2json_path": (
            "/opt/dwarf2json/bin/dwarf2json",
            Path("/opt/dwarf2json/bin/dwarf2json"),
        ),
        "vol3_dwarf2json_sha256": (
            "c" * 64,
            "c" * 64,
        ),
        "vol3_volatility_bin": (
            "/opt/volatility3/bin/vol",
            "/opt/volatility3/bin/vol",
        ),
    }
    if field_name in special_values:
        return special_values[field_name]
    if annotation is bool:
        return "true", True
    if annotation is int:
        return "5", 5
    if annotation is Path:
        path_value = Path(f"/tmp/astinuslab-config-smoke/{field_name}")
        return str(path_value), path_value
    if annotation is str:
        return "sample", "sample"
    raise AssertionError(f"No round-trip env sample for {field_name}: {annotation!r}")


def test_all_settings_env_overrides_accept_representative_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_values: dict[str, object] = {}
    for field_name, field_info in Settings.model_fields.items():
        env_value, expected_value = _round_trip_env_case(
            field_name,
            field_info.annotation,
        )
        monkeypatch.setenv(_env_key(field_name), env_value)
        expected_values[field_name] = expected_value

    settings = load_settings()

    assert set(expected_values) == set(Settings.model_fields)
    for field_name, expected_value in expected_values.items():
        assert getattr(settings, field_name) == expected_value


def test_data_dir_moves_mutable_paths_off_the_code_root(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A versioned release keeps code under releases/<id>/ while storage,
    config and images stay at the stable install root, so the mutable defaults
    must follow the data root and never the directory the code was loaded from."""
    data_root = tmp_path / "install"
    (data_root / "config" / "catalog").mkdir(parents=True)
    monkeypatch.setenv("ASTINUSLAB_DATA_DIR", str(data_root))

    settings = load_settings()

    assert settings.data_dir == data_root
    assert settings.storage_dir == data_root / "storage"
    assert settings.db_path == data_root / "storage" / "astinuslab.db"
    assert settings.images_dir == data_root / "images" / "definitions"
    assert settings.vol3_symbols_path == data_root / "vol3" / "symbols"
    assert settings.playground_capture_dir == data_root / "storage" / "playground" / "captures"
    assert settings.targets_catalog_path == data_root / "config" / "catalog" / "targets.yaml"
    assert settings.celery_broker_url == f"sqla+sqlite:///{data_root}/storage/celery_broker.db"
    assert settings.celery_result_backend == f"db+sqlite:///{data_root}/storage/celery_results.db"
    assert settings.base_dir == config._BASE, "the code root must stay where the source lives"


def test_data_dir_defaults_to_the_code_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASTINUSLAB_DATA_DIR", raising=False)

    settings = load_settings()

    # storage_dir itself is not asserted here: a development checkout ships an
    # astinuslab.toml that legitimately pins it, and TOML must keep winning.
    assert settings.data_dir == config._BASE


def test_no_module_derives_storage_or_config_from_the_code_root() -> None:
    """`base_dir` is the code root. Deriving a mutable path from it would put
    storage or config inside the immutable release directory."""
    offenders = []
    for path in sorted((Path(config.__file__).parent).rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if 'base_dir / "storage"' in line or 'base_dir / "config"' in line:
                offenders.append(f"{path.name}:{number}")
    assert offenders == [], offenders
