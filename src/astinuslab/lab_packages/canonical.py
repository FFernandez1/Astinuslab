"""Canonical JSON serialization for signed and content-addressed package data."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel


def canonical_bytes(model: BaseModel) -> bytes:
    payload = model.model_dump(mode="json", exclude_none=True)
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(model: BaseModel) -> str:
    return hashlib.sha256(canonical_bytes(model)).hexdigest()
