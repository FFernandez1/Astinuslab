"""Preparation-only unattended Windows configuration; not runtime acceptance."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

_NS = "urn:schemas-microsoft-com:unattend"
_WCM = "http://schemas.microsoft.com/WMIConfig/2002/State"


@dataclass(frozen=True)
class UnattendedSetup:
    """Public configuration bytes only; no disk writer or VM launcher."""

    volume_label: str
    answer_file: bytes
    initializer: bytes


def validate_initialization_receipt(
    raw: str,
    *,
    nonce: str,
    account_enabled: bool,
    password_last_set_ticks: str,
) -> None:
    """Validate fixed evidence against separately observed post-Setup metadata.

    Caller must acquire the literal nonce-bound file safely after the Setup gate.
    This comparison is not a password read, entropy proof, or an attestation of
    the collector. A successful receipt alone must never enable runtime access.
    """

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("invalid initialization receipt")
            out[key] = value
        return out

    def invalid_constant(_):
        raise ValueError("invalid initialization receipt")

    try:
        if (
            type(raw) is not str
            or len(raw.encode("utf-8")) > 1024
            or type(nonce) is not str
            or not re.fullmatch(r"[0-9a-f]{32}", nonce)
            or type(account_enabled) is not bool
            or type(password_last_set_ticks) is not str
            or not re.fullmatch(r"[1-9][0-9]{0,18}", password_last_set_ticks)
            or int(password_last_set_ticks) > 3155378975999999999
        ):
            raise ValueError("invalid")
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
        expected = {
            "schema_version": 1,
            "nonce": nonce,
            "stage": "specialize",
            "initialized": True,
            "enabled": account_enabled,
            "password_last_set_ticks": password_last_set_ticks,
        }
        if (
            type(value) is not dict
            or set(value) != set(expected)
            or any(type(value[k]) is not type(v) or value[k] != v for k, v in expected.items())
        ):
            raise ValueError("invalid")
    except (ValueError, TypeError, RecursionError):
        raise ValueError("invalid initialization receipt") from None


def build_unattended_setup(
    *,
    nonce: str,
    locale: str,
    input_locale: str,
    terms_accepted: bool,
    allow_guest_password_initialization: bool,
) -> UnattendedSetup:
    """Render the approved Server 2022 experiment; never select it automatically.

    Callers must bind a fresh nonce and immutable read-only CD to a separately
    approved disposable candidate. XML schema and actual Setup pass discovery
    still require validation against that installation. These bytes establish
    neither image provenance nor unattended acceptance.
    """
    if terms_accepted is not True or allow_guest_password_initialization is not True:
        raise ValueError("unattended setup mechanism approval required")
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise ValueError("invalid setup nonce")
    if locale != "en-US" or input_locale != "0409:00000409":
        raise ValueError("unsupported setup regional profile")
    # The full nonce is bound by the code digest; label collisions fail closed.
    label = "AL" + nonce[:12].upper()
    script = _INITIALIZER.replace("__NONCE__", nonce).replace("__LABEL__", label).encode("utf-8")
    digest = hashlib.sha256(script).hexdigest().upper()
    command = (
        'powershell.exe -nop -noni -c "'
        f"$v=@(Get-Volume|? FileSystemLabel -ceq '{label}');"
        "if($v.Count-ne1){exit 1};$p=$v.DriveLetter+':\\s.ps1';"
        f"if((Get-FileHash $p).Hash-ceq'{digest}')"
        '{& $p}else{exit 1}"'
    )
    if len(command) > 259:
        raise ValueError("setup command exceeds Windows limit")
    root = ET.Element("unattend", {"xmlns": _NS, "xmlns:wcm": _WCM})

    def component(parent, name):
        return ET.SubElement(
            parent,
            "component",
            {
                "name": name,
                "processorArchitecture": "amd64",
                "publicKeyToken": "31bf3856ad364e35",
                "language": "neutral",
                "versionScope": "nonSxS",
            },
        )

    specialize = ET.SubElement(root, "settings", {"pass": "specialize"})
    deployment = component(specialize, "Microsoft-Windows-Deployment")
    sync = ET.SubElement(deployment, "RunSynchronous")
    cmd = ET.SubElement(sync, "RunSynchronousCommand", {"wcm:action": "add"})
    for key, value in [("Order", "1"), ("Path", command), ("WillReboot", "Never")]:
        ET.SubElement(cmd, key).text = value
    oobe = ET.SubElement(root, "settings", {"pass": "oobeSystem"})
    international = component(oobe, "Microsoft-Windows-International-Core")
    for key, value in [
        ("InputLocale", input_locale),
        ("SystemLocale", locale),
        ("UILanguage", locale),
        ("UserLocale", locale),
    ]:
        ET.SubElement(international, key).text = value
    shell = component(oobe, "Microsoft-Windows-Shell-Setup")
    pages = ET.SubElement(shell, "OOBE")
    ET.SubElement(pages, "HideEULAPage").text = "true"
    ET.SubElement(pages, "HideLocalAccountScreen").text = "true"
    return UnattendedSetup(label, ET.tostring(root, encoding="utf-8", xml_declaration=True), script)


_INITIALIZER = r"""$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version 2

