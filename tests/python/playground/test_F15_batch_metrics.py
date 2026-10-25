from datetime import UTC, datetime
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from astinuslab import db as db_mod
from astinuslab.main import app
from astinuslab.playground import metrics as playground_metrics
from astinuslab.playground import repositories as pr
from tests.python._support.helpers import autospec_setattr


def test_F15_batch_metrics_returns_one_entry_per_running_vm(in_memory_session_scope, monkeypatch):
    monkeypatch.setattr(db_mod, "session_scope", in_memory_session_scope)

    with in_memory_session_scope() as s:
        pr.create_playground_session(s, job_id="pg-m")
        for vm_id in ("vm-a", "vm-b"):
            pr.register_vm(
                s,
                vm_id=vm_id,
                job_id="pg-m",
                label=vm_id,
                image_id="img",
                domain_name=f"astinuslab-pg-pg-m-{vm_id}",
                disk_path=f"/tmp/{vm_id}.qcow2",
            )
            pr.update_vm_state(s, vm_id, "running")

    sampler = MagicMock()
    sampler.sample.side_effect = lambda vm_id, domain_name: {
        "vm_id": vm_id,
        "state": "running",
        "cpu_pct": 1.0,
        "mem_pct": 2.0,
        "mem_used_mb": 100,
        "mem_total_mb": 1024,
        "rx_bytes": {},
        "tx_bytes": {},
        "sampled_at": datetime.now(UTC),
    }
    autospec_setattr(monkeypatch, playground_metrics, "get_sampler", lambda: sampler)

    res = TestClient(app).get("/api/v1/playground/sessions/current/metrics")

    assert res.status_code == 200
    assert set(res.json()) == {"vm-a", "vm-b"}
    assert sampler.sample.call_count == 2
