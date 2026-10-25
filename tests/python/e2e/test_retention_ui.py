"""Retention: the control that deletes evidence on a schedule.

The backend proves the windows are persisted and that a dry run deletes nothing.
Neither can prove the operator's Preview button is wired to the dry run rather
than to the real one -- a single wrong `hx-post` would delete evidence from a
control labelled Preview, and every backend test would still pass.

Run with:
    pytest tests/python/e2e/ -q
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

LOGS_WINDOW_DAYS = 2
OLD_FILE_AGE_DAYS = 30


def _seed_logs(storage_dir: Path) -> tuple[Path, Path]:
    """One log outside the retention window, one comfortably inside it."""
    logs = storage_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stale = logs / "astinuslab-ancient.log"
    fresh = logs / "astinuslab-today.log"
    stale.write_text("old\n", encoding="utf-8")
    fresh.write_text("new\n", encoding="utf-8")
    old_mtime = time.time() - OLD_FILE_AGE_DAYS * 86400
    os.utime(stale, (old_mtime, old_mtime))
    return stale, fresh


def _open_retention_form(page: Page, base_url: str) -> None:
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{base_url}/settings")
    form = page.locator("form.settings-retention-form").first
    expect(form).to_be_visible()
    form.locator('input[name="logs_days"]').fill(str(LOGS_WINDOW_DAYS))
    form.locator('input[name="dumps_days"]').fill("365")


# The control that actually deletes is labelled for what it does to the windows
# as well as to the evidence.
RUN_BUTTON = "Apply + cleanup now"


def _click(page: Page, label: str) -> None:
    page.locator("form.settings-retention-form").first.get_by_role("button", name=label, exact=True).click()


def _result(page: Page):
    """The outcome lands in the out-of-band toast region, not inside the form."""
    return page.locator("#settings-toasts")


def test_preview_reports_what_would_go_and_deletes_nothing(
    disposable_base_url: str, disposable_storage_dir: Path, page: Page
):
    """The distinction the whole control rests on."""
    stale, fresh = _seed_logs(disposable_storage_dir)

    _open_retention_form(page, disposable_base_url)
    _click(page, "Preview")

    # Preview says what a run would do, and says it did nothing.
    result = _result(page)
    expect(result).to_contain_text("would delete 0 dumps and 1 logs")
    expect(result).to_contain_text("nothing deleted")
    assert stale.exists(), "Preview deleted a file"
    assert fresh.exists()


def test_running_retention_obeys_the_confirmation(disposable_base_url: str, disposable_storage_dir: Path, page: Page):
    stale, fresh = _seed_logs(disposable_storage_dir)

    _open_retention_form(page, disposable_base_url)
    _click(page, RUN_BUTTON)

    dialog = page.locator(".modal-overlay--confirm")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("cannot be undone")
    dialog.locator("[data-confirm-no]").click()

    expect(dialog).to_have_count(0)
    assert stale.exists(), "Cancel deleted evidence"
    assert fresh.exists()


def test_a_confirmed_run_deletes_only_what_is_past_the_window(
    disposable_base_url: str, disposable_storage_dir: Path, page: Page
):
    """The window is the rule; a run that ignored it would take everything."""
    stale, fresh = _seed_logs(disposable_storage_dir)

    _open_retention_form(page, disposable_base_url)
    _click(page, RUN_BUTTON)
    page.locator(".modal-overlay--confirm [data-confirm-yes]").click()

    expect(_result(page)).to_contain_text("logs_deleted: 1")
    assert not stale.exists()
    assert fresh.exists(), "a file inside the window was deleted"
