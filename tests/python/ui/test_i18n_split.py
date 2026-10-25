"""Guards the split i18n data package behind the public facade."""

import ast
import importlib
import re
from pathlib import Path

from astinuslab import i18n

I18N_DATA_DIR = Path(__file__).resolve().parents[3] / "src" / "astinuslab" / "i18n_data"
HIGH_TRAFFIC_TEMPLATE_KEYS = {
    "admin_images.html": {"select_image"},
    "partials/workload_panel.html": {"change", "workload_none_compatible", "workload_target_required"},
}


def test_language_modules_back_facade_translations():
    for lang in sorted(i18n.SUPPORTED_LANGS):
        module = importlib.import_module(f"astinuslab.i18n_data.{lang}")

        assert module.TRANSLATIONS is i18n.TRANSLATIONS[lang]


def test_i18n_facade_preserves_public_symbols():
    public_names = {name for name in vars(i18n) if not name.startswith("_")}

    assert public_names == {
        "DEFAULT_LANG",
        "LANG_NAMES",
        "SUPPORTED_LANGS",
        "TRANSLATIONS",
        "annotations",
        "get_translations",
    }


def test_language_modules_do_not_repeat_literal_keys():
    repeated: dict[str, dict[str, list[int]]] = {}

    for path in sorted(I18N_DATA_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue

        module = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(module):
            if not isinstance(node, ast.Dict):
                continue

            seen: dict[str, int] = {}
            for key in node.keys:
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    continue
                if key.value in seen:
                    repeated.setdefault(path.name, {}).setdefault(key.value, []).extend([seen[key.value], key.lineno])
                else:
                    seen[key.value] = key.lineno

    assert repeated == {}


def test_high_traffic_template_keys_are_translated():
    required_keys = set().union(*HIGH_TRAFFIC_TEMPLATE_KEYS.values())
    missing = {
        lang: sorted(required_keys - set(i18n.TRANSLATIONS[lang]))
        for lang in sorted(i18n.SUPPORTED_LANGS)
        if required_keys - set(i18n.TRANSLATIONS[lang])
    }

    assert missing == {}


def test_playground_workflow_translations_preserve_placeholders_and_product_terms():
    english = i18n.TRANSLATIONS["en"]
    keys = [key for key in english if key.startswith(("pg_wiz_", "pg_status_", "pg_images_"))]
    for lang, translations in i18n.TRANSLATIONS.items():
        for key in keys:
            assert set(re.findall(r"\{\w+\}", translations[key])) == set(re.findall(r"\{\w+\}", english[key])), (
                lang,
                key,
            )
        assert "Workload" in translations["pg_wiz_field_workload"]
        if lang != "en":
            assert "Target" in translations["pg_wiz_create_target"]
            assert "Attacker" in translations["pg_wiz_create_attacker"]
