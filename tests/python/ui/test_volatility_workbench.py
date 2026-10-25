from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from astinuslab.api.schemas import JobCreate
from astinuslab.db.repositories import ArtifactRepository, JobRepository
from astinuslab.forensics import services as forensic_services


def _readiness(*, enabled: bool = True, analysis: bool = True, isf_build: bool = True):
    from astinuslab.forensics.vol3_tools import ToolStatus, Vol3Readiness

    return Vol3Readiness(
        enabled=enabled,
        analysis=ToolStatus(
            analysis,
            "ready" if analysis else ("feature_disabled" if not enabled else "volatility_not_configured"),
            "Volatility 3 is ready." if analysis else "unsafe internal analysis detail /secret/vol3",
            "2.28.0" if analysis else None,
        ),
        isf_build=ToolStatus(
            isf_build,
            "ready" if isf_build else ("feature_disabled" if not enabled else "dwarf2json_not_configured"),
            "dwarf2json is ready." if isf_build else "unsafe internal ISF detail /secret/dwarf2json",
        ),
    )


def _button_attrs_after(html: str, marker: str) -> str:
    match = re.search(re.escape(marker) + r".*?<button\b(?P<attrs>[^>]*)>", html, re.DOTALL)
    assert match is not None, f"button after {marker!r} not found"
    return match.group("attrs")


@pytest.fixture(autouse=True)
def _configured_volatility_tool(monkeypatch):
    from astinuslab.forensics.vol3_tools import ToolStatus

    monkeypatch.setattr(
        forensic_services,
        "check_volatility_tool",
        lambda _settings: ToolStatus(
            True,
            "ready",
            "Volatility 3 is ready.",
            "2.28.0",
            resolved_path=Path("/test/vol3"),
        ),
    )


def _workbench_js() -> str:
    """Concatenate the workbench ES modules so source-content assertions
    survive the split of volatility-workbench.js into src/astinuslab/web/static/volatility/."""
    repo_root = Path(__file__).resolve().parents[3]
    vol_dir = repo_root / "src" / "astinuslab" / "web" / "static" / "volatility"
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(vol_dir.glob("*.js")))


