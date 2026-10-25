from __future__ import annotations

import base64
import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from .qga_exec import GuestExecError as GuestExecError
from .qga_exec import GuestExecResult, run_guest_exec
from .windows_readiness import probe_windows_agent

_COMPUTER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,15}$")
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_BOOT_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_HOST_FINGERPRINT_RE = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")

# Use the same UTC tick representation before and after the scheduled reboot.
_BOOT_ID_SCRIPT = "$bootId = (Get-CimInstance -ClassName Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().Ticks.ToString([Globalization.CultureInfo]::InvariantCulture)"
_IDENTITY_SCRIPT = (
    "$ErrorActionPreference = 'Stop'\n" + _BOOT_ID_SCRIPT + "\n"
    "[pscustomobject]@{computer_name = [Environment]::MachineName; boot_id = $bootId} | ConvertTo-Json -Compress"
)

# Fixed Setup metadata only. No account secrets, file contents or guest logs.
_SETUP_SCRIPT = r"""$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$base = $null; $state = $null; $setup = $null
try {
    $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
        [Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryView]::Registry64)
    $state = $base.OpenSubKey('SOFTWARE\Microsoft\Windows\CurrentVersion\Setup\State', $false)
    $setup = $base.OpenSubKey('SYSTEM\Setup', $false)
    if ($null -eq $state -or $null -eq $setup) { throw 'missing_setup' }
    if ($state.GetValueKind('ImageState') -ne [Microsoft.Win32.RegistryValueKind]::String -or
        $setup.GetValueKind('SystemSetupInProgress') -ne [Microsoft.Win32.RegistryValueKind]::DWord -or
        $setup.GetValueKind('OOBEInProgress') -ne [Microsoft.Win32.RegistryValueKind]::DWord) {
        throw 'setup_value_kind'
    }
    $boot = (Get-CimInstance -ClassName Win32_OperatingSystem).LastBootUpTime.ToUniversalTime()
    $result = [pscustomobject]@{
        image_state = $state.GetValue('ImageState')
        system_setup_in_progress = $setup.GetValue('SystemSetupInProgress')
        oobe_in_progress = $setup.GetValue('OOBEInProgress')
        execution_is_system = ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ceq 'S-1-5-18')
        boot_id = $boot.Ticks.ToString([Globalization.CultureInfo]::InvariantCulture)
        uptime_seconds = [long][Math]::Floor(([DateTime]::UtcNow - $boot).TotalSeconds)
    }
    $result | ConvertTo-Json -Compress
} catch {
    Write-Output '{"query_failed":true}'
    exit 1
} finally {
    if ($null -ne $setup) { $setup.Dispose() }
    if ($null -ne $state) { $state.Dispose() }
    if ($null -ne $base) { $base.Dispose() }
}
"""


def _parse_setup_observation(raw: str) -> dict:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate")
            value[key] = item
        return value

    def invalid_constant(_):
        raise ValueError("nonfinite")

    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 4096:
        raise ValueError("size")
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if type(value) is not dict or set(value) != {
        "image_state",
        "system_setup_in_progress",
        "oobe_in_progress",
        "execution_is_system",
        "boot_id",
        "uptime_seconds",
    }:
        raise ValueError("shape")
    if value["image_state"] not in (
        "IMAGE_STATE_COMPLETE",
        "IMAGE_STATE_UNDEPLOYABLE",
        "IMAGE_STATE_GENERALIZE_RESEAL_TO_OOBE",
        "IMAGE_STATE_SPECIALIZE_RESEAL_TO_OOBE",
    ):
        raise ValueError("state")
    if any(
        type(value[key]) is not int or value[key] not in (0, 1)
        for key in ("system_setup_in_progress", "oobe_in_progress")
    ):
        raise ValueError("flags")
    if value["execution_is_system"] is not True:
        raise ValueError("principal")
    if (
        not isinstance(value["boot_id"], str)
        or not _BOOT_ID_RE.fullmatch(value["boot_id"])
        or int(value["boot_id"]) > 3155378975999999999
        or type(value["uptime_seconds"]) is not int
        or not 0 <= value["uptime_seconds"] <= 315360000
    ):
        raise ValueError("boot_metadata")
    return value


