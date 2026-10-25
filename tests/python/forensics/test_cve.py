"""Tests for M2: CVE library — workload ↔ CVE associations and search."""

from __future__ import annotations

WORDPRESS_ID = "wordpress-6.4"


# ── catalog helpers ────────────────────────────────────────────────────────────


class TestCatalogCveHelpers:
    def test_workloads_have_cve_ids(self, client):
        workloads = {w["id"]: w for w in client.get("/api/v1/workloads").json()}
        assert "cve_ids" in workloads[WORDPRESS_ID]
        assert isinstance(workloads[WORDPRESS_ID]["cve_ids"], list)

    def test_wordpress_cve_ids_not_empty(self, client):
        workloads = {w["id"]: w for w in client.get("/api/v1/workloads").json()}
        assert len(workloads[WORDPRESS_ID]["cve_ids"]) > 0

    def test_cve_ids_are_strings(self, client):
        workloads = client.get("/api/v1/workloads").json()
        for w in workloads:
            for cve in w["cve_ids"]:
                assert isinstance(cve, str)

    def test_cve_ids_match_format(self, client):
        import re

        pattern = re.compile(r"^CVE-\d{4}-\d{4,}$")
        workloads = client.get("/api/v1/workloads").json()
        for w in workloads:
            for cve in w["cve_ids"]:
                assert pattern.match(cve), f"Malformed CVE ID: {cve}"

    def test_all_cve_ids_from_catalog(self):
        from astinuslab.catalog import all_cve_ids

        ids = all_cve_ids()
        assert isinstance(ids, list)
        assert len(ids) > 0
        assert ids == sorted(ids)  # must be sorted

    def test_all_cve_ids_are_unique(self):
        from astinuslab.catalog import all_cve_ids

        ids = all_cve_ids()
        assert len(ids) == len(set(ids))

    def test_workloads_by_cve_returns_matching(self):
        from astinuslab.catalog import workloads_by_cve

        results = workloads_by_cve("CVE-2023-2745")
        assert any(w["id"] == WORDPRESS_ID for w in results)

    def test_workloads_by_cve_case_insensitive(self):
        from astinuslab.catalog import workloads_by_cve

        upper = workloads_by_cve("CVE-2023-2745")
        lower = workloads_by_cve("cve-2023-2745")
        assert [w["id"] for w in upper] == [w["id"] for w in lower]

    def test_workloads_by_cve_unknown_returns_empty(self):
        from astinuslab.catalog import workloads_by_cve

        assert workloads_by_cve("CVE-0000-0000") == []


# ── /api/v1/cves endpoint ──────────────────────────────────────────────────────


class TestCvesEndpoint:
    def test_returns_200(self, client):
        assert client.get("/api/v1/cves").status_code == 200

    def test_returns_list(self, client):
        assert isinstance(client.get("/api/v1/cves").json(), list)

    def test_items_have_cve_id_and_workload_ids(self, client):
        for item in client.get("/api/v1/cves").json():
            assert "cve_id" in item
            assert "workload_ids" in item
            assert isinstance(item["workload_ids"], list)

    def test_wordpress_cves_are_present(self, client):
        cve_ids = {item["cve_id"] for item in client.get("/api/v1/cves").json()}
        assert "CVE-2023-2745" in cve_ids

    def test_wordpress_mapped_to_correct_workload(self, client):
        index = {item["cve_id"]: item["workload_ids"] for item in client.get("/api/v1/cves").json()}
        assert WORDPRESS_ID in index["CVE-2023-2745"]

    def test_sorted_by_cve_id(self, client):
        items = client.get("/api/v1/cves").json()
        ids = [item["cve_id"] for item in items]
        assert ids == sorted(ids)


# ── /api/v1/workloads?cve= filter ─────────────────────────────────────────────


class TestWorkloadsCveFilter:
    def test_filter_returns_matching_workload(self, client):
        r = client.get("/api/v1/workloads?cve=CVE-2023-2745")
        assert r.status_code == 200
        ids = [w["id"] for w in r.json()]
        assert WORDPRESS_ID in ids

    def test_filter_excludes_non_matching(self, client):
        # CVE-2023-2745 is only in wordpress, not dvwa
        r = client.get("/api/v1/workloads?cve=CVE-2023-2745")
        ids = [w["id"] for w in r.json()]
        assert "dvwa" not in ids

    def test_filter_unknown_cve_returns_empty(self, client):
        r = client.get("/api/v1/workloads?cve=CVE-0000-0000")
        assert r.status_code == 200
        assert r.json() == []

    def test_filter_case_insensitive(self, client):
        upper = client.get("/api/v1/workloads?cve=CVE-2023-2745").json()
        lower = client.get("/api/v1/workloads?cve=cve-2023-2745").json()
        assert [w["id"] for w in upper] == [w["id"] for w in lower]

    def test_no_filter_returns_all(self, client):
        all_w = client.get("/api/v1/workloads").json()
        no_filter = client.get("/api/v1/workloads?cve=").json()
        assert len(all_w) == len(no_filter)


# ── Admin UI CVE page ──────────────────────────────────────────────────────────


class TestAdminWorkloadsCveUI:
    def test_page_loads(self, client):
        r = client.get("/workloads")
        assert r.status_code == 200

    def test_cve_badges_shown(self, client):
        r = client.get("/workloads")
        assert "service, version, workload id, tag or CVE" in r.text

    def test_cve_filter_input_present(self, client):
        r = client.get("/workloads")
        assert 'name="q"' in r.text

    def test_filtered_page_shows_only_matching(self, client):
        r = client.get("/workloads?cve=CVE-2023-2745")
        assert r.status_code == 200
        assert WORDPRESS_ID in r.text

    def test_filtered_page_hides_non_matching(self, client):
        r = client.get("/workloads?cve=CVE-2023-2745")
        # dvwa doesn't share this CVE, should not appear
        assert "dvwa" not in r.text.lower() or "CVE-2023-2745" in r.text

    def test_clear_filter_link_shown_when_filtered(self, client):
        r = client.get("/workloads?cve=CVE-2023-2745")
        assert 'href="/workloads"' in r.text

    def test_no_results_message_on_unknown_cve(self, client):
        r = client.get("/workloads?cve=CVE-0000-0000")
        assert r.status_code == 200
        assert "CVE-0000-0000" in r.text
