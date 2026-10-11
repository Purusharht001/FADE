"""Parsers for FreeSurfer / FastSurfer stats files.

Two file kinds are read:

* `aseg.stats` (also FastSurfer's `aseg+DKT.stats` / `aseg+DKT.VINN.stats`):
  subcortical segmentation volumes. Header lines of the form
  `# Measure <Name>, <Key>, <Description>, <value>, <unit>` carry whole-brain
  measures; a whitespace-separated table, described by a `# ColHeaders`
  line, carries one row per structure.
* `lh.aparc.stats` / `rh.aparc.stats` (also the `aparc.DKTatlas` variants):
  cortical parcellation, one table row per region.

Column positions are never assumed: both tables are read through their
`# ColHeaders` line, so a file with extra or reordered columns still parses.

Nothing here interprets the numbers. Turning volumes into the engine's
three inputs is `biomarker_derivation.py`'s job.

A stats file's header records who ran FreeSurfer, on which host and under
which subject name. None of that is read, stored or echoed: errors name a
line number, never its content.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import status

from app.core.exceptions import AppError

Hemisphere = Literal["lh", "rh"]

# The intracranial volume measure. FreeSurfer >= 5 writes it as
# `EstimatedTotalIntraCranialVol, eTIV`; older versions as `IntraCranialVol, ICV`.
_ICV_KEYS = ("eTIV", "ICV")
_ICV_NAMES = ("EstimatedTotalIntraCranialVol", "IntraCranialVol")
_BRAIN_SEG_KEY = "BrainSegVol"

# Structures the engine's inputs are derived from. All must be present and
# positive, or the file is rejected.
REQUIRED_STRUCTURES = (
    "Left-Hippocampus",
    "Right-Hippocampus",
    "Left-Lateral-Ventricle",
    "Right-Lateral-Ventricle",
    "Left-Inf-Lat-Vent",
    "Right-Inf-Lat-Vent",
)

# File names, in order of preference. FastSurfer writes the `+DKT` names.
ASEG_FILENAMES = ("aseg.stats", "aseg+DKT.stats", "aseg+DKT.VINN.stats")
APARC_FILENAMES = (
    "{hemi}.aparc.stats",
    "{hemi}.aparc.DKTatlas.stats",
    "{hemi}.aparc.DKTatlas.mapped.stats",
)


class StatsParseError(AppError):
    """A stats file could not be read into volumes the pipeline can trust."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "stats_parse_error"


@dataclass(frozen=True, slots=True)
class AsegStats:
    """Volumes in mm3, exactly as written in the file."""

    icv_mm3: float
    # Which measure the ICV was read from ("eTIV" or "ICV").
    icv_source: str
    brain_seg_vol_mm3: float
    left_hippocampus_mm3: float
    right_hippocampus_mm3: float
    left_lateral_ventricle_mm3: float
    right_lateral_ventricle_mm3: float
    left_inf_lat_vent_mm3: float
    right_inf_lat_vent_mm3: float
    # Not inputs to the engine, so their absence does not reject the file.
    third_ventricle_mm3: float | None
    fourth_ventricle_mm3: float | None
    left_cerebral_cortex_mm3: float | None
    right_cerebral_cortex_mm3: float | None
    # Where the cortex volumes came from: "structure_table", "measures"
    # (lhCortexVol / rhCortexVol) or "dkt_sum" (FastSurfer ctx-lh-* rows).
    cortex_source: str | None
    measures: dict[str, float] = field(default_factory=dict)
    structures: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AparcRegion:
    thick_avg_mm: float
    surf_area_mm2: float


@dataclass(frozen=True, slots=True)
class AparcStats:
    hemisphere: Hemisphere
    regions: dict[str, AparcRegion]


def decode_stats(content: bytes) -> str:
    """Stats files are ASCII in practice; anything else is replaced rather
    than allowed to raise, since the header may hold arbitrary site text.
    """
    return content.decode("utf-8", errors="replace")


def _to_float(token: str, what: str, line_no: int, source: str) -> float:
    try:
        value = float(token)
    except ValueError as exc:
        raise StatsParseError(
            f"{source}: line {line_no} is not parseable ({what} is not a number)."
        ) from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise StatsParseError(f"{source}: line {line_no} is not parseable ({what} is not finite).")
    return value


