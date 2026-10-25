from __future__ import annotations

# Uses the shared `client` fixture from tests/python/conftest.py rather than a
# module-scope TestClient. A bare `TestClient(app)` built at import time never
# enters the app's lifespan and reads the session-wide database instead of the
# per-test in-memory scope every other route test runs against, so it is the one
# client in the suite whose results depend on what ran before it.


def test_storage_page_renders_storage_workbench(client):
    response = client.get("/storage", follow_redirects=False)
    assert response.status_code == 200
    assert "Storage Admin" in response.text
    assert "Inspect evidence capacity" in response.text
    assert "tab-strip" in response.text
    assert 'id="storage-summary"' in response.text
    assert 'class="settings-storage-row"' in response.text
    assert "symbol cache" in response.text
    assert "available for captures" in response.text
    assert "Retention and host actions live on" not in response.text


def test_storage_page_tabs_show_storage_dumps_and_cache(client):
    response = client.get("/storage")
    assert response.status_code == 200
    assert "tab-strip" in response.text
    assert 'href="/storage"' in response.text
    assert 'href="/dumps"' in response.text
    assert 'href="/workload-cache"' in response.text
