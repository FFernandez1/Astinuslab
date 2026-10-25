# Lab 3 — Configured firewall and protected Target

**DRAFT / unlocked / image-free. Live acceptance: NOT RUN.**
The lab contains one OpenWrt Network Node and one protected web Target. The
external Attacker is excluded from the inventory and all machine counts.

```text
Entry network ---- OpenWrt firewall ---- Protected network ---- web Target
```

## Inventory and preparation

| Machine | Prepared image | Networks / addresses | vCPU / RAM / disk |
| --- | --- | --- | --- |
| `firewall` | `training-openwrt-firewall` | Entry `10.77.30.254/24`, protected `10.77.31.254/24` | 1 / 512 MiB / 4 GiB |
| `protected-web` | `training-protected-web` | Protected `10.77.31.10/24` only | 1 / 1 GiB / 12 GiB |

Use a BIOS-compatible OpenWrt build: the current package recipe adapter rejects
UEFI. Require working QGA and a measured observation provider before accepting
the image. Pin its version and image SHA-256 during preparation.

Use static exercise interfaces and disable their DHCP clients; disable OpenWrt's
DHCP/DNS services for these segments. Libvirt's DHCP service remains present:
reject any unexpected client or duplicate lease. Reserve `.1` bridge addresses
for operator management. Disable guest IPv6 and verify it; do not leave a second
protocol family as an untested bypass. Resolve interface names inside the image
and persist separate UCI networks `entry` and `protected` for those NICs.

The external client uses `10.77.30.200/24` and a route to `10.77.31.0/24` via
`10.77.30.254`. The protected Target has a return route to `10.77.30.0/24` via
`10.77.31.254`. Persist the guest routes, with no Internet default route. The
host bridge `.1` is not the inter-segment gateway. No NAT is needed between the
exercise subnets. Never apply these routes to the operator host as a substitute.

The protected Target runs HTTP on TCP 80 returning `ASTINUSLAB_PROTECTED_WEB`
and a working SSH listener on TCP 22. SSH is deliberately present so a deny
test cannot pass merely because the service does not exist.

## Firewall configuration contract

Persist the following in OpenWrt UCI/firewall4, then inspect the effective nftables
ruleset. A two-NIC recipe alone does not install these policies.

| Scope | Required policy |
| --- | --- |
| Entry and protected zones | Input REJECT, output ACCEPT, forward REJECT |
| Established/related traffic | Allow return traffic for accepted flows |
| Entry → protected | Allow TCP 80 only to `10.77.31.10` |
| Other entry → protected traffic | Reject/drop; named rule with counter and rate-limited log |
| Protected → entry new connections | Deny |
| Firewall management | SSH from the corresponding host bridge `.1` only; deny from the entry client |
| Forwarding | IPv4 enabled in the firewall guest; no generic zone forwarding allow |
| Flow offload | Disabled during acceptance so counters remain observable |

For the HTTP exception, the UCI rule fields are `src=entry`, `dest=protected`,
`dest_ip=10.77.31.10`, `proto=tcp`, `dest_port=80`, `target=ACCEPT`, `family=ipv4`.
The explicit deny rule follows that exception and precedes any broader accept.
Keep each interface in exactly one zone and remove pre-existing permissive
LAN→WAN forwarding from this disposable image. Verify the effective rules,
not just the saved text. Preserve operator console access throughout preparation.

Both guests produce actual baseline `readiness.json` measurements under
`/var/lib/astinuslab/observations`. Expected shape:
`"v1:training-openwrt-firewall:ready"`,
using `training-protected-web` on the Target. The firewall producer measures
addresses, forwarding and the effective policy; the Target producer measures
both live listeners and its return route. No symlinks or application-writable
observation directory. Never create a constant success file.

## Live acceptance

1. Record digests, NICs and guest route tables. Verify the external client is on
   entry only, and the protected Target has only its protected NIC.
2. Establish HTTP and SSH positive controls from the protected side. Confirm the
   HTTP body and an SSH banner; record both immediately before the deny tests.
3. From the entry client, request `http://10.77.31.10/` and require the expected
   marker. Observe packets on both firewall interfaces. The destination is the
   protected Target, not a page served by the firewall itself.
4. From that same client, attempt TCP 22 to `10.77.31.10`; require refusal or
   timeout and an increment on the corresponding firewall deny counter. Repeat
   for an unapproved port with a temporary healthy listener on the Target, then
   stop that listener. A timeout without positive health/counter controls fails.
5. Attempt entry-client SSH to the firewall itself; require denial. Check the
   management exception remains limited to the host bridge source address.
6. Inspect forwarding, return routes and effective rules to exclude alternate
   paths. With the task-owned firewall powered off, a new HTTP connection must
   fail; restore it and require HTTP success without changing any rules.
7. Reboot the firewall again and repeat HTTP allow, SSH deny and counter checks.
   Success requires the saved configuration to reconstruct the policy on boot.
8. After final locked export, Reset and rerun the controls. Teardown only the
   task-owned session; retain counters and resource IDs if cleanup fails.

## Pass rule and result record

Save the effective ruleset, pre/post counters, service-health controls, HTTP
marker, blocked connection result, reboot result and candidate digests privately.
Do not infer filtering from an unavailable service or host-to-guest tests.
Baseline observations alone do not prove the policy. Status remains NOT RUN
until these traffic tests execute successfully.

References: [firewall4 configuration source](https://github.com/openwrt/firewall4/blob/master/root/etc/config/firewall)
and [firewall4 parser](https://lxr.openwrt.org/source/firewall4/root/usr/share/ucode/fw4.uc).
