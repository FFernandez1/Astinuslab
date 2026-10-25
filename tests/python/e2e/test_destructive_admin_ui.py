"""The controls that delete things, and the dialog that is supposed to gate them.

Image deletion, keystore deletion and evidence deletion are all covered by
backend tests, and none of them had ever been clicked. What no backend test can
answer is whether the confirmation actually gates the action: a dialog that
deletes on Cancel, or a button that deletes without asking, would pass every
test in the repository.

Each test gets its own backend, because these effects are files on disk and no
amount of truncating tables undoes them.

Run with:
    pytest tests/python/e2e/ -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def _dismiss(page: Page, answer: str) -> None:
    """Answer the shared confirmation dialog. `answer` is "yes" or "no"."""
    dialog = page.locator(".modal-overlay--confirm")
    expect(dialog).to_be_visible()
    # Cancel holds focus, so a stray Enter cannot destroy anything.
    expect(dialog.locator("[data-confirm-no]")).to_be_focused()
    dialog.locator(f"[data-confirm-{answer}]").click()
    expect(dialog).to_have_count(0)


def _generate_key(page: Page, base_url: str, name: str) -> None:
    page.goto(f"{base_url}/ssh-keys")
    page.locator('form[action="/ssh-keys/generate"] input[name="name"]').fill(name)
    page.locator('form[action="/ssh-keys/generate"] button[type="submit"]').click()
    expect(page.locator("tr.admin-table__row", has_text=name)).to_have_count(1)


@pytest.mark.parametrize("answer,surviving", [("no", 1), ("yes", 0)])
def test_deleting_a_keystore_key_obeys_the_confirmation(
    disposable_base_url: str, page: Page, answer: str, surviving: int
):
    page.set_viewport_size({"width": 1600, "height": 900})
    _generate_key(page, disposable_base_url, "doomed-key")

    row = page.locator("tr.admin-table__row", has_text="doomed-key")
    row.locator('button[formaction="/ssh-keys/delete"]').click()
    _dismiss(page, answer)

    expect(page.locator("tr.admin-table__row", has_text="doomed-key")).to_have_count(surviving)


def test_deleting_the_active_key_warns_that_jobs_will_fail(disposable_base_url: str, page: Page):
    """The active key is the one Job creation refuses without."""
    page.set_viewport_size({"width": 1600, "height": 900})
    _generate_key(page, disposable_base_url, "active-key")
    row = page.locator("tr.admin-table__row", has_text="active-key")
    row.locator('button[formaction="/ssh-keys/set-active"]').click()

    page.locator("tr.admin-table__row", has_text="active-key").locator('button[formaction="/ssh-keys/delete"]').click()

    dialog = page.locator(".modal-overlay--confirm")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("cannot be undone")
    expect(dialog).to_contain_text("jobs will fail")
    _dismiss(page, "no")
    expect(page.locator("tr.admin-table__row", has_text="active-key")).to_have_count(1)


@pytest.mark.parametrize("answer,surviving", [("no", 1), ("yes", 0)])
def test_deleting_an_image_obeys_the_confirmation(disposable_base_url: str, page: Page, answer: str, surviving: int):
    """Base images are large and re-downloading one is an operator's afternoon."""
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{disposable_base_url}/images")

    target = page.locator("tr", has_text="fedora-40-test.qcow2")
    expect(target).to_have_count(1)
    target.locator('button[formaction="/images/delete"]').click()
    _dismiss(page, answer)

    expect(page.locator("tr", has_text="fedora-40-test.qcow2")).to_have_count(surviving)


@pytest.mark.parametrize("answer,surviving", [("no", 1), ("yes", 0)])
def test_deleting_evidence_obeys_the_confirmation(
    disposable_base_url: str,
    disposable_storage_dir: Path,
    page: Page,
    answer: str,
    surviving: int,
):
    """Evidence deletion is the one that cannot be re-run: the VM is long gone."""
    # A directory only counts as evidence if it holds an acquisition: the vault
    # deliberately ignores directories carrying only Volatility byproducts, so
    # the seeded file has to use one of the acquisition names.
    orphan = disposable_storage_dir / "dumps" / "job-orphaned"
    orphan.mkdir(parents=True)
    (orphan / "capture-memory.dump").write_bytes(b"synthetic capture\n")

    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{disposable_base_url}/dumps")

    # Orphaned capture directories render as rows in the vault list, keyed by
    # directory name on the delete control itself.
    control = page.locator('button[name="dirnames"][value="job-orphaned"]')
    expect(control).to_have_count(1)
    control.click()
    _dismiss(page, answer)

    expect(page.locator('button[name="dirnames"][value="job-orphaned"]')).to_have_count(surviving)
    assert orphan.exists() == bool(surviving)
