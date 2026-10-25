"""Guards i18n key parity across all supported languages.

Every supported language must expose the same set of translation keys as the
default language, with no empty values. This keeps backend UI responses
consistent and prevents silent fallback gaps when copy is added in one
language but not the others.
"""

from pathlib import Path
from string import Formatter

import pytest

from astinuslab.i18n import DEFAULT_LANG, SUPPORTED_LANGS, TRANSLATIONS

ROOT = Path(__file__).resolve().parents[3]
PACKAGE_EXPORT_KEYS = {
    "pg_eyebrow_package",
    "pg_export_package_title",
    "pg_export_package_close_title",
    "pg_export_package_close_aria",
    "pg_export_package_hint",
    "pg_label_package_id",
    "pg_label_version",
    "pg_label_title",
    "pg_label_creator",
    "pg_label_license",
    "pg_label_description",
    "pg_label_runbook",
    "pg_export_package_runbook_default",
    "pg_label_host_resources",
    "pg_export_package_min_cpu",
    "pg_export_package_min_memory",
    "pg_export_package_min_disk",
    "pg_export_package_recommended_cpu",
    "pg_export_package_recommended_memory",
    "pg_export_package_recommended_disk",
    "pg_btn_export",
    "pg_foot_export_package",
}


def test_default_lang_is_supported():
    assert DEFAULT_LANG in SUPPORTED_LANGS


def test_every_supported_lang_has_translations():
    assert set(TRANSLATIONS) == SUPPORTED_LANGS


@pytest.mark.parametrize("lang", sorted(SUPPORTED_LANGS - {DEFAULT_LANG}))
def test_key_parity_with_default(lang):
    base_keys = set(TRANSLATIONS[DEFAULT_LANG])
    lang_keys = set(TRANSLATIONS[lang])
    missing = base_keys - lang_keys
    extra = lang_keys - base_keys
    assert not missing, f"{lang} missing keys: {sorted(missing)}"
    assert not extra, f"{lang} has extra keys absent from {DEFAULT_LANG}: {sorted(extra)}"


@pytest.mark.parametrize("lang", sorted(SUPPORTED_LANGS))
def test_no_empty_values(lang):
    empty = [key for key, value in TRANSLATIONS[lang].items() if not value.strip()]
    assert not empty, f"{lang} has empty values for keys: {sorted(empty)}"


@pytest.mark.parametrize("lang", sorted(SUPPORTED_LANGS))
def test_package_export_copy_is_translated_in_every_language(lang):
    missing = PACKAGE_EXPORT_KEYS - set(TRANSLATIONS[lang])

    assert not missing, f"{lang} missing package export keys: {sorted(missing)}"


def test_package_export_template_uses_translations_without_english_fallbacks():
    template = (ROOT / "src" / "astinuslab" / "web" / "templates" / "playground.html").read_text(encoding="utf-8")

    for key in PACKAGE_EXPORT_KEYS:
        assert f"t.{key} or" not in template


@pytest.mark.parametrize("lang", ["es", "fr", "ca"])
def test_portable_lab_docs_have_localized_siblings(lang):
    assert (ROOT / "docs" / "user" / f"PORTABLE_LABS.{lang}.md").is_file()


READINESS_KEYS = {
    "vol_readiness_title": set(),
    "vol_readiness_disabled": {"guide"},
    "vol_readiness_analysis_unavailable": {"guide"},
    "vol_readiness_isf_unavailable": {"guide"},
}


@pytest.mark.parametrize("lang", sorted(SUPPORTED_LANGS))
def test_volatility_readiness_copy_has_exact_placeholders(lang):
    translations = TRANSLATIONS[lang]
    for key, expected in READINESS_KEYS.items():
        assert translations[key].strip()
        actual = {name for _, name, _, _ in Formatter().parse(translations[key]) if name}
        assert actual == expected, f"{lang}.{key} placeholders: {actual}"