function New-AstinusSecret {
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] 64
    $secret = $null
    $alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
    try {
        for ($round = 0; $round -lt 32; $round++) {
            $rng.GetBytes($bytes)
            $groups = 0
            for ($i = 0; $i -lt 64; $i++) {
                $bytes[$i] = $bytes[$i] -band 63
                $n = $bytes[$i]
                if ($n -lt 26) { $groups = $groups -bor 1 }
                elseif ($n -lt 52) { $groups = $groups -bor 2 }
                elseif ($n -lt 62) { $groups = $groups -bor 4 }
                else { $groups = $groups -bor 8 }
            }
            if ($groups -ne 15) { continue }
            $secret = New-Object Security.SecureString
            for ($i = 0; $i -lt 64; $i++) { $secret.AppendChar($alphabet[$bytes[$i]]) }
            $secret.MakeReadOnly()
            return $secret
        }
        throw 'random_budget'
    } catch {
        if ($null -ne $secret) { $secret.Dispose() }
        throw 'credential_generation'
    } finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
        $rng.Dispose()
    }
}

function Get-AstinusSetupAccount {
    param([string]$Label)
    if (-not [Environment]::Is64BitProcess -or
        [Security.Principal.WindowsIdentity]::GetCurrent().User.Value -cne 'S-1-5-18') {
        throw 'principal'
    }
    $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
    $computer = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop
    if ($os.BuildNumber -cne '20348' -or $os.ProductType -ne 3 -or
        $computer.PartOfDomain -or $computer.DomainRole -ne 2) { throw 'server_profile' }
    $setup = Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\Setup' -Name SystemSetupInProgress -ErrorAction Stop
    if ($setup.SystemSetupInProgress -ne 1) { throw 'setup_phase' }
    $ssh = @(Get-CimInstance -ClassName Win32_Service -Filter "Name='sshd'" -ErrorAction Stop)
    $qga = @(Get-CimInstance -ClassName Win32_Service -Filter "Name='QEMU-GA'" -ErrorAction Stop)
    if ($ssh.Count -ne 1 -or $ssh[0].State -cne 'Stopped' -or $ssh[0].StartMode -cne 'Disabled' -or
        $qga.Count -ne 1 -or $qga[0].State -cne 'Running' -or $qga[0].StartMode -cne 'Auto') {
        throw 'services'
    }
    $volumes = @(Get-Volume -ErrorAction Stop | Where-Object FileSystemLabel -CEQ $Label)
    if ($volumes.Count -ne 1 -or [string]$volumes[0].DriveType -cne 'CD-ROM' -or
        [string]$volumes[0].DriveLetter -cnotmatch '^[A-Z]$') { throw 'medium' }
    # Do not query password values, SAM/SECURITY, user profiles or guest logs.
    $accounts = @(Get-LocalUser -ErrorAction Stop | Where-Object { $_.SID.Value -match '-500$' })
    if ($accounts.Count -ne 1 -or $accounts[0].PrincipalSource -ne 'Local') { throw 'local_account' }
    return $accounts[0]
}

