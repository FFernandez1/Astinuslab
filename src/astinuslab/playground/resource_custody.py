"""Resource-claim renewal shared by Playground execution and reconciliation."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class LostResourceCustody(RuntimeError):
    pass


class ResourceClaimLease:
    """Renew and fence one state+timestamp worker claim while host I/O blocks."""

    def __init__(
        self,
        *,
        kind: str,
        resource_id: str,
        state: str,
        token: datetime,
        session_scope_factory: Callable[[], AbstractContextManager[Session]],
        grace_seconds: int,
    ) -> None:
        self.kind = kind
        self.resource_id = resource_id
        self.state = state
        self._token = token
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._session_scope = session_scope_factory
        grace = max(3, int(grace_seconds))
        self._interval = max(1.0, min(30.0, grace / 3))
        self._thread: threading.Thread | None = None

    def _renew(self) -> bool:
        from ..db.models import JobNetwork, JobVM

        model = JobVM if self.kind == "vm" else JobNetwork
        with self._lock:
            if self._lost.is_set():
                return False
            renewed_at = datetime.now(UTC)
            if self._token.tzinfo is None:
                renewed_at = renewed_at.replace(tzinfo=None)
            if renewed_at <= self._token:
                from datetime import timedelta

                renewed_at = self._token + timedelta(microseconds=1)
            with self._session_scope() as session:
                renewed = (
                    session.query(model)
                    .filter(
                        model.id == self.resource_id,
                        model.state == self.state,
                        model.updated_at == self._token,
                    )
                    .update({model.updated_at: renewed_at}, synchronize_session=False)
                )
                session.flush()
            if renewed != 1:
                self._lost.set()
                return False
            self._token = renewed_at
            return True

    def _heartbeat(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                if not self._renew():
                    return
            except Exception:
                logger.exception("resource claim heartbeat failed for %s %s", self.kind, self.resource_id)
                self._lost.set()
                return

    def start(self) -> None:
        try:
            renewed = self._renew()
        except Exception as exc:
            self._lost.set()
            raise LostResourceCustody(f"{self.kind} {self.resource_id} could not prove {self.state} custody") from exc
        if not renewed:
            raise LostResourceCustody(f"{self.kind} {self.resource_id} lost {self.state} custody")
        self._thread = threading.Thread(
            target=self._heartbeat,
            name=f"playground-{self.kind}-lease-{self.resource_id}",
            daemon=True,
        )
        self._thread.start()

    def assert_owned(self) -> None:
        try:
            renewed = not self._lost.is_set() and self._renew()
        except Exception as exc:
            self._lost.set()
            raise LostResourceCustody(f"{self.kind} {self.resource_id} could not prove {self.state} custody") from exc
        if not renewed:
            raise LostResourceCustody(f"{self.kind} {self.resource_id} lost {self.state} custody")

    def publication_token(self) -> datetime:
        """Stop renewal and return one final state+timestamp fencing token."""
        self.stop()
        self.assert_owned()
        with self._lock:
            return self._token

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
