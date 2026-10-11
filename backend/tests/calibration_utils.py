"""Test-only calibration files.

No real-data calibration exists yet, and inventing clinical breakpoints is
out of bounds even in a test. So the "real" calibration used by the suite
is the shipped phantom file with its `domain` and `unit` strings relabelled:
structurally a real calibration, numerically the phantom one. It exercises
the engine's gating and must never be copied into `app/calibration/`.
"""

import json
from pathlib import Path

from app.services.biomarkers import UNIT_MM, UNIT_PERMILLE_ICV, UNIT_RATIO
from app.services.calibration import CALIBRATION_DIR, Calibration, load_calibration

TEST_REAL_VERSION = "TEST-ONLY.real-from-phantom"

REAL_UNITS = {
    "hippocampal_volume": UNIT_PERMILLE_ICV,
    "ventricle_brain_ratio": UNIT_RATIO,
    "cortical_thickness": UNIT_MM,
}


def phantom_calibration_dict() -> dict:
    return json.loads((CALIBRATION_DIR / "phantom.v0.json").read_text(encoding="utf-8"))


def write_calibration(directory: Path, raw: dict, name: str = "calibration.json") -> Path:
    path = directory / name
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def write_test_real_calibration(directory: Path) -> Path:
    raw = phantom_calibration_dict()
    raw["version"] = TEST_REAL_VERSION
    raw["domain"] = "real"
    raw["notes"] = "TEST ONLY. Phantom numbers relabelled as real. Not a calibration."
    for key, unit in REAL_UNITS.items():
        raw["biomarkers"][key]["unit"] = unit
    return write_calibration(directory, raw, "real.test.json")


def load_test_real_calibration(directory: Path) -> Calibration:
    return load_calibration(write_test_real_calibration(directory))
