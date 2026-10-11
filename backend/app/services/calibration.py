"""Calibration files: every number the fuzzy engine depends on.

A calibration is a versioned JSON file under `app/calibration/` holding,
per biomarker, the unit, the fuzzy-set breakpoints, `normal_range` and
`display_range`, plus the output sets used for centroid defuzzification.
`CALIBRATION_FILE` selects the active one, and its `version` is recorded
on every result.

Two properties of a calibration decide what the engine may do with it:

* `domain` says what kind of measurements it was fitted to: "phantom"
  (the in-house volumetry on the synthetic phantom) or "real"
  (FreeSurfer / FastSurfer measurements of real scans). The engine only
  stages data whose domain matches.
* `complete` is False while any biomarker number is null, which is how
  `real.template.json` ships. An incomplete calibration loads without
  error and makes every result indeterminate.

A file that is malformed (wrong shape, unordered breakpoints, an unknown
set name) is different from one that is incomplete: it raises
`CalibrationError` at load, and the application refuses to start.
"""

import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.models.enums import BiomarkerKey, DataSource, Stage
from app.services.biomarkers import BIOMARKER_STRUCTURE, BiomarkerDef, FuzzyLabel

CALIBRATION_DIR = Path(__file__).resolve().parents[1] / "calibration"
SUPPORTED_SCHEMA_VERSION = 1

Trapezoid = tuple[float, float, float, float]

# A version label is written to fixed-width columns and to the audit log,
# which accepts only code-like tokens.
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")


class DataDomain(StrEnum):
    PHANTOM = "phantom"
    REAL = "real"


def data_domain_for(source: DataSource) -> DataDomain:
    """A patient's data is phantom only if the patient is labelled PHANTOM.
    Everything else, including any source added later, is real data and is
    held to the stricter standard.
    """
    return DataDomain.PHANTOM if source == DataSource.PHANTOM else DataDomain.REAL


class CalibrationError(Exception):
    """The calibration file is missing or malformed. Not an HTTP error: it
    is a deployment mistake and is raised at startup.
    """


@dataclass(frozen=True, slots=True)
class Calibration:
    version: str
    domain: DataDomain
    provisional: bool
    # False while any biomarker number in the file is null.
    complete: bool
    # Paths of the null numbers, for error messages and the startup log.
    missing: tuple[str, ...]
    # Declared unit per biomarker. Present even when `complete` is False.
    units: dict[BiomarkerKey, str]
    # Empty when `complete` is False.
    biomarkers: dict[BiomarkerKey, BiomarkerDef]
    # None when the file has no output sets or any of their numbers is null;
    # the engine then reports a stage without a severity.
    output_sets: dict[Stage, Trapezoid] | None
    output_sets_provisional: bool
    source_path: str


def _fail(path: Path, message: str) -> CalibrationError:
    return CalibrationError(f"Calibration file {path.name}: {message}")


def _number_or_none(value: Any, where: str, path: Path) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _fail(path, f"{where} must be a number or null, got {type(value).__name__}.")
    number = float(value)
    if not math.isfinite(number):
        raise _fail(path, f"{where} must be finite.")
    return number


def _read_numbers(
    value: Any, length: int, where: str, path: Path, missing: list[str]
) -> tuple[float, ...] | None:
    """Reads a fixed-length list of numbers. Returns None, and records the
    null positions in `missing`, if any entry is null.
    """
    if not isinstance(value, list) or len(value) != length:
        raise _fail(path, f"{where} must be a list of {length} numbers.")
    numbers = [_number_or_none(v, f"{where}[{i}]", path) for i, v in enumerate(value)]
    nulls = [f"{where}[{i}]" for i, n in enumerate(numbers) if n is None]
    if nulls:
        missing.extend(nulls)
        return None
    return tuple(n for n in numbers if n is not None)


def _read_trapezoid(value: Any, where: str, path: Path, missing: list[str]) -> Trapezoid | None:
    numbers = _read_numbers(value, 4, where, path, missing)
    if numbers is None:
        return None
    a, b, c, d = numbers
    if not a <= b <= c <= d:
        raise _fail(path, f"{where} must satisfy a <= b <= c <= d, got {list(numbers)}.")
    return (a, b, c, d)


def _read_range(
    value: Any, where: str, path: Path, missing: list[str]
) -> tuple[float, float] | None:
    numbers = _read_numbers(value, 2, where, path, missing)
    if numbers is None:
        return None
    lo, hi = numbers
    if lo > hi:
        raise _fail(path, f"{where} must be [low, high] with low <= high.")
    return (lo, hi)