def _completed_dump_job(
    scope,
    *,
    distro: str = "ubuntu",
    version: str = "22.04",
    kernel: str = "6.8.0-31-generic",
    with_isf: bool = False,
    dump: bool = True,
    dump_path: Path | None = None,
) -> str:
    with scope() as session:
        repo = JobRepository(session)
        job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        repo.force_transition(
            job.id,
            "completed",
            detected_distro=distro,
            detected_version=version,
            detected_kernel_release=kernel,
            detected_architecture="x86_64",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        if dump:
            artifact_path = dump_path or Path(f"/tmp/dumps/{job.id}/memory.lime")
            if dump_path is not None:
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_bytes(b"memdump")
            ArtifactRepository(session).create_artifact(
                kind="memory",
                producer="job_capture",
                path=str(artifact_path),
                filename=artifact_path.name,
                size_bytes=4 * 1024**3,
                job_id=job.id,
            )
        if with_isf:
            repo.create_isf_artifact(
                job.id,
                kernel,
                f"/tmp/isf/{kernel}.json",
                "cache",
                distro=distro,
                version=version,
                validation_status="valid",
            )
        return job.id


def test_list_vol3_dumps_returns_completed_dumps(in_memory_session_scope, tmp_path):
    job_id = _completed_dump_job(in_memory_session_scope, with_isf=True, dump_path=tmp_path / "memory.lime")
    with in_memory_session_scope() as session:
        dumps = forensic_services.list_vol3_dumps_service(session)
    assert len(dumps) == 1
    dump = dumps[0]
    assert dump["job_id"] == job_id
    assert dump["file"] == "memory.lime"
    assert dump["os_family"] == "linux"
    assert dump["os_label"] == "Ubuntu 22.04"
    assert dump["kernel"] == "6.8.0-31-generic"
    assert dump["source"] == "ubuntu-22-04-default"
    assert dump["size_bytes"] == 4 * 1024**3
    assert dump["symbols"] == "ready"


def test_list_vol3_dumps_ready_when_isf_built_under_other_job(in_memory_session_scope, tmp_path):
    # Dump captured by one job; its ISF built separately under a different job.
    dump_job = _completed_dump_job(in_memory_session_scope, with_isf=False, dump_path=tmp_path / "memory.lime")
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        isf_job = repo.create_job(
            JobCreate(
                target_id="ubuntu-22-04-default",
                scenario_id="immediate-capture",
                execution_mode="immediate",
            )
        )
        repo.create_isf_artifact(
            isf_job.id,
            "6.8.0-31-generic",
            "/tmp/isf/6.8.0-31-generic.json",
            "cache",
            distro="ubuntu",
            validation_status="valid",
            architecture="x86_64",
        )
    with in_memory_session_scope() as session:
        dumps = forensic_services.list_vol3_dumps_service(session)
    match = next(d for d in dumps if d["job_id"] == dump_job)
    assert match["symbols"] == "ready"


def test_list_vol3_dumps_marks_missing_symbols_without_isf(in_memory_session_scope, tmp_path):
    _completed_dump_job(in_memory_session_scope, with_isf=False, dump_path=tmp_path / "memory.lime")
    with in_memory_session_scope() as session:
        dumps = forensic_services.list_vol3_dumps_service(session)
    assert dumps[0]["symbols"] == "missing"


def test_list_vol3_dumps_windows_symbols_ready_without_isf(in_memory_session_scope, tmp_path):
    _completed_dump_job(
        in_memory_session_scope,
        distro="windows",
        version="11",
        kernel="10.0.26100",
        with_isf=False,
        dump_path=tmp_path / "memory.lime",
    )
    with in_memory_session_scope() as session:
        dumps = forensic_services.list_vol3_dumps_service(session)
    assert dumps[0]["os_family"] == "windows"
    assert dumps[0]["symbols"] == "ready"


def test_list_vol3_dumps_excludes_jobs_without_dump(in_memory_session_scope):
    _completed_dump_job(in_memory_session_scope, dump=False)
    with in_memory_session_scope() as session:
        dumps = forensic_services.list_vol3_dumps_service(session)
    assert dumps == []


def test_list_vol3_dumps_excludes_missing_dump_file(in_memory_session_scope, tmp_path):
    dump_path = tmp_path / "memory.lime"
    job_id = _completed_dump_job(in_memory_session_scope, with_isf=True, dump_path=dump_path)
    dump_path.unlink()

    with in_memory_session_scope() as session:
        dumps = forensic_services.list_vol3_dumps_service(session)

    assert all(dump["job_id"] != job_id for dump in dumps)


# ── Read APIs ──────────────────────────────────────────────────────────────


def test_legacy_aliases_do_not_mutate_volatility_api_globals(client):
    import astinuslab.forensics.api as volatility_api

    settings_before = volatility_api.settings
    scope_before = volatility_api.session_scope

    resp = client.get("/volatility/api/catalog?os=linux")
    assert resp.status_code == 200

    assert volatility_api.settings is settings_before
    assert volatility_api.session_scope is scope_before


def test_api_dumps_endpoint(client, in_memory_session_scope, tmp_path):
    job_id = _completed_dump_job(in_memory_session_scope, with_isf=True, dump_path=tmp_path / "memory.lime")
    resp = client.get("/api/v1/volatility/dumps")
    assert resp.status_code == 200
    dumps = resp.json()["dumps"]
    match = next(d for d in dumps if d["job_id"] == job_id)
    assert match["size"] == "4.0 GB"
    assert match["symbols"] == "ready"


def test_api_catalog_endpoint(client):
    resp = client.get("/api/v1/volatility/catalog?os=linux")
    assert resp.status_code == 200
    names = {p["name"] for c in resp.json()["categories"] for p in c["plugins"]}
    assert "linux.pslist.PsList" in names


def test_api_queue_endpoint_empty(client):
    resp = client.get("/api/v1/volatility/queue")
    assert resp.status_code == 200
    assert isinstance(resp.json()["runs"], list)


def test_legacy_volatility_api_aliases_are_deprecated(client, in_memory_session_scope, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import astinuslab.forensics.api as volatility_api
    import astinuslab.ui.volatility_routes as vr

    job_id = _completed_dump_job(in_memory_session_scope, with_isf=True, dump_path=tmp_path / "memory.lime")
    runs_dir = tmp_path / "dumps" / job_id / "vol3" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "rid7.json").write_text(
        '{"run_id": "rid7", "job_id": "' + job_id + '", "status": "completed", "columns": [], "rows": []}',
        encoding="utf-8",
    )
    fake_settings = SimpleNamespace(storage_dir=tmp_path)
    monkeypatch.setattr(vr, "settings", fake_settings)
    monkeypatch.setattr(volatility_api, "settings", fake_settings)

    aliases = [
        ("get", "/volatility/api/dumps", None),
        ("get", "/volatility/api/catalog?os=linux", None),
        ("get", "/volatility/api/queue", None),
        ("post", "/volatility/api/runs", {"job_id": job_id, "plugin": "linux.pslist.PsList"}),
        ("get", f"/volatility/api/runs/{job_id}/rid7", None),
        ("get", f"/volatility/api/runs/latest?job_id={job_id}&plugin=linux.pslist.PsList", None),
    ]
    for method, path, json_body in aliases:
        resp = getattr(client, method)(path, json=json_body) if json_body is not None else getattr(client, method)(path)
        assert resp.status_code == 200
        assert resp.headers["Deprecation"] == "true"


# ── Workbench page ─────────────────────────────────────────────────────────


def test_api_run_enqueues_and_polls(client, in_memory_session_scope):
    job_id = _completed_dump_job(in_memory_session_scope, with_isf=True)
    resp = client.post("/api/v1/volatility/runs", json={"job_id": job_id, "plugin": "linux.pslist.PsList"})
    assert resp.status_code == 200
    run = resp.json()
    assert run["status"] == "queued"
    run_id = run["run_id"]

    poll = client.get(f"/api/v1/volatility/runs/{job_id}/{run_id}")
    assert poll.status_code == 200
    assert poll.json()["status"] in ("queued", "running", "completed", "failed")


def test_api_run_rejects_unknown_plugin(client, in_memory_session_scope):
    job_id = _completed_dump_job(in_memory_session_scope, with_isf=True)
    resp = client.post("/api/v1/volatility/runs", json={"job_id": job_id, "plugin": "linux.bogus.Nope"})
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "plugin",
    [
        "a" * 200 + ".Oversized",  # bounded length
        "--config-file=/etc/shadow",  # argv flag smuggling
        "linux.pslist.PsList; rm -rf /",  # shell metacharacters
        "../../etc/passwd",  # traversal shape
        "",  # empty
    ],
    ids=("oversized", "flag", "metacharacters", "traversal", "empty"),
)
def test_api_run_rejects_malformed_plugin_at_the_schema_boundary(client, in_memory_session_scope, plugin):
    """Pydantic rejects the shape before any handler reflects it back to the caller.

    `plugin` reaches the 400 `detail` and `context["plugins"]` of the unknown-plugin
    error, so it must be bounded and shaped at the boundary, not only inside the
    service.  A well-formed-but-unknown name still gets the service's 400 (see
    test_api_run_rejects_unknown_plugin); these never get that far.
    """
    job_id = _completed_dump_job(in_memory_session_scope, with_isf=True)

    resp = client.post("/api/v1/volatility/runs", json={"job_id": job_id, "plugin": plugin})

    assert resp.status_code == 422
    with in_memory_session_scope() as session:
        assert [a for a in JobRepository(session).list_actions(job_id) if a.action_type == "run_vol3_plugin"] == []


def test_api_run_rejects_yarascan_without_rules(client, in_memory_session_scope):
    job_id = _completed_dump_job(in_memory_session_scope, with_isf=True)
    resp = client.post("/api/v1/volatility/runs", json={"job_id": job_id, "plugin": "yarascan.YaraScan"})
    assert resp.status_code == 400
    assert "requires YARA rules" in resp.text