def _parse_measure(line: str, line_no: int, source: str) -> tuple[str, str, float] | None:
    """`# Measure <Name>, <Key>, <Description>, <value>, <unit>` -> (name, key, value)."""
    body = line[1:].strip()
    if not body.startswith("Measure"):
        return None
    parts = [p.strip() for p in body[len("Measure") :].split(",")]
    if len(parts) < 5:
        raise StatsParseError(f"{source}: line {line_no} is not a parseable Measure line.")
    # The description may itself contain commas; value and unit are last.
    name, key, value_token = parts[0], parts[1], parts[-2]
    return name, key, _to_float(value_token, f"measure {key}", line_no, source)


def _parse_table(
    text: str, source: str, required_columns: tuple[str, ...]
) -> tuple[dict[str, tuple[str, float, int]], list[dict[str, str]], dict[str, str]]:
    """Reads the header and the data table of a stats file.

    Returns (measures by key -> (name, value, line), table rows as
    column -> token dicts, other `# key value` header fields).
    """
    measures: dict[str, tuple[str, float, int]] = {}
    header_fields: dict[str, str] = {}
    columns: list[str] | None = None
    rows: list[dict[str, str]] = []

    if not text.strip():
        raise StatsParseError(f"{source} is empty.")

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            measure = _parse_measure(line, line_no, source)
            if measure is not None:
                name, key, value = measure
                measures[key] = (name, value, line_no)
                continue
            tokens = line[1:].split()
            if tokens and tokens[0] == "ColHeaders":
                columns = tokens[1:]
            elif len(tokens) >= 2:
                header_fields[tokens[0]] = tokens[1]
            continue

        if columns is None:
            raise StatsParseError(
                f"{source}: data found on line {line_no} before any '# ColHeaders' line."
            )
        tokens = line.split()
        if len(tokens) != len(columns):
            raise StatsParseError(
                f"{source}: line {line_no} is not parseable "
                f"(expected {len(columns)} columns, found {len(tokens)})."
            )
        row = dict(zip(columns, tokens, strict=True))
        row["__line__"] = str(line_no)
        rows.append(row)

    if columns is None:
        raise StatsParseError(f"{source} has no '# ColHeaders' line; not a stats table.")
    absent = [c for c in required_columns if c not in columns]
    if absent:
        raise StatsParseError(f"{source} is missing the column(s) {', '.join(absent)}.")
    return measures, rows, header_fields


def _require_positive(value: float, what: str, source: str) -> float:
    if value <= 0:
        raise StatsParseError(f"{source}: {what} is zero or negative ({value:g} mm3).")
    return value


