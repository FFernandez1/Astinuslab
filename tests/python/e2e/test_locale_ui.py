"""The product ships in four languages and is tested in one.

Key parity is checked in tests/python/ui/test_i18n_parity.py, which proves every
locale defines every key. It cannot prove that the page still works once those
longer strings are in it: a missing key renders as its own name, and a label that
is comfortable in English can push a control out of its container in French. The
orphaned footer tests exist because that happened.

Run with:
    pytest tests/python/e2e/ -q
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

TRANSLATED_LANGS = ["es", "fr", "ca"]
# Narrow enough that the footer and the sidebar have to compete for room.
NARROW_WIDTH = 390
NARROW_HEIGHT = 844


def _wait_for_scripts(page: Page) -> None:
    """Wait until the page's scripts have run, not merely until its DOM exists.

    `globals.js` stamps `nav-ready` on the sidebar as soon as it boots, which
    makes it the one cheap signal that DOMContentLoaded handlers have attached.
    The language switcher submits a form, so every step after it runs against a
    freshly loaded document, and Playwright's actionability checks wait for an
    element to be visible and enabled -- never for the listener bound to it. The
    wizard's open control is wired on DOMContentLoaded, so clicking it too early
    does nothing and `#create-vm-modal` stays hidden. That is how
    `test_the_wizard_walks_to_review_in_every_language` failed on roughly two
    runs in five while every assertion in it was correct.
    """
    expect(page.locator("nav.sidebar.nav-ready")).to_be_attached()


def _reveal_sidebar(page: Page) -> None:
    """Expand the navigation when the viewport has collapsed it behind the menu.

    At 600px and below the sidebar links -- which hold the language form --
    collapse behind `.sidebar-menu-toggle`, deliberately, so the page reflows at
    320px without two-dimensional scrolling (WCAG 1.4.10; the rule is annotated
    in static/css/layout.css). The switcher stays in the DOM, so a test that
    only reaches for it times out on an invisible element instead of reporting
    a control it cannot reach. Submitting the form navigates, which collapses
    the menu again, so every read of the sidebar has to expand it first.
    """
    toggle = page.locator(".sidebar-menu-toggle")
    if toggle.is_visible() and toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
        expect(page.locator("[data-lang-select]")).to_be_visible()


def _switch_language(page: Page, base_url: str, lang: str) -> None:
    """Use the sidebar switcher rather than posting to /set-lang directly.

    Asserts the interface actually changed, not merely that the select holds a
    new value: without that, every test here could be quietly running in English
    and proving nothing. Both reads of the sidebar are taken in the same state
    -- expanded -- so the comparison measures the translation and not the menu.
    """
    page.goto(f"{base_url}/playground")
    _wait_for_scripts(page)
    _reveal_sidebar(page)
    english_sidebar = page.locator("nav.sidebar").inner_text()

    # Choosing an option submits the form, so everything below runs against a
    # newly loaded document and has to wait for it before touching anything.
    page.locator("[data-lang-select]").select_option(lang)
    expect(page.locator("[data-lang-select]")).to_have_value(lang)
    _wait_for_scripts(page)
    _reveal_sidebar(page)
    assert page.locator("nav.sidebar").inner_text() != english_sidebar, lang


def _untranslated_markers(page: Page) -> list[str]:
    """Visible text that is a translation key rather than a translation.

    `window.t(key)` returns the key itself when the locale has no entry and the
    call site passed no English fallback, so a gap surfaces as `pg_destroy_vm`
    sitting in the interface.

    Detected by shape, not by membership of the served dictionary: the page is
    only given the active locale, and a key that is missing from it is exactly
    the one that will not be in that set. Interface copy is words and spaces; a
    bare snake_case token with two or more underscores is not copy.
    """
    return page.evaluate(
        """() => {
            const served = new Set(
                Object.keys(JSON.parse(document.getElementById("al-i18n")?.textContent || "{}"))
            );
            const keyShaped = /^[a-z][a-z0-9]*(_[a-z0-9]+){2,}$/;
            const found = [];
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
                const node = walker.currentNode;
                if (!node.parentElement || node.parentElement.closest("script, style")) continue;
                const text = node.textContent.trim();
                if (!text) continue;
                if (served.has(text) || keyShaped.test(text)) found.push(text);
            }
            return found;
        }"""
    )


def _controls_outside_their_container(page: Page, selector: str) -> list[str]:
    return page.evaluate(
        """(selector) => {
            const host = document.querySelector(selector);
            if (!host) return ["missing container: " + selector];
            const bounds = host.getBoundingClientRect();
            return Array.from(host.querySelectorAll("button, a, select"))
                .filter((node) => node.offsetParent !== null)
                .filter((node) => {
                    const box = node.getBoundingClientRect();
                    return box.left < bounds.left - 1 || box.right > bounds.right + 1;
                })
                .map((node) => (node.textContent || node.tagName).trim().slice(0, 40));
        }""",
        selector,
    )


@pytest.mark.parametrize("lang", TRANSLATED_LANGS)
def test_the_wizard_walks_to_review_in_every_language(
    playground_base_url: str, page: Page, create_vm_wizard, lang: str
):
    """Same flow, same step count, no key names leaking into the interface."""
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    page.set_viewport_size({"width": 1600, "height": 900})
    _switch_language(page, playground_base_url, lang)

    wizard = create_vm_wizard(playground_base_url).open(label="web-1", goto=False)
    assert _untranslated_markers(page) == []

    wizard.advance_to_submit()
    assert _untranslated_markers(page) == []
    wizard.submit()

    expect(wizard.modal).to_be_hidden()
    node = page.locator('.pg-v2-map-node[data-type="vm"]', has_text="web-1")
    expect(node).to_be_visible(timeout=15_000)

    # The detail panel is the densest translated surface in the product -- key
    # rows, phase labels, action names -- and several of its strings are looked
    # up with no English fallback, so a gap there surfaces as the key itself.
    node.click()
    expect(page.locator("#pg-detail-panel")).to_contain_text("web-1")
    assert _untranslated_markers(page) == []
    assert errors == []


@pytest.mark.parametrize("lang", TRANSLATED_LANGS)
def test_a_translated_playground_keeps_its_controls_inside_the_page(playground_base_url: str, page: Page, lang: str):
    """Longer strings must not push controls out of the layout.

    Checked at 390px, where the footer has least room -- the width the orphaned
    remediation tests already measure in English.
    """
    page.set_viewport_size({"width": NARROW_WIDTH, "height": NARROW_HEIGHT})
    _switch_language(page, playground_base_url, lang)

    assert _untranslated_markers(page) == []
    assert not page.evaluate("() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
    assert _controls_outside_their_container(page, ".pg-footer") == []
    assert _controls_outside_their_container(page, "nav.sidebar") == []


def test_the_language_choice_survives_a_reload(playground_base_url: str, page: Page):
    """The switcher writes a year-long cookie; a reload must not undo it."""
    page.set_viewport_size({"width": 1600, "height": 900})
    _switch_language(page, playground_base_url, "fr")
    chosen = page.locator("nav.sidebar").inner_text()

    page.reload()

    expect(page.locator("[data-lang-select]")).to_have_value("fr")
    assert page.locator("nav.sidebar").inner_text() == chosen
