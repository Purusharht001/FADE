"""The research export: one CSV row per scan, for evaluation.

This is the only place the reference label (ground-truth CDR) leaves the
database, and the only place a scan's engine output sits beside a
clinician's blind rating. Access is restricted to researchers and
administrators at the route.
"""

import csv
import io
import json
from collections.abc import Iterable, Mapping
from typing import Any

from app.models.enums import BiomarkerKey
from app.models.scan import Scan
from app.models.study import ClinicianRating

LEADING_COLUMNS = [
    "scan_id",
    "external_subject_id",
    "source",
    "extraction_method",
    "status",
    "scan_date",
    "age",
    "sex",
]
DERIVED_COLUMNS = [column for key in BiomarkerKey for column in (key.value, f"{key.value}_unit")]
RESULT_COLUMNS = [
    "qc_flags",
    "determinate",
    "indeterminate_reason",
    "stage",
    "severity",
    "confidence",
    "uncertainty",
    "needs_review",
    "calibration_version",
    "engine_version",
    "reference_label",
]
RATING_COLUMNS = [
    "n_ratings",
    "rating_stage_called",
    "rating_borderline",
    "rating_confidence",
    "rating_notes",
    "rating_rater_id",
    "rating_at",
]

# A spreadsheet treats a cell starting with one of these as a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def flatten_raw_volumes(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """`raw_volumes` as flat `raw_<path>` columns. Nested objects become
    dotted paths (`raw_thickness_mm.lh.entorhinal`); lists are kept as JSON.
    """
    flat: dict[str, Any] = {}

    def walk(prefix: str, node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                walk(f"{prefix}.{key}" if prefix else str(key), value)
        elif isinstance(node, list):
            flat[f"raw_{prefix}"] = json.dumps(node, ensure_ascii=False)
        else:
            flat[f"raw_{prefix}"] = node

    walk("", raw or {})
    return flat


def _cell(value: Any) -> Any:
    """Neutralises spreadsheet formula injection in text cells. Numbers are
    left alone, so a negative value stays a number.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def build_rows(
    scans: Iterable[Scan], ratings: Iterable[ClinicianRating]
) -> tuple[list[str], list[dict[str, Any]]]:
    """(column names, rows). `ratings` must be ordered oldest change first."""
    latest: dict[Any, ClinicianRating] = {}
    counts: dict[Any, int] = {}
    for rating in ratings:
        latest[rating.scan_id] = rating
        counts[rating.scan_id] = counts.get(rating.scan_id, 0) + 1

    rows: list[dict[str, Any]] = []
    raw_columns: set[str] = set()
    for scan in scans:
        patient = scan.patient
        fis = scan.fis_result
        row: dict[str, Any] = {
            "scan_id": str(scan.id),
            "external_subject_id": scan.external_subject_id,
            "source": patient.source.value,
            "extraction_method": scan.extraction_method.value if scan.extraction_method else None,
            "status": scan.status.value,
            "scan_date": scan.scan_date.isoformat(),
            "age": patient.age,
            "sex": patient.sex.value,
            "qc_flags": ";".join(str(f.get("code", "")) for f in scan.qc_flags or []),
            "calibration_version": scan.calibration_version,
            "engine_version": scan.engine_version,
            "reference_label": scan.reference_label,
        }
        for reading in scan.biomarkers:
            row[reading.key.value] = reading.value
            row[f"{reading.key.value}_unit"] = reading.unit
        if fis is not None:
            row.update(
                determinate=fis.determinate,
                indeterminate_reason=fis.indeterminate_reason,
                stage=fis.stage.value if fis.stage else None,
                severity=fis.severity,
                confidence=fis.confidence,
                uncertainty=fis.uncertainty,
                needs_review=fis.needs_review,
            )
            row["calibration_version"] = fis.calibration_version

        raw = flatten_raw_volumes(scan.raw_volumes)
        raw_columns.update(raw)
        row.update(raw)

        row["n_ratings"] = counts.get(scan.id, 0)
        latest_rating = latest.get(scan.id)
        if latest_rating is not None:
            row.update(
                rating_stage_called=latest_rating.stage_called.value,
                rating_borderline=latest_rating.borderline,
                rating_confidence=latest_rating.confidence,
                rating_notes=latest_rating.notes,
                rating_rater_id=str(latest_rating.rater_id),
                rating_at=latest_rating.updated_at.isoformat(),
            )
        rows.append(row)

    columns = [
        *LEADING_COLUMNS,
        *DERIVED_COLUMNS,
        *sorted(raw_columns),
        *RESULT_COLUMNS,
        *RATING_COLUMNS,
    ]
    return columns, rows


def render_csv(columns: list[str], rows: Iterable[Mapping[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row.get(column)) for column in columns])
    return buffer.getvalue()
