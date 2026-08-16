"""Loading and validating `config/sources.yaml` (PRD req. 4.1).

Validation is deliberately forgiving at the record level and strict at the field level:
a malformed record is skipped and logged, and the rest of the registry still loads
(req. 4.1.4). A run must never be blocked by one bad YAML entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ba_radar.models import Source


@dataclass
class RegistryLoadResult:
    sources: list[Source] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def active(self) -> list[Source]:
        """Only records with `active: true` are processed (req. 1.1.2)."""
        return [s for s in self.sources if s.active]


def load_registry(path: Path) -> RegistryLoadResult:
    result = RegistryLoadResult()

    if not path.exists():
        result.errors.append(f"source registry not found: {path}")
        return result

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        result.errors.append(f"source registry is not valid YAML: {exc}")
        return result

    if raw is None:
        return result
    if not isinstance(raw, list):
        result.errors.append(f"source registry must be a list of records, got {type(raw).__name__}")
        return result

    seen_ids: set[str] = set()

    for index, record in enumerate(raw):
        label = f"record #{index + 1}"
        if not isinstance(record, dict):
            result.errors.append(f"{label}: expected a mapping, got {type(record).__name__}")
            continue

        record_id = record.get("id")
        if isinstance(record_id, str) and record_id:
            label = f"source '{record_id}'"

        # req. 4.1.5 — on a duplicate id the first record wins and an error is logged.
        if isinstance(record_id, str) and record_id in seen_ids:
            result.errors.append(f"{label}: duplicate id, keeping the first record")
            continue

        try:
            source = _parse_record(record)
        except ValidationError as exc:
            # req. 4.1.4 — skip the source, log the error, keep going.
            result.errors.append(f"{label}: {_format_validation_error(exc)}")
            continue

        seen_ids.add(source.id)
        result.sources.append(source)

    return result


def _parse_record(record: dict[str, Any]) -> Source:
    return Source.model_validate(record)


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        location = ".".join(str(piece) for piece in err["loc"]) or "record"
        parts.append(f"{location}: {err['msg']}")
    return "; ".join(parts)
