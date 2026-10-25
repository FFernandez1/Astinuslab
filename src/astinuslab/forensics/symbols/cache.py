from __future__ import annotations

import fcntl
import json
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from .identity import SymbolIdentity, isf_filename


class SymbolCache:
    def __init__(self, root: Path | str, *, max_bytes: int, eviction_grace_seconds: int = 3600) -> None:
        root = Path(root)
        if not root.is_absolute():
            raise ValueError(f"SymbolCache root must be absolute: {root}")
        self.root = root
        self.max_bytes = max_bytes
        self.eviction_grace_seconds = eviction_grace_seconds
        (self.root / "isf").mkdir(parents=True, exist_ok=True)

    def path_for(self, identity: SymbolIdentity) -> Path:
        return self.root / "isf" / isf_filename(identity)

    @contextmanager
    def lock_for_identity(self, identity: SymbolIdentity):
        lock_dir = self.root / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / (isf_filename(identity) + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def store(self, identity: SymbolIdentity, isf_path: Path, metadata: dict) -> Path:
        with self.lock_for_identity(identity):
            return self._store_locked(identity, isf_path, metadata)

    def _store_locked(self, identity: SymbolIdentity, isf_path: Path, metadata: dict) -> Path:
        target = self.path_for(identity)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(isf_path, target)
        meta_path = target.with_suffix(target.suffix + ".meta.json")
        meta_path.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
        self._evict()
        return target

    def _evict(self) -> None:
        isf_dir = self.root / "isf"
        primaries = [p for p in isf_dir.glob("*.json") if p.is_file() and not p.name.endswith(".meta.json")]

        def pair_size(path: Path) -> int:
            meta = path.with_suffix(path.suffix + ".meta.json")
            return path.stat().st_size + (meta.stat().st_size if meta.exists() else 0)

        total = sum(pair_size(path) for path in primaries)
        # Files inside the grace window are never evicted: eviction runs under
        # only THIS store's identity lock, so a fresh file may belong to a
        # concurrent chain (other worker process) that has stored but not yet
        # staged it. Age, not the lock, is what makes eviction safe here.
        eviction_floor = time.time() - self.eviction_grace_seconds
        for path in sorted(primaries, key=lambda p: p.stat().st_mtime):
            if total <= self.max_bytes:
                break
            try:
                if path.stat().st_mtime > eviction_floor:
                    continue
            except OSError:
                continue
            size = pair_size(path)
            path.with_suffix(path.suffix + ".meta.json").unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            total -= size
