from pathlib import Path

import pytest
import yaml

from astinuslab.forensics.tool_metadata import DWARF2JSON_AMD64
from astinuslab.forensics.vol3_tools import preferred_tested_volatility_version

ROOT = Path(__file__).resolve().parents[3]
MKDOCS_CONFIG = ROOT / "docs" / "mkdocs.yml"
DOC_LOCALES = ("es", "fr", "ca")
pytestmark = pytest.mark.unit


def _nav_entries(items):
    for item in items:
        if isinstance(item, str):
            yield item, item
        elif isinstance(item, dict):
            for label, value in item.items():
                if isinstance(value, str):
                    yield label, value
                elif isinstance(value, list):
                    yield label, None
                    yield from _nav_entries(value)


def _mkdocs_i18n_config(config):
    for plugin in config["plugins"]:
        if isinstance(plugin, dict) and "i18n" in plugin:
            return plugin["i18n"]
    raise AssertionError("mkdocs-static-i18n plugin must be configured")


def test_mkdocs_uses_toggleable_light_and_dark_palettes():
    config = yaml.safe_load(MKDOCS_CONFIG.read_text())

    theme = config["theme"]
    assert theme["name"] == "material"
    assert (MKDOCS_CONFIG.parent / theme["custom_dir"]).resolve() == (ROOT / "docs" / "overrides").resolve()

    palettes = theme["palette"]
    assert isinstance(palettes, list)
    assert [palette["scheme"] for palette in palettes] == ["default", "slate"]
    assert palettes[0]["toggle"]["name"] == "Switch to dark mode"
    assert palettes[1]["toggle"]["name"] == "Switch to light mode"


def test_mkdocs_localized_nav_pages_have_locale_files():
    config = yaml.safe_load(MKDOCS_CONFIG.read_text())

    # The Windows guide is explicitly linked as English in localized image docs.
    # Keep one authoritative guide and use the configured i18n fallback.
    english_fallback_pages = {
        "community/SECURITY.md",
        "user/WINDOWS_IMAGES.md",
        "architecture/backend-boundaries.md",
        "development/GENERATED_RUNTIME_FILES.md",
        "reference/FORENSIC_INTEGRITY.md",
    }
    assert _mkdocs_i18n_config(config)["fallback_to_default"] is True

    missing = []
    for _, path in _nav_entries(config["nav"]):
        if path is None:
            continue
        source = ROOT / "docs" / path
        assert source.exists(), f"nav source page is missing: {path}"
        if path in english_fallback_pages:
            continue
        for locale in DOC_LOCALES:
            localized = source.with_name(f"{source.stem}.{locale}.md")
            if not localized.exists():
                missing.append(str(localized.relative_to(ROOT)))

    assert missing == []


def test_mkdocs_localized_nav_labels_are_translated():
    config = yaml.safe_load(MKDOCS_CONFIG.read_text())
    expected_labels = {label for label, _ in _nav_entries(config["nav"])}
    i18n = _mkdocs_i18n_config(config)

    missing_by_locale = {}
    for language in i18n["languages"]:
        locale = language["locale"]
        if locale not in DOC_LOCALES:
            continue
        translations = language.get("nav_translations", {})
        missing = sorted(expected_labels - set(translations))
        if missing:
            missing_by_locale[locale] = missing

    assert missing_by_locale == {}


def test_mkdocs_excludes_internal_plans_from_localized_builds():
    config = yaml.safe_load(MKDOCS_CONFIG.read_text())

    assert {"plans/**", "superpowers/**", "audits/**", "remediation/**"} <= set(config["exclude_docs"].splitlines())


def test_release_acceptance_operator_doc_builds_without_public_nav_or_locales():
    config = yaml.safe_load(MKDOCS_CONFIG.read_text())
    internal_path = "development/RELEASE_ACCEPTANCE.md"

    assert internal_path in config["not_in_nav"].splitlines()
    assert internal_path not in {path for _, path in _nav_entries(config["nav"])}
    assert (ROOT / "docs" / internal_path).is_file()


def test_header_exposes_ollama_style_search_and_theme_controls():
    header = (ROOT / "docs/overrides/partials/header.html").read_text()

    assert "astinuslab-topbar" in header
    assert "astinuslab-nav__link" in header
    assert "astinuslab-search-trigger" in header
    assert 'for="__search"' in header
    assert "Search..." in header
    assert "Ctrl K" in header
    assert "partials/palette.html" in header
    assert "astinuslab-theme-toggle" in header
    assert 'for="__palette_1"' in header
    assert 'for="__palette_0"' in header
    assert "partials/javascripts/palette.html" in header


def test_docs_mention_installer_smoke_and_setup_host_commands():
    docs = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in [
            "README.md",
            "docs/usage/getting-started.md",
            "docs/usage/running-locally.md",
            "docs/usage/troubleshooting.md",
        ]
    )

    assert "sudo ./astinuslab.sh setup-host" in docs
    assert "./astinuslab.sh smoke" in docs
    assert "--require-running" in docs
    assert "--install-dir" in docs
    assert "--service-user" in docs
    assert "--preserve-config" not in docs


def test_canonical_onboarding_docs_include_copy_paste_readiness_commands():
    required_text = {
        "README.md": ["smoke --require-running", "guide-volatility"],
        "docs/development/setup.md": ["npm ci", "npx playwright install chromium"],
        "docs/user/VOLATILITY3.md": ["./astinuslab.sh guide-volatility", "This command is read-only"],
    }

    for relative, fragments in required_text.items():
        text = (ROOT / relative).read_text(encoding="utf-8")
        for fragment in fragments:
            assert fragment in text, f"{relative} is missing {fragment!r}"


