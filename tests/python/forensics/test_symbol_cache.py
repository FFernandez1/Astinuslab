from __future__ import annotations


def test_evict_does_not_remove_meta_independently(tmp_path):
    from astinuslab.forensics.symbols.cache import SymbolCache
    from astinuslab.forensics.symbols.identity import SymbolIdentity

    cache = SymbolCache(tmp_path / "cache", max_bytes=1)
    identity = SymbolIdentity("x86_64", "6.8.0-test", "abc", "host_fetch")
    src = tmp_path / "source.json"
    src.write_text('{"metadata":{},"symbols":{"init_task":{}}}', encoding="utf-8")

    cache.store(identity, src, {"distro": "ubuntu"})

    isf_dir = tmp_path / "cache" / "isf"
    primaries = [p for p in isf_dir.glob("*.json") if not p.name.endswith(".meta.json")]
    metas = list(isf_dir.glob("*.meta.json"))
    assert len(primaries) == len(metas), "meta count must match primary count"


def test_cache_root_must_be_absolute(tmp_path, monkeypatch):
    import pytest

    from astinuslab.forensics.symbols.cache import SymbolCache

    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        SymbolCache("relative/cache", max_bytes=1024)


def test_lock_for_identity_context_manager(tmp_path):
    from astinuslab.forensics.symbols.cache import SymbolCache
    from astinuslab.forensics.symbols.identity import SymbolIdentity

    cache = SymbolCache(tmp_path / "cache", max_bytes=10_000_000)
    identity = SymbolIdentity("x86_64", "6.8.0-test", "abc", "host_fetch")

    with cache.lock_for_identity(identity):
        pass

    assert (tmp_path / "cache" / "locks").is_dir()


def test_concurrent_store_is_serialized(tmp_path):
    import threading

    from astinuslab.forensics.symbols.cache import SymbolCache
    from astinuslab.forensics.symbols.identity import SymbolIdentity

    cache = SymbolCache(tmp_path / "cache", max_bytes=10_000_000)
    identity = SymbolIdentity("x86_64", "6.8.0-test", "abc", "host_fetch")

    def worker():
        src = tmp_path / f"source-{threading.get_ident()}.json"
        src.write_text('{"metadata":{},"symbols":{"init_task":{}}}', encoding="utf-8")
        cache.store(identity, src, {})

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    isf_dir = tmp_path / "cache" / "isf"
    primaries = [p for p in isf_dir.glob("*.json") if not p.name.endswith(".meta.json")]
    metas = list(isf_dir.glob("*.meta.json"))
    assert len(primaries) == 1
    assert len(metas) == 1


def test_evict_skips_files_within_grace_window(tmp_path):
    from astinuslab.forensics.symbols.cache import SymbolCache
    from astinuslab.forensics.symbols.identity import SymbolIdentity

    cache = SymbolCache(tmp_path / "cache", max_bytes=1, eviction_grace_seconds=3600)
    src = tmp_path / "source.json"
    src.write_text('{"metadata":{},"symbols":{"init_task":{}}}', encoding="utf-8")
    a = SymbolIdentity("x86_64", "6.8.0-a", "aaa", "host_fetch")
    b = SymbolIdentity("x86_64", "6.8.0-b", "bbb", "host_fetch")

    stored_a = cache.store(a, src, {})
    stored_b = cache.store(b, src, {})

    assert stored_a.exists()
    assert stored_b.exists()


def test_evict_removes_old_files_over_budget(tmp_path):
    import os

    from astinuslab.forensics.symbols.cache import SymbolCache
    from astinuslab.forensics.symbols.identity import SymbolIdentity

    cache = SymbolCache(tmp_path / "cache", max_bytes=1, eviction_grace_seconds=3600)
    src = tmp_path / "source.json"
    src.write_text('{"metadata":{},"symbols":{"init_task":{}}}', encoding="utf-8")
    a = SymbolIdentity("x86_64", "6.8.0-a", "aaa", "host_fetch")
    stored_a = cache.store(a, src, {})
    old = 7200
    past = stored_a.stat().st_mtime - old
    os.utime(stored_a, (past, past))
    meta_a = stored_a.with_suffix(stored_a.suffix + ".meta.json")
    os.utime(meta_a, (past, past))

    b = SymbolIdentity("x86_64", "6.8.0-b", "bbb", "host_fetch")
    stored_b = cache.store(b, src, {})

    assert not stored_a.exists(), "aged-out entry must be evicted"
    assert not meta_a.exists(), "meta must go with its primary"
    assert stored_b.exists(), "fresh entry must survive"
