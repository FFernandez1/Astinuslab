"""The guide-link macro turns a {guide} placeholder into a real link.

The message text around the placeholder is translator-supplied, so the macro
must never mark it safe. These tests pin that: only the anchor the macro itself
writes may reach the page.
"""

from __future__ import annotations

import pytest

from astinuslab.ui.job_context import templates

pytestmark = [pytest.mark.unit]

_RENDER = '{% import "partials/_guide_link.html" as guide %}{{ guide.guide_message(text) }}'
_ANCHOR = '<a href="/help/user/VOLATILITY3.md">docs/user/VOLATILITY3.md</a>'


def _render(text: str | None) -> str:
    return templates.env.from_string(_RENDER).render(text=text)


def test_placeholder_becomes_the_guide_link():
    out = _render("Enable it in configuration and review {guide}.")
    assert out == f"Enable it in configuration and review {_ANCHOR}."


def test_message_without_the_placeholder_is_passed_through_unchanged():
    out = _render("Volatility integration is disabled.")
    assert out == "Volatility integration is disabled."
    assert "<a " not in out


def test_markup_in_the_translated_text_is_escaped_not_rendered():
    out = _render("<script>alert(1)</script> review {guide}.")
    assert "<script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    # The macro's own anchor still renders; nothing else does.
    assert out.count("<a ") == 1
    assert _ANCHOR in out


def test_a_forged_anchor_in_the_translated_text_cannot_reach_the_page():
    out = _render('<a href="javascript:alert(1)">click</a> and review {guide}.')
    assert "javascript:" not in out or "&lt;a href=&#34;javascript:" in out
    assert out.count("<a ") == 1, "only the macro's own anchor may render"
    assert _ANCHOR in out


def test_a_second_placeholder_is_left_literal_rather_than_duplicating_the_link():
    # Splitting must not turn a stray second {guide} into another link; the
    # remainder is rejoined verbatim so translators see their own text back.
    out = _render("review {guide} and also {guide}")
    assert out.count("<a ") == 1
    assert out == f"review {_ANCHOR} and also {{guide}}"


def test_empty_and_none_render_empty():
    assert _render("") == ""
    assert _render(None) == ""


def test_the_linked_guide_is_actually_served(client):
    """A banner that links to a 404 is worse than one naming a path.

    ops/deploy/install.sh ships published guides so this resolves on an
    operator's machine; tests/python/tooling/test_install_script.py pins the shipping half.
    """
    response = client.get("/docs/user/VOLATILITY3.md")

    assert response.status_code == 200
    assert response.content, "the guide must not be served empty"


def test_the_docs_mount_exposes_only_user_docs(client):
    # docs/api and the rest of docs/ are not installed and must not be reachable.
    assert client.get("/docs/api/errors.md").status_code == 404
    assert client.get("/docs/user/../api/errors.md").status_code == 404


def test_the_docs_mount_is_not_public():
    """Unlike /static/, the guide sits behind admin auth: it is not an anonymous route."""
    from astinuslab.core.security import requires_admin_auth

    assert requires_admin_auth("/docs/user/VOLATILITY3.md") is True
    assert requires_admin_auth("/static/css/pages/volatility.css") is False
