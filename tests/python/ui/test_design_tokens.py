"""Contracts that keep the interface visually consistent across pages.

The stylesheets used to carry two parallel type ladders and 31 distinct literal
font sizes, six near-identical panel boxes, and duplicate definitions of the
shared card primitives. Each of those reads as a different page to an operator
moving between Jobs, Playground, Volatility, Settings and the guides. The tests
below pin the single source for each of those decisions, so drift fails here
instead of showing up as a page that looks like another product.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB = Path(__file__).resolve().parents[3] / "src" / "astinuslab" / "web"
CSS_DIR = WEB / "static" / "css"
TOKENS = CSS_DIR / "tokens.css"
LAYOUT = CSS_DIR / "layout.css"
COMPONENTS = CSS_DIR / "components.css"
TEMPLATES = WEB / "templates"

FONT_SIZE = re.compile(r"font-size:\s*([^;]+);")
CUSTOM_PROPERTY = re.compile(r"^\s*(--[a-z0-9-]+)\s*:", re.MULTILINE)

# Custom properties written by JavaScript onto inline styles at runtime; they
# are referenced by the stylesheets but never declared in one.
RUNTIME_PROPERTIES = {"--fill", "--pct", "--map-world-w", "--map-world-h"}

# Primitives whose whole point is to be defined exactly once.
SHARED_PRIMITIVES = (".panel", ".card", ".card-title", ".card-header", ".section-label")

# Pages that used to declare their own copy of the panel box.
PANEL_ALIASES = (
    ".vol-panel",
    ".pg-panel",
    ".package-panel",
    ".settings-card",
    ".wiz-vm-col",
    ".image-download-settings__panel",
)


def stylesheets() -> list[Path]:
    return sorted([*CSS_DIR.glob("*.css"), *(CSS_DIR / "pages").glob("*.css")])


def test_every_rendered_font_size_comes_from_the_type_ladder():
    offenders: list[str] = []
    for path in stylesheets():
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for value in FONT_SIZE.findall(line):
                value = value.strip()
                if value == "inherit" or re.fullmatch(r"var\(--text-[a-z0-9]+\)", value):
                    continue
                offenders.append(f"{path.relative_to(CSS_DIR)}:{line_number}: {value}")

    assert not offenders, (
        "Font sizes must come from the --text-* ladder in tokens.css, not from a literal:\n" + "\n".join(offenders)
    )


def test_templates_do_not_inline_a_literal_font_size():
    offenders = [
        f"{path.relative_to(TEMPLATES)}:{line_number}: {value.strip()}"
        for path in sorted(TEMPLATES.rglob("*.html"))
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        for value in FONT_SIZE.findall(line)
        if not value.strip().startswith("var(--text-")
    ]

    assert not offenders, "Inline template styles must use the --text-* ladder:\n" + "\n".join(offenders)


def test_the_type_ladder_has_exactly_one_definition():
    """A second ladder is how the playground HUD drifted to its own sizes."""
    ladder = set(re.findall(r"^\s*(--text-[a-z0-9]+)\s*:", TOKENS.read_text(encoding="utf-8"), re.MULTILINE))
    assert ladder, "tokens.css no longer defines a --text-* ladder"

    referenced: set[str] = set()
    for path in stylesheets():
        text = path.read_text(encoding="utf-8")
        referenced |= {match.strip() for match in FONT_SIZE.findall(text) if match.strip() != "inherit"}

    steps = {value: re.fullmatch(r"var\((--text-[a-z0-9]+)\)", value) for value in referenced}
    literals = sorted(value for value, step in steps.items() if step is None)
    assert not literals, f"font-size must name a --text-* step, not a literal: {literals}"
    used = {step.group(1) for step in steps.values() if step is not None}
    assert used <= ladder, f"font-size references a step that tokens.css does not define: {sorted(used - ladder)}"


def test_no_stylesheet_references_an_undeclared_custom_property():
    declared: set[str] = set()
    used: dict[str, set[str]] = {}
    for path in stylesheets():
        text = path.read_text(encoding="utf-8")
        # Strip comments so a property named only in prose does not count as a use.
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        declared |= set(CUSTOM_PROPERTY.findall(text))
        for name in re.findall(r"var\((--[a-z0-9-]+)", text):
            used.setdefault(name, set()).add(path.name)

    missing = {name: sorted(files) for name, files in used.items() if name not in declared | RUNTIME_PROPERTIES}
    assert not missing, (
        "These custom properties are referenced but never declared, so the declaration "
        f"is dropped at computed-value time: {missing}"
    )


@pytest.mark.parametrize("selector", SHARED_PRIMITIVES)
def test_shared_primitive_is_defined_once(selector):
    """components.css carried a re-skin layer that silently redefined these."""
    # The selector may head its own rule or sit in a comma-separated list, so
    # match it as a whole selector followed by either a comma or the block.
    definitions = re.findall(
        rf"^{re.escape(selector)}\s*(?:,|\{{)", COMPONENTS.read_text(encoding="utf-8"), re.MULTILINE
    )

    assert len(definitions) == 1, (
        f"{selector} is defined {len(definitions)} times in components.css; "
        "a second definition makes the first one dead code that still reads as authoritative"
    )


def test_shared_panel_primitive_covers_every_page_alias():
    components = COMPONENTS.read_text(encoding="utf-8")
    panel_rule = re.search(r"^\.panel,\n((?:^[^{\n]+\n)*?[^{\n]+)\{", components, re.MULTILINE)
    assert panel_rule is not None, "components.css no longer declares a shared .panel box"

    covered = panel_rule.group(0)
    missing = [alias for alias in PANEL_ALIASES if not re.search(rf"^{re.escape(alias)}[,\s]", covered, re.MULTILINE)]
    assert not missing, f"These pages are not covered by the shared .panel box: {missing}"


@pytest.mark.parametrize("alias", PANEL_ALIASES)
def test_page_does_not_redeclare_the_panel_box(alias):
    """The geometry lives in components.css; pages may only add their own deltas."""
    box_properties = {"background", "border", "border-radius"}
    offenders: list[str] = []
    for path in (CSS_DIR / "pages").glob("*.css"):
        for rule in re.finditer(
            rf"^{re.escape(alias)} \{{(.*?)\}}", path.read_text(encoding="utf-8"), re.DOTALL | re.MULTILINE
        ):
            declared = {line.split(":")[0].strip() for line in rule.group(1).splitlines() if ":" in line}
            clashing = declared & box_properties
            if clashing:
                offenders.append(f"{path.name}: {alias} re-declares {sorted(clashing)}")

    assert not offenders, "\n".join(offenders)


def test_headings_have_no_user_agent_size():
    """Without this reset an unclassed heading renders at 2em and breaks the scale."""
    layout = LAYOUT.read_text(encoding="utf-8")
    reset = re.search(r"^h1,\nh2,\nh3,\nh4,\nh5,\nh6 \{(.*?)\}", layout, re.DOTALL | re.MULTILINE)

    assert reset is not None, "layout.css no longer resets heading sizes"
    assert "font-size: inherit;" in reset.group(1)


def test_every_page_uses_the_shared_page_head_shell():
    """One shell, so every page's title lands on the same gutter."""
    exempt = {"base.html", "error_not_found.html", "job_new.html", "job_detail.html"}
    missing = [
        path.name
        for path in sorted(TEMPLATES.glob("*.html"))
        if path.name not in exempt and 'class="page-head"' not in path.read_text(encoding="utf-8")
    ]

    assert not missing, f"These pages do not use the shared page-head shell: {missing}"


