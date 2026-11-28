"""Simulation case description: the single source of truth shared by every module.

Owned by: core. A case is saved as YAML and validated with pydantic.

Coordinate system: metres, z is up (gravity along -z). The enclosure is the box
[0, size.x] x [0, size.y] x [0, size.z]. Every component is an axis-aligned box
inside it, optionally carrying the real shape of an imported CAD part (fitted in
its box, see :mod:`airconstruct.cad`). Openings (inlets/outlets) are rectangles lying on one enclosure face.
"""

from __future__ import annotations

import itertools
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .materials import GASES, SOLIDS, Solid


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


class Shape(BaseModel):
    """Real shape of a component, from an imported CAD file: a closed triangle surface
    fitted onto the component's box (scaled per axis from its own bounding box), so
    moving or resizing the box moves or stretches it. The mesh gives the part the
    cells whose centre is inside the surface.

    ``data`` holds the triangles (float32, zlib, base64, see :func:`cad.encode`);
    ``source`` is the file it came from and ``triangles`` their number, for display."""

    data: str
    source: str | None = None
    triangles: int | None = None

    @field_validator("data")
    @classmethod
    def _readable(cls, v: str) -> str:
        from .cad import decode

        if len(decode(v)) < 4:
            raise ValueError("a shape needs at least 4 triangles")
        return v

    def triangles_in(self, box: Box):
        """The triangles (n, 3, 3) fitted onto ``box``."""
        from .cad import placed

        return placed(self.data, box.min, box.max)


class Component(BaseModel):
    """A solid part (chip, heatsink, board...). power > 0 makes it a heat source.

    ``material`` is a built-in solid (materials.SOLIDS) or one of the case's own
    ``materials`` (checked by :class:`Case`). With a ``shape`` the part is the imported
    CAD surface fitted in ``box``, otherwise the whole box. ``max_temperature`` is the highest
    temperature the part may reach [degC]: a pass/fail criterion for the results,
    not used by the solver."""

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    box: Box
    material: str = "aluminium"
    power: float = 0.0  # dissipated power [W]
    max_temperature: float | None = None  # [degC], criterion only
    shape: Shape | None = None  # imported CAD shape; None = the whole box


class OpeningKind(str, Enum):
    inlet = "inlet"  # air comes in: imposed flow, intake fan, or free (pressure) vent
    outlet = "outlet"  # air goes out: fixed static pressure, or exhaust fan


class Grille(BaseModel):
    """Grille, perforated plate or filter covering an opening.

    Modelled as a thin porous layer of air just inside the opening (Darcy-Forchheimer,
    quadratic term only): pressure loss dp = K * rho * u^2 / 2, u being the approach
    velocity through the opening. K comes from the open-area ratio (Idelchik, thin
    sharp-edged perforated plate) unless ``loss_coefficient`` is given."""

    open_area: float = Field(default=0.5, gt=0.0, le=1.0)  # free area / opening area [-]
    loss_coefficient: float | None = Field(default=None, ge=0.0)  # K [-], overrides open_area

    def k(self) -> float:
        if self.loss_coefficient is not None:
            return self.loss_coefficient
        b = self.open_area
        return (0.707 * (1.0 - b) ** 0.5 + 1.0 - b) ** 2 / b**2


class Fan(BaseModel):
    """Fan mounted on an opening: on an inlet it blows air in, on an outlet it extracts.

    Either a fixed volumetric ``flow_rate``, or a fan ``curve``: (flow rate [m3/s],
    static pressure rise [Pa]) points, flow rates increasing. With a curve the operating
    point comes out of the simulation (where the curve meets the system resistance)."""

    flow_rate: float | None = Field(default=None, gt=0.0)  # [m3/s]
    curve: list[tuple[float, float]] | None = None  # [(Q m3/s, dp Pa), ...]

    @model_validator(mode="after")
    def _one_spec(self) -> Fan:
        if (self.flow_rate is None) == (self.curve is None):
            raise ValueError("a fan needs either flow_rate or curve")
        if self.curve is not None:
            c = self.curve
            if len(c) < 2:
                raise ValueError("a fan curve needs at least 2 points")
            if any(q < 0 or dp < 0 for q, dp in c):
                raise ValueError("fan curve flow rates and pressures must be >= 0")
            if any(b[0] <= a[0] for a, b in itertools.pairwise(c)):
                raise ValueError("fan curve flow rates must be increasing")
            if any(b[1] > a[1] + 1e-12 for a, b in itertools.pairwise(c)):
                raise ValueError("fan curve pressure must not increase with flow rate")
        return self

    def pressure_at(self, q: float) -> float:
        """Static pressure rise [Pa] at flow rate q (curve clamped at its ends)."""
        assert self.curve is not None
        qs, dps = zip(*self.curve)
        if q <= qs[0]:
            return dps[0]
        for (q0, p0), (q1, p1) in itertools.pairwise(self.curve):
            if q <= q1:
                return p0 + (p1 - p0) * (q - q0) / (q1 - q0)
        return dps[-1]


