from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / "src" / "astinuslab" / "web"
TEMPLATE = WEB / "templates" / "playground.html"
API = WEB / "static" / "playground" / "api.js"
TOP = WEB / "static" / "playground" / "render-top.js"


def test_playground_template_has_end_session_button():
    html = TEMPLATE.read_text(encoding="utf-8")

    assert 'id="btn-end-session"' in html
    assert "pg_foot_end_session" in html
    assert "hidden" in html


def test_playground_script_requests_cleanup_for_the_selected_session():
    api = API.read_text(encoding="utf-8")
    top = TOP.read_text(encoding="utf-8")

    assert "export async function endSession" in api
    assert 'request("DELETE", `/sessions/${encodeURIComponent(sessionId)}`)' in api
    assert "function sessionId(session)" in top
    assert "session.job_id" in top
    assert "await endSession(id)" in top
    # Acceptance is not terminal success. State transitions are exercised by
    # remediation/playground.spec.js against delayed DELETE/status responses.
    assert "persistCleanup(id)" in top
    assert "refreshSessionPoll()" in top
    assert "btn-end-session" in top


def test_end_session_copy_preserves_captures():
    """The new module entry keeps End Session modal-less and must not add
    destructive capture-storage copy."""
    html = TEMPLATE.read_text(encoding="utf-8")
    top = TOP.read_text(encoding="utf-8")

    assert "destroy all VMs, networks, disks, captures, and topology state" not in html
    assert "destroy all VMs, networks, disks, captures, and topology state" not in top
