"""Pure binding of an exact cold libvirt inventory to two reviewed clone XMLs."""

import hashlib
import xml.etree.ElementTree as ET


def require(value, phase):
    if not value:
        raise ValueError(phase)


def parse_xml(raw):
    require(type(raw) is str and len(raw.encode()) <= 16384 and "<!DOCTYPE" not in raw, "xml_bound")
    # XML entity declarations require a DOCTYPE; reject it before parsing.
    # Keep this stdlib-only for the pinned system-Python inspection worker.
    return ET.fromstring(raw)  # noqa: S314


def normalized(raw):
    root = parse_xml(raw)
    require(root.tag == "domain" and root.attrib == {"type": "kvm"}, "domain_type")
    for label in list(root.findall("seclabel")):
        require(
            label.get("type") == "dynamic"
            and label.get("model") in {"apparmor", "dac"}
            and label.get("relabel", "yes") == "yes"
            and set(label.attrib) <= {"type", "model", "relabel"},
            "security_label",
        )
        require(
            all(n.tag in {"label", "imagelabel", "baselabel"} and len(n.text or "") <= 128 for n in label),
            "security_label_fields",
        )
        root.remove(label)
    return ET.canonicalize(ET.tostring(root, encoding="unicode"), strip_text=True)


def collect(connection, plan, anchors, templates, defined):
    pair = {c["name"]: c for c in plan["clones"]}
    wanted = {c["name"] for c in pair.values() if c["label"] in defined}
    require(len(pair) == 2 and set(defined) <= {"a", "b"}, "pair")
    rows = connection.listAllDomains(0)
    domains = {d.name(): d for d in rows}
    require(len(rows) == len(domains) and set(domains) == set(anchors["domain_uuids"]) | wanted, "domain_set")
    hashes = {}
    for name, domain in domains.items():
        require(domain.isActive() == 0 and domain.autostart() == 0 and domain.state()[0] == 5, "domain_state")
        raw = domain.XMLDesc(2)
        require(type(raw) is str and len(raw.encode()) <= 16384 and "<!DOCTYPE" not in raw, "domain_xml")
        if name in pair:
            candidate = pair[name]
            require(domain.UUIDString() == candidate["uuid"], "candidate_uuid")
            require(
                domain.isPersistent() == 1
                and domain.hasManagedSaveImage(0) == 0
                and domain.snapshotNum(0) == 0
                and domain.listAllCheckpoints(0) == [],
                "candidate_saved_state",
            )
            require(normalized(raw) == normalized(templates[candidate["label"]]), "candidate_xml")
            hashes[candidate["label"]] = hashlib.sha256(raw.encode()).hexdigest()
        else:
            require(domain.UUIDString() == anchors["domain_uuids"][name], "protected_uuid")
            require(
                hashlib.sha256(raw.encode()).hexdigest() == anchors["protected_snapshot"]["domain_xml_sha256"][name],
                "protected_xml",
            )
            tree = parse_xml(raw)
            for candidate in pair.values():
                require(domain.UUIDString() != candidate["uuid"], "uuid_collision")
                require(
                    all(
                        mac.get("address", "").lower() != candidate["mac"]
                        for mac in tree.findall("devices/interface/mac")
                    ),
                    "mac_collision",
                )
            for node in tree.iter():
                paths = [node.get(k, "") for k in ("file", "dir", "path")] + [(node.text or "").strip()]
                for prefix in [plan["source_directory"], *(c["directory"] for c in pair.values())]:
                    require(not any(p == prefix or p.startswith(prefix + "/") for p in paths), "path_collision")
    networks = connection.listAllNetworks(0)
    require(len(networks) == 1 and networks[0].name() == "default" and networks[0].isActive() == 1, "network")
    raw = networks[0].XMLDesc(0)
    require(type(raw) is str and len(raw.encode()) <= 16384 and "<!DOCTYPE" not in raw, "network_xml")
    require(
        hashlib.sha256(raw.encode()).hexdigest() == anchors["protected_snapshot"]["network_xml_sha256"],
        "network_changed",
    )
    for host in parse_xml(raw).findall("ip/dhcp/host"):
        require(all(host.get("mac", "").lower() != c["mac"] for c in pair.values()), "dhcp_collision")
    return {"all_domains_off": True, "definition_sha256": hashes}