class Opening(BaseModel):
    """Rectangle on an enclosure face. center/size are the 2 in-plane coordinates
    in axis order (for xmin/xmax: (y, z); ymin/ymax: (x, z); zmin/zmax: (x, y)).

    What drives the flow through it:
    - inlet with flow_rate or velocity: imposed flow (as a fan with a known flow rate);
    - inlet with a fan: fixed fan flow rate, or a fan curve;
    - inlet with neither: free vent, air enters at ambient total pressure;
    - outlet: ambient static pressure, or an exhaust fan (flow rate or curve).
    A grille adds its pressure loss to any of them."""

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    kind: OpeningKind
    face: Face
    center: tuple[float, float]
    size: tuple[float, float]
    flow_rate: float | None = None  # inlet volumetric flow [m3/s]; or velocity
    velocity: float | None = None  # inlet normal velocity [m/s]
    temperature: float | None = None  # temperature of the air coming in [degC]; default ambient
    fan: Fan | None = None
    grille: Grille | None = None

    @model_validator(mode="after")
    def _flow_spec(self) -> Opening:
        imposed = self.flow_rate is not None or self.velocity is not None
        if imposed and self.kind == OpeningKind.outlet:
            raise ValueError(f"outlet {self.name!r}: set the flow with an exhaust fan, "
                             "not flow_rate/velocity")
        if imposed and self.fan is not None:
            raise ValueError(f"opening {self.name!r}: give flow_rate/velocity or a fan, not both")
        return self

    def area(self) -> float:
        return self.size[0] * self.size[1]

    @property
    def fixed_flow(self) -> bool:
        """True when the flow rate through the opening is imposed."""
        return (self.flow_rate is not None or self.velocity is not None
                or (self.fan is not None and self.fan.flow_rate is not None))

    @property
    def fan_curve(self) -> bool:
        return self.fan is not None and self.fan.curve is not None

    def normal_velocity(self) -> float:
        """Imposed speed [m/s] through the opening, positive in the opening's own
        direction (into the domain for an inlet, out of it for an outlet)."""
        if self.velocity is not None:
            return self.velocity
        if self.flow_rate is not None:
            return self.flow_rate / self.area()
        if self.fan is not None and self.fan.flow_rate is not None:
            return self.fan.flow_rate / self.area()
        raise ValueError(f"opening {self.name!r} has no imposed flow rate")


class WallMode(str, Enum):
    adiabatic = "adiabatic"  # no heat through the wall
    exchange = "exchange"  # conduction through the wall + convection (and radiation) outside


class WallThermal(BaseModel):
    """Thermal behaviour of an enclosure wall.

    exchange: heat crosses the wall (``thickness`` of ``material``, 1D conduction) and
    leaves to the surroundings at ``t_external`` (default: ambient) through an outside
    convection coefficient ``h_external`` and, if ``emissivity`` > 0, by radiation.
    OpenFOAM: externalWallHeatFluxTemperature (mode coefficient) on air and solid sides."""

    mode: WallMode = WallMode.adiabatic
    material: str = "aluminium"
    thickness: float = Field(default=0.001, gt=0.0)  # [m]
    h_external: float = Field(default=5.0, gt=0.0)  # [W/m2/K], ~5 for still air
    t_external: float | None = None  # [degC]; default: ambient temperature
    emissivity: float = Field(default=0.0, ge=0.0, le=1.0)  # outer surface, 0 = no radiation

    @field_validator("material")
    @classmethod
    def _known_solid(cls, v: str) -> str:
        if v not in SOLIDS:
            raise ValueError(f"unknown solid material {v!r}, known: {sorted(SOLIDS)}")
        return v

    def resistance(self) -> float:
        """Conduction + outside convection resistance per unit area [m2.K/W]."""
        return self.thickness / SOLIDS[self.material].kappa + 1.0 / self.h_external


class Walls(WallThermal):
    """Enclosure walls: one setting for every face, with optional per-face overrides
    (``faces: {zmin: {mode: adiabatic}}`` for a box standing on an insulating floor)."""

    faces: dict[Face, WallThermal] = {}

    def for_face(self, face: Face) -> WallThermal:
        if face in self.faces:
            return self.faces[face]
        return WallThermal(**self.model_dump(exclude={"faces"}))

    @property
    def per_face(self) -> bool:
        """True when faces differ, so each face needs its own patch."""
        return bool(self.faces)


class Precision(BaseModel):
    """Mesh resolution. level 1 (very coarse, fast) .. 5 (very fine, slow).
    cell_size overrides the level when given (expert mode)."""

    level: int = Field(default=3, ge=1, le=5)
    cell_size: float | None = None


class Probe(BaseModel):
    """A point where temperature (and air velocity, in the air) is recorded every
    ``solver.probe_interval`` iterations: a virtual thermocouple / anemometer."""

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    point: Vec3


