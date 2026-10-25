from __future__ import annotations

from dataclasses import dataclass

from .deps import JobRunDeps


@dataclass(frozen=True)
class JobRunContext:
    """Per-run identity plus the explicit dependency container.

    Bundles the values repeated across the boot-capture and live-session runners:
    the ``job_id`` / ``action_id`` threaded through every step, and ``deps`` — the
    ``JobRunDeps`` instance the run resolves its collaborators through. Tests
    override behaviour by overriding fields on the ``deps`` they pass in.
    """

    job_id: str
    action_id: int | None
    deps: JobRunDeps

    @classmethod
    def create(cls, job_id: str, action_id: int | None, deps: JobRunDeps | None = None) -> JobRunContext:
        return cls(job_id=job_id, action_id=action_id, deps=deps if deps is not None else JobRunDeps.default())
