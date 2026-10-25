"""Regenerate the env-override reference table in docs/usage/configuration.md.

Usage: .venv/bin/python tools/dev/generate_env_reference.py
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from astinuslab.core.config import Settings

DOC = Path(__file__).resolve().parents[2] / "docs" / "usage" / "configuration.md"
BEGIN = "<!-- BEGIN GENERATED ENV REFERENCE (tools/dev/generate_env_reference.py) -->"
END = "<!-- END GENERATED ENV REFERENCE -->"


def _md_code(value: str) -> str:
    return "`" + value.replace("|", "&#124;").replace("`", "\\`") + "`"


def _type_name(annotation: Any) -> str:
    rendered = getattr(annotation, "__name__", str(annotation))
    # Python 3.13 defines Path in the private pathlib._local, so stringified
    # annotations spell it that way. Public docs use the public name, and the
    # table must not depend on which interpreter generated it.
    return rendered.replace("pathlib._local.", "pathlib.")


def _default_for(field: Any) -> Any:
    if field.default_factory is not None:
        return field.default_factory()
    return field.default


def _format_default(value: Any, field_name: str, base: str, data: str) -> str:
    rendered = repr(value)
    if base == data:
        placeholder = "<base_dir>" if field_name == "base_dir" else "<data_dir>"
        return rendered.replace(base, placeholder)
    roots = sorted(((base, "<base_dir>"), (data, "<data_dir>")), key=lambda item: len(item[0]), reverse=True)
    for root, placeholder in roots:
        rendered = rendered.replace(root, placeholder)
    return rendered


def render_table() -> str:
    base = str(Settings.model_fields["base_dir"].default)
    data = str(Settings.model_fields["data_dir"].default)
    lines = [BEGIN, "", "| Variable | Type | Default |", "|---|---|---|"]
    for name, field in Settings.model_fields.items():
        env = f"ASTINUSLAB_{name.upper()}"
        default = _format_default(_default_for(field), name, base, data)
        lines.append(f"| {_md_code(env)} | {_md_code(_type_name(field.annotation))} | {_md_code(default)} |")
    lines.extend(
        [
            "",
            "List-typed settings accept comma-separated values or a JSON array; dict-typed settings accept a JSON object.",
            "",
            END,
        ]
    )
    return "\n".join(lines)


def main() -> None:
    text = DOC.read_text(encoding="utf-8")
    block = render_table()
    pattern = re.compile(re.escape(BEGIN) + ".*?" + re.escape(END), re.DOTALL)
    if pattern.search(text):
        text = pattern.sub(block, text)
    else:
        text = text.rstrip() + "\n\n## Environment variable reference\n\n" + block + "\n"
    DOC.write_text(text, encoding="utf-8")
    print(f"updated {DOC}")


if __name__ == "__main__":
    main()
