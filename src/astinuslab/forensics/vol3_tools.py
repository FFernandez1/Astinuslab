"""Readiness checks for operator-supplied Volatility tooling."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from packaging.version import InvalidVersion, Version

from ..core.config import Settings, settings

logger = logging.getLogger(__name__)

# Changes require automated regression coverage and fresh real-host evidence.
#
# Two distinct thresholds, deliberately not one.  MINIMUM_VOLATILITY_VERSION is a
# hard floor: below it the tool is refused and nothing runs.  TESTED_VOLATILITY_VERSIONS
# is the narrower list this release was actually validated against.  A version at or
# above the floor but outside the tested set still runs, flagged `untested`, so the
# day upstream ships a new release every install keeps working instead of refusing
# every job — with the caveat carried all the way to the operator.
MINIMUM_VOLATILITY_VERSION = "2.28.0"
TESTED_VOLATILITY_VERSIONS = frozenset({"2.28.0"})

_VOLATILITY_BANNER = re.compile(r"Volatility 3 Framework (?P<version>\d+\.\d+\.\d+[A-Za-z0-9._+-]{0,64})(?=\s|$)")
_HASH_CHUNK_SIZE = 1024 * 1024
_DIAGNOSTIC_LIMIT = 2048
_MAX_VERSION_TOKEN_LENGTH = 80


def preferred_tested_volatility_version() -> str:
    """Return the newest Volatility version validated for this release."""
    return max(TESTED_VOLATILITY_VERSIONS, key=Version)


@dataclass(frozen=True)
class ToolStatus:
    ready: bool
    code: str
    detail: str
    version: str | None = None
    untested: bool = False
    resolved_path: Path | None = field(default=None, repr=False)

    def public_dict(self) -> dict[str, bool | str | None]:
        return {
            "ready": self.ready,
            "code": self.code,
            "detail": self.detail,
            "version": self.version,
            "untested": self.untested,
        }


@dataclass(frozen=True)
class Vol3Readiness:
    enabled: bool
    analysis: ToolStatus
    isf_build: ToolStatus

    def public_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "analysis": self.analysis.public_dict(),
            "isf_build": self.isf_build.public_dict(),
        }


class ToolDependencyError(RuntimeError):
    """A safe internal dependency failure with a stable machine code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _disabled_status() -> ToolStatus:
    return ToolStatus(
        ready=False,
        code="feature_disabled",
        detail="Volatility integration is disabled.",
    )


def _resolve_volatility_executable(configured: str | Path | None) -> ToolStatus | Path:
    if configured is None or not str(configured).strip():
        return ToolStatus(
            ready=False,
            code="volatility_not_configured",
            detail="Configure [vol3].volatility_bin to enable Volatility analysis.",
        )

    value = str(configured).strip()
    if isinstance(configured, Path) or os.sep in value:
        resolved = Path(value).expanduser()
        if not resolved.exists():
            return ToolStatus(
                ready=False,
                code="volatility_not_found",
                detail="The configured Volatility executable was not found.",
            )
    else:
        found = shutil.which(value)
        if found is None:
            return ToolStatus(
                ready=False,
                code="volatility_not_found",
                detail="The configured Volatility executable was not found.",
            )
        resolved = Path(found)

    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return ToolStatus(
            ready=False,
            code="volatility_not_executable",
            detail="The configured Volatility path is not an executable file.",
        )
    return resolved


def _bounded(value: object) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text[:_DIAGNOSTIC_LIMIT]


