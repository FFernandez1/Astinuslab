"""Input contract for the streaming archive writer.

``write_archive_to`` is the path a creator's export takes when boot layers are
too large to hold in memory, so it accepts assets as open streams and paths as
well as bytes. Everything it refuses here would otherwise become a member of a
package someone else imports: a symlinked source, a stream that never ends, an
asset whose bytes do not hash to the name it was filed under, a supporting file
outside ``experience/``. A refusal must also leave no spool file behind.
"""

from __future__ import annotations

import hashlib
import io
import os
import zipfile
from pathlib import Path

import pytest

from astinuslab.lab_packages.archive import (
    PackageArchiveError,
    _stream_source,
    write_archive,
    write_archive_to,
)
from astinuslab.lab_packages.models import (
    HostResources,
    LabDefinition,
    LabExperience,
    LabNetwork,
    LabVM,
    ResourceRecommendations,
)

pytestmark = pytest.mark.unit

ASSET = b"embedded clean boot layer"
ASSET_DIGEST = hashlib.sha256(ASSET).hexdigest()
RUNBOOK = "# Writer runbook\n"


def _definition() -> LabDefinition:
    return LabDefinition(
        package_id="writer-lab",
        version="1.0.0",
        title="Writer lab",
        creator="Researcher",
        license="CC-BY-4.0",
        reproducibility="unlocked",
        resources=ResourceRecommendations(
            minimum=HostResources(cpu_threads=2, memory_mb=4096, free_disk_bytes=40 * 1024**3),
            recommended=HostResources(cpu_threads=4, memory_mb=8192, free_disk_bytes=40 * 1024**3),
        ),
        networks=(LabNetwork(id="isolated", subnet_cidr="192.0.2.0/29"),),
        vms=(
            LabVM(
                id="target",
                role="target",
                image_id="ubuntu-24.04",
                vcpus=2,
                memory_mb=2048,
                disk_gb=10,
                network_ids=("isolated",),
            ),
        ),
        experience=LabExperience(),
    )


def _spools() -> list[str]:
    import tempfile

    return sorted(p for p in os.listdir(tempfile.gettempdir()) if p.startswith("astinuslab-write-"))


# --- _stream_source ------------------------------------------------------------


def test_stream_source_wraps_raw_bytes():
    handle, should_close = _stream_source(ASSET)

    assert handle.read() == ASSET
    assert should_close is True


def test_stream_source_wraps_a_memoryview():
    handle, _ = _stream_source(memoryview(ASSET))

    assert handle.read() == ASSET


def test_stream_source_opens_a_regular_path(tmp_path: Path):
    layer = tmp_path / "layer.bin"
    layer.write_bytes(ASSET)

    handle, should_close = _stream_source(layer)
    try:
        assert handle.read() == ASSET
        assert should_close is True
    finally:
        handle.close()


def test_stream_source_passes_an_existing_stream_through():
    stream = io.BytesIO(ASSET)

    handle, should_close = _stream_source(stream)

    assert handle is stream
    assert should_close is False


def test_stream_source_refuses_a_symlinked_path(tmp_path: Path):
    (tmp_path / "real.bin").write_bytes(ASSET)
    link = tmp_path / "link.bin"
    link.symlink_to(tmp_path / "real.bin")

    with pytest.raises(PackageArchiveError, match="could not open asset source"):
        _stream_source(link)


def test_stream_source_refuses_a_non_regular_path(tmp_path: Path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)

    with pytest.raises(PackageArchiveError, match="regular non-symlink file"):
        _stream_source(fifo)


def test_stream_source_reports_a_missing_path(tmp_path: Path):
    with pytest.raises(PackageArchiveError, match="could not open asset source"):
        _stream_source(tmp_path / "absent.bin")


def test_stream_source_refuses_an_unsupported_object():
    with pytest.raises(TypeError, match="binary streams, paths, or bytes"):
        _stream_source(42)  # type: ignore[arg-type]


# --- write_archive_to argument validation --------------------------------------


def test_writer_requires_a_seekable_sink():
    with pytest.raises(TypeError, match="seekable binary stream"):
        write_archive_to(object(), _definition(), RUNBOOK, asset_streams={"a": ASSET})  # type: ignore[arg-type]


def test_writer_requires_a_definition():
    with pytest.raises(TypeError, match="must be a LabDefinition"):
        write_archive_to(io.BytesIO(), {"package_id": "x"}, RUNBOOK, asset_streams={"a": ASSET})  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", [0, -1, True, 2.5, "4096"])
def test_writer_refuses_an_invalid_asset_ceiling(limit):
    with pytest.raises(ValueError, match="max_asset_bytes must be a positive integer"):
        write_archive_to(
            io.BytesIO(),
            _definition(),
            RUNBOOK,
            asset_streams={ASSET_DIGEST: ASSET},
            max_asset_bytes=limit,
        )


def test_writer_requires_text_or_bytes_for_the_runbook():
    with pytest.raises(TypeError, match="runbook must be text or bytes"):
        write_archive_to(io.BytesIO(), _definition(), 42, asset_streams={ASSET_DIGEST: ASSET})  # type: ignore[arg-type]


def test_writer_refuses_an_empty_runbook():
    with pytest.raises(PackageArchiveError, match="runbook must not be empty"):
        write_archive_to(io.BytesIO(), _definition(), "", asset_streams={ASSET_DIGEST: ASSET})