def test_api_run_accepts_yarascan_with_rules(client, in_memory_session_scope):
    job_id = _completed_dump_job(in_memory_session_scope, with_isf=True)
    rules = 'rule t { strings: $a = "root" condition: $a }'
    resp = client.post(
        "/api/v1/volatility/runs",
        json={"job_id": job_id, "plugin": "yarascan.YaraScan", "yara_rules": rules},
    )
    assert resp.status_code == 200

    with in_memory_session_scope() as session:
        action = next(a for a in JobRepository(session).list_actions(job_id) if a.action_type == "run_vol3_plugin")
        payload = action.payload
        assert payload is not None
        assert payload
        assert rules == json.loads(payload)["yara_rules"]


def test_api_run_rejects_yara_rules_for_non_yara_plugin(client, in_memory_session_scope):
    job_id = _completed_dump_job(in_memory_session_scope, with_isf=True)
    resp = client.post(
        "/api/v1/volatility/runs",
        json={"job_id": job_id, "plugin": "linux.pslist.PsList", "yara_rules": "rule t { condition: true }"},
    )
    assert resp.status_code == 400
    assert "does not accept YARA rules" in resp.text


def test_api_run_rejects_oversize_yara_rules(client, in_memory_session_scope):
    job_id = _completed_dump_job(in_memory_session_scope, with_isf=True)
    resp = client.post(
        "/api/v1/volatility/runs",
        json={"job_id": job_id, "plugin": "yarascan.YaraScan", "yara_rules": "a" * 65537},
    )
    assert resp.status_code == 400
    assert "too large" in resp.text


def test_api_run_rejects_duplicate_yarascan(client, in_memory_session_scope):
    job_id = _completed_dump_job(in_memory_session_scope, with_isf=True)
    body = {"job_id": job_id, "plugin": "yarascan.YaraScan", "yara_rules": "rule t { condition: true }"}
    first = client.post("/api/v1/volatility/runs", json=body)
    assert first.status_code == 200
    with in_memory_session_scope() as session:
        repo = JobRepository(session)
        action = next(a for a in repo.list_actions(job_id) if a.action_type == "run_vol3_plugin")
        repo.update_action(action.id, status="running")

    second = client.post("/api/v1/volatility/runs", json=body)

    assert second.status_code == 409


def test_api_run_result_404_for_unknown(client):
    resp = client.get("/api/v1/volatility/runs/job-missing/deadbeef")
    assert resp.status_code == 404


def test_workbench_page_renders(client):
    resp = client.get("/volatility")
    assert resp.status_code == 200
    html = resp.text
    for needle in (
        'id="dump-select"',
        'id="catalog"',
        'id="results-panel"',
        'id="queue-body"',
        'id="isf-list"',
        'id="run-btn"',
        'id="rerun-cmd"',
        'id="results-notrun"',
        'action="/volatility/build"',
        "data-target-id=",
        "data-upload-isf-target",
        "Upload ISF",
        'class="wiz-btn wiz-btn--secondary wiz-btn--xs"',
        "volatility/index.js",
    ):
        assert needle in html, f"missing: {needle}"


def test_workbench_disabled_feature_shows_safe_plain_text_banner_and_native_gates(client, monkeypatch):
    import astinuslab.ui.volatility_routes as routes

    calls = 0

    def probe_once(*_args):
        nonlocal calls
        calls += 1
        return _readiness(enabled=False, analysis=False, isf_build=False)

    monkeypatch.setattr(routes, "get_vol3_readiness", probe_once, raising=False)

    response = client.get("/volatility")

    assert response.status_code == 200
    assert calls == 1
    html = response.text
    assert 'id="vol-readiness-banner"' in html
    assert "docs/user/VOLATILITY3.md" in html
    assert "/secret/" not in html
    banner = re.search(r'<div[^>]+id="vol-readiness-banner".*?</div>', html, re.DOTALL)
    assert banner is not None
    # The guide opens the installed HTML help surface, but it
    # must be the ONLY markup the banner contains: every other character in there
    # is translator- or tool-supplied text and has to stay inert.
    assert re.findall(r"<a\b[^>]*>", banner.group(0)) == ['<a href="/help/user/VOLATILITY3.md">']
    assert "install" not in banner.group(0).lower()
    assert 'data-analysis-ready="0"' in html
    assert 'data-isf-build-ready="0"' in html
    run_attrs = re.search(r'<button\b(?P<attrs>[^>]*id="run-btn"[^>]*)>', html, re.DOTALL)
    assert run_attrs is not None
    assert "disabled" in run_attrs.group("attrs")
    assert 'aria-describedby="vol-readiness-banner"' in run_attrs.group("attrs")
    build_attrs = _button_attrs_after(html, 'action="/volatility/build"')
    assert "disabled" in build_attrs
    assert 'aria-describedby="vol-readiness-banner"' in build_attrs
    upload_attrs = re.search(r"<button\b(?P<attrs>[^>]*data-upload-isf-target[^>]*)>", html, re.DOTALL)
    assert upload_attrs is not None
    assert "disabled" not in upload_attrs.group("attrs")


def test_workbench_enabled_tools_absent_shows_configuration_banner(client, monkeypatch):
    import astinuslab.ui.volatility_routes as routes

    monkeypatch.setattr(
        routes,
        "get_vol3_readiness",
        lambda *_args: _readiness(enabled=True, analysis=False, isf_build=False),
    )

    html = client.get("/volatility").text

    banner = re.search(r'<div[^>]+id="vol-readiness-banner".*?</div>', html, re.DOTALL)
    assert banner is not None
    banner_html = banner.group(0)
    assert "operator-supplied Volatility tool" in banner_html
    assert "operator-supplied dwarf2json tool" in banner_html
    assert "docs/user/VOLATILITY3.md" in banner_html
    # One template-authored guide link per message, and nothing else. Two messages
    # render here (analysis and ISF), so both must carry exactly that same anchor.
    assert re.findall(r"<a\b[^>]*>", banner_html) == ['<a href="/help/user/VOLATILITY3.md">'] * 2
    assert "/secret/" not in html