def _version_status(version: str, resolved: Path) -> ToolStatus:
    """Rule on a probed banner version: refuse it, clear it, or clear it with a caveat.

    Ordering uses `packaging.version.Version`, never string comparison, which sorts
    `2.9.0` above `2.28.0` and would clear a tool three years too old.

    Two deliberate edge decisions:

    * A pre-release sorts *below* its own final release under PEP 440, so `2.28.0rc1`
      lands under the floor and is refused.  A release candidate of the minimum is
      not the minimum, and nothing about the floor was validated against it.  A
      pre-release of a *later* version (`2.29.0rc1`) is above the floor and is
      cleared as untested, like any other unvalidated version.
    * A string `Version()` cannot parse is refused rather than waved through: a
      version that cannot be compared cannot be cleared against the floor.  The
      probe itself succeeded, so this is a version verdict (`..._version_unsupported`)
      and not a probe failure, and the offending string is returned so the operator
      can see what the tool actually reported.
    """
    try:
        parsed = Version(version)
    except InvalidVersion:
        logger.warning("Volatility reported an uninterpretable version %s for %s", _bounded(version), resolved)
        return ToolStatus(
            ready=False,
            code="volatility_version_unsupported",
            detail=(
                f"Volatility version {version} could not be interpreted; "
                f"install version {MINIMUM_VOLATILITY_VERSION} or newer."
            ),
            version=version,
        )

    if parsed < Version(MINIMUM_VOLATILITY_VERSION):
        return ToolStatus(
            ready=False,
            code="volatility_version_unsupported",
            detail=(
                f"Volatility version {version} is unsupported; install version {MINIMUM_VOLATILITY_VERSION} or newer."
            ),
            version=version,
        )

    if version in TESTED_VOLATILITY_VERSIONS:
        return ToolStatus(
            ready=True,
            code="ready",
            detail="Volatility 3 is ready.",
            version=version,
            resolved_path=resolved,
        )

    return ToolStatus(
        ready=True,
        code="ready",
        detail=(
            f"Volatility {version} is newer than the versions tested with this release; "
            "analysis will run, but results have not been validated against this version."
        ),
        version=version,
        untested=True,
        resolved_path=resolved,
    )


