from __future__ import annotations

from importlib.resources import files
import json
from typing import Any


SCHEMA_FILES = {
    "rtl-advisor-proof-v1": "rtl-advisor-proof-v1.schema.json",
    "rtl-advisor-reference-v1": "rtl-advisor-reference-v1.schema.json",
    "rtl-advisor-variant-v1": "rtl-advisor-variant-v1.schema.json",
}


def read_schema(schema_id: str) -> dict[str, Any]:
    """Read one packaged Corpus Registry V1 JSON schema."""

    try:
        filename = SCHEMA_FILES[schema_id]
    except KeyError as exc:
        raise ValueError(f"unknown RTL Advisor schema: {schema_id!r}") from exc
    resource = files(__package__).joinpath(filename)
    payload = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"packaged schema is not a JSON object: {schema_id!r}")
    return payload
