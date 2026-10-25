"""Orphan cleanup reports refusals and partial cleanup without weakening custody."""

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from astinuslab.db import models
from astinuslab.ui import artifact_routes
from tests.python._support.helpers import create_evidence_triad


@pytest.fixture
def vault(tmp_path, monkeypatch, in_memory_session_scope):
    storage = tmp_path / "storage"
    monkeypatch.setattr(artifact_routes, "settings", SimpleNamespace(base_dir=tmp_path, storage_dir=storage))
    monkeypatch.setattr(artifact_routes, "session_scope", in_memory_session_scope)

    def create(dirname, *, protected=False):
        (storage / "dumps" / dirname).mkdir(parents=True)
        with in_memory_session_scope() as session:
            case_id = None
            if protected:
                case_id = f"case-{dirname}"
                session.add(models.Case(id=case_id, name="Synthetic protected evidence", status="sealed"))
                session.flush()
            artifact, paths = create_evidence_triad(session, storage, case_id=case_id, name=f"{dirname}/memory.dump")
            return artifact.id, paths

    return create


def submit(client, names):
    token = "orphan-feedback-test"
    client.cookies.set("astinuslab_csrf", token)
    response = client.post(
        "/dumps/orphans/delete",
        data={"dirnames": names},
        headers={"X-CSRF-Token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    params = parse_qs(urlsplit(response.headers["location"]).query)
    rendered = client.get(response.headers["location"])
    assert rendered.status_code == 200
    return params, rendered.text


@pytest.mark.parametrize("include_eligible", [False, True])
def test_protected_orphan_reports_incomplete_cleanup(client, vault, in_memory_session_scope, include_eligible):
    artifact_id, protected_paths = vault("protected", protected=True)
    names = ["protected"]
    if include_eligible:
        _eligible_id, eligible_paths = vault("eligible")
        names.append("eligible")
    params, html = submit(client, names)
    assert params["message_type"] == ["error"]
    assert f"Deleted: {int(include_eligible)}. Not removed: 1." in params["message"][0]
    assert 'role="alert"' in html
    assert all(path.exists() for path in protected_paths)
    with in_memory_session_scope() as session:
        assert session.get(models.Artifact, artifact_id) is not None
        assert session.query(models.EvidenceTombstone).filter_by(artifact_id=artifact_id).count() == 0
    if include_eligible:
        assert not any(path.exists() for path in eligible_paths)


def test_failed_unlink_reports_incomplete_cleanup(client, vault, in_memory_session_scope, monkeypatch):
    artifact_id, paths = vault("blocked")
    unlink = Path.unlink

    def deny_payload(path, *args, **kwargs):
        if path == paths[0]:
            raise PermissionError("synthetic cleanup failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_payload)
    params, html = submit(client, ["blocked"])
    assert params["message_type"] == ["error"]
    assert "Deleted: 0. Not removed: 1." in params["message"][0]
    assert 'role="alert"' in html
    assert paths[0].exists()
    with in_memory_session_scope() as session:
        assert session.get(models.Artifact, artifact_id) is None
        tombstone = session.query(models.EvidenceTombstone).filter_by(artifact_id=artifact_id).one()
        assert tombstone.cleanup_state == "failed"


@pytest.mark.parametrize("duplicate", [False, True])
def test_completed_cleanup_reports_success_once(client, vault, in_memory_session_scope, duplicate):
    artifact_id, paths = vault("eligible")
    params, html = submit(client, ["eligible"] * (2 if duplicate else 1))
    assert params["message_type"] == ["success"]
    assert params["message"] == ["Orphan dumps deleted: 1."]
    assert 'role="status"' in html
    assert not paths[0].parent.exists()
    with in_memory_session_scope() as session:
        assert session.get(models.Artifact, artifact_id) is None
        assert (
            session.query(models.EvidenceTombstone).filter_by(artifact_id=artifact_id).one().cleanup_state == "complete"
        )


def test_empty_selection_does_not_claim_success(client, vault):
    params, html = submit(client, [])
    assert params["message_type"] == ["error"]
    assert 'role="alert"' in html