function Assert-AstinusDirectory {
    param([string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'directory_ancestry'
    }
}

function Write-AstinusNewRecord {
    param([string]$Path, [string]$Json)
    $raw = [Text.Encoding]::UTF8.GetBytes($Json)
    $file = $null
    try {
        $file = [IO.FileStream]::new($Path, [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write, [IO.FileShare]::None, 4096, [IO.FileOptions]::WriteThrough)
        $file.Write($raw, 0, $raw.Length)
        $file.Flush($true)
    } finally {
        if ($null -ne $file) { $file.Dispose() }
    }
}

function New-AstinusAttempt {
    param([string]$Nonce)
    if ($Nonce -cnotmatch '^[0-9a-f]{32}$' -or $env:ProgramData -cne 'C:\ProgramData') { throw 'attempt_path' }
    Assert-AstinusDirectory 'C:\'
    Assert-AstinusDirectory 'C:\ProgramData'
    $root = 'C:\ProgramData\AstinusLabSetup-' + $Nonce
    # No -Force: an existing directory consumes the attempt, including partial failures.
    $null = New-Item -Path $root -ItemType Directory -ErrorAction Stop
    Assert-AstinusDirectory $root
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $admins = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $acl.SetOwner($system)
    foreach ($sid in @($system, $admins)) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl',
            'ContainerInherit, ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $root -AclObject $acl -ErrorAction Stop
    Assert-AstinusDirectory $root
    Write-AstinusNewRecord -Path ($root + '\attempt.json') -Json ('{"nonce":"' + $Nonce + '"}')
    return $root
}

function Write-AstinusReceipt {
    param([string]$Root, [string]$Nonce, $Account)
    Assert-AstinusDirectory $Root
    $record = [ordered]@{schema_version=1; nonce=$Nonce; stage='specialize';
        initialized=$true; enabled=[bool]$Account.Enabled;
        password_last_set_ticks=$Account.PasswordLastSet.ToUniversalTime().Ticks.ToString()}
    Write-AstinusNewRecord -Path ($Root + '\initialized.json') -Json ($record | ConvertTo-Json -Compress)
}

function Invoke-AstinusInitialization {
    param([string]$Nonce, [string]$Label)
    $phase = 'context'
    $secret = $null
    try {
        $account = Get-AstinusSetupAccount -Label $Label
        $phase = 'claim'
        $root = New-AstinusAttempt -Nonce $Nonce
        $phase = 'credential'
        $secret = New-AstinusSecret
        Set-LocalUser -SID $account.SID -Password $secret -ErrorAction Stop
        $secret.Dispose()
        $secret = $null
        $phase = 'verify'
        $after = Get-LocalUser -SID $account.SID -ErrorAction Stop
        if ($after.SID -ne $account.SID -or $after.Enabled -ne $account.Enabled -or
            $after.PasswordRequired -ne $true -or $null -eq $after.PasswordLastSet) { throw 'account_metadata' }
        $phase = 'receipt'
        Write-AstinusReceipt -Root $root -Nonce $Nonce -Account $after
        return [ordered]@{schema_version=1; nonce=$Nonce; success=$true; phase='complete'}
    } catch {
        return [ordered]@{schema_version=1; nonce=$Nonce; success=$false; phase=$phase}
    } finally {
        if ($null -ne $secret) { $secret.Dispose() }
    }
}

$result = Invoke-AstinusInitialization -Nonce '__NONCE__' -Label '__LABEL__'
$result | ConvertTo-Json -Compress
if (-not $result.success) { exit 1 }
exit 0
"""
