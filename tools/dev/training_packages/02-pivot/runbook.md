# Lab 2 — Three Targets with a required pivot

**DRAFT / unlocked / image-free. Live acceptance: NOT RUN.**
Three lab machines: vulnerable pivot, internal web Target and internal database
Target. The external Attacker is excluded from the package inventory and counts.

```text
Entry network ---- pivot ---- Internal network
                                |-- internal-web
                                `-- internal-db
```

## Inventory and preparation

| Target | Prepared image | Networks / addresses | vCPU / RAM / disk |
| --- | --- | --- | --- |
| `pivot` | `training-dvwa-pivot` | Entry `10.77.20.10/24`, internal `10.77.21.10/24` | 2 / 2 GiB / 16 GiB |
| `internal-web` | `training-internal-web` | Internal `10.77.21.20/24` only | 1 / 1 GiB / 12 GiB |
| `internal-db` | `training-internal-db` | Internal `10.77.21.30/24` only | 1 / 1 GiB / 12 GiB |

Attach the external client only to entry, using `10.77.20.200/24` without a
default route. Reserve `.1` on each network for the libvirt host bridge.
All exercise NICs use static addressing and no DHCP clients. Verify no address
duplicates; libvirt's DHCP service still exists and must not allocate an exercise
address to an unexpected client. Check guest interface identity/order at boot.
Disable guest IPv6 and verify that no alternate interface bypasses isolation.

The pivot supplies the same pinned DVWA 2.1 command-injection exercise as lab 1,
plus a dedicated, unprivileged SSH tunneling account. IP forwarding starts
disabled for IPv4 and IPv6; there is no pre-existing tunnel or inter-segment route
on the external client. The internal web service returns the unique body
`ASTINUSLAB_INTERNAL_WEB`. MariaDB listens on the database's internal address and
contains a non-sensitive `training.proof` table with marker
`ASTINUSLAB_INTERNAL_DB`; use a session-specific read-only training account.

Generate lab-only access material outside tracked source and keep operator
credentials distinct from exercise credentials. The instructor releases the
temporary tunneling identity only after the learner demonstrates the DVWA
foothold. Do not use operator root access as evidence of a compromised pivot.

Each guest needs QGA and a measured `readiness.json` observation under
`/var/lib/astinuslab/observations`. Use its prepared-image ID in
`"v1:training-dvwa-pivot:ready"` and substitute
the corresponding web/database profile on the other guests. The producer must
measure local service health, correct NICs and baseline routing; it cannot be
a hardcoded success file. The directory must have no symlinks and must not be
writable by the vulnerable application user.

## Live acceptance

1. Record candidate digests and NIC/route tables. Prove only `pivot` is connected
   to both segments and that the entry client has no internal NIC or alternate route.
2. Establish same-run health controls from the internal side: HTTP returns the
   web marker; a read-only SQL query returns the database marker. Operator host
   access may establish health, but never establishes entry-network isolation.
3. From the entry client, direct HTTP to `10.77.21.20:80` and a direct connection
   to `10.77.21.30:3306` must fail while those services remain healthy. Even with
   a route via `10.77.20.10`, ordinary forwarding must fail while pivot IP
   forwarding is disabled. Record interface captures/counters, not timeouts alone.
4. Demonstrate harmless command execution on the pivot through DVWA. Keep a
   fresh marker response as the foothold proof, then use the released lab identity
   to open a tunnel through the pivot. For an SSH SOCKS exercise, run this **inside
   the entry client**, with its temporary identity already configured:

   ```bash
   ssh -N -D 127.0.0.1:1080 trainee@10.77.20.10
   ```

5. Through that tunnel, fetch the web marker:

   ```bash
   curl --fail --max-time 10 --noproxy '' --socks5-hostname 127.0.0.1:1080 http://10.77.21.20/
   ```

   For the database test, establish an SSH local forward through the same pivot
   (`-L 127.0.0.1:13306:10.77.21.30:3306`) and run the read-only marker query
   against local port 13306. Verify connections reach the expected destination;
   a listening local tunnel port is not sufficient evidence.
6. Close every tunnel. Repeat the direct checks; they must fail again while the
   internal health controls still pass. Check that no IP forwarding, residual
   route or background proxy has made the isolation permanent.
7. For the final locked export, Reset and rerun steps 1–6 with new session
   identities. Teardown task-owned resources and verify removal.

## Pass rule and result record

Keep the foothold response, healthy-service controls, direct refusals, tunneled
marker responses and post-tunnel refusals in one private candidate record. Packet
evidence must identify the source as the entry client and show the pivot path.
All three Targets must participate. Baseline observations are not exercise scores.
Any missing control is BLOCKED/FAIL; status remains NOT RUN until executed.

Reference: [pinned DVWA source](https://github.com/digininja/DVWA/tree/3663d72c4c4f51ac5bcd9f0c8a16b9d890383347).
