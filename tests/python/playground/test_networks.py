from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest

from astinuslab.playground.networks import PlaygroundNetworks


def _fake_libvirt():
    mod = MagicMock()
    conn = MagicMock()
    mod.open.return_value = conn
    return mod, conn


def test_define_isolated_network_renders_no_forward_element():
    libvirt_mod, conn = _fake_libvirt()
    pn = PlaygroundNetworks(libvirt_module=libvirt_mod)
    pn.create_network(
        libvirt_net_name="astinuslab-pg-pg1-ab",
        cidr="10.42.1.0/24",
        isolated=True,
    )
    xml = conn.networkDefineXML.call_args.args[0]
    assert "<forward" not in xml
    assert "10.42.1.1" in xml  # gateway = first usable
    assert "astinuslab-pg-pg1-ab" in xml
    # bridge name in XML must be ≤ 15 chars (Linux IFNAMSIZ)
    bridge_match = re.search(r"name='([^']+)'", xml)
    assert bridge_match and len(bridge_match.group(1)) <= 15


def test_define_routed_network_includes_forward_nat():
    libvirt_mod, conn = _fake_libvirt()
    pn = PlaygroundNetworks(libvirt_module=libvirt_mod)
    pn.create_network(
        libvirt_net_name="astinuslab-pg-pg1-ext",
        cidr="10.42.2.0/24",
        isolated=False,
    )
    xml = conn.networkDefineXML.call_args.args[0]
    assert "<forward" in xml and "nat" in xml
    # bridge name in XML must be ≤ 15 chars (Linux IFNAMSIZ)
    bridge_match = re.search(r"name='([^']+)'", xml)
    assert bridge_match and len(bridge_match.group(1)) <= 15


def test_bridge_names_use_stable_network_id_and_avoid_suffix_collisions():
    libvirt_mod, conn = _fake_libvirt()
    pn = PlaygroundNetworks(libvirt_module=libvirt_mod)

    pn.create_network(
        libvirt_net_name="astinuslab-pg-pg1-alpha-sharedtail",
        network_id="net-alpha-id",
        cidr="10.42.10.0/24",
        isolated=True,
    )
    first_xml = conn.networkDefineXML.call_args.args[0]
    pn.create_network(
        libvirt_net_name="astinuslab-pg-pg2-bravo-sharedtail",
        network_id="net-bravo-id",
        cidr="10.42.11.0/24",
        isolated=True,
    )
    second_xml = conn.networkDefineXML.call_args.args[0]

    first_bridge_match = re.search(r"<bridge name='([^']+)'", first_xml)
    second_bridge_match = re.search(r"<bridge name='([^']+)'", second_xml)
    assert first_bridge_match
    assert second_bridge_match
    first_bridge = first_bridge_match.group(1)
    second_bridge = second_bridge_match.group(1)
    assert first_bridge != second_bridge
    assert len(first_bridge) <= 15
    assert len(second_bridge) <= 15


def test_destroy_network_cleans_up_active_and_persistent_definitions():
    libvirt_mod, conn = _fake_libvirt()
    net = MagicMock()
    net.isActive.side_effect = [True, False]
    conn.networkLookupByName.return_value = net

    pn = PlaygroundNetworks(libvirt_module=libvirt_mod)
    pn.destroy_network("astinuslab-pg-pg1-ab")
    net.destroy.assert_called_once()
    net.undefine.assert_called_once()


def test_destroy_network_is_idempotent_when_missing():
    libvirt_mod, conn = _fake_libvirt()
    libvirt_mod.VIR_ERR_NO_NETWORK = 42

    class MissingNetworkError(Exception):
        def get_error_code(self):
            return 42

    libvirt_mod.libvirtError = MissingNetworkError
    conn.networkLookupByName.side_effect = libvirt_mod.libvirtError("not found")

    pn = PlaygroundNetworks(libvirt_module=libvirt_mod)
    pn.destroy_network("astinuslab-pg-pg1-missing")  # must not raise