def check_volatility_tool(settings_obj: Settings = settings, *, timeout_seconds: float = 60) -> ToolStatus:
    if not settings_obj.vol3_enabled:
        return _disabled_status()

    resolved = _resolve_volatility_executable(settings_obj.vol3_volatility_bin)
    if isinstance(resolved, ToolStatus):
        return resolved

    try:
        result = subprocess.run(
            [resolved, "-q", "frameworkinfo.FrameworkInfo"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (subprocess.TimeoutExpired, OSError, UnicodeError) as exc:
        logger.warning("Volatility probe failed for %s: %s", resolved, _bounded(exc))
        return ToolStatus(
            ready=False,
            code="volatility_probe_failed",
            detail="The configured Volatility executable could not be probed.",
        )

    if result.returncode != 0:
        logger.warning(
            "Volatility probe exited %s for %s; stdout=%r stderr=%r",
            result.returncode,
            resolved,
            _bounded(result.stdout),
            _bounded(result.stderr),
        )
        return ToolStatus(
            ready=False,
            code="volatility_probe_failed",
            detail="The configured Volatility executable could not be probed.",
        )

    output = f"{result.stdout or ''}\n{result.stderr or ''}"
    match = _VOLATILITY_BANNER.search(output)
    if match is None:
        logger.warning(
            "Volatility probe returned an unrecognized banner for %s; stdout=%r stderr=%r",
            resolved,
            _bounded(result.stdout),
            _bounded(result.stderr),
        )
        return ToolStatus(
            ready=False,
            code="volatility_probe_failed",
            detail="The configured Volatility executable could not be probed.",
        )

    version = match.group("version")
    if len(version) > _MAX_VERSION_TOKEN_LENGTH:
        logger.warning("Volatility probe returned an oversized version token for %s", resolved)
        return ToolStatus(
            ready=False,
            code="volatility_probe_failed",
            detail="The configured Volatility executable could not be probed.",
        )
    return _version_status(version, resolved)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def check_dwarf2json_tool(settings_obj: Settings = settings) -> ToolStatus:
    if not settings_obj.vol3_enabled:
        return _disabled_status()

    configured_path = settings_obj.vol3_dwarf2json_path
    if configured_path is None or not str(configured_path).strip():
        return ToolStatus(
            ready=False,
            code="dwarf2json_not_configured",
            detail=(
                "Configure [vol3].dwarf2json_path and dwarf2json_sha256 to build a Linux ISF "
                "from kernel debug symbols. Prebuilt symbol sources and manual ISF upload "
                "remain available."
            ),
        )

    configured_hash = settings_obj.vol3_dwarf2json_sha256
    if configured_hash is None or not configured_hash.strip():
        return ToolStatus(
            ready=False,
            code="dwarf2json_hash_not_configured",
            detail=(
                "Configure [vol3].dwarf2json_sha256 to build a Linux ISF from kernel debug "
                "symbols. Prebuilt symbol sources and manual ISF upload remain available."
            ),
        )

    resolved = Path(configured_path).expanduser()
    if not resolved.exists():
        return ToolStatus(
            ready=False,
            code="dwarf2json_not_found",
            detail="The configured dwarf2json executable was not found.",
        )
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return ToolStatus(
            ready=False,
            code="dwarf2json_not_executable",
            detail="The configured dwarf2json path is not an executable file.",
        )

    expected = configured_hash.strip().lower().removeprefix("sha256:")
    try:
        actual = _hash_file(resolved)
    except FileNotFoundError:
        return ToolStatus(
            ready=False,
            code="dwarf2json_not_found",
            detail="The configured dwarf2json executable was not found.",
        )
    except OSError as exc:
        # The executable bit is already proven above, so a read failure here is an
        # unreadable-or-unusable file (EIO, EACCES on the content, a stale mount),
        # not necessarily a non-executable one.  The code stays stable; the detail
        # must not claim more than the failure supports.
        logger.warning("Could not hash configured dwarf2json file %s: %s", resolved, _bounded(exc))
        return ToolStatus(
            ready=False,
            code="dwarf2json_not_executable",
            detail="The configured dwarf2json path is not a usable executable file: it could not be read.",
        )

    if not hmac.compare_digest(actual, expected):
        logger.warning(
            "Configured dwarf2json hash mismatch for %s; expected=%s actual=%s",
            resolved,
            expected,
            actual,
        )
        return ToolStatus(
            ready=False,
            code="dwarf2json_hash_mismatch",
            detail="The configured dwarf2json file does not match its SHA-256 pin.",
        )

    return ToolStatus(
        ready=True,
        code="ready",
        detail="dwarf2json is ready.",
        resolved_path=resolved,
    )


def get_vol3_readiness(
    settings_obj: Settings = settings,
    *,
    volatility_timeout_seconds: float = 60,
) -> Vol3Readiness:
    if not settings_obj.vol3_enabled:
        disabled = _disabled_status()
        return Vol3Readiness(enabled=False, analysis=disabled, isf_build=disabled)
    return Vol3Readiness(
        enabled=True,
        analysis=check_volatility_tool(settings_obj, timeout_seconds=volatility_timeout_seconds),
        isf_build=check_dwarf2json_tool(settings_obj),
    )


def _require_ready(status: ToolStatus) -> Path:
    """Narrow a readiness probe to its executable, or raise the stable coded error."""
    if not status.ready or status.resolved_path is None:
        raise ToolDependencyError(status.code, status.detail)
    return status.resolved_path


@dataclass(frozen=True)
class VerifiedVolatility:
    """A Volatility executable proven ready at the moment of verification."""

    path: Path
    version: str


def require_verified_volatility(settings_obj: Settings = settings) -> VerifiedVolatility:
    """Verify Volatility immediately before execution and return both known facts.

    This is the single execution-time gate: every call site calls it directly, in
    the statement that precedes the spawn it guards, so the tool is re-resolved and
    re-probed against the configuration as it stands at that instant.  Hoisting the
    call away from its spawn — out of a per-plugin loop, or into a wrapper cached
    for a whole batch — widens the window in which the executable can be swapped
    under a running job, so it is a security regression, not a micro-optimisation.

    Callers need the resolved executable and the probed version.  Probing is a full
    framework load, so this returns both from a single probe rather than making the
    caller pay twice; that is also why exactly one probe per execution is the
    contract, and no caller should wrap it to issue a second.  The non-optional
    return type is what lets call sites use the values without re-narrowing.
    """
    status = check_volatility_tool(settings_obj)
    if not status.ready or status.resolved_path is None or status.version is None:
        raise ToolDependencyError(status.code, status.detail)
    return VerifiedVolatility(path=status.resolved_path, version=status.version)


def require_dwarf2json_tool(settings_obj: Settings = settings) -> Path:
    return _require_ready(check_dwarf2json_tool(settings_obj))
