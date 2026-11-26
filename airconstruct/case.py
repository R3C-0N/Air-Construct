"""Simulation case description: the single source of truth shared by every module.

Owned by: core. A case is saved as YAML and validated with pydantic.

Coordinate system: metres, z is up (gravity along -z). The enclosure is the box
[0, size.x] x [0, size.y] x [0, size.z]. Every component is an axis-aligned box
inside it. Openings (inlets/outlets) are rectangles lying on one enclosure face.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .materials import GASES, SOLIDS


class Vec3(BaseModel):
    x: float
    y: float
    z: float

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


class Face(str, Enum):
    xmin = "xmin"
    xmax = "xmax"
    ymin = "ymin"
    ymax = "ymax"
    zmin = "zmin"
    zmax = "zmax"


class Box(BaseModel):
    """Axis-aligned box given by its min corner and its size."""

    origin: Vec3
    size: Vec3

    @property
    def min(self) -> tuple[float, float, float]:
        return self.origin.as_tuple()

    @property
    def max(self) -> tuple[float, float, float]:
        o, s = self.origin, self.size
        return (o.x + s.x, o.y + s.y, o.z + s.z)


class Component(BaseModel):
    """A solid part (chip, heatsink, board...). power > 0 makes it a heat source."""

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    box: Box
    material: str = "aluminium"
    power: float = 0.0  # dissipated power [W]

    @field_validator("material")
    @classmethod
    def _known_solid(cls, v: str) -> str:
        if v not in SOLIDS:
            raise ValueError(f"unknown solid material {v!r}, known: {sorted(SOLIDS)}")
        return v


class OpeningKind(str, Enum):
    inlet = "inlet"  # fixed velocity / flow rate, fixed temperature
    outlet = "outlet"  # fixed static pressure


class Opening(BaseModel):
    """Rectangle on an enclosure face. center/size are the 2 in-plane coordinates
    in axis order (for xmin/xmax: (y, z); ymin/ymax: (x, z); zmin/zmax: (x, y))."""

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    kind: OpeningKind
    face: Face
    center: tuple[float, float]
    size: tuple[float, float]
    flow_rate: float | None = None  # inlet volumetric flow [m3/s]; or velocity
    velocity: float | None = None  # inlet normal velocity [m/s]
    temperature: float | None = None  # inlet temperature [degC]; default ambient

    @model_validator(mode="after")
    def _inlet_has_flow(self) -> Opening:
        if self.kind == OpeningKind.inlet and self.flow_rate is None and self.velocity is None:
            raise ValueError(f"inlet {self.name!r} needs flow_rate or velocity")
        return self

    def area(self) -> float:
        return self.size[0] * self.size[1]

    def normal_velocity(self) -> float:
        """Inlet speed [m/s] (positive = into the domain)."""
        if self.velocity is not None:
            return self.velocity
        assert self.flow_rate is not None
        return self.flow_rate / self.area()


class Precision(BaseModel):
    """Mesh resolution. level 1 (very coarse, fast) .. 5 (very fine, slow).
    cell_size overrides the level when given (expert mode)."""

    level: int = Field(default=3, ge=1, le=5)
    cell_size: float | None = None


class Solver(BaseModel):
    iterations: int = 20000  # cap on total solver iterations
    cores: int = 1
    turbulence: str = "kOmegaSST"  # or "laminar"
    gravity: bool = True
    convergence_tol: float = 1e-4


class Case(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    enclosure: Vec3
    fluid: str = "air"
    ambient_temperature: float = 25.0  # [degC]
    ambient_pressure: float = 101325.0  # [Pa]
    components: list[Component] = []
    openings: list[Opening] = []
    precision: Precision = Precision()
    solver: Solver = Solver()

    @field_validator("fluid")
    @classmethod
    def _known_gas(cls, v: str) -> str:
        if v not in GASES:
            raise ValueError(f"unknown fluid {v!r}, known: {sorted(GASES)}")
        return v

    @model_validator(mode="after")
    def _check_geometry(self) -> Case:
        names = [c.name for c in self.components] + [o.name for o in self.openings]
        reserved = {"air", self.fluid, "walls", "defaultFaces"}
        dup = {n for n in names if names.count(n) > 1} | (set(names) & reserved)
        if dup:
            raise ValueError(f"duplicate or reserved names: {sorted(dup)}")
        E = self.enclosure.as_tuple()
        for c in self.components:
            lo, hi = c.box.min, c.box.max
            if any(s <= 0 for s in c.box.size.as_tuple()):
                raise ValueError(f"component {c.name!r} has a non-positive size")
            if any(lo[i] < 0 or hi[i] > E[i] + 1e-12 for i in range(3)):
                raise ValueError(f"component {c.name!r} is outside the enclosure")
        if not any(o.kind == OpeningKind.inlet for o in self.openings):
            raise ValueError("the case needs at least one inlet")
        if not any(o.kind == OpeningKind.outlet for o in self.openings):
            raise ValueError("the case needs at least one outlet")
        return self

    # --- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> Case:
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False))

    @property
    def fluid_region(self) -> str:
        return "air"

    @property
    def solid_regions(self) -> list[str]:
        return [c.name for c in self.components]