@pytest.mark.parametrize(
    ("analysis", "isf_build", "expected_analysis", "expected_isf"),
    [(False, True, "0", "1"), (True, False, "1", "0"), (True, True, "1", "1")],
    ids=("dwarf2json-only", "volatility-only", "fully-ready"),
)
def test_workbench_capability_state_is_split(client, monkeypatch, analysis, isf_build, expected_analysis, expected_isf):
    import astinuslab.ui.volatility_routes as routes

    monkeypatch.setattr(
        routes,
        "get_vol3_readiness",
        lambda *_args: _readiness(analysis=analysis, isf_build=isf_build),
        raising=False,
    )

    html = client.get("/volatility").text

    assert f'data-analysis-ready="{expected_analysis}"' in html
    assert f'data-isf-build-ready="{expected_isf}"' in html
    build_attrs = _button_attrs_after(html, 'action="/volatility/build"')
    # The build button stays usable when only dwarf2json is unready: the local-cache
    # and prebuilt providers can still deliver a ready ISF.  The capability state is
    # surfaced as an explanation, not as a disabled control.
    assert "disabled" not in build_attrs
    # An enabled, working control must never be described by the "unavailable" banner:
    # a screen-reader user focusing a button that succeeds would hear it announced as
    # unavailable.  aria-describedby tracks `disabled`, which is off for every case here.
    assert 'aria-describedby="vol-readiness-banner"' not in build_attrs
    upload_attrs = re.search(r"<button\b(?P<attrs>[^>]*data-upload-isf-target[^>]*)>", html, re.DOTALL)
    assert upload_attrs is not None
    assert "disabled" not in upload_attrs.group("attrs")


@pytest.mark.parametrize(
    ("enabled", "expected_class", "forbidden_class"),
    [(False, "banner-info", "banner-error"), (True, "banner-error", "banner-info")],
    ids=("disabled-is-neutral", "unready-tool-is-an-error"),
)
def test_job_detail_readiness_banner_style_matches_the_cause(
    client,
    in_memory_session_scope,
    monkeypatch,
    enabled,
    expected_class,
    forbidden_class,
):
    """A deliberately disabled integration is not an error condition.

    `vol3_enabled = false` is a supported configuration, so an operator running
    without Volatility must not see a red banner on every completed job forever.
    An unready tool while the feature is on is a genuine fault and stays red.
    """
    import astinuslab.ui.job_context as job_context

    job_id = _completed_dump_job(in_memory_session_scope)
    monkeypatch.setattr(
        job_context,
        "get_vol3_readiness",
        lambda *_args: _readiness(enabled=enabled, analysis=False, isf_build=False),
        raising=False,
    )

    html = client.get(f"/jobs/{job_id}").text

    banner = re.search(r'<div class="(?P<cls>[^"]*)" id="vol-readiness-banner-[^"]+"', html)
    assert banner is not None, "readiness banner missing from job detail"
    assert expected_class in banner.group("cls")
    assert forbidden_class not in banner.group("cls")


def test_job_detail_renders_redirect_message(client, in_memory_session_scope):
    """A failed analysis request redirects here with ?message=...; it must be visible.

    Readiness is evaluated when the page renders but re-verified at execution, so a
    tool removed or a hash changed in between lands on exactly this path.  Without a
    rendered banner the operator sees a silent page reload.
    """
    job_id = _completed_dump_job(in_memory_session_scope)

    html = client.get(f"/jobs/{job_id}?message=Analysis+request+failed&message_type=error").text

    assert 'class="banner-error vol-banner" role="alert"' in html
    assert "Analysis request failed" in html

    clean = client.get(f"/jobs/{job_id}").text
    assert "vol-banner" not in clean or "banner-error" not in clean


@pytest.mark.parametrize("analysis_ready", [False, True], ids=("unavailable", "ready"))
def test_job_detail_analysis_button_uses_readiness(client, in_memory_session_scope, monkeypatch, analysis_ready):
    import astinuslab.ui.job_context as job_context

    job_id = _completed_dump_job(in_memory_session_scope)
    monkeypatch.setattr(
        job_context,
        "get_vol3_readiness",
        lambda *_args: _readiness(analysis=analysis_ready),
        raising=False,
    )

    html = client.get(f"/jobs/{job_id}").text

    attrs = _button_attrs_after(html, f'action="/jobs/{job_id}/vol3-analysis"')
    assert ("disabled" in attrs) is (not analysis_ready)
    assert (f'aria-describedby="vol-readiness-banner-{job_id}"' in attrs) is (not analysis_ready)
    if not analysis_ready:
        assert f'id="vol-readiness-banner-{job_id}"' in html
        assert "docs/user/VOLATILITY3.md" in html


def test_live_status_fragment_does_not_probe_volatility_readiness(client, in_memory_session_scope, monkeypatch):
    import astinuslab.ui.job_context as job_context

    job_id = _completed_dump_job(in_memory_session_scope)
    calls = []

    def unexpected_probe():
        calls.append(True)
        return _readiness()

    monkeypatch.setattr(job_context, "get_vol3_readiness", unexpected_probe)

    response = client.get(f"/jobs/{job_id}/live-status-fragment")

    assert response.status_code == 200
    assert calls == []


def test_job_detail_probes_volatility_readiness_once(client, in_memory_session_scope, monkeypatch):
    import astinuslab.ui.job_context as job_context

    job_id = _completed_dump_job(in_memory_session_scope)
    calls = []

    def readiness_probe():
        calls.append(True)
        return _readiness()

    monkeypatch.setattr(job_context, "get_vol3_readiness", readiness_probe)

    response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert calls == [True]