def test_no_page_declares_two_level_one_headings():
    """help.html renders a guide that brings its own <h1>; the shell label above
    it must stay an eyebrow, or the page ships two competing document titles."""
    offenders = {
        path.name: count
        for path in sorted(TEMPLATES.glob("*.html"))
        if (count := len(re.findall(r"<h1[ >]", path.read_text(encoding="utf-8")))) > 1
    }

    assert not offenders, f"These templates declare more than one <h1>: {offenders}"


PAGE_MODULES = WEB / "static" / "packages"

CLASS_ATTRIBUTE = re.compile(r'class="([^"{}]*)"')
# Three ways a module names a class: the el() property, a direct assignment, and
# classList. The property form admits template literals, with their ${...} holes
# stripped — without that, every `wiz-badge ${badgeVariant(...)}` was unscanned
# and a typo'd variant shipped green.
CLASS_PROPERTY = re.compile(r'className\s*[:=]\s*(?:"([^"]*)"|`([^`]*)`)')
CLASS_LIST = re.compile(r'classList\.(?:add|remove|toggle)\(\s*"([^"]*)"')
# A hole is replaced by a sentinel rather than a space: a token that still
# contains one was built at runtime (`wiz-btn--${variant}`) and is not a
# literal class, while its static neighbours in the same string are.
INTERPOLATION = re.compile(r"\$\{[^}]*\}")
DYNAMIC = "\x00"
CSS_CLASS = re.compile(r"\.(-?[_a-zA-Z][_a-zA-Z0-9-]*)")