def _read_output_sets(raw: Any, path: Path) -> tuple[dict[Stage, Trapezoid] | None, bool]:
    if raw is None:
        return None, True
    if not isinstance(raw, dict) or not isinstance(raw.get("sets"), dict):
        raise _fail(path, "output_sets must be an object with a `sets` object.")
    provisional = bool(raw.get("provisional", True))
    raw_sets = raw["sets"]
    unknown = set(raw_sets) - {s.value for s in Stage}
    absent = {s.value for s in Stage} - set(raw_sets)
    if unknown or absent:
        raise _fail(
            path,
            f"output_sets.sets must have exactly the keys {[s.value for s in Stage]}.",
        )

    missing: list[str] = []
    sets: dict[Stage, Trapezoid] = {}
    for stage in Stage:
        where = f"output_sets.sets.{stage.value}"
        trapezoid = _read_trapezoid(raw_sets[stage.value], where, path, missing)
        if trapezoid is None:
            continue
        if trapezoid[0] < 0.0 or trapezoid[3] > 1.0:
            raise _fail(path, f"{where} must lie within the 0-1 severity axis.")
        sets[stage] = trapezoid
    return (None if missing else sets), provisional


def parse_calibration(raw: Any, path: Path) -> Calibration:
    if not isinstance(raw, dict):
        raise _fail(path, "top level must be a JSON object.")
    if raw.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise _fail(path, f"schema_version must be {SUPPORTED_SCHEMA_VERSION}.")

    version = raw.get("version")
    if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
        raise _fail(
            path,
            "`version` must be 1-64 characters of letters, digits, '_', '.', ':' or '-' "
            '(for example "real.v1"). It is stored on every result and in the audit log.',
        )
    try:
        domain = DataDomain(str(raw.get("domain")))
    except ValueError as exc:
        raise _fail(path, '`domain` must be "phantom" or "real".') from exc

    raw_biomarkers = raw.get("biomarkers")
    if not isinstance(raw_biomarkers, dict):
        raise _fail(path, "`biomarkers` must be an object.")

    missing: list[str] = []
    units: dict[BiomarkerKey, str] = {}
    defs: dict[BiomarkerKey, BiomarkerDef] = {}

    for key, structure in BIOMARKER_STRUCTURE.items():
        entry = raw_biomarkers.get(key.value)
        if not isinstance(entry, dict):
            raise _fail(path, f"biomarkers.{key.value} is missing.")
        where = f"biomarkers.{key.value}"

        unit = entry.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            raise _fail(path, f"{where}.unit must be a non-empty string.")
        units[key] = unit
        description = entry.get("description")
        if not isinstance(description, str):
            raise _fail(path, f"{where}.description must be a string.")

        raw_sets = entry.get("sets")
        expected = {label.value for label in structure.set_labels}
        if not isinstance(raw_sets, dict) or set(raw_sets) != expected:
            raise _fail(path, f"{where}.sets must have exactly the keys {sorted(expected)}.")

        before = len(missing)
        normal_range = _read_range(
            entry.get("normal_range"), f"{where}.normal_range", path, missing
        )
        display_range = _read_range(
            entry.get("display_range"), f"{where}.display_range", path, missing
        )
        sets: dict[FuzzyLabel, Trapezoid] = {}
        for label in structure.set_labels:
            trapezoid = _read_trapezoid(
                raw_sets[label.value], f"{where}.sets.{label.value}", path, missing
            )
            if trapezoid is not None:
                sets[label] = trapezoid

        if len(missing) == before and normal_range is not None and display_range is not None:
            defs[key] = BiomarkerDef(
                key=key,
                label=structure.label,
                short_label=structure.short_label,
                unit=unit,
                normal_range=normal_range,
                lower_is_worse=structure.lower_is_worse,
                description=description,
                sets=sets,
                display_range=display_range,
            )

    output_sets, output_provisional = _read_output_sets(raw.get("output_sets"), path)
    complete = not missing
    return Calibration(
        version=version,
        domain=domain,
        provisional=bool(raw.get("provisional", True)),
        complete=complete,
        missing=tuple(missing),
        units=units,
        biomarkers=defs if complete else {},
        output_sets=output_sets,
        output_sets_provisional=output_provisional,
        source_path=str(path),
    )


def load_calibration(path: str | Path) -> Calibration:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CalibrationError(f"Calibration file not found or unreadable: {path}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _fail(path, f"is not valid JSON ({exc.msg}, line {exc.lineno}).") from exc
    return parse_calibration(raw, path)


def resolve_calibration_path(name: str) -> Path:
    """`CALIBRATION_FILE` may be a path, or the bare name of a file shipped
    in `app/calibration/`.
    """
    candidate = Path(name)
    if candidate.is_absolute() or candidate.exists():
        return candidate
    return CALIBRATION_DIR / name


_cache: dict[str, Calibration] = {}


def get_calibration() -> Calibration:
    """The active calibration, loaded once per file."""
    path = resolve_calibration_path(get_settings().calibration_file)
    key = str(path)
    if key not in _cache:
        _cache[key] = load_calibration(path)
    return _cache[key]


def clear_calibration_cache() -> None:
    _cache.clear()