def wait_for_windows_setup(
    libvirt_uri: str,
    vm_name: str,
    *,
    cancel_requested: Callable[[], bool],
    max_wait_seconds: int = 300,
    poll_interval_seconds: int = 2,
    ready_stability_seconds: int = 30,
    time_module=time,
) -> str:
    """Wait for two complete Setup observations on the same boot before mutation.

    QGA availability during OOBE is not completion. This gate never completes
    OOBE itself, changes an account, reads credentials, or returns guest output.
    Require continuous harmless readiness pings before observing an unfinished
    Setup boot: its agent can start shortly before an automatic reboot.
    """
    if max_wait_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("Windows setup wait intervals must be positive")
    if ready_stability_seconds < 0:
        raise ValueError("Windows setup readiness stability must be non-negative")
    encoded = base64.b64encode(_SETUP_SCRIPT.encode("utf-16le")).decode("ascii")
    deadline = time_module.monotonic() + max_wait_seconds
    previous = None
    ready_since = None
    while time_module.monotonic() < deadline:
        if cancel_requested():
            raise RuntimeError("cancel_requested")
        remaining = deadline - time_module.monotonic()
        if remaining <= 0:
            break
        ready = probe_windows_agent(libvirt_uri, vm_name, timeout=min(5, math.ceil(remaining)))
        if cancel_requested():
            raise RuntimeError("cancel_requested")
        remaining = deadline - time_module.monotonic()
        if remaining <= 0:
            break
        if not ready:
            previous = None
            ready_since = None
            time_module.sleep(min(poll_interval_seconds, remaining))
            continue
        if ready_since is None:
            ready_since = time_module.monotonic()
        if previous is None and time_module.monotonic() - ready_since < ready_stability_seconds:
            time_module.sleep(min(poll_interval_seconds, remaining))
            continue
        # A failed guest-exec can leave a live process even when no PID was
        # returned. Only readiness pings may be retried across Setup/reboot.
        result = run_guest_exec(
            libvirt_uri,
            vm_name,
            "powershell.exe",
            ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            timeout=min(15, math.ceil(remaining)),
            discard_stderr=True,
        )
        try:
            if type(result.exitcode) is not int or result.exitcode != 0:
                raise ValueError("query")
            current = _parse_setup_observation(result.stdout)
        except (ValueError, TypeError):
            raise RuntimeError(f"Windows Setup observation invalid for {vm_name}") from None
        if cancel_requested():
            raise RuntimeError("cancel_requested")
        remaining = deadline - time_module.monotonic()
        if remaining <= 0:
            break
        complete = current is not None and (
            current["image_state"] == "IMAGE_STATE_COMPLETE"
            and current["system_setup_in_progress"] == 0
            and current["oobe_in_progress"] == 0
        )
        if complete and current is not None:
            if (
                previous is not None
                and current["boot_id"] == previous["boot_id"]
                and current["uptime_seconds"] > previous["uptime_seconds"]
            ):
                return current["boot_id"]
            previous = current
        else:
            previous = None
            ready_since = None
        time_module.sleep(min(poll_interval_seconds, remaining))
    raise RuntimeError(f"Windows Setup did not complete for {vm_name}; unattended setup is not established")


@dataclass(frozen=True)
class WindowsBootstrapResult:
    computer_name: str
    ssh_host_key_fingerprint: str
    reboot_required: bool
    boot_id: str