def markup_classes() -> dict[str, set[str]]:
    """Every class the packages page puts on an element, template and module."""
    found: dict[str, set[str]] = {}
    sources = [TEMPLATES / "packages.html", *sorted(PAGE_MODULES.glob("*.js"))]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        values = [match.group(1) for match in CLASS_ATTRIBUTE.finditer(text)]
        values += [
            INTERPOLATION.sub(DYNAMIC, match.group(1) or match.group(2)) for match in CLASS_PROPERTY.finditer(text)
        ]
        values += [match.group(1) for match in CLASS_LIST.finditer(text)]
        for value in values:
            for name in value.split():
                if name and DYNAMIC not in name:
                    found.setdefault(name, set()).add(path.name)
    return found


def styled_classes() -> set[str]:
    names: set[str] = set()
    for path in stylesheets():
        text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        names |= set(CSS_CLASS.findall(text))
    return names


def test_every_class_the_packages_page_uses_has_a_rule():
    """A class with no rule renders with no treatment, which is how the empty
    library, the inline error paragraphs and every verification history row
    came to have none."""
    offenders = {name: sorted(files) for name, files in markup_classes().items() if name not in styled_classes()}

    assert not offenders, (
        f"These classes are used but have no rule in any stylesheet, so they render with no treatment: {offenders}"
    )


def selector_list(css: str, head: str) -> str | None:
    """The full comma-separated selector list of the rule headed by `head`."""
    match = re.search(rf"^{re.escape(head)}(?=\s*[,{{])(.*?)\{{", css, re.MULTILINE | re.DOTALL)
    return match.group(0) if match else None


# A primitive that two or more pages need, with the page class names that were
# folded into it. Each page class stays valid markup; only the declaration moves.
PROMOTED_PRIMITIVES = {
    ".form-error": (".pg-form-error",),
    ".pagetabs": (".vol-pagetabs", ".package-tabs"),
    ".pagetab": (".vol-pagetab",),
    ".file-drop": (".image-drop-zone",),
    ".file-drop__icon": (".image-drop-icon",),
    ".file-drop__body": (".image-drop-body",),
    ".file-drop__name": (".image-file-name",),
    ".file-drop__help": (".image-file-help",),
}


@pytest.mark.parametrize("primitive,aliases", sorted(PROMOTED_PRIMITIVES.items()))
def test_promoted_primitive_lives_in_components_with_its_aliases(primitive, aliases):
    """A primitive two pages need belongs in components.css once, with the
    original page class kept as an alias so no template or module moves."""
    covered = selector_list(COMPONENTS.read_text(encoding="utf-8"), primitive)
    assert covered is not None, f"{primitive} is not declared in components.css"

    missing = [alias for alias in aliases if alias not in covered]
    assert not missing, f"{primitive} does not cover its aliases: {missing}"

    for path in sorted((CSS_DIR / "pages").glob("*.css")):
        text = path.read_text(encoding="utf-8")
        for name in (primitive, *aliases):
            # `^.vol-pagetab {` is a redeclaration; `^.vol-pagetab .n {` is a
            # page-local delta on a descendant and stays where it is.
            assert not re.search(rf"^{re.escape(name)}\s*(?:,|\{{)", text, re.MULTILINE), (
                f"{path.name} still declares {name}; the rule in components.css is the only one"
            )


