from __future__ import annotations

import hashlib
import ipaddress
import logging
import time

logger = logging.getLogger(__name__)


_NETWORK_XML_TEMPLATE_ISOLATED = """\
<network>
  <name>{name}</name>
  <bridge name='{bridge}' stp='on' delay='0'/>
  <ip address='{gateway}' netmask='{netmask}'>
    <dhcp>
      <range start='{dhcp_start}' end='{dhcp_end}'/>
    </dhcp>
  </ip>
</network>
"""


_NETWORK_XML_TEMPLATE_ROUTED = """\
<network>
  <name>{name}</name>
  <forward mode='nat'/>
  <bridge name='{bridge}' stp='on' delay='0'/>
  <ip address='{gateway}' netmask='{netmask}'>
    <dhcp>
      <range start='{dhcp_start}' end='{dhcp_end}'/>
    </dhcp>
  </ip>
</network>
"""


def bridge_name_for_network_id(network_id: str) -> str:
    digest = hashlib.sha256(network_id.encode()).hexdigest()[:11]
    return f"vbr-{digest}"


class PlaygroundNetworks:
    def __init__(self, *, libvirt_module, libvirt_uri: str = "qemu:///system") -> None:
        self._libvirt = libvirt_module
        self._libvirt_uri = libvirt_uri

    def reap_orphans(self, *, session_scope) -> int:
        """Destroy host networks left behind by dead sessions so their subnets
        are freed before a new network is defined. Delegates to the reconciler's
        orphan-network logic."""
        from .reconciliation import reap_orphan_networks

        return reap_orphan_networks(
            session_scope=session_scope,
            libvirt_module=self._libvirt,
            libvirt_uri=self._libvirt_uri,
        )

    def create_network(
        self, *, libvirt_net_name: str, cidr: str, isolated: bool, network_id: str | None = None
    ) -> None:
        net = ipaddress.ip_network(cidr, strict=True)
        if net.num_addresses < 4:
            raise ValueError(f"CIDR too small: {cidr}")
        gateway = str(net.network_address + 1)
        netmask = str(net.netmask)
        bridge = bridge_name_for_network_id(network_id or libvirt_net_name)
        dhcp_start = str(net.network_address + 2)
        dhcp_end = str(net.broadcast_address - 1)
        template = _NETWORK_XML_TEMPLATE_ISOLATED if isolated else _NETWORK_XML_TEMPLATE_ROUTED
        xml = template.format(
            name=libvirt_net_name,
            bridge=bridge,
            gateway=gateway,
            netmask=netmask,
            dhcp_start=dhcp_start,
            dhcp_end=dhcp_end,
        )
        conn = self._libvirt.open(self._libvirt_uri)
        net_obj = None
        try:
            net_obj = conn.networkDefineXML(xml)
            try:
                net_obj.create()
                net_obj.setAutostart(False)
            except Exception as create_exc:
                try:
                    self._teardown_defined_network(net_obj)
                except Exception as cleanup_exc:
                    raise RuntimeError("network create failed and cleanup was incomplete") from cleanup_exc
                raise create_exc
        finally:
            conn.close()

    @staticmethod
    def _teardown_defined_network(net_obj) -> None:
        if net_obj.isActive():
            net_obj.destroy()
            for attempt in range(10):
                if not net_obj.isActive():
                    break
                if attempt != 9:
                    time.sleep(0.1)
            else:
                raise RuntimeError("network remained active after destroy")
        net_obj.undefine()

    def destroy_network(self, libvirt_net_name: str) -> None:
        conn = self._libvirt.open(self._libvirt_uri)
        try:
            try:
                net_obj = conn.networkLookupByName(libvirt_net_name)
            except self._libvirt.libvirtError as exc:
                error_code = getattr(exc, "get_error_code", lambda: None)()
                if error_code == getattr(self._libvirt, "VIR_ERR_NO_NETWORK", object()):
                    logger.info("Network %s not present; nothing to destroy", libvirt_net_name)
                    return
                raise
            self._teardown_defined_network(net_obj)
        finally:
            conn.close()
