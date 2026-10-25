from __future__ import annotations

from contextlib import contextmanager


def test_default_network_check_and_registration_share_one_locked_transaction(monkeypatch):
    from astinuslab.playground.routes import networks as routes

    session = object()
    opened_sessions: list[object] = []
    calls: list[str] = []

    @contextmanager
    def session_scope():
        opened_sessions.append(session)
        yield session

    def list_session_networks(current: object, job_id: str) -> list[object]:
        calls.append("list")
        return []

    monkeypatch.setattr(routes.db_mod, "session_scope", session_scope)
    monkeypatch.setattr(routes.pr, "lock_resource_transitions", lambda current: calls.append("lock"))
    monkeypatch.setattr(routes, "_raise_if_authoring_closed", lambda current, job_id: None)
    monkeypatch.setattr(routes.pr, "list_session_networks", list_session_networks)
    monkeypatch.setattr(routes, "_register_pending_network", lambda *args, **kwargs: calls.append("register"))
    monkeypatch.setattr(routes, "_enqueue_create_network", lambda **kwargs: calls.append("enqueue"))

    network_id = routes._ensure_default_network("pg-race")

    assert network_id.startswith("net-")
    assert opened_sessions == [session]
    assert calls == ["lock", "list", "register", "enqueue"]
