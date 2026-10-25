"""Read-only libvirt binding with injected domains; no VM is defined or booted."""

import hashlib
import importlib.util
from pathlib import Path

import pytest


def module():
    path = Path(__file__).parents[3] / "tools/dev/windows_candidate_trial/cold_state.py"
    spec = importlib.util.spec_from_file_location("cold_state", path)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def xml(name, uid, mac, directory):
    return (
        f'<domain type="kvm"><name>{name}</name><uuid>{uid}</uuid><os><nvram>{directory}/runtime.vars</nvram></os>'
        f'<devices><interface><mac address="{mac}"/></interface><disk><source file="{directory}/disk.qcow2"/>'
        "</disk></devices></domain>"
    )


class Domain:
    def __init__(self, name, uid, raw):
        self.n, self.u, self.raw = name, uid, raw

    def name(self):
        return self.n

    def UUIDString(self):
        return self.u

    def isActive(self):
        return 0

    def autostart(self):
        return 0

    def state(self):
        return [5, 2]

    def XMLDesc(self, flags):
        assert flags == 2
        return self.raw

    def isPersistent(self):
        return 1

    def hasManagedSaveImage(self, flags):
        return 0

    def snapshotNum(self, flags):
        return 0

    def listAllCheckpoints(self, flags):
        return []


class Network:
    raw = "<network><name>default</name></network>"

    def name(self):
        return "default"

    def isActive(self):
        return 1

    def XMLDesc(self, flags):
        return self.raw


class Connection:
    def __init__(self, domains):
        self.domains, self.network = domains, Network()

    def listAllDomains(self, flags):
        return self.domains

    def listAllNetworks(self, flags):
        return [self.network]


def fixture():
    old = Domain("old", "old-uuid", xml("old", "old-uuid", "52:54:00:00:00:01", "/old"))
    pair = [
        {
            "label": s,
            "name": "trial-" + s,
            "uuid": "uuid-" + s,
            "mac": "52:54:00:00:00:0" + str(i + 2),
            "directory": "/pair/" + s,
        }
        for i, s in enumerate(("a", "b"))
    ]
    templates = {c["label"]: xml(c["name"], c["uuid"], c["mac"], c["directory"]) for c in pair}
    anchors = {
        "domain_uuids": {"old": "old-uuid"},
        "protected_snapshot": {
            "domain_xml_sha256": {"old": hashlib.sha256(old.raw.encode()).hexdigest()},
            "network_xml_sha256": hashlib.sha256(Network.raw.encode()).hexdigest(),
        },
    }
    return Connection([old]), {"clones": pair, "source_directory": "/source"}, anchors, templates


@pytest.mark.parametrize("labels", [[], ["a"], ["a", "b"]])
def test_exact_cold_inventory_and_new_definition_hashes(labels):
    c, p, a, t = fixture()
    for row in p["clones"]:
        if row["label"] in labels:
            c.domains.append(Domain(row["name"], row["uuid"], t[row["label"]]))
    result = module().collect(c, p, a, t, labels)
    assert result["all_domains_off"] is True
    assert set(result["definition_sha256"]) == set(labels)


@pytest.mark.parametrize(
    "bad",
    [
        "active",
        "autostart",
        "uuid",
        "old_xml",
        "network",
        "extra",
        "missing",
        "duplicate",
        "mac_collision",
        "source_reference",
    ],
)
def test_cold_binding_rejects_inventory_or_protected_changes(bad):
    c, p, a, t = fixture()
    old = c.domains[0]
    if bad == "active":
        old.isActive = lambda: 1
    if bad == "autostart":
        old.autostart = lambda: 1
    if bad == "uuid":
        old.u = "changed"
    if bad == "old_xml":
        old.raw += " "
    if bad == "network":
        c.network.raw = "<network><name>changed</name></network>"
    if bad == "extra":
        c.domains.append(Domain("extra", "unused", old.raw))
    if bad == "missing":
        c.domains.clear()
    if bad == "duplicate":
        c.domains.append(old)
    if bad == "mac_collision":
        p["clones"][0]["mac"] = "52:54:00:00:00:01"
    if bad == "source_reference":
        p["source_directory"] = "/old"
    with pytest.raises(ValueError):
        module().collect(c, p, a, t, [])


@pytest.mark.parametrize("bad", ["active", "xml", "transient", "managed_save", "snapshot", "checkpoint"])
def test_new_definitions_must_be_exact_cold_and_unconsumed(monkeypatch, bad):
    c, p, a, t = fixture()
    row = p["clones"][0]
    d = Domain(row["name"], row["uuid"], t["a"])
    c.domains.append(d)
    if bad == "active":
        monkeypatch.setattr(d, "isActive", lambda: 1)
    if bad == "xml":
        d.raw = d.raw.replace("/a/disk.qcow2", "/b/disk.qcow2")
    if bad == "transient":
        monkeypatch.setattr(d, "isPersistent", lambda: 0)
    if bad == "managed_save":
        monkeypatch.setattr(d, "hasManagedSaveImage", lambda _: 1)
    if bad == "snapshot":
        monkeypatch.setattr(d, "snapshotNum", lambda _: 1)
    if bad == "checkpoint":
        monkeypatch.setattr(d, "listAllCheckpoints", lambda _: [object()])
    with pytest.raises(ValueError):
        module().collect(c, p, a, t, ["a"])


def test_xml_security_labels_are_limited_to_dynamic_confined_models():
    m = module()
    raw = '<domain type="kvm"><name>x</name></domain>'
    for tag in (
        '<seclabel type="static" model="dac"/>',
        '<seclabel type="none"/>',
        '<seclabel type="dynamic" model="dac" relabel="no"/>',
    ):
        with pytest.raises(ValueError):
            m.normalized(raw.replace("</domain>", tag + "</domain>"))
    good = raw.replace("</domain>", '<seclabel type="dynamic" model="apparmor" relabel="yes"/></domain>')
    assert m.normalized(good) == m.normalized(raw)
