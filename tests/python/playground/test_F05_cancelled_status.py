from unittest.mock import MagicMock

from astinuslab.db.models import PlaygroundSession
from astinuslab.playground import cleanup as playground_cleanup
from astinuslab.playground import network_lifecycle as nl
from astinuslab.playground import orchestrator as po
from astinuslab.playground import repositories as pr
from tests.python._support.helpers import autospec_setattr


def test_F05_destroy_session_lands_as_cancelled(monkeypatch, in_memory_session_scope):
    monkeypatch.setattr(po, "session_scope", in_memory_session_scope)
    monkeypatch.setattr(playground_cleanup, "session_scope", in_memory_session_scope)
    autospec_setattr(monkeypatch, po, "_vm_lifecycle", lambda: MagicMock())
    autospec_setattr(monkeypatch, nl, "_networks_helper", lambda: MagicMock())

    with in_memory_session_scope() as s:
        pr.create_playground_session(s, job_id="pg-2")

    playground_cleanup.purge_session_resources(job_id="pg-2", final_status="cancelled")

    with in_memory_session_scope() as s:
        playground_session = s.query(PlaygroundSession).filter_by(id="pg-2").one()
        assert playground_session.status == "cancelled"
