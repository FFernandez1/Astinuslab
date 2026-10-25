"""Banner severity contract for `?message=` redirects.

`redirect_with_message` is error-only and `redirect_with_success` is the happy
path; every consumer template renders `banner-{{ message_type }}` and defaults an
unlabelled `?message=` to `error`.  The default matters twice over: a mutation
failure must not land in a green success banner, and a hand-typed or
attacker-crafted `/jobs/<id>?message=Account+suspended` must not borrow the
authority of one.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from astinuslab.ui.jobs_helpers import redirect_with_message, redirect_with_success


def _location_params(response) -> dict[str, list[str]]:
    return parse_qs(urlparse(response.headers["location"]).query)


def test_redirect_with_message_labels_the_banner_as_an_error():
    response = redirect_with_message("/dumps", "Archivo no encontrado: job-1:memory")

    assert response.status_code == 303
    params = _location_params(response)
    assert params["message_type"] == ["error"]
    assert params["message"] == ["Archivo no encontrado: job-1:memory"]


def test_redirect_with_success_labels_the_banner_as_a_success():
    response = redirect_with_success("/images", "Image deleted: base.qcow2.")

    assert response.status_code == 303
    params = _location_params(response)
    assert params["message_type"] == ["success"]
    assert params["message"] == ["Image deleted: base.qcow2."]


def test_redirect_helpers_preserve_unrelated_query_parameters():
    response = redirect_with_success("/workloads?edit=my%20workload", "Saved workload template: my workload")

    parsed = urlparse(response.headers["location"])
    params = _location_params(response)
    assert parsed.path == "/workloads"
    assert params["edit"] == ["my workload"]
    assert params["message_type"] == ["success"]


@pytest.mark.parametrize(
    ("helper", "expected"),
    [(redirect_with_message, "error"), (redirect_with_success, "success")],
    ids=("error", "success"),
)
def test_redirect_helpers_never_emit_a_duplicated_message_type(helper, expected):
    """A caller that already put message_type in the URL must not produce two keys.

    `query_params.get()` returns the first value, so a duplicate would let a stale
    label win over the one the helper intends.
    """
    response = helper("/volatility?message_type=success&message=stale", "fresh")

    params = _location_params(response)
    assert params["message_type"] == [expected]
    assert params["message"] == ["fresh"]


@pytest.mark.parametrize("bulk", [False, True])
def test_evidence_cleanup_failure_is_not_a_success_banner(client, monkeypatch, bulk):
    from astinuslab.ui import artifact_routes

    # The custody service committed a tombstone but could not unlink a file.
    monkeypatch.setattr(artifact_routes, "_remove_dump", lambda *_: (1, ["device busy: sidecar retained"]))
    response = client.post(
        "/dumps/bulk-delete" if bulk else "/dumps/delete",
        data={"items" if bulk else "item": "job-fixture:memory"},
        follow_redirects=False,
    )
    params = _location_params(response)
    assert params["message_type"] == ["error"]
    assert "device busy" in params["message"][0]


def _completed_job_id(in_memory_session_scope) -> str:
    from astinuslab.api.schemas import JobCreate
    from astinuslab.db.repositories import JobRepository

    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
                capture_type="memory",
            )
        )
        job.status = "completed"
        session.flush()
        return job.id


def test_job_detail_renders_an_error_redirect_as_a_red_alert_banner(client, in_memory_session_scope):
    job_id = _completed_job_id(in_memory_session_scope)
    location = client.post(f"/jobs/{job_id}/vol3-analysis", follow_redirects=False).headers["location"]
    # Precondition: Volatility is not configured in the test environment, so the
    # request fails and the handler must label the redirect as an error.
    assert parse_qs(urlparse(location).query)["message_type"] == ["error"]

    html = client.get(location).text

    assert 'class="banner-error vol-banner" role="alert"' in html
    assert "banner-success" not in html


def test_job_detail_renders_a_success_redirect_as_a_green_status_banner(client, in_memory_session_scope):
    job_id = _completed_job_id(in_memory_session_scope)

    html = client.get(f"/jobs/{job_id}?message=Job+queued.&message_type=success").text

    assert 'class="banner-success vol-banner" role="status"' in html
    assert "banner-error" not in html
    assert "Job queued." in html


def test_job_detail_defaults_an_unlabelled_message_to_the_error_banner(client, in_memory_session_scope):
    """A crafted link carries no message_type, so it must never render as a success."""
    job_id = _completed_job_id(in_memory_session_scope)

    html = client.get(f"/jobs/{job_id}?message=Account+suspended").text

    assert 'class="banner-error vol-banner" role="alert"' in html
    assert "banner-success" not in html
    assert "Account suspended" in html


def test_bulk_delete_with_a_skipped_job_renders_the_error_banner(client, in_memory_session_scope):
    """A partial failure must not render green just because the tally leads the message.

    `BulkDeleteJobsResult.message` is a success tally with the failure appended, so
    the severity has to come from `failed`, not from the text.
    """
    job_id = _completed_job_id(in_memory_session_scope)

    location = client.post(
        "/jobs/bulk-delete",
        data={"job_ids": [job_id, "job-does-not-exist"]},
        follow_redirects=False,
    ).headers["location"]

    assert urlparse(location).path == "/jobs"
    assert parse_qs(urlparse(location).query)["message_type"] == ["error"]
    html = client.get(location).text
    assert 'class="banner-error banner--compact' in html
    assert "banner-success" not in html
    assert "Jobs deleted: 1. Skipped: 1." in html


def test_clean_bulk_delete_renders_the_success_banner(client, in_memory_session_scope):
    job_id = _completed_job_id(in_memory_session_scope)

    location = client.post(
        "/jobs/bulk-delete",
        data={"job_ids": [job_id]},
        follow_redirects=False,
    ).headers["location"]

    assert parse_qs(urlparse(location).query)["message_type"] == ["success"]
    html = client.get(location).text
    assert 'class="banner-success banner--compact' in html
    assert "banner-error" not in html
    assert "Jobs deleted: 1. Skipped: 0." in html


def test_bulk_delete_with_nothing_selected_renders_the_error_banner(client):
    """Nothing was deleted, so the corrective notice must not borrow a success banner."""
    location = client.post("/jobs/bulk-delete", data={}, follow_redirects=False).headers["location"]

    assert parse_qs(urlparse(location).query)["message_type"] == ["error"]
    html = client.get(location).text
    assert 'class="banner-error banner--compact' in html
    assert "No jobs selected for deletion." in html


@pytest.mark.parametrize(
    "path",
    ["/dumps", "/images", "/ssh-keys", "/workload-cache"],
)
def test_admin_pages_default_an_unlabelled_message_to_the_error_banner(client, path):
    unlabelled = client.get(f"{path}?message=Account+suspended").text
    assert 'class="banner-error banner--compact' in unlabelled
    assert 'role="alert"' in unlabelled
    assert "banner-success" not in unlabelled

    labelled = client.get(f"{path}?message=Deleted.&message_type=success").text
    assert 'class="banner-success banner--compact' in labelled