def test_packages_panels_use_the_shared_head_and_body():
    """The head bar is where `display: flex` lives. packages.css declared
    justify-content on a head that no rule ever gave flex, so the Library
    heading and its checkbox never shared a row and the declarations were
    inert."""
    components = COMPONENTS.read_text(encoding="utf-8")

    head = selector_list(components, ".panel__head")
    assert head is not None, "components.css no longer declares a shared .panel__head"
    assert ".package-panel__head" in head, (
        ".package-panel__head is not covered by the shared head rule, so it has no display: flex"
    )

    body = selector_list(components, ".panel__body")
    assert body is not None and ".package-panel__body" in body, (
        ".package-panel__body is not covered by the shared body rule"
    )

    title = selector_list(components, ".panel__title")
    assert title is not None and ".package-panel__title" in title, (
        ".package-panel__title is not covered by the shared title rule"
    )
    assert ".package-panel__head h2" not in title, (
        "the descendant selector is replaced by the .package-panel__title class"
    )

    pad = selector_list(components, ".panel--pad")
    assert pad is not None and ".package-panel" not in pad, (
        ".package-panel carries a body that owns the padding; it must leave the --pad list"
    )
    assert ".package-panel" in (selector_list(components, ".panel") or ""), (
        ".package-panel must stay in the shared box rule; only the padding moves"
    )


def test_packages_template_has_no_inert_or_dead_hooks():
    template = (TEMPLATES / "packages.html").read_text(encoding="utf-8")
    page_css = (CSS_DIR / "pages" / "packages.css").read_text(encoding="utf-8")

    for gone in ("package-panel__head--between", "package-shell", "package-library-panel"):
        assert gone not in template, f"{gone} is removed from the template"
        assert gone not in page_css, f"{gone} is removed from packages.css"

    assert "package-workflow-grid" not in page_css, "dead rule, referenced by nothing"
    assert ".package-panel__head .section-label" not in page_css, (
        "dead rule: the eyebrows were removed from the template"
    )

    # Every panel head goes straight to its titled heading. The classless
    # wrapper divs the removed eyebrows left behind are gone.
    collapsed = re.sub(r"\s+", " ", template)
    heads = re.findall(r'<div class="package-panel__head"> *(<[a-z0-9]+)', collapsed)
    assert heads, "the template declares no panel heads"
    assert all(head == "<h2" for head in heads), (
        f"a panel head opens with something other than its heading: {sorted(set(heads))}"
    )


def test_no_template_exposes_a_bare_file_input():
    """Every upload in the interface puts the input behind a styled label. A raw
    file input renders as the operating system's own control and reads as a
    different application.

    Two idioms count as behind a label: the `hidden` attribute, and `sr-only`,
    which hides the input visually but keeps it in the tab order. Both are in
    use, and both are correct; an input with neither is the bare control.
    """
    offenders: list[str] = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"<input\b[^>]*type=\"file\"[^>]*>", text, re.DOTALL):
            tag = match.group(0)
            if "hidden" not in tag and "sr-only" not in tag:
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(TEMPLATES)}:{line}")

    assert not offenders, f"These file inputs are not hidden behind a styled control: {offenders}"


def test_creator_labels_use_the_shared_field_shape():
    """A control nested inside its own label inherits the label's uppercase
    micro-type. The rest of the interface puts the label and the control side
    by side inside .field-block."""
    template = (TEMPLATES / "packages.html").read_text(encoding="utf-8")
    creator = template[template.index('id="package-creator-panel"') :]

    nested = re.findall(r'<label class="field-label"[^>]*>[^<]*<(?:input|textarea|select)\b', creator)
    assert not nested, f"{len(nested)} creator controls are still nested inside their label"

    assert "field-block" in creator, "the creator form uses the shared .field-block wrapper"
    assert "control-mono" in creator, "the JSON textareas use the shared monospace control"


