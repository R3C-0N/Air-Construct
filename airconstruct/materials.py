"""Built-in material library (SI units).

Owned by: core. Other modules look materials up with :func:`get_material`.
"""

from __future__ import annotations

from pydantic import BaseModel


class Solid(BaseModel):
    name: str
    kappa: float  # thermal conductivity [W/m/K]
    rho: float  # density [kg/m3]
    cp: float  # specific heat [J/kg/K]


class Gas(BaseModel):
    name: str
    mol_weight: float  # [g/mol]
    cp: float  # [J/kg/K]
    mu: float  # dynamic viscosity [Pa.s]
    pr: float  # Prandtl number [-]


SOLIDS: dict[str, Solid] = {
    s.name: s
    for s in [
        Solid(name="aluminium", kappa=200.0, rho=2700.0, cp=900.0),
        Solid(name="copper", kappa=390.0, rho=8900.0, cp=385.0),
        Solid(name="fr4", kappa=0.3, rho=1850.0, cp=1100.0),
        Solid(name="silicon", kappa=148.0, rho=2330.0, cp=710.0),
        Solid(name="steel", kappa=16.0, rho=8000.0, cp=500.0),
        Solid(name="plastic_abs", kappa=0.17, rho=1050.0, cp=1400.0),
    ]
}

GASES: dict[str, Gas] = {
    "air": Gas(name="air", mol_weight=28.96, cp=1005.0, mu=1.81e-5, pr=0.71),
}


def get_material(name: str) -> Solid | Gas:
    """Return a solid or gas by name, raising KeyError with the known names."""
    if name in SOLIDS:
        return SOLIDS[name]
    if name in GASES:
        return GASES[name]
    raise KeyError(f"Unknown material {name!r}. Known: {sorted(SOLIDS) + sorted(GASES)}")
