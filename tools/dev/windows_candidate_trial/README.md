# Operator-prepared Windows runtime trials

`cold_pair.py` supplies failure-aware ordering for preparing two independent cold
runtime clones. `cold_state.py` binds the exact inactive libvirt inventory and
the reviewed definitions. The Python modules are side-effect free until the caller
invokes them with a host adapter or connection.

The private, candidate-specific adapter supplies approval, one-use markers,
descriptor-bound storage, historical evidence, protected resource checks, fresh
OVMF variables, byte-verified setup media, and bounded image-tool operations.
The adapter must finish both logical copy comparisons before defining either VM.
No source mutation, Windows boot, guest operation, cleanup, image registration,
or acceptance claim is provided by the cold preparation modules.

`firstboot_pair.py` sequences two separately supervised first boots. Clone B
cannot start until clone A has passed observation and clean shutdown and its
completion has been recorded. It compares machine SID prefixes, records whether
the initial computer names differ, and keeps reusable-image acceptance false.
The private adapter binds the actual cold-pair result, source digest, clone
resource identities, inactive definitions, setup media, and protected assets.

`NativeAnswer.cs` provides a Windows metadata reader for the fixed
`C:\Windows\Panther\unattend.xml` ancestry. It holds parent handles, opens single
components relative to those handles without following reparse points, and
checks identity, type and exact expected owner/group/ordered DACL fields twice.
It queries no file contents or SACL and changes no privileges. A missing final
leaf is recorded as absence with `LeafAclVerified=false`; unknown parent, open,
ACL, mutation and closure failures stop the check. The Linux PowerShell tests
execute its C# policy with injected APIs; they do not prove native Windows API
behavior. Expected descriptors must come from the reviewed candidate evidence.

On the measured Server 2022 guest, combining `FILE_DIRECTORY_FILE` with the
reader's no-recall/reparse options returns `STATUS_INVALID_PARAMETER`. Parent
opens retain the other flags and validate the held handle's directory type
before opening a child. Native receipt checks passed with this correction;
the answer reader then correctly rejected a post-Setup leaf DACL that granted
read access to additional groups. This observation does not relax the intended
SYSTEM/Administrators ACL or establish reusable-image acceptance.

`guarded_shutdown.py` supports stopping an already-running failed trial after
two fresh same-boot completed-Setup observations. Its adapter must bind the
specific domain/resources, confirm there are no uncertain probe processes, and
durably claim the single nonforced shutdown. Probe failures remain separate from
the final VM snapshot. It never starts a VM or treats successful shutdown as
image acceptance.

`COLD_RUNTIME_PAIR_PREPARED` means two cold copies and their definitions were
prepared. Native unattended Setup, identity/SID independence, strict SSH,
restart persistence, and resource-specific teardown still require separate
evidence. The supported operator path for Windows images is
`docs/user/WINDOWS_IMAGES.md`.
# Post-Setup cached-answer security experiment

`NativeAnswerRestriction.cs` and `Restrict-CachedAnswer.ps1` implement a separate,
explicit post-Setup trial. They are not automatically invoked by runtime boot.
The measured Server 2022 cached-file ACL or exact offline descriptor must match
before one handle-based `SetSecurityInfo` call can establish the native protected
SYSTEM/Admin descriptor. An exact native target is verified without a write;
absence and any other descriptor fail. The read-only guard still compares every
expected field exactly. Held-handle verification and a fresh read-only reopen
must both pass.

The leaf alone requests WRITE_DAC/WRITE_OWNER. Fixed ancestry, exact parent
ACLs, reparse/recall/type/volume/link/identity checks and metadata resampling
precede mutation. Parent handles exclude delete sharing; the leaf excludes
data-write/delete sharing. The helper reads no file contents, adjusts no token
privileges or SACL, and never deletes the cached answer. Setup, boot identity,
SYSTEM, Server 2022/workgroup, disabled SSH and absence of other probes are
checked around the operation. Native verification requires the single exact
reviewed post-set descriptor; other control values remain failures.

The injected C# tests execute under PowerShell and distinguish precondition,
setter, held verification, closure and reopen failures. They are not native
Windows proof. A candidate-specific reviewed operation must still demonstrate
the setter on Windows and read-only persistence after a new boot. This stage
must precede runtime credentials; it does not complete two-clone acceptance.

The first native restriction trial returned setter success but failed the exact
post-write control comparison. A separate read-only probe measured `0x9404`
against the requested `0x9004`: owner/group and both explicit ACEs match, but
`SE_DACL_AUTO_INHERITED` remains set. `SetSecurityInfo` receives SID/ACL pointers
and security-information flags, not the source descriptor's control header.
Microsoft documents the [native inheritance model](https://learn.microsoft.com/en-us/windows/win32/secauthz/automatic-propagation-of-inheritable-aces)
and its [control bits](https://learn.microsoft.com/en-us/windows/win32/secauthz/security-descriptor-control).
Following a tools-disabled Claude consultation and code/documentation review,
native post-set verification now requires exactly `0x9404`, SYSTEM owner,
Administrators group, and two ordered explicit full-control ALLOW ACEs for those
identities. No control bits are masked. The native decoder still rejects other
ACL revisions and nonbasic/callback/deny ACEs. Independent negative tests cover
control, owner/group, mask, flags, identity, count and order at each boundary.
The offline setter and its exact 100-byte `0x9004` proof remain unchanged;
`0x9004` is not accepted as a native post-set result. Neither path makes a SACL
claim. This is the measured Server 2022 contract, not a universal Windows API
normalization guarantee. The consumed native setter must not be retried.

`ObservedSecurity` retains the canonical descriptor on validation failures.
Native reboot persistence remains unproven. A new candidate-specific read-only
A reboot/check/clean-shutdown procedure is prepared; see the latest checkpoint
for its review and separate boot-approval boundary. No automatic runtime hook
or image-acceptance claim is introduced by this contract correction.