def test_no_page_head_carries_an_eyebrow():
    """One page shipped a section label above its h1 and no other did, so the
    packages title sat lower than every other page's."""
    offenders: list[str] = []
    for path in sorted(TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'<div class="page-head">(.*?)<h1', text, re.DOTALL):
            if "section-label" in match.group(1):
                offenders.append(path.name)

    assert not offenders, f"These page heads carry an eyebrow above the title: {offenders}"


def test_the_archive_picker_input_stays_focusable():
    """`hidden` is display:none: the control leaves the tab order and the
    accessibility tree, so the page's primary action becomes pointer-only, and
    a `required` control that cannot be focused aborts submission silently
    instead of reporting. `sr-only` hides it visually and keeps both."""
    template = (TEMPLATES / "packages.html").read_text(encoding="utf-8")
    picker = re.search(r'<input\b[^>]*id="package-file"[^>]*>', template, re.DOTALL)
    assert picker is not None, "the archive picker input is gone"

    assert "sr-only" in picker.group(0), (
        "the archive picker must be sr-only, not hidden: a display:none input cannot be "
        "reached by keyboard and is not announced"
    )
    assert "hidden" not in picker.group(0), "sr-only replaces hidden; both together is still hidden"
    assert "required" not in picker.group(0), (
        "native validation on a visually hidden control anchors its bubble to a 1px box; "
        "index.js raises the localised pkg_archive_required instead"
    )

    components = COMPONENTS.read_text(encoding="utf-8")
    assert ".file-drop:focus-within" in components, (
        "the styled label must show a focus ring when the input behind it takes focus, "
        "or keyboard users get no indication of where they are"
    )


def test_grid_status_regions_do_not_stretch_their_inline_children():
    """A grid item defaults to justify-self: stretch, which blockifies an inline
    badge into a full-width bar and an inline-flex button into a full-width
    slab. Both of these regions hold inline controls the renderers emit."""
    page_css = (CSS_DIR / "pages" / "packages.css").read_text(encoding="utf-8")
    for selector in (".package-status", ".package-verification-row"):
        rule = re.search(rf"^{re.escape(selector)} \{{(.*?)\}}", page_css, re.DOTALL | re.MULTILINE)
        assert rule is not None, f"{selector} has no rule"
        assert "display: grid" in rule.group(1), f"{selector} is expected to be a grid"
        assert "justify-items: start" in rule.group(1), (
            f"{selector} is a grid holding inline controls; without justify-items: start its "
            "badges and buttons stretch to the full panel width"
        )


def test_the_page_does_not_override_the_shared_mono_control():
    """`font: inherit` resets family and size, and at (0,1,1) it outranks
    .control-mono at (0,1,0). It existed only to escape the uppercase micro-type
    of a label the controls used to be nested inside; they are siblings now."""
    page_css = (CSS_DIR / "pages" / "packages.css").read_text(encoding="utf-8")
    assert "font: inherit" not in page_css, "font: inherit silently defeats .control-mono on every creator textarea"


def test_every_badge_variant_the_page_can_emit_has_a_rule():
    """badgeVariant's values reach the DOM through a template literal, so the
    class scanner sees only the `wiz-badge` prefix. Read the map itself: a
    typo'd variant would otherwise render as an unstyled bare badge."""
    source = (PAGE_MODULES / "render-library.js").read_text(encoding="utf-8")
    variants = set(re.findall(r'"(wiz-badge--[a-z-]+)"', source))
    assert variants, "render-library.js no longer maps statuses onto badge variants"

    styled = styled_classes()
    missing = sorted(name for name in variants if name not in styled)
    assert not missing, f"badgeVariant can emit these, but no stylesheet declares them: {missing}"
