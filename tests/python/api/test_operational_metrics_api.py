"""The metrics endpoint: shape, bounds and the auth gate."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.api


class TestMetricsEndpoint:
    def test_it_returns_the_report_shape(self, client):
        response = client.get("/api/v1/metrics")

        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"generated_at", "window_hours", "actions", "jobs", "queue"}
        assert payload["window_hours"] == 24

    def test_the_window_is_selectable(self, client):
        assert client.get("/api/v1/metrics?window_hours=6").json()["window_hours"] == 6

    def test_an_out_of_range_window_is_rejected_rather_than_clamped_silently(self, client):
        assert client.get("/api/v1/metrics?window_hours=0").status_code == 422
        assert client.get("/api/v1/metrics?window_hours=99999").status_code == 422

    def test_a_non_numeric_window_is_rejected(self, client):
        assert client.get("/api/v1/metrics?window_hours=lots").status_code == 422

    def test_the_queue_block_is_always_present(self, client):
        queue = client.get("/api/v1/metrics").json()["queue"]

        assert queue["queued"] == 0
        assert queue["running"] == 0
        assert queue["oldest_queued_age_seconds"] is None
