"""First-boot receipt and cached-answer gate, before runtime credentials."""

from __future__ import annotations

import base64
import gzip
import json
import re
from dataclasses import dataclass
from importlib.resources import files

from ..images.windows_bundle import PROFILE, WindowsPreparedBundle
from .qga_exec import run_guest_exec

_TARGET = "37892|S-1-5-18|S-1-5-32-544|0,0,2032127,S-1-5-18;0,0,2032127,S-1-5-32-544;"
_ASSETS = files("astinuslab.virtualization").joinpath("windows_assets")


def native_source(bundle: WindowsPreparedBundle) -> str:
    if bundle.profile != PROFILE or not re.fullmatch(r"[0-9a-f]{32}", bundle.nonce):
        raise ValueError("unreviewed Windows prepared guest profile")
    sources = [
        _ASSETS.joinpath(name).read_text(encoding="utf-8")
        for name in ("NativeAnswer.cs", "NativeAnswerRestriction.cs", "NativeReceipt.cs")
    ]
    source = "\n".join(sorted({line for text in sources for line in text.splitlines() if line.startswith("using ")}))
    source += "\n" + "\n".join(line for text in sources for line in text.splitlines() if not line.startswith("using "))
    return source.replace("__ASTINUS_NONCE__", bundle.nonce)


def build_prepared_guest_script(bundle: WindowsPreparedBundle, boot_id: str) -> str:
    if type(boot_id) is not str or not re.fullmatch(r"[1-9][0-9]{0,18}", boot_id) or int(boot_id) > 3155378975999999999:
        raise ValueError("Windows prepared guest requires the current boot identity")
    source = native_source(bundle)
    parents = json.loads(_ASSETS.joinpath("parent-acls-v1.json").read_text(encoding="utf-8"))
    # Only reviewed, packaged policy is interpolated; no guest/sidecar ACL input.
    if len(parents) != 3 or any(type(v) is not str or "'" in v or not v.isascii() for v in parents):
        raise ValueError("packaged Windows ACL profile invalid")
    if "\n'@" in source:
        raise ValueError("packaged Windows native source invalid")
    script = (
        "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'\nAdd-Type -TypeDefinition @'\n"
        + source
        + "\n'@ -ErrorAction Stop\n"
    )
    script += "\n".join(
        _ASSETS.joinpath(name).read_text(encoding="utf-8")
        for name in ("Setup.ps1", "Restrict-CachedAnswer.ps1", "PreparedGuest.ps1")
    )
    literal = "[string[]]@(" + ",".join("'" + value + "'" for value in parents) + ")"
    return (
        script
        + "\nInvoke-AstinusPreparedGate -ExpectedBoot '"
        + boot_id
        + "' -ParentAcls ("
        + literal
        + ") | ConvertTo-Json -Depth 4 -Compress\n"
    )


def script_command(script: str) -> str:
    """Bound both expanded bytes and the Windows command line; no guest file."""
    raw = script.encode("utf-8")
    if not 0 < len(raw) <= 65536:
        raise ValueError("Windows prepared guest script bound")
    data = base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
    wrapper = (
        "$ErrorActionPreference='Stop';try{$m=$null;$g=$null;try{"
        "$m=[IO.MemoryStream]::new([Convert]::FromBase64String('" + data + "'));"
        "$g=[IO.Compression.GZipStream]::new($m,[IO.Compression.CompressionMode]::Decompress);"
        "$b=[byte[]]::new(" + str(len(raw)) + ");$n=0;while($n -lt $b.Length){"
        "$r=$g.Read($b,$n,$b.Length-$n);if($r -le 0){throw 'short'};$n+=$r};"
        "if($g.ReadByte() -ne -1){throw 'long'};$s=[Text.UTF8Encoding]::new($false,$true).GetString($b)"
        "}finally{if($null -ne $g){$g.Dispose()};if($null -ne $m){$m.Dispose()}};"
        "& ([scriptblock]::Create($s))}catch{exit 1}"
    )
    # QGA receives an argv array, not a shell command. The fixed ASCII wrapper
    # contains only code-owned syntax, a decimal bound and base64 data. Passing
    # this as -Command avoids a second UTF-16/base64 expansion above 32K.
    # No external text or double quotes enter the wrapper itself.
    if not wrapper.isascii() or '"' in wrapper or len(wrapper) + 256 >= 32767:
        raise ValueError("Windows prepared guest command-line bound")
    return wrapper


@dataclass(frozen=True)
class WindowsPreparationProof:
    boot_id: str
    computer_name: str
    machine_sid_prefix: str
    answer_descriptor_hash: str


def prepare_windows_guest(
    libvirt_uri: str, vm_name: str, bundle: WindowsPreparedBundle, boot_id: str
) -> WindowsPreparationProof:
    script = build_prepared_guest_script(bundle, boot_id)
    result = run_guest_exec(
        libvirt_uri,
        vm_name,
        "powershell.exe",
        ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script_command(script)],
        timeout=60,
        discard_stderr=True,
    )
    phase = "result"
    try:
        if type(result.exitcode) is not int or result.exitcode != 0 or len(result.stdout.encode("utf-8")) > 4096:
            raise ValueError("process")

        def unique(items):
            value = dict(items)
            if len(value) != len(items):
                raise ValueError("duplicate")
            return value

        value = json.loads(result.stdout, object_pairs_hook=unique)
        if type(value) is not dict:
            raise ValueError("shape")
        if value.get("success") is False and value.get("phase") in {
            "context",
            "initialization",
            "answer",
            "initialization_after",
            "context_after",
        }:
            phase = value["phase"]
            raise ValueError("gate")
        expected = {
            "schema_version": 1,
            "scope": "prepared_windows_guest",
            "success": True,
            "phase": "complete",
            "boot_id": boot_id,
            "receipt_nonce": bundle.nonce,
            "answer_security": _TARGET,
            "answer_handles_closed": 8,
            "receipt_handles_closed": 10,
        }
        if set(value) != set(expected) | {
            "computer_name",
            "machine_sid_prefix",
            "answer_descriptor_hash",
            "mutation_attempted",
        }:
            raise ValueError("shape")
        if (
            any(type(value[k]) is not type(v) or value[k] != v for k, v in expected.items())
            or type(value["mutation_attempted"]) is not bool
        ):
            raise ValueError("evidence")
        patterns = {
            "computer_name": r"[A-Za-z0-9-]{1,15}",
            "machine_sid_prefix": r"S-1-5-21-(?:0|[1-9][0-9]{0,9})-(?:0|[1-9][0-9]{0,9})-(?:0|[1-9][0-9]{0,9})",
            "answer_descriptor_hash": r"[0-9a-f]{64}",
        }
        if any(type(value[k]) is not str or not re.fullmatch(p, value[k]) for k, p in patterns.items()):
            raise ValueError("identity")
        if any(int(v) > 2**32 - 1 for v in value["machine_sid_prefix"].split("-")[-3:]):
            raise ValueError("sid")
        return WindowsPreparationProof(
            boot_id, value["computer_name"], value["machine_sid_prefix"], value["answer_descriptor_hash"]
        )
    except (KeyError, TypeError, ValueError):
        raise RuntimeError(f"Windows prepared guest failed during {phase} for {vm_name}") from None
