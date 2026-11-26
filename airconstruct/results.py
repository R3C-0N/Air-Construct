"""Post-processing with PyVista: key numbers, images, report.

Owned by: workstream "results".

Public interface (used by cli and ui):
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .case import Case


@dataclass
class ComponentResult:
    name: str
    power_w: float
    t_max_c: float
    t_mean_c: float


@dataclass
class Summary:
    components: list[ComponentResult]
    air_t_max_c: float
    air_v_max: float  # [m/s]
    pressure_drop_pa: float  # mean inlet pressure - mean outlet pressure
    outlet_t_mean_c: float


def load_regions(case_dir: Path) -> dict:
    """Return {region_name: pyvista.UnstructuredGrid} for the latest time."""
    raise NotImplementedError


def summarize(case: Case, case_dir: Path) -> Summary:
    raise NotImplementedError


def render_images(case: Case, case_dir: Path, out_dir: Path) -> list[Path]:
    """Off-screen PNGs: temperature slice, velocity slice, streamlines, component temps."""
    raise NotImplementedError


def write_report(case: Case, case_dir: Path, out_dir: Path) -> Path:
    """HTML report (numbers + images). Returns its path."""
    raise NotImplementedError
