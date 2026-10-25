from __future__ import annotations

import json


def parse_metadata(output: str) -> dict:
    start = output.find("{")
    end = output.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError("Could not parse guest metadata JSON")
    return json.loads(output[start : end + 1])


def parse_keyed_output(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def parse_required_keyed_output(output: str, required_keys: set[str], context: str) -> dict[str, str]:
    parsed = parse_keyed_output(output)
    missing = sorted(key for key in required_keys if not parsed.get(key))
    if missing:
        raise RuntimeError(f"{context} output missing required keys: {', '.join(missing)}")
    return parsed