def test_plugin_post_dependency_gate_survives_client_side_disabled_removal(
    client, in_memory_session_scope, monkeypatch
):
    from astinuslab.forensics.vol3_tools import ToolStatus

    job_id = _completed_dump_job(in_memory_session_scope)
    monkeypatch.setattr(
        forensic_services,
        "check_volatility_tool",
        lambda _settings: ToolStatus(False, "volatility_not_configured", "Volatility is unavailable."),
    )

    response = client.post(
        "/api/v1/volatility/runs",
        json={"job_id": job_id, "plugin": "linux.pslist.PsList"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Volatility is unavailable.",
        "code": "dependency_unavailable",
        "context": {"dependency_code": "volatility_not_configured"},
    }
    with in_memory_session_scope() as session:
        assert JobRepository(session).list_actions(job_id) == []


def test_workbench_js_caps_poll_failures_and_wires_isf_builds():
    js = _workbench_js()
    assert "MAX_POLL_FAILURES" in js
    assert "state.pollFailures += 1" in js
    assert "statusUnavailable(context, error)" in js
    assert 'failUI(t("vol_poll_error"))' not in js
    assert "catch(function () { pollTimer = setTimeout" not in js
    assert "function wireIsfBuildForms" in js
    assert "function wireIsfUploadButtons" in js
    assert '"/api/v1/volatility/targets/"' in js
    assert "new FormData(form)" in js
    assert '"X-Requested-With": "fetch"' in js
    assert "refreshQueue();" in js


def test_build_volatility_symbols_accepts_json(client, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    import astinuslab.ui.volatility_routes as vr

    @contextmanager
    def fake_scope():
        yield object()

    def fake_create_job_service(_session, payload):
        assert payload.target_id == "ubuntu-22-04-default"
        assert payload.build_vol3_isf is True
        # ISF-only job: never a memory capture, and the scenario supplies the mode.
        assert payload.scenario_id == "immediate-capture"
        assert payload.capture_type == "none"
        assert payload.execution_mode == "immediate"
        return SimpleNamespace(id="job-isf-1")

    monkeypatch.setattr(vr, "session_scope", fake_scope)
    monkeypatch.setattr(vr, "create_job_service", fake_create_job_service)
    monkeypatch.setattr(vr, "get_vol3_readiness", lambda: _readiness())
    monkeypatch.setattr(
        vr,
        "_linux_symbol_targets",
        lambda: [{"id": "ubuntu-22-04-default", "enabled": True, "distro": "ubuntu", "role": "target"}],
    )
    monkeypatch.setattr(
        vr, "_isf_scenarios", lambda: [{"id": "immediate-capture", "mode": "immediate", "enabled": True}]
    )

    resp = client.post(
        "/volatility/build",
        data={"target_id": "ubuntu-22-04-default", "scenario_id": "immediate-capture"},
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"job_id": "job-isf-1", "status": "queued", "url": "/jobs/job-isf-1"}


def test_build_volatility_symbols_queues_despite_unready_dwarf2json(client, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    import astinuslab.ui.volatility_routes as routes

    create_calls = []

    @contextmanager
    def fake_scope():
        yield object()

    def fake_create_job_service(_session, _payload):
        create_calls.append(True)
        return SimpleNamespace(id="job-must-not-exist")

    monkeypatch.setattr(routes, "session_scope", fake_scope)
    monkeypatch.setattr(routes, "create_job_service", fake_create_job_service)
    monkeypatch.setattr(
        routes,
        "get_vol3_readiness",
        lambda: _readiness(enabled=True, analysis=True, isf_build=False),
    )
    monkeypatch.setattr(
        routes,
        "_linux_symbol_targets",
        lambda: [{"id": "ubuntu-22-04-default", "enabled": True, "distro": "ubuntu", "role": "target"}],
    )
    monkeypatch.setattr(
        routes,
        "_isf_scenarios",
        lambda: [{"id": "immediate-capture", "mode": "immediate", "enabled": True}],
    )

    response = client.post(
        "/volatility/build",
        data={"target_id": "ubuntu-22-04-default", "scenario_id": "immediate-capture"},
        headers={"Accept": "application/json", "X-Requested-With": "fetch"},
    )

    # An unready dwarf2json no longer blocks the queue: the provider chain runs first
    # and prebuilt/debuginfod can deliver a ready ISF without the tool.  Enforcement
    # lives on the paths that actually execute dwarf2json.
    assert response.status_code == 200, response.text
    assert create_calls == [True]


def test_build_volatility_symbols_returns_documented_503_envelope_when_feature_disabled(client, monkeypatch):
    """The JSON caller contract for a disabled integration is a 503 dependency envelope.

    docs/api/errors.md and CHANGELOG.md publish this exact body, and no job may be
    created.  Without this test the `wants_json` branch in volatility_routes could be
    deleted with every other test still green.
    """
    from contextlib import contextmanager
    from types import SimpleNamespace

    import astinuslab.ui.volatility_routes as routes

    create_calls = []

    @contextmanager
    def fake_scope():
        yield object()

    def fake_create_job_service(_session, _payload):
        create_calls.append(True)
        return SimpleNamespace(id="job-must-not-exist")

    monkeypatch.setattr(routes, "session_scope", fake_scope)
    monkeypatch.setattr(routes, "create_job_service", fake_create_job_service)
    monkeypatch.setattr(
        routes,
        "get_vol3_readiness",
        lambda: _readiness(enabled=False, analysis=False, isf_build=False),
    )
    monkeypatch.setattr(
        routes,
        "_linux_symbol_targets",
        lambda: [{"id": "ubuntu-22-04-default", "enabled": True, "distro": "ubuntu", "role": "target"}],
    )
    monkeypatch.setattr(
        routes,
        "_isf_scenarios",
        lambda: [{"id": "immediate-capture", "mode": "immediate", "enabled": True}],
    )

    response = client.post(
        "/volatility/build",
        data={"target_id": "ubuntu-22-04-default", "scenario_id": "immediate-capture"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body == {
        "detail": (
            "Volatility integration is disabled. Enable it in configuration and review docs/user/VOLATILITY3.md."
        ),
        "code": "dependency_unavailable",
        "context": {"dependency_code": "feature_disabled"},
    }
    assert body["context"]["dependency_code"] == "feature_disabled"
    assert create_calls == []
    # The unsafe internal status detail must never reach an API consumer.
    assert "/secret/" not in response.text


def test_browser_disabled_feature_redirects_to_error_banner(client, monkeypatch):
    """Only a disabled integration blocks the browser build form.

    `vol3_enabled = false` is an operator decision, so the queue refuses it outright.
    An unready dwarf2json is not the same thing: it leaves the local-cache and prebuilt
    providers usable, so it must not block submission.
    """
    import astinuslab.ui.volatility_routes as routes

    monkeypatch.setattr(
        routes,
        "get_vol3_readiness",
        lambda: _readiness(enabled=False, analysis=False, isf_build=False),
    )

    blocked = client.post(
        "/volatility/build",
        data={"target_id": "ubuntu-22-04-default", "scenario_id": "immediate-capture"},
        follow_redirects=False,
    )

    assert blocked.status_code == 303
    assert "message_type=error" in blocked.headers["location"]
    rendered = client.get(blocked.headers["location"])
    assert rendered.status_code == 200
    error_banner = re.search(
        r'<div class="banner-error vol-banner" role="alert">(?P<text>.*?)</div>',
        rendered.text,
        re.DOTALL,
    )
    assert error_banner is not None
    assert "Volatility integration is disabled" in error_banner.group("text")
    assert "docs/user/VOLATILITY3.md" in error_banner.group("text")

    monkeypatch.setattr(routes, "get_vol3_readiness", lambda: _readiness())
    success = client.get("/volatility?message=Symbols%20ready&message_type=success")
    assert '<div class="banner-success vol-banner" role="status">Symbols ready</div>' in success.text

    # An unlabelled ?message= is not this app's own success redirect, so it renders red.
    unlabelled = client.get("/volatility?message=Symbols%20ready")
    assert '<div class="banner-error vol-banner" role="alert">Symbols ready</div>' in unlabelled.text
    assert "banner-success" not in unlabelled.text


def test_workbench_end_to_end_smoke(client, in_memory_session_scope, tmp_path):
    """Page + all read APIs + enqueue/poll wired together against a real dump."""
    job_id = _completed_dump_job(in_memory_session_scope, with_isf=True, dump_path=tmp_path / "memory.lime")

    assert client.get("/volatility").status_code == 200

    dumps = client.get("/api/v1/volatility/dumps").json()["dumps"]
    assert any(d["job_id"] == job_id for d in dumps)

    catalog = client.get("/api/v1/volatility/catalog?os=linux").json()
    names = {p["name"] for c in catalog["categories"] for p in c["plugins"]}
    assert "linux.pslist.PsList" in names

    run = client.post("/api/v1/volatility/runs", json={"job_id": job_id, "plugin": "linux.pslist.PsList"}).json()
    assert run["status"] == "queued"

    poll = client.get(f"/api/v1/volatility/runs/{job_id}/{run['run_id']}")
    assert poll.status_code == 200
    assert poll.json()["status"] in ("queued", "running", "completed", "failed")

    assert "runs" in client.get("/api/v1/volatility/queue").json()


def test_queue_excludes_manifest_sidecars(client, tmp_path, monkeypatch):
    import astinuslab.forensics.api as volatility_api
    import astinuslab.ui.volatility_routes as vr

    runs_dir = tmp_path / "dumps" / "job-xyz" / "vol3" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "run1.json").write_text(
        '{"run_id": "run1", "job_id": "job-xyz", "plugin": "linux.pslist.PsList", "status": "completed"}',
        encoding="utf-8",
    )
    (runs_dir / "run1.json.manifest.json").write_text('{"schema_version": "artifact-manifest/v1"}', encoding="utf-8")

    from types import SimpleNamespace

    monkeypatch.setattr(volatility_api, "settings", SimpleNamespace(storage_dir=tmp_path))

    runs = vr._recent_runs()
    assert [r["run_id"] for r in runs] == ["run1"]


def test_queue_prefers_meta_sidecar_and_skips_config(client, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import astinuslab.forensics.api as volatility_api
    import astinuslab.ui.volatility_routes as vr

    runs_dir = tmp_path / "dumps" / "job-meta" / "vol3" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "r1.json").write_text(
        json.dumps(
            {
                "run_id": "r1",
                "job_id": "job-meta",
                "plugin": "from-full",
                "dump_file": "mem.lime",
                "status": "completed",
                "elapsed": "9.0s",
                "finished_at": "2026-06-12T10:00:00",
                "rows": [[1]] * 1000,
            }
        ),
        encoding="utf-8",
    )
    (runs_dir / "r1.meta.json").write_text(
        json.dumps(
            {
                "run_id": "r1",
                "job_id": "job-meta",
                "plugin": "from-meta",
                "dump_file": "mem.lime",
                "status": "completed",
                "elapsed": "1.0s",
                "finished_at": "2026-06-12T10:00:01",
            }
        ),
        encoding="utf-8",
    )
    (runs_dir / "r2.config.json").write_text('{"plugin": "config"}', encoding="utf-8")
    monkeypatch.setattr(volatility_api, "settings", SimpleNamespace(storage_dir=tmp_path))

    runs = vr._recent_runs()

    assert len(runs) == 1
    assert runs[0]["plugin"] == "from-meta"


def test_queue_falls_back_to_full_result_without_meta(client, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import astinuslab.forensics.api as volatility_api
    import astinuslab.ui.volatility_routes as vr

    runs_dir = tmp_path / "dumps" / "job-old" / "vol3" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "r3.json").write_text(
        json.dumps(
            {
                "run_id": "r3",
                "job_id": "job-old",
                "plugin": "legacy-full",
                "dump_file": "mem.lime",
                "status": "completed",
                "elapsed": "2.0s",
                "finished_at": "2026-06-12T10:00:00",
                "rows": [[1]],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(volatility_api, "settings", SimpleNamespace(storage_dir=tmp_path))

    runs = vr._recent_runs()

    assert len(runs) == 1
    assert runs[0]["run_id"] == "r3"
    assert runs[0]["plugin"] == "legacy-full"


def test_run_result_includes_artifact_id(client, in_memory_session_scope, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import astinuslab.forensics.api as volatility_api
    import astinuslab.ui.volatility_routes as vr
    from astinuslab.db.repositories import ArtifactRepository

    job_id = _completed_dump_job(in_memory_session_scope, with_isf=True)
    runs_dir = tmp_path / "dumps" / job_id / "vol3" / "runs"
    runs_dir.mkdir(parents=True)
    run_file = runs_dir / "rid7.json"
    run_file.write_text(
        '{"run_id": "rid7", "job_id": "' + job_id + '", "status": "completed", "columns": [], "rows": []}',
        encoding="utf-8",
    )
    fake_settings = SimpleNamespace(storage_dir=tmp_path)
    monkeypatch.setattr(vr, "settings", fake_settings)
    monkeypatch.setattr(volatility_api, "settings", fake_settings)

    with in_memory_session_scope() as session:
        art = ArtifactRepository(session).create_artifact(
            kind="report",
            producer="vol3_plugin_run",
            path=str(run_file),
            filename="rid7.json",
            job_id=job_id,
        )
        art_id = art.id

    resp = client.get(f"/api/v1/volatility/runs/{job_id}/rid7")
    assert resp.status_code == 200
    assert resp.json()["artifact_id"] == art_id


def test_workbench_js_renders_low_trust_isf_warning():
    js = _workbench_js()
    assert "isf_low_trust" in js
    assert "vol_low_trust" in js


# ── /runs/latest cache lookup ───────────────────────────────────────────────


def _write_run(runs_dir, run_id, *, job_id, plugin, status="completed", finished_at, columns=None, rows=None):
    runs_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "run_id": run_id,
        "job_id": job_id,
        "plugin": plugin,
        "status": status,
        "finished_at": finished_at,
        "elapsed": "1.0s",
        "dump_file": "memory.lime",
        "columns": columns or [],
        "rows": rows or [],
    }
    (runs_dir / f"{run_id}.json").write_text(json.dumps(result), encoding="utf-8")
    meta = {k: result[k] for k in ("run_id", "job_id", "plugin", "dump_file", "status", "elapsed", "finished_at")}
    (runs_dir / f"{run_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")


def test_api_run_latest_returns_newest_completed(client, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import astinuslab.forensics.api as volatility_api

    monkeypatch.setattr(volatility_api, "settings", SimpleNamespace(storage_dir=tmp_path))
    job_id = "job-latest1"
    runs_dir = tmp_path / "dumps" / job_id / "vol3" / "runs"
    _write_run(
        runs_dir,
        "old1",
        job_id=job_id,
        plugin="linux.pslist.PsList",
        finished_at="2026-06-01T10:00:00+00:00",
        columns=["PID"],
        rows=[[1]],
    )
    _write_run(
        runs_dir,
        "new1",
        job_id=job_id,
        plugin="linux.pslist.PsList",
        finished_at="2026-06-02T10:00:00+00:00",
        columns=["PID"],
        rows=[[1], [2]],
    )

    resp = client.get(f"/api/v1/volatility/runs/latest?job_id={job_id}&plugin=linux.pslist.PsList")
    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == "new1"
    assert data["cached"] is True
    assert data["rows"] == [[1], [2]]


def test_api_run_latest_none_when_never_run(client, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import astinuslab.forensics.api as volatility_api

    monkeypatch.setattr(volatility_api, "settings", SimpleNamespace(storage_dir=tmp_path))
    resp = client.get("/api/v1/volatility/runs/latest?job_id=job-none&plugin=linux.pslist.PsList")
    assert resp.status_code == 200
    assert resp.json() == {"status": "none"}


def test_api_run_latest_ignores_other_plugin_and_dump(client, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import astinuslab.forensics.api as volatility_api

    monkeypatch.setattr(volatility_api, "settings", SimpleNamespace(storage_dir=tmp_path))
    job_id = "job-mix1"
    runs_dir = tmp_path / "dumps" / job_id / "vol3" / "runs"
    _write_run(
        runs_dir, "otherplugin", job_id=job_id, plugin="linux.bash.Bash", finished_at="2026-06-02T10:00:00+00:00"
    )
    _write_run(
        runs_dir,
        "failed1",
        job_id=job_id,
        plugin="linux.pslist.PsList",
        status="failed",
        finished_at="2026-06-03T10:00:00+00:00",
    )
    other_runs = tmp_path / "dumps" / "job-other" / "vol3" / "runs"
    _write_run(
        other_runs,
        "elsewhere",
        job_id="job-other",
        plugin="linux.pslist.PsList",
        finished_at="2026-06-04T10:00:00+00:00",
    )

    resp = client.get(f"/api/v1/volatility/runs/latest?job_id={job_id}&plugin=linux.pslist.PsList")
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"
    assert resp.json()["run_id"] == "failed1"


def test_api_run_latest_rejects_unsafe_job_id(client, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import astinuslab.forensics.api as volatility_api

    monkeypatch.setattr(volatility_api, "settings", SimpleNamespace(storage_dir=tmp_path))
    resp = client.get("/api/v1/volatility/runs/latest?job_id=../../etc&plugin=linux.pslist.PsList")
    assert resp.status_code == 404


def test_workbench_js_is_cache_first_with_manual_rerun():
    js = _workbench_js()
    # Selecting a plugin must NOT auto-run; it loads the cached result instead.
    assert "else if (!isYaraPlugin(state.plugin)) runPlugin();" not in js
    assert "function loadCachedResult" in js
    assert "/api/v1/volatility/runs/latest?job_id=" in js
    assert "function showRunPrompt" in js
    assert 'rerunBtn = $("rerun-cmd")' in js


# ── Delete run ─────────────────────────────────────────────────────────────


def test_delete_run_uses_custody_deletion_and_marks_report_deleted(
    client, in_memory_session_scope, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    import astinuslab.forensics.api as volatility_api
    from astinuslab.db.repositories import ArtifactRepository, ReportRepository

    job_id = _completed_dump_job(in_memory_session_scope, with_isf=True, dump_path=tmp_path / "memory.lime")
    runs_dir = tmp_path / "dumps" / job_id / "vol3" / "runs"
    runs_dir.mkdir(parents=True)
    for suffix in (".json", ".meta.json", ".stdout.json", ".stderr.log", ".config.json", ".manifest.json"):
        (runs_dir / f"rid7{suffix}").write_text("{}", encoding="utf-8")
    (runs_dir / "rid7.json").write_text(
        '{"run_id": "rid7", "job_id": "' + job_id + '", "status": "completed", "columns": [], "rows": []}',
        encoding="utf-8",
    )
    # The report survives as a disposed record; its output Artifact is tombstoned.
    with in_memory_session_scope() as session:
        artifact = ArtifactRepository(session).create_artifact(
            kind="report",
            producer="vol3_plugin_run",
            path=str(runs_dir / "rid7.json"),
            filename="rid7.json",
            job_id=job_id,
        )
        ReportRepository(session).create_report(
            report_type="volatility3_plugin",
            status="completed",
            path=str(runs_dir / "rid7.json"),
            title="t",
            job_id=job_id,
            output_artifact_id=artifact.id,
        )

    monkeypatch.setattr(volatility_api, "settings", SimpleNamespace(storage_dir=tmp_path))

    resp = client.delete(f"/api/v1/volatility/runs/{job_id}/rid7")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}
    assert not (runs_dir / "rid7.json").exists()
    assert (runs_dir / "rid7.stdout.json").exists()  # diagnostics aren't an untracked wildcard victim
    with in_memory_session_scope() as session:
        from astinuslab.db.models import EvidenceTombstone

        assert not [a for a in ArtifactRepository(session).list_artifacts(job_id=job_id) if a.kind == "report"]
        assert session.query(EvidenceTombstone).filter_by(job_id=job_id).count() == 1
        assert ReportRepository(session).list_reports(job_id=job_id)[0].status == "deleted"


def test_delete_run_missing_returns_deleted_false(client, in_memory_session_scope, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import astinuslab.forensics.api as volatility_api

    job_id = _completed_dump_job(in_memory_session_scope, dump_path=tmp_path / "memory.lime")
    monkeypatch.setattr(volatility_api, "settings", SimpleNamespace(storage_dir=tmp_path))
    resp = client.delete(f"/api/v1/volatility/runs/{job_id}/nope123")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": False}


def test_delete_run_rejects_bad_run_id(client, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import astinuslab.forensics.api as volatility_api

    monkeypatch.setattr(volatility_api, "settings", SimpleNamespace(storage_dir=tmp_path))
    resp = client.delete("/api/v1/volatility/runs/job1/bad.id")  # dot is illegal in run_id
    assert resp.status_code == 404


# ── Clear queue ────────────────────────────────────────────────────────────


def test_clear_queue_hides_runs_and_preserves_all_files(client, in_memory_session_scope, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import astinuslab.forensics.api as volatility_api

    job_a = _completed_dump_job(in_memory_session_scope, dump_path=tmp_path / "a.lime")
    job_b = _completed_dump_job(in_memory_session_scope, dump_path=tmp_path / "b.lime")
    for job_id, rid in ((job_a, "r1"), (job_a, "r2"), (job_b, "r3")):
        d = tmp_path / "dumps" / job_id / "vol3" / "runs"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{rid}.json").write_text(
            '{"run_id": "' + rid + '", "job_id": "' + job_id + '", "status": "completed"}', encoding="utf-8"
        )
        (d / f"{rid}.stderr.log").write_text("x", encoding="utf-8")

    monkeypatch.setattr(volatility_api, "settings", SimpleNamespace(storage_dir=tmp_path))

    resp = client.delete("/api/v1/volatility/queue")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 3}
    assert client.get("/api/v1/volatility/queue").json()["runs"] == []
    assert len(list((tmp_path / "dumps").glob("*/vol3/runs/*.json"))) == 3
    assert len(list((tmp_path / "dumps").glob("*/vol3/runs/*.stderr.log"))) == 3


def test_clear_queue_empty_is_zero(client, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import astinuslab.forensics.api as volatility_api

    monkeypatch.setattr(volatility_api, "settings", SimpleNamespace(storage_dir=tmp_path))
    resp = client.delete("/api/v1/volatility/queue")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 0}


def _untested_readiness(*, isf_build: bool = True, version: str = "2.29.0"):
    from astinuslab.forensics.vol3_tools import ToolStatus, Vol3Readiness

    return Vol3Readiness(
        enabled=True,
        analysis=ToolStatus(True, "ready", "Volatility 3 is ready.", version, untested=True),
        isf_build=ToolStatus(
            isf_build,
            "ready" if isf_build else "dwarf2json_not_configured",
            "dwarf2json is ready." if isf_build else "Configure the operator-supplied dwarf2json tool.",
        ),
    )


def _banner(html: str) -> str:
    match = re.search(r'<div[^>]+id="vol-readiness-banner".*?</div>', html, re.DOTALL)
    assert match is not None, "readiness banner did not render"
    return match.group(0)


def test_untested_volatility_version_shows_a_neutral_banner_naming_the_version(client, monkeypatch):
    """An untested version runs; the banner is a caveat on the results, not a refusal.

    This is the only readiness state that renders while every capability reports
    ready, so it must not borrow a "capabilities unavailable" title.
    """
    import astinuslab.ui.volatility_routes as routes

    monkeypatch.setattr(routes, "get_vol3_readiness", lambda *_a, **_k: _untested_readiness(), raising=False)

    html = client.get("/volatility").text
    banner = _banner(html)

    assert "banner-info" in banner and "banner-error" not in banner
    assert 'role="status"' in banner
    assert "Volatility version not validated" in banner
    assert "2.29.0" in banner, "the operator must see which version is untested"
    assert "unavailable" not in banner.lower(), "nothing is unavailable in this state"
    # The controls stay usable: untested is a caveat, never a block.
    run_attrs = re.search(r'<button\b(?P<attrs>[^>]*id="run-btn"[^>]*)>', html, re.DOTALL)
    assert run_attrs is not None
    assert "aria-describedby" not in _button_attrs_after(html, 'action="/volatility/build"')


def test_untested_version_alongside_an_unready_tool_keeps_the_degraded_title(client, monkeypatch):
    import astinuslab.ui.volatility_routes as routes

    monkeypatch.setattr(
        routes, "get_vol3_readiness", lambda *_a, **_k: _untested_readiness(isf_build=False), raising=False
    )

    banner = _banner(client.get("/volatility").text)

    assert "Some Volatility capabilities unavailable" in banner
    assert "Volatility version not validated" not in banner
    # Both messages render: the unready tool and the version caveat.
    assert "dwarf2json" in banner
    assert "2.29.0" in banner


def test_tested_volatility_version_renders_no_readiness_banner(client, monkeypatch):
    import astinuslab.ui.volatility_routes as routes

    monkeypatch.setattr(routes, "get_vol3_readiness", lambda *_a, **_k: _readiness(), raising=False)

    html = client.get("/volatility").text

    assert 'id="vol-readiness-banner"' not in html, "a fully ready, tested install shows no banner"