def build_windows_bootstrap_script(
    public_key_b64: str, computer_name: str, *, expected_boot_id: str | None = None
) -> str:
    """Render key-only bootstrap; each invocation rotates and activates host keys.

    Sequential retries repeat setup and return the newly activated fingerprint.
    They must finish before the caller pins the result for SSH access.
    """
    if not _COMPUTER_NAME_RE.fullmatch(computer_name):
        raise ValueError("invalid computer name")
    if expected_boot_id is not None and (
        type(expected_boot_id) is not str or not _BOOT_ID_RE.fullmatch(expected_boot_id)
    ):
        raise ValueError("invalid expected boot identity")
    if not _BASE64_RE.fullmatch(public_key_b64):
        raise ValueError("public key must be base64 data")
    try:
        base64.b64decode(public_key_b64, validate=True)
    except ValueError as exc:
        raise ValueError("public key must be base64 data") from exc

    script = r"""$ErrorActionPreference = 'Stop'
__ASTINUS_BOOT_ID_SCRIPT__
__ASTINUS_EXPECTED_BOOT__
$publicKeyB64 = '__ASTINUS_PUBLIC_KEY_B64__'
$desiredComputerName = '__ASTINUS_COMPUTER_NAME__'
$publicKey = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($publicKeyB64)).Trim()
if ($publicKey -notmatch '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256)\s+') { throw 'invalid public key data' }

$sshRoot = 'C:\ProgramData\ssh'
$authorizedKeyPath = 'C:\ProgramData\ssh\administrators_authorized_keys'
$keygen = Join-Path $env:WINDIR 'System32\OpenSSH\ssh-keygen.exe'
Set-Service -Name sshd -StartupType Automatic
Stop-Service -Name sshd -ErrorAction Stop
if ((Get-Service -Name sshd).Status -ne 'Stopped') { throw 'sshd did not stop' }
New-Item -ItemType Directory -Path $sshRoot -Force | Out-Null
[IO.File]::WriteAllText($authorizedKeyPath, $publicKey + "`r`n", [Text.Encoding]::ASCII)

$acl = New-Object System.Security.AccessControl.FileSecurity
$acl.SetAccessRuleProtection($true, $false)
$systemRule = New-Object System.Security.AccessControl.FileSystemAccessRule('NT AUTHORITY\SYSTEM', 'FullControl', 'Allow')
$administratorsRule = New-Object System.Security.AccessControl.FileSystemAccessRule('BUILTIN\Administrators', 'FullControl', 'Allow')
$acl.SetAccessRule($systemRule)
$acl.SetAccessRule($administratorsRule)
Set-Acl -LiteralPath $authorizedKeyPath -AclObject $acl

Get-ChildItem -LiteralPath $sshRoot -Filter 'ssh_host_*' -File -Force | Remove-Item -Force
& $keygen -A | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'host key generation failed' }
$fingerprintLines = @(& $keygen -lf (Join-Path $sshRoot 'ssh_host_ed25519_key.pub') -E sha256)
if ($LASTEXITCODE -ne 0 -or $fingerprintLines.Count -ne 1) { throw 'host key fingerprint query failed' }
$fingerprintMatch = [regex]::Match($fingerprintLines[0], '^256\s+(SHA256:[A-Za-z0-9+/]{43})\s+.*\(ED25519\)$')
if (-not $fingerprintMatch.Success) { throw 'invalid ED25519 host key fingerprint' }
$fingerprint = $fingerprintMatch.Groups[1].Value
Start-Service -Name sshd -ErrorAction Stop
if ((Get-Service -Name sshd).Status -ne 'Running') { throw 'sshd did not start' }

$rebootRequired = $env:COMPUTERNAME -cne $desiredComputerName
if ($rebootRequired) {
    Rename-Computer -NewName $desiredComputerName -Force
}
$result = [pscustomobject]@{
    computer_name = $desiredComputerName
    ssh_host_key_fingerprint = $fingerprint
    reboot_required = [bool]$rebootRequired
    boot_id = $bootId
}
if ($rebootRequired) {
    shutdown.exe /r /t 5 /d p:4:1 /c 'AstinusLab runtime identity change' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'runtime reboot request failed' }
}
$result | ConvertTo-Json -Compress
"""
    return (
        script.replace("__ASTINUS_PUBLIC_KEY_B64__", public_key_b64)
        .replace("__ASTINUS_COMPUTER_NAME__", computer_name)
        .replace("__ASTINUS_BOOT_ID_SCRIPT__", _BOOT_ID_SCRIPT)
        .replace(
            "__ASTINUS_EXPECTED_BOOT__",
            ""
            if expected_boot_id is None
            else f"if ($bootId -cne '{expected_boot_id}') {{ throw 'preparation boot changed' }}",
        )
    )