def test_writer_refuses_a_supporting_file_outside_the_experience_tree():
    with pytest.raises(PackageArchiveError, match="invalid supporting file path"):
        write_archive_to(
            io.BytesIO(),
            _definition(),
            RUNBOOK,
            asset_streams={ASSET_DIGEST: ASSET},
            supporting_files={"experience/notes.md": b"ok", "elsewhere.md": b"no"},
        )


def test_writer_refuses_overwriting_the_runbook_as_a_supporting_file():
    with pytest.raises(PackageArchiveError, match="invalid supporting file path"):
        write_archive_to(
            io.BytesIO(),
            _definition(),
            RUNBOOK,
            asset_streams={ASSET_DIGEST: ASSET},
            supporting_files={"experience/runbook.md": b"shadowed"},
        )


# --- streaming asset publication -----------------------------------------------


def test_writer_publishes_a_streamed_asset(tmp_path: Path):
    sink = io.BytesIO()
    before = _spools()

    write_archive_to(
        sink,
        _definition(),
        RUNBOOK,
        asset_streams={ASSET_DIGEST: io.BytesIO(ASSET)},
        allow_legacy_unreferenced_assets=True,
    )

    with zipfile.ZipFile(io.BytesIO(sink.getvalue())) as archive:
        assert f"assets/sha256/{ASSET_DIGEST}" in archive.namelist()
        assert archive.read(f"assets/sha256/{ASSET_DIGEST}") == ASSET
    assert _spools() == before


def test_writer_publishes_an_asset_supplied_as_a_path(tmp_path: Path):
    layer = tmp_path / "layer.bin"
    layer.write_bytes(ASSET)
    sink = io.BytesIO()

    write_archive_to(
        sink,
        _definition(),
        RUNBOOK,
        asset_streams={ASSET_DIGEST: layer},
        allow_legacy_unreferenced_assets=True,
    )

    with zipfile.ZipFile(io.BytesIO(sink.getvalue())) as archive:
        assert archive.read(f"assets/sha256/{ASSET_DIGEST}") == ASSET


def test_writer_refuses_a_streamed_asset_that_does_not_match_its_digest():
    before = _spools()

    with pytest.raises(PackageArchiveError, match="digest mismatch"):
        write_archive_to(
            io.BytesIO(),
            _definition(),
            RUNBOOK,
            asset_streams={ASSET_DIGEST: io.BytesIO(b"entirely different bytes")},
            allow_legacy_unreferenced_assets=True,
        )

    assert _spools() == before


def test_writer_refuses_an_empty_streamed_asset():
    before = _spools()

    with pytest.raises(PackageArchiveError, match="must not be empty"):
        write_archive_to(
            io.BytesIO(),
            _definition(),
            RUNBOOK,
            asset_streams={ASSET_DIGEST: io.BytesIO(b"")},
            allow_legacy_unreferenced_assets=True,
        )

    assert _spools() == before


def test_writer_refuses_a_streamed_asset_over_the_ceiling():
    before = _spools()

    with pytest.raises(PackageArchiveError, match="too large"):
        write_archive_to(
            io.BytesIO(),
            _definition(),
            RUNBOOK,
            asset_streams={ASSET_DIGEST: io.BytesIO(ASSET)},
            max_asset_bytes=4,
            allow_legacy_unreferenced_assets=True,
        )

    assert _spools() == before


def test_writer_reports_a_failing_asset_stream():
    class _Failing(io.RawIOBase):
        def readable(self) -> bool:
            return True

        def read(self, size: int = -1) -> bytes:
            raise OSError("layer device disappeared")

    with pytest.raises(PackageArchiveError, match="could not read asset stream"):
        write_archive_to(
            io.BytesIO(),
            _definition(),
            RUNBOOK,
            asset_streams={ASSET_DIGEST: _Failing()},  # type: ignore[dict-item]
            allow_legacy_unreferenced_assets=True,
        )


def test_writer_refuses_a_stream_with_nothing_ready():
    class _NoneReturning(io.RawIOBase):
        def readable(self) -> bool:
            return True

        def read(self, size: int = -1) -> None:
            return None

    with pytest.raises(PackageArchiveError, match="returned no bytes"):
        write_archive_to(
            io.BytesIO(),
            _definition(),
            RUNBOOK,
            asset_streams={ASSET_DIGEST: _NoneReturning()},  # type: ignore[dict-item]
            allow_legacy_unreferenced_assets=True,
        )


def test_writer_without_streams_matches_the_in_memory_writer():
    sink = io.BytesIO()

    write_archive_to(sink, _definition(), RUNBOOK)

    assert sink.getvalue() == write_archive(_definition(), RUNBOOK)


# --- write_archive inline validation -------------------------------------------


def test_inline_assets_must_be_bytes_keyed_by_string():
    with pytest.raises(TypeError, match="asset keys must be strings"):
        write_archive(_definition(), RUNBOOK, {ASSET_DIGEST: "not bytes"})  # type: ignore[dict-item]


def test_inline_asset_over_the_ceiling_is_refused():
    with pytest.raises(PackageArchiveError, match="too large"):
        write_archive(
            _definition(),
            RUNBOOK,
            {ASSET_DIGEST: ASSET},
            max_asset_bytes=4,
            allow_legacy_unreferenced_assets=True,
        )