class Refinement(BaseModel):
    """Finer mesh around a component: cells of ``cell_size`` (default: bulk size /
    ``factor``) over the component's extent, growing back to the bulk size over
    ``margin`` (default: 3 bulk cells) around it. The mesh is a structured grid, so the
    refinement spans the whole enclosure along each axis (a slab through the part)."""

    target: str  # component name
    factor: int = Field(default=2, ge=1, le=8)
    cell_size: float | None = Field(default=None, gt=0.0)  # [m], overrides factor
    margin: float | None = Field(default=None, ge=0.0)  # [m]


class Solver(BaseModel):
    iterations: int = 20000  # cap on total solver iterations
    probe_interval: int = Field(default=10, ge=1)  # iterations between probe samples
    cores: int = 1
    turbulence: str = "kOmegaSST"  # or "laminar"
    gravity: bool = True
    convergence_tol: float = 1e-4


class Material(Solid):
    """A solid defined in the case file (custom material), same fields as the
    built-in ones: conductivity kappa [W/m/K], density rho [kg/m3], cp [J/kg/K]."""

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    kappa: float = Field(gt=0.0)
    rho: float = Field(gt=0.0)
    cp: float = Field(gt=0.0)


class Case(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    enclosure: Vec3
    fluid: str = "air"
    materials: list[Material] = []  # custom solids, usable by components
    ambient_temperature: float = 25.0  # [degC]
    ambient_pressure: float = 101325.0  # [Pa]
    components: list[Component] = []
    openings: list[Opening] = []
    walls: Walls = Walls()
    probes: list[Probe] = []
    refinements: list[Refinement] = []
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
        reserved = {"air", self.fluid, "walls", "defaultFaces"} | {f"walls_{f.value}" for f in Face}
        dup = {n for n in names if names.count(n) > 1} | (set(names) & reserved)
        if dup:
            raise ValueError(f"duplicate or reserved names: {sorted(dup)}")
        mnames = [m.name for m in self.materials]
        clash = {n for n in mnames if mnames.count(n) > 1} | (set(mnames) & set(SOLIDS))
        if clash:
            raise ValueError(f"duplicate or built-in material names: {sorted(clash)}")
        known = set(SOLIDS) | set(mnames)
        for c in self.components:
            if c.material not in known:
                raise ValueError(f"component {c.name!r}: unknown solid material "
                                 f"{c.material!r}, known: {sorted(known)}")
        E = self.enclosure.as_tuple()
        for c in self.components:
            lo, hi = c.box.min, c.box.max
            if any(s <= 0 for s in c.box.size.as_tuple()):
                raise ValueError(f"component {c.name!r} has a non-positive size")
            if any(lo[i] < 0 or hi[i] > E[i] + 1e-12 for i in range(3)):
                raise ValueError(f"component {c.name!r} is outside the enclosure")
        comp_names = {c.name for c in self.components}
        for r in self.refinements:
            if r.target not in comp_names:
                raise ValueError(f"refinement target {r.target!r} is not a component")
        pnames = [p.name for p in self.probes]
        if len(set(pnames)) != len(pnames):
            raise ValueError("duplicate probe names")
        for pr in self.probes:
            pt = pr.point.as_tuple()
            if any(pt[i] < 0 or pt[i] > E[i] for i in range(3)):
                raise ValueError(f"probe {pr.name!r} is outside the enclosure")
        if not any(o.kind == OpeningKind.inlet for o in self.openings):
            raise ValueError("the case needs at least one inlet")
        if not any(o.kind == OpeningKind.outlet for o in self.openings):
            raise ValueError("the case needs at least one outlet")
        if all(o.fixed_flow for o in self.openings):
            raise ValueError("every opening has an imposed flow rate: at least one must be "
                             "a free vent, a plain outlet or a fan with a curve")
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

    def wall_patches(self) -> dict[str, list[Face]]:
        """Enclosure wall patch name(s) -> the faces each covers ("walls", or
        "walls_<face>" when walls.faces overrides some faces)."""
        if self.walls.per_face:
            return {f"walls_{f.value}": [f] for f in Face}
        return {"walls": list(Face)}

    def probe_region(self, probe: Probe) -> str:
        """Region holding the probe: the first component listed whose box (or imported
        shape) contains it (overlaps belong to the first one, as in the mesh), else
        the air."""
        pt = probe.point.as_tuple()
        for c in self.components:
            lo, hi = c.box.min, c.box.max
            if all(lo[i] <= pt[i] <= hi[i] for i in range(3)):
                if c.shape is not None:
                    from .cad import voxelize

                    if not voxelize(c.shape.triangles_in(c.box), *([v] for v in pt))[0, 0, 0]:
                        continue
                return c.name
        return self.fluid_region

    def solid(self, material: str) -> Solid:
        """A material by name: the case's own materials first, then the built-in ones."""
        for m in self.materials:
            if m.name == material:
                return m
        return SOLIDS[material]

    @property
    def solid_regions(self) -> list[str]:
        return [c.name for c in self.components]