def parse_aseg_stats(text: str, *, source: str = "aseg.stats") -> AsegStats:
    measures, rows, _header = _parse_table(text, source, ("StructName", "Volume_mm3"))

    structures: dict[str, float] = {}
    for row in rows:
        line_no = int(row["__line__"])
        structures[row["StructName"]] = _to_float(row["Volume_mm3"], "Volume_mm3", line_no, source)

    measure_values = {key: value for key, (_name, value, _line) in measures.items()}
    names_to_keys = {name: key for key, (name, _value, _line) in measures.items()}

    icv_source = next((k for k in _ICV_KEYS if k in measure_values), None)
    if icv_source is None:
        icv_source = next((names_to_keys[n] for n in _ICV_NAMES if n in names_to_keys), None)
    if icv_source is None:
        raise StatsParseError(
            f"{source} has no intracranial volume: expected a '# Measure' line with key "
            "eTIV or ICV. FastSurfer writes it only when its Talairach registration runs."
        )
    icv = _require_positive(
        measure_values[icv_source], f"intracranial volume ({icv_source})", source
    )

    if _BRAIN_SEG_KEY not in measure_values:
        raise StatsParseError(f"{source} has no '# Measure' line with key {_BRAIN_SEG_KEY}.")
    brain_seg = _require_positive(measure_values[_BRAIN_SEG_KEY], _BRAIN_SEG_KEY, source)

    missing = [name for name in REQUIRED_STRUCTURES if name not in structures]
    if missing:
        raise StatsParseError(f"{source} is missing the structure(s) {', '.join(missing)}.")
    for name in REQUIRED_STRUCTURES:
        _require_positive(structures[name], name, source)

    for name in ("3rd-Ventricle", "4th-Ventricle"):
        if name in structures and structures[name] < 0:
            raise StatsParseError(f"{source}: {name} is negative ({structures[name]:g} mm3).")

    left_cortex, right_cortex, cortex_source = _cortex_volumes(structures, measure_values)
    for label, value in (("left", left_cortex), ("right", right_cortex)):
        if value is not None and value <= 0:
            raise StatsParseError(
                f"{source}: {label} cerebral cortex volume is zero or negative ({value:g} mm3)."
            )

    return AsegStats(
        icv_mm3=icv,
        icv_source=icv_source,
        brain_seg_vol_mm3=brain_seg,
        left_hippocampus_mm3=structures["Left-Hippocampus"],
        right_hippocampus_mm3=structures["Right-Hippocampus"],
        left_lateral_ventricle_mm3=structures["Left-Lateral-Ventricle"],
        right_lateral_ventricle_mm3=structures["Right-Lateral-Ventricle"],
        left_inf_lat_vent_mm3=structures["Left-Inf-Lat-Vent"],
        right_inf_lat_vent_mm3=structures["Right-Inf-Lat-Vent"],
        third_ventricle_mm3=structures.get("3rd-Ventricle"),
        fourth_ventricle_mm3=structures.get("4th-Ventricle"),
        left_cerebral_cortex_mm3=left_cortex,
        right_cerebral_cortex_mm3=right_cortex,
        cortex_source=cortex_source,
        measures=measure_values,
        structures=structures,
    )


def _cortex_volumes(
    structures: dict[str, float], measures: dict[str, float]
) -> tuple[float | None, float | None, str | None]:
    """Cerebral cortex volume per hemisphere, from whichever of the three
    places this file records it.
    """
    if "Left-Cerebral-Cortex" in structures and "Right-Cerebral-Cortex" in structures:
        return (
            structures["Left-Cerebral-Cortex"],
            structures["Right-Cerebral-Cortex"],
            "structure_table",
        )
    if "lhCortexVol" in measures and "rhCortexVol" in measures:
        return measures["lhCortexVol"], measures["rhCortexVol"], "measures"
    left = [v for name, v in structures.items() if name.startswith("ctx-lh-")]
    right = [v for name, v in structures.items() if name.startswith("ctx-rh-")]
    if left and right:
        return sum(left), sum(right), "dkt_sum"
    return None, None, None


def parse_aparc_stats(
    text: str, hemisphere: Hemisphere, *, source: str | None = None
) -> AparcStats:
    source = source or f"{hemisphere}.aparc.stats"
    _measures, rows, header = _parse_table(text, source, ("StructName", "ThickAvg", "SurfArea"))

    declared = header.get("hemi")
    if declared in ("lh", "rh") and declared != hemisphere:
        raise StatsParseError(
            f"{source} is a {declared} file but was supplied as the {hemisphere} hemisphere."
        )

    regions: dict[str, AparcRegion] = {}
    for row in rows:
        line_no = int(row["__line__"])
        thickness = _to_float(row["ThickAvg"], "ThickAvg", line_no, source)
        area = _to_float(row["SurfArea"], "SurfArea", line_no, source)
        if thickness < 0 or area < 0:
            raise StatsParseError(
                f"{source}: line {line_no} has a negative thickness or surface area."
            )
        regions[row["StructName"]] = AparcRegion(thick_avg_mm=thickness, surf_area_mm2=area)

    if not regions:
        raise StatsParseError(f"{source} has no region rows.")
    return AparcStats(hemisphere=hemisphere, regions=regions)


# --------------------------------------------------------------------------
# Files on disk
# --------------------------------------------------------------------------


def vinn_sibling(path: Path) -> Path:
    """`aseg+DKT.stats` -> `aseg+DKT.VINN.stats`."""
    return path.with_name(f"{path.stem}.VINN{path.suffix}")


