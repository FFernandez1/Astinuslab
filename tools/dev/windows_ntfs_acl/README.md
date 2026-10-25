# Isolated Windows NTFS ACL preparation

These preparation-only Perl modules do not open a guestfs handle, launch a VM,
or authorize a Windows candidate operation. `SyntheticAclProbe::collect` must
be called by a separately reviewed, one-use host wrapper that binds a **new**
128 MiB raw fixture on the separate volume, checks preservation and exclusive
ownership, and enforces helper termination and closure. No existing candidate
may be used as the fixture.

`RestrictedAcl` encodes one fixed 100-byte descriptor: SYSTEM owner,
Administrators group, protected DACL, only SYSTEM and Administrators full
control, no inherited ACEs or SACL. It generates one ASCII shell command for
`guestfs debug sh`; `/usr/bin/setfattr` decodes hex **inside the appliance**.
It never uses the installed `lsetxattr` binary-value RPC. Paths have a restricted
ASCII component grammar; the setter does not follow the final symlink. Callers
must independently verify ancestors, filesystem, file identity and link count.

The synthetic collector requires the reviewed `setfattr` version **and SHA-256**
inside the appliance before formatting. It preserves the established NTFS-3G
permissions mount profile. Exact descriptor bytes are required immediately
before payload, after writing, and after unmount/read-only remount. No chmod,
chown, ACL normalization, alternate setter, or mount fallback is allowed.

Capability phases separately check `root`, executable tools, version and digest.
`debug sh` folds stdout into its error buffer; libguestfs removes every trailing
LF from that buffer. Expected replies have no trailing LF, and the setter emits
its fixed status without one. No other reply bytes are normalized. Tests model
this transport behavior and execute the capability commands against installed
tools. See the [command runner](https://raw.githubusercontent.com/libguestfs/libguestfs/v1.52.0/daemon/command.c).

Failures distinguish `acl_precheck`, `acl_set_rpc`, `acl_set_returned`,
`acl_readback`, payload verification and remount verification. `setter_exit`
is a process status, not a filesystem errno. `setter_rpc_errno` is only the
bounded libguestfs error from a failed RPC. Raw setter output is discarded.
Network, verbose output and RPC tracing remain disabled; invoking the `debug`
API does not enable those settings. Unmount, shutdown and close are attempted
independently on both success and failure.

Run the public checks with:

```sh
.venv/bin/python -m pytest tests/python/host/test_windows_ntfs_acl.py -q --no-cov
```

These checks include mocked guestfs failures, installed libtirpc memory encoding,
and the installed hex decoder on a disposable host **user xattr**. They do not
mount NTFS on the host or prove NTFS persistence/native Windows access.

The installed libguestfs 1.52 debug API is internal and version dependent:
[API declaration](https://raw.githubusercontent.com/libguestfs/libguestfs/v1.52.0/generator/actions_debug.ml),
[appliance shell implementation](https://raw.githubusercontent.com/libguestfs/libguestfs/v1.52.0/daemon/debug.c).
Installed capability, static encoding inspection and native synthetic proof
must be repeated for a new implementation; unit tests alone are insufficient.

`CandidateInspection` is a separate read-only collector for a **new standalone
copy** of the allocated Server 2022 candidate B. Its host wrapper must bind the
original disk identity, cold host state, preservation records and separate
volume, then compare the copy to the candidate before and after inspection.
The collector cannot attach the candidate's existing path. It checks the exact
GPT/NTFS partition, read-only mount, three ancestor directories, reparse flags,
case collisions, destination absence and repeated metadata. It contains no
filesystem mutation or shell-command API. No guest file contents are read.

`acl_report.py` decodes bounded descriptor metadata for review. Unknown ACE types
remain opaque; malformed or unsupported layouts raise an error. The conservative
parser requires zero unused padding and distinguishes NULL and empty DACLs.
It does not calculate effective access or authorize placement. See Microsoft's
[ACL layout](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-acl)
and [basic ACE layout](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-access_allowed_ace).
Raw descriptors belong in private evidence; the operator report contains their
digests, owner/group SIDs, masks, flags and bounded ACE structure.

Additional checks:

```sh
.venv/bin/python -m pytest tests/python/host/test_windows_candidate_inspection.py tests/python/host/test_windows_acl_report.py -q --no-cov
```

Neither these mocked checks nor successful synthetic ACL persistence constitute
native Windows access, unattended Setup, or two-clone acceptance.

The read-only collector validates the complete installed `StatNS` (22 fields)
and `StatVFS` (11 fields) schemas. In particular, `st_spare1` through `st_spare6`
are legitimate numbered field names; they must not be rejected by a letters-only
filter. Missing, duplicate and unknown fields still stop inspection. See the
[libguestfs 1.52 struct declaration](https://raw.githubusercontent.com/libguestfs/libguestfs/v1.52.0/generator/structs.ml).
Tests use the full StatNS shape and check its names against the installed Perl
binding. Ancestry failures retain completed rows and bounded last-stat values,
with distinct stat, binding, attribute, ACL, lookup and repeat phases.

`CandidatePlacement` is an approval-gated preparation component for candidate B,
not part of normal runtime boot dispatch. Its separate host wrapper must bind a
successful copy audit, exact raw parent descriptors, rendered public XML, all
preserved assets and candidate-specific host access changes before attaching B.
The module requires exclusive empty-file creation, then the exact restricted
descriptor before writing. File identity, single-link status, ancestor ACLs and
payload are checked again after writing and after a read-only remount. Failures
retain partial state; it provides no overwrite, rollback or retry operation.
It does not define or boot Windows, stage a CD, change NVRAM, or register an image.

Its mocked mutation-order tests are:

```sh
.venv/bin/python -m pytest tests/python/host/test_windows_candidate_placement.py -q --no-cov
```

The outer approval must explicitly identify the existing candidate disk and any
necessary per-resource host ownership/traversal changes. Preparation and mocked
tests never authorize execution. Native Windows validation remains a separate gate.
