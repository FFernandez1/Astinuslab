"""F-25: silent exception swallowing replaced with logger.exception.

These tests cover the remaining live failure-reporting paths: reconciliation
orphan handlers and failed network-state publication.
"""

import logging
from unittest.mock import MagicMock, patch

from astinuslab.playground import reconciliation as playground_reconciliation
from astinuslab.playground.routes import networks as networks_routes


def test_F25_reconciler_orphan_domain_failure_is_logged(caplog):
    """Orphan-domain destroy failures must be logged, not swallowed."""
    bad_domain = MagicMock()
    bad_domain.isActive.return_value = True
    bad_domain.destroy.side_effect = RuntimeError("libvirt exploded")
    bad_domain.undefine.side_effect = RuntimeError("also broken")

    fake_libvirt = MagicMock()
    fake_libvirt.libvirtError = RuntimeError
    fake_conn = MagicMock()
    fake_conn.listAllDomains.return_value = [bad_domain]
    fake_conn.listAllNetworks.return_value = []
    bad_domain.name.return_value = "astinuslab-bad-domain"
    fake_libvirt.open.return_value = fake_conn

    def fake_session_scope():
        from contextlib import contextmanager

        @contextmanager
        def _noop():
            session = MagicMock()
            session.scalars.return_value = []
            yield session

        return _noop()

    with caplog.at_level(logging.ERROR, logger="astinuslab.playground.reconciliation"):
        playground_reconciliation.reconcile_libvirt_resources(
            session_scope=fake_session_scope,
            libvirt_module=fake_libvirt,
            libvirt_uri="qemu:///system",
            grace_seconds=0,
        )

    assert any("orphan" in rec.message.lower() or "bad-domain" in rec.message for rec in caplog.records)


def test_F25_mark_network_failed_logs_on_failure(caplog):
    """_mark_network_failed must log failures instead of silently swallowing them."""
    with caplog.at_level(logging.ERROR, logger="astinuslab.playground.routes.networks"):
        with patch("astinuslab.playground.routes.networks.db_mod.session_scope", side_effect=RuntimeError("db gone")):
            networks_routes._mark_network_failed("net-xyz", "some detail")
    assert any("mark_network_failed" in rec.message or "db gone" in rec.message for rec in caplog.records)