def read_stats_file(path: Path) -> tuple[str, Path]:
    """Reads a stats file, returning its text and the path actually read.

    FastSurfer writes `aseg+DKT.stats` as a symlink to
    `aseg+DKT.VINN.stats`. Copied to or mounted on Windows, the link reads
    as a zero-byte file, so an empty file falls back to its `.VINN` sibling.
    """
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise StatsParseError(f"{path.name} could not be read.") from exc
    if content.strip():
        return decode_stats(content), path

    sibling = vinn_sibling(path)
    if sibling != path and sibling.is_file():
        sibling_content = sibling.read_bytes()
        if sibling_content.strip():
            return decode_stats(sibling_content), sibling
    raise StatsParseError(
        f"{path.name} is empty (0 bytes) and has no usable {sibling.name} beside it. A "
        "FastSurfer symlink reads as empty on Windows; supply the .VINN file."
    )


def _first_usable(directory: Path, names: tuple[str, ...]) -> Path | None:
    """The first of `names` that exists in `directory` and is, or has a
    `.VINN` sibling that is, non-empty.
    """
    for name in names:
        candidate = directory / name
        if not candidate.is_file():
            continue
        if candidate.stat().st_size > 0:
            return candidate
        sibling = vinn_sibling(candidate)
        if sibling.is_file() and sibling.stat().st_size > 0:
            return candidate  # read_stats_file follows it to the sibling
    return None


@dataclass(frozen=True, slots=True)
class StatsFileSet:
    aseg: Path
    lh_aparc: Path | None
    rh_aparc: Path | None


def find_stats_files(stats_dir: Path) -> StatsFileSet:
    """Locates the aseg and aparc files in a subject's `stats/` directory,
    accepting FreeSurfer and FastSurfer names.
    """
    if not stats_dir.is_dir():
        raise StatsParseError("No stats directory was found for this subject.")
    aseg = _first_usable(stats_dir, ASEG_FILENAMES)
    if aseg is None:
        raise StatsParseError(
            f"No aseg stats file was found (looked for {', '.join(ASEG_FILENAMES)})."
        )
    return StatsFileSet(
        aseg=aseg,
        lh_aparc=_first_usable(stats_dir, tuple(n.format(hemi="lh") for n in APARC_FILENAMES)),
        rh_aparc=_first_usable(stats_dir, tuple(n.format(hemi="rh") for n in APARC_FILENAMES)),
    )


@dataclass(frozen=True, slots=True)
class ParsedStats:
    aseg: AsegStats
    lh_aparc: AparcStats | None
    rh_aparc: AparcStats | None


def parse_stats_bytes(
    aseg: bytes, lh_aparc: bytes | None = None, rh_aparc: bytes | None = None
) -> ParsedStats:
    """Parses uploaded file contents. Both aparc files or neither."""
    if (lh_aparc is None) != (rh_aparc is None):
        raise StatsParseError(
            "Supply both lh_aparc and rh_aparc, or neither. Cortical thickness needs both "
            "hemispheres."
        )
    if not aseg.strip():
        raise StatsParseError(
            "The aseg file is empty (0 bytes). A FastSurfer aseg+DKT.stats symlink reads as "
            "empty on Windows; upload aseg+DKT.VINN.stats instead."
        )
    return ParsedStats(
        aseg=parse_aseg_stats(decode_stats(aseg)),
        lh_aparc=parse_aparc_stats(decode_stats(lh_aparc), "lh") if lh_aparc is not None else None,
        rh_aparc=parse_aparc_stats(decode_stats(rh_aparc), "rh") if rh_aparc is not None else None,
    )


def parse_stats_dir(stats_dir: Path) -> ParsedStats:
    """Parses a subject's `stats/` directory from disk."""
    files = find_stats_files(stats_dir)
    aseg_text, aseg_path = read_stats_file(files.aseg)
    aseg = parse_aseg_stats(aseg_text, source=aseg_path.name)

    lh = rh = None
    if files.lh_aparc is not None and files.rh_aparc is not None:
        lh_text, lh_path = read_stats_file(files.lh_aparc)
        rh_text, rh_path = read_stats_file(files.rh_aparc)
        lh = parse_aparc_stats(lh_text, "lh", source=lh_path.name)
        rh = parse_aparc_stats(rh_text, "rh", source=rh_path.name)
    return ParsedStats(aseg=aseg, lh_aparc=lh, rh_aparc=rh)
