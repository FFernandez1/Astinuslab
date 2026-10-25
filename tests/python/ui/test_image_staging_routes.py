from __future__ import annotations

from types import SimpleNamespace


def test_url_fetch_existing_destination_returns_409_before_starting_thread(client, tmp_images_dir, monkeypatch):
    import astinuslab.ui.image_routes as image_routes

    destination = tmp_images_dir / "existing.qcow2"
    destination.write_bytes(b"existing-image")
    monkeypatch.setattr(image_routes, "_validate_remote_target", lambda *_args: None)
    started: list[dict] = []
    monkeypatch.setattr(
        image_routes.threading,
        "Thread",
        lambda **kwargs: SimpleNamespace(start=lambda: started.append(kwargs)),
    )

    response = client.post(
        "/images/fetch-url",
        data={"url": "https://images.example/existing.qcow2"},
    )

    assert response.status_code == 409
    assert destination.read_bytes() == b"existing-image"
    assert started == []


def test_two_url_fetches_for_same_destination_reserve_one_operation(client, tmp_images_dir, monkeypatch):
    import astinuslab.ui.image_routes as image_routes
    from astinuslab.images.image_admin_support import release_image_destination

    monkeypatch.setattr(image_routes, "_validate_remote_target", lambda *_args: None)
    started: list[dict] = []
    monkeypatch.setattr(
        image_routes.threading,
        "Thread",
        lambda **kwargs: SimpleNamespace(start=lambda: started.append(kwargs)),
    )

    first = client.post("/images/fetch-url", data={"url": "https://images.example/same.qcow2"})
    second = client.post("/images/fetch-url", data={"url": "https://images.example/same.qcow2"})

    assert first.status_code == 200
    assert second.status_code == 409
    assert len(started) == 1
    operation_id, _url, destination, _config = started[0]["args"]
    release_image_destination(destination, operation_id)


def test_open_folder_without_desktop_session_reports_the_path(client, tmp_images_dir, monkeypatch):
    import astinuslab.ui.image_routes as image_routes

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    spawned: list[list[str]] = []
    monkeypatch.setattr(image_routes.subprocess, "Popen", lambda argv, **_kwargs: spawned.append(argv))

    response = client.post("/images/open-folder")

    assert response.status_code == 409
    assert str(tmp_images_dir.resolve()) in response.json()["detail"]
    assert spawned == []


def test_open_folder_reaps_xdg_open(client, tmp_images_dir, monkeypatch):
    import astinuslab.ui.image_routes as image_routes

    monkeypatch.setenv("DISPLAY", ":0")
    process = SimpleNamespace(wait=lambda: 0)
    spawned: list[tuple[list[str], dict]] = []
    started: list[dict] = []

    def fake_popen(argv, **kwargs):
        spawned.append((argv, kwargs))
        return process

    monkeypatch.setattr(image_routes.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        image_routes.threading,
        "Thread",
        lambda **kwargs: SimpleNamespace(start=lambda: started.append(kwargs)),
    )

    response = client.post("/images/open-folder")

    assert response.status_code == 204
    assert spawned[0][0] == ["xdg-open", str(tmp_images_dir.resolve())]
    assert spawned[0][1]["start_new_session"] is True
    assert [kwargs["target"] for kwargs in started] == [process.wait]