def test_destroy_network_propagates_non_absence_lookup_errors():
    libvirt_mod, conn = _fake_libvirt()
    libvirt_mod.VIR_ERR_NO_NETWORK = 42

    class ConnectionError(Exception):
        def get_error_code(self):
            return 7

    libvirt_mod.libvirtError = ConnectionError
    conn.networkLookupByName.side_effect = ConnectionError("permission denied")

    with pytest.raises(ConnectionError, match="permission denied"):
        PlaygroundNetworks(libvirt_module=libvirt_mod).destroy_network("astinuslab-pg-pg1-ab")


def test_destroy_network_refuses_to_undefine_while_still_active(monkeypatch):
    libvirt_mod, conn = _fake_libvirt()
    net = MagicMock()
    net.isActive.return_value = True
    conn.networkLookupByName.return_value = net
    monkeypatch.setattr("astinuslab.playground.networks.time.sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="remained active"):
        PlaygroundNetworks(libvirt_module=libvirt_mod).destroy_network("astinuslab-pg-pg1-ab")

    net.destroy.assert_called_once()
    net.undefine.assert_not_called()


@pytest.mark.parametrize("failure_point", ["create", "autostart"])
def test_create_network_cleans_up_a_partially_defined_network(failure_point):
    libvirt_mod, conn = _fake_libvirt()
    net = MagicMock()
    conn.networkDefineXML.return_value = net
    net.isActive.side_effect = [failure_point == "autostart", False]
    if failure_point == "create":
        net.create.side_effect = RuntimeError("create failed")
    else:
        net.setAutostart.side_effect = RuntimeError("autostart failed")

    with pytest.raises(RuntimeError, match=f"{failure_point} failed"):
        PlaygroundNetworks(libvirt_module=libvirt_mod).create_network(
            libvirt_net_name="astinuslab-pg-pg1-ab",
            cidr="10.42.1.0/24",
            isolated=True,
        )

    if failure_point == "autostart":
        net.destroy.assert_called_once()
    net.undefine.assert_called_once()


def test_destroy_network_raises_when_active_destroy_fails():
    libvirt_mod, conn = _fake_libvirt()
    net = MagicMock()
    net.isActive.return_value = True
    net.destroy.side_effect = RuntimeError("destroy failed")
    conn.networkLookupByName.return_value = net

    pn = PlaygroundNetworks(libvirt_module=libvirt_mod)

    with pytest.raises(RuntimeError, match="destroy failed"):
        pn.destroy_network("astinuslab-pg-pg1-ab")

    net.undefine.assert_not_called()


def test_destroy_network_raises_when_undefine_fails():
    libvirt_mod, conn = _fake_libvirt()
    net = MagicMock()
    net.isActive.return_value = False
    net.undefine.side_effect = RuntimeError("undefine failed")
    conn.networkLookupByName.return_value = net

    pn = PlaygroundNetworks(libvirt_module=libvirt_mod)

    with pytest.raises(RuntimeError, match="undefine failed"):
        pn.destroy_network("astinuslab-pg-pg1-ab")


def test_create_network_uses_configured_libvirt_uri():
    libvirt_mod, conn = _fake_libvirt()
    pn = PlaygroundNetworks(libvirt_module=libvirt_mod, libvirt_uri="qemu:///session")
    pn.create_network(
        libvirt_net_name="astinuslab-pg-pg1-ab",
        cidr="10.42.1.0/24",
        isolated=True,
    )
    libvirt_mod.open.assert_called_with("qemu:///session")


def test_destroy_network_uses_configured_libvirt_uri():
    libvirt_mod, conn = _fake_libvirt()
    net = MagicMock()
    net.isActive.side_effect = [True, False]
    conn.networkLookupByName.return_value = net
    pn = PlaygroundNetworks(libvirt_module=libvirt_mod, libvirt_uri="qemu:///session")
    pn.destroy_network("astinuslab-pg-pg1-ab")
    libvirt_mod.open.assert_called_with("qemu:///session")


def test_default_libvirt_uri_is_qemu_system():
    libvirt_mod, conn = _fake_libvirt()
    pn = PlaygroundNetworks(libvirt_module=libvirt_mod)
    pn.create_network(
        libvirt_net_name="astinuslab-pg-pg1-ab",
        cidr="10.42.1.0/24",
        isolated=True,
    )
    libvirt_mod.open.assert_called_with("qemu:///system")