def test_volatility_docs_match_audited_tool_metadata():
    scoped = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8") for relative in ("README.md", "docs/user/VOLATILITY3.md")
    )

    assert preferred_tested_volatility_version() in scoped
    assert DWARF2JSON_AMD64.version in scoped
    assert DWARF2JSON_AMD64.commit in scoped
    assert DWARF2JSON_AMD64.sha256 in scoped
    assert DWARF2JSON_AMD64.download_url in scoped


def test_docs_css_styles_ollama_like_shell():
    css = (ROOT / "docs/stylesheets/extra.css").read_text()

    for selector in [
        ".astinuslab-topbar",
        ".astinuslab-nav",
        ".astinuslab-nav__link",
        ".astinuslab-actions",
        ".astinuslab-search-trigger",
        ".astinuslab-kbd",
        ".astinuslab-theme-toggle",
    ]:
        assert selector in css


def test_docs_css_preserves_local_color_and_typography_tokens():
    css = (ROOT / "docs/stylesheets/extra.css").read_text()

    assert "fonts.googleapis.com" not in css
    assert "--astinuslab-font:" in css
    assert '"Inter", system-ui' in css
    assert "--astinuslab-mono:" in css
    assert '"Fira Code", ui-monospace' in css
    assert "--astinuslab-bg: #0b0d10" in css
    assert "--astinuslab-accent: #3ecf7a" in css
    assert "--md-accent-fg-color: var(--astinuslab-accent)" in css


def test_docs_css_hides_material_search_until_triggered():
    css = (ROOT / "docs/stylesheets/extra.css").read_text()

    assert ".astinuslab-actions > .md-search" in css
    assert "display: none" in css
    assert "#__search:checked ~ .md-header .astinuslab-actions > .md-search" in css


def test_docs_css_overrides_material_hidden_palette_button():
    css = (ROOT / "docs/stylesheets/extra.css").read_text()

    assert ".md-header__option" in css
    assert "clip: rect(0 0 0 0)" in css
    assert ".astinuslab-theme-toggle__button--dark" in css
    assert ".astinuslab-theme-toggle__button--light" in css
    assert 'body[data-md-color-scheme="default"] .astinuslab-theme-toggle__button--dark' in css
    assert 'body[data-md-color-scheme="slate"] .astinuslab-theme-toggle__button--light' in css


def test_docs_css_centers_content_column_and_tightens_spacing():
    css = (ROOT / "docs/stylesheets/extra.css").read_text()

    assert "--astinuslab-sidebar-width: 248px" in css
    assert "--astinuslab-toc-width: 248px" in css
    assert "--astinuslab-content-width: 700px" in css
    assert ".md-content__inner" in css
    assert "margin: 0 auto !important" in css
    assert "padding: 34px 32px 72px !important" in css


def test_docs_css_offsets_heading_anchors_for_toc_selection():
    css = (ROOT / "docs/stylesheets/extra.css").read_text()

    assert ".md-typeset :is(h2, h3, h4, h5, h6)[id]:target" in css
    assert "scroll-margin-top: calc(var(--astinuslab-header-height) - 2px)" in css


def test_homepage_is_concise_docs_landing_page():
    index = (ROOT / "docs/index.md").read_text()

    assert "## Get started" in index
    assert "## Guides" in index
    assert "## Reference" in index
    assert "Host Requirements" not in index
    assert len(index.splitlines()) < 80


def test_volatility_operator_runbook_covers_external_tools_and_recovery():
    runbook = (ROOT / "docs/user/VOLATILITY3.md").read_text(encoding="utf-8")
    scoped_docs = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in [
            "README.md",
            "OVERVIEW.md",
            "vol3/README.md",
            "vol3/docs/astinuslab-integration.md",
            "vol3/docs/rollout.md",
            "docs/user/VOLATILITY3.md",
            "docs/usage/configuration.md",
            "docs/usage/troubleshooting.md",
            "docs/reference/ADVANCED_CONFIG.md",
            "docs/reference/FORENSIC_INTEGRITY.md",
            "docs/architecture/components.md",
            "docs/development/REPOSITORY_LAYOUT.md",
        ]
    )

    workflow = [
        "THIRD_PARTY_NOTICES.md",
        "python3.12 -m venv /opt/volatility3-2.28.0",
        "/opt/volatility3-2.28.0/bin/vol -q frameworkinfo.FrameworkInfo",
        "sha256sum /opt/dwarf2json/0.9.0/dwarf2json-linux-amd64",
        "dwarf2json_sha256",
        "/api/v1/volatility/readiness",
        "non-evidentiary smoke",
        "release/incident evidence",
    ]
    positions = [runbook.index(fragment) for fragment in workflow]
    assert positions == sorted(positions)

    for failure in [
        "volatility_not_configured",
        "volatility_not_found",
        "volatility_not_executable",
        "volatility_version_unsupported",
        "volatility_probe_failed",
        "dwarf2json_hash_not_configured",
        "dwarf2json_hash_mismatch",
        "guest transfer mismatch",
        "Upload ISF",
    ]:
        assert failure in runbook

    assert "AstinusLab never accepts upstream licenses" in runbook
    assert "never downloads" in runbook
    for recovery_step in [
        "Volatility 3 Framework 2.28.0",
        "stop queueing",
        "new",
        "versioned path",
        "configuration atomically",
        "Never overwrite",
    ]:
        assert recovery_step in runbook
    assert "vol3/bin" not in scoped_docs
    assert "bundled binary" not in scoped_docs.lower()
    assert "/usr/local/bin/vol3" not in scoped_docs