def bootstrap_windows_guest(
    libvirt_uri: str,
    vm_name: str,
    public_key: str,
    computer_name: str,
    *,
    expected_boot_id: str | None = None,
) -> WindowsBootstrapResult:
    """Install one runtime key and identity through QGA."""
    public_key_b64 = base64.b64encode(public_key.encode("utf-8")).decode("ascii")
    script = build_windows_bootstrap_script(public_key_b64, computer_name, expected_boot_id=expected_boot_id)
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result: GuestExecResult = run_guest_exec(
        libvirt_uri,
        vm_name,
        "powershell.exe",
        ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded_script],
    )
    if result.exitcode != 0:
        raise RuntimeError(f"Windows bootstrap failed for {vm_name} during powershell")
    try:
        payload = json.loads(result.stdout.strip())
        fingerprint = payload["ssh_host_key_fingerprint"]
        returned_name = payload["computer_name"]
        reboot_required = payload["reboot_required"]
        boot_id = payload["boot_id"]
    except (KeyError, TypeError, ValueError):
        raise RuntimeError(f"Windows bootstrap failed for {vm_name} during result") from None
    if not isinstance(returned_name, str) or not isinstance(fingerprint, str) or not isinstance(reboot_required, bool):
        raise RuntimeError(f"Windows bootstrap failed for {vm_name} during result")
    if (
        returned_name.casefold() != computer_name.casefold()
        or not _HOST_FINGERPRINT_RE.fullmatch(fingerprint)
        or not isinstance(boot_id, str)
        or not _BOOT_ID_RE.fullmatch(boot_id)
    ):
        raise RuntimeError(f"Windows bootstrap failed for {vm_name} during result")
    return WindowsBootstrapResult(returned_name, fingerprint, reboot_required, boot_id)


def wait_for_windows_identity(
    libvirt_uri: str,
    vm_name: str,
    bootstrap: WindowsBootstrapResult,
    *,
    cancel_requested: Callable[[], bool],
    max_wait_seconds: int = 300,
    poll_interval_seconds: int = 2,
    time_module=time,
) -> None:
    """Require the active name and, when requested, a changed boot identity.

    A successful QGA ping (or an observed outage) alone is not reboot proof.
    Queries are read-only, with a shared deadline and no guest output in errors.
    """
    if max_wait_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("Windows identity wait intervals must be positive")
    if not isinstance(bootstrap.boot_id, str) or not _BOOT_ID_RE.fullmatch(bootstrap.boot_id):
        raise ValueError("Windows identity verification requires the original boot identity")
    encoded_script = base64.b64encode(_IDENTITY_SCRIPT.encode("utf-16le")).decode("ascii")
    deadline = time_module.monotonic() + max_wait_seconds
    while time_module.monotonic() < deadline:
        if cancel_requested():
            raise RuntimeError("cancel_requested")
        remaining = deadline - time_module.monotonic()
        if remaining <= 0:
            break
        identity = None
        ready = probe_windows_agent(libvirt_uri, vm_name, timeout=min(5, math.ceil(remaining)))
        if cancel_requested():
            raise RuntimeError("cancel_requested")
        remaining = deadline - time_module.monotonic()
        if remaining <= 0:
            break
        if not ready:
            time_module.sleep(min(poll_interval_seconds, remaining))
            continue
        try:
            result = run_guest_exec(
                libvirt_uri,
                vm_name,
                "powershell.exe",
                ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script],
                timeout=min(15, math.ceil(remaining)),
            )
            if result.exitcode == 0:
                payload = json.loads(result.stdout)
                name, boot_id = payload["computer_name"], payload["boot_id"]
                if (
                    isinstance(name, str)
                    and _COMPUTER_NAME_RE.fullmatch(name)
                    and isinstance(boot_id, str)
                    and _BOOT_ID_RE.fullmatch(boot_id)
                ):
                    identity = (name, boot_id)
        except (KeyError, TypeError, ValueError):
            # Only a confirmed, exited observation may be repeated. Transport
            # errors propagate, retaining their phase and stopping new probes.
            pass
        if cancel_requested():
            raise RuntimeError("cancel_requested")
        remaining = deadline - time_module.monotonic()
        if remaining <= 0:
            break
        if identity is not None:
            name, boot_id = identity
            if name.casefold() == bootstrap.computer_name.casefold() and (
                not bootstrap.reboot_required or boot_id != bootstrap.boot_id
            ):
                return
        time_module.sleep(min(poll_interval_seconds, remaining))
    raise RuntimeError(f"Windows identity verification timed out for {vm_name}")
