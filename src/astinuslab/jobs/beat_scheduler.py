"""Keep periodic reconciliation recoverable after a broker publication failure."""

from __future__ import annotations

import dbm
import logging
import shelve
from collections.abc import MutableMapping
from pathlib import Path
from typing import cast

from celery.beat import PersistentScheduler, SchedulingError

logger = logging.getLogger(__name__)


class RecoveringPersistentScheduler(PersistentScheduler):
    """Recreate Beat's cached publisher on the next tick after a failed send.

    Kombu's SQLAlchemy transport can leave a producer bound to a disconnected
    channel after SQLite lock retries are exhausted. Keeping that producer
    prevents all later periodic tasks from publishing, even after recovery.
    """

    def _open_schedule(self) -> shelve.Shelf[object]:
        # A freshly registered storage root has no scratch directory until some
        # other component creates it, and a failed open kills Beat for good while
        # the worker keeps running. Create only that leaf, never the storage root,
        # with the split-identity tmp/ mode so no group gains write access.
        Path(self.schedule_filename).parent.mkdir(mode=0o750, exist_ok=True)
        # Match the installer's tmp-file policy at creation. Passing DBM's mode
        # avoids changing the process-wide umask used by concurrent worker tasks.
        database = cast(MutableMapping[bytes, bytes], dbm.open(self.schedule_filename, "c", 0o640))
        return shelve.Shelf(database, writeback=True)

    def apply_async(self, entry, producer=None, advance=True, **kwargs):
        try:
            return super().apply_async(entry, producer=producer, advance=advance, **kwargs)
        except SchedulingError:
            self.__dict__.pop("producer", None)
            connection = self.__dict__.pop("connection", None)
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    logger.warning("Failed to close Beat's failed broker connection", exc_info=True)
            # Preserve Celery's normal scheduling/error handling. The next due
            # task retries with a fresh connection; do not republish here since
            # an interrupted send may already have reached the broker.
            raise
