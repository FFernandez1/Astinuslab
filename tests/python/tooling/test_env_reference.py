from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GENERATOR = ROOT / "tools" / "dev" / "generate_env_reference.py"
DOC = ROOT / "docs" / "usage" / "configuration.md"


def _load_generator():
    spec = importlib.util.spec_from_file_location("generate_env_reference", GENERATOR)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_type_name_renders_pathlib_with_its_public_spelling():
    """Python 3.13 moved `Path` into `pathlib._local`; the docs must not say so.

    `str(Path | None)` follows `__module__`, which is `pathlib` on 3.12 and
    `pathlib._local` on 3.13. Without normalization the generated table depends on
    the interpreter that ran it.
    """
    generator = _load_generator()

    rendered = generator._type_name(Path | None)

    assert rendered == "pathlib.Path | None"


def test_rendered_table_never_leaks_the_private_pathlib_module():
    """The whole table, not just one row, must be interpreter-independent."""
    generator = _load_generator()

    table = generator.render_table()

    assert "`pathlib.Path &#124; None`" in table
    assert "pathlib._local" not in table


def test_root_placeholders_distinguish_immutable_code_from_mutable_data():
    generator = _load_generator()

    assert generator._format_default(Path("/release"), "base_dir", "/release", "/install") == (
        "PosixPath('<base_dir>')"
    )
    assert generator._format_default(Path("/install/storage"), "storage_dir", "/release", "/install") == (
        "PosixPath('<data_dir>/storage')"
    )
    assert (
        generator._format_default(
            Path("/install/.venv/lib/python3.12"),
            "base_dir",
            "/install/.venv/lib/python3.12",
            "/install",
        )
        == "PosixPath('<base_dir>')"
    )


def test_env_reference_is_current():
    generator = _load_generator()
    text = DOC.read_text(encoding="utf-8")
    expected = generator.render_table()
    begin = generator.BEGIN
    end = generator.END
    start = text.index(begin)
    stop = text.index(end, start) + len(end)

    assert text[start:stop] == expected, "run tools/dev/generate_env_reference.py"
    assert str(ROOT) not in text[start:stop]
