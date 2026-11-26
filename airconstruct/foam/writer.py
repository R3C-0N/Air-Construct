"""Write a complete chtMultiRegionSimpleFoam case (everything except the mesh).

Owned by: workstream "solver setup".

Layout produced in case_dir (OpenFOAM.com, steady conjugate heat transfer):
  constant/regionProperties
  constant/air/{thermophysicalProperties, g, turbulenceProperties, radiationProperties}
  constant/<solid>/{thermophysicalProperties, radiationProperties, fvOptions (if power>0)}
  system/{controlDict, fvSchemes, fvSolution, decomposeParDict}
  system/<region>/{fvSchemes, fvSolution, decomposeParDict}
  0/air/{U, p, p_rgh, T, k, omega, nut, alphat}
  0/<solid>/{T, p}
Boundary names follow mesh.py: openings by name, "walls", interfaces "<a>_to_<b>"
(matched with regex keys).
"""

from __future__ import annotations

import math
from pathlib import Path

from ..case import Case, Component, OpeningKind
from ..materials import GASES, SOLIDS
from ..mesh import FACE_AXES
from .dictionary import Raw, write_foam_dict

SOLVER_APP = "chtMultiRegionSimpleFoam"
K0 = 273.15


def _tk(c: float) -> float:
    return round(c + K0, 6)


# --------------------------------------------------------------------------- constant
def _fluid_thermo(case: Case) -> dict:
    g = GASES[case.fluid]
    return {
        "thermoType": {
            "type": "heRhoThermo", "mixture": "pureMixture", "transport": "const",
            "thermo": "hConst", "equationOfState": "perfectGas", "specie": "specie",
            "energy": "sensibleEnthalpy",
        },
        "mixture": {
            "specie": {"molWeight": g.mol_weight},
            "thermodynamics": {"Cp": g.cp, "Hf": 0},
            "transport": {"mu": g.mu, "Pr": g.pr},
        },
    }


def _solid_thermo(c: Component) -> dict:
    m = SOLIDS[c.material]
    return {
        "thermoType": {
            "type": "heSolidThermo", "mixture": "pureMixture", "transport": "constIso",
            "thermo": "hConst", "equationOfState": "rhoConst", "specie": "specie",
            "energy": "sensibleEnthalpy",
        },
        "mixture": {
            "specie": {"molWeight": 50},
            "transport": {"kappa": m.kappa},
            "thermodynamics": {"Hf": 0, "Cp": m.cp},
            "equationOfState": {"rho": m.rho},
        },
    }


def _heat_source(c: Component) -> dict:
    return {
        "heatSource": {
            "type": "scalarSemiImplicitSource",
            "active": True,
            "selectionMode": "all",
            "volumeMode": "absolute",
            "injectionRateSuSp": {"h": f"({c.power:g} 0)"},
            "scalarSemiImplicitSourceCoeffs": {
                "selectionMode": "all",
                "volumeMode": "absolute",
                "injectionRateSuSp": {"h": f"({c.power:g} 0)"},
            },
        }
    }


# --------------------------------------------------------------------------- system
def _control_dict(case: Case) -> dict:
    return {
        "application": SOLVER_APP,
        "startFrom": "latestTime",
        "startTime": 0,
        "stopAt": "endTime",
        "endTime": case.solver.iterations,
        "deltaT": 1,
        "writeControl": "timeStep",
        "writeInterval": max(50, case.solver.iterations // 4),
        "purgeWrite": 2,
        "writeFormat": "ascii",
        "writePrecision": 8,
        "writeCompression": "off",
        "timeFormat": "general",
        "timePrecision": 6,
        "runTimeModifiable": True,
        # SIGUSR1 = write now and stop: used by the runner to end a phase cleanly
        "OptimisationSwitches": {"stopAtWriteNowSignal": 10},
        # live monitors (function objects) written by the runner, see foam/monitors.py
        "functions": {"monitors": Raw('#includeIfPresent "monitors"')},
    }


def _fluid_schemes() -> dict:
    up = "bounded Gauss upwind"
    return {
        "ddtSchemes": {"default": "steadyState"},
        "gradSchemes": {"default": "Gauss linear"},
        "divSchemes": {
            "default": "none",
            "div(phi,U)": "bounded Gauss linearUpwind grad(U)",
            "div(phi,K)": up,
            "div(phi,h)": up,
            "div(phi,k)": up,
            "div(phi,omega)": up,
            "div(phi,Ekp)": up,
            "div(((rho*nuEff)*dev2(T(grad(U)))))": "Gauss linear",
        },
        "laplacianSchemes": {"default": "Gauss linear corrected"},
        "interpolationSchemes": {"default": "linear"},
        "snGradSchemes": {"default": "corrected"},
        "wallDist": {"method": "meshWave"},
    }


def _fluid_solution(case: Case) -> dict:
    # convergence is decided by runner.ResidualParser (fluid residuals + stable temperatures)
    return {
        "solvers": {
            "rho.*": {"solver": "PCG", "preconditioner": "DIC", "tolerance": 0, "relTol": 0},
            "p_rgh": {"solver": "GAMG", "smoother": "GaussSeidel", "tolerance": 1e-7, "relTol": 0.01},
            "(U|k|omega)": {
                "solver": "PBiCGStab", "preconditioner": "DILU", "tolerance": 1e-7, "relTol": 0.1,
            },
            "h": {"solver": "PBiCGStab", "preconditioner": "DILU", "tolerance": 1e-9, "relTol": 0},
        },
        "SIMPLE": {
            "momentumPredictor": True,
            "nNonOrthogonalCorrectors": 0,
            "pRefCell": 0,
            "pRefValue": case.ambient_pressure,
            "rhoMin": 0.2,
            "rhoMax": 2.0,
            "frozenFlow": False,  # toggled by the runner for energy-only phases
        },
        "relaxationFactors": {
            "fields": {"rho": 1.0, "p_rgh": 0.7},
            "equations": {"U": 0.3, "h": 1.0, "(k|omega)": 0.7},
        },
    }


def _solid_schemes() -> dict:
    return {
        "ddtSchemes": {"default": "steadyState"},
        "gradSchemes": {"default": "Gauss linear"},
        "divSchemes": {"default": "none"},
        "laplacianSchemes": {"default": "Gauss linear corrected"},
        "interpolationSchemes": {"default": "linear"},
        "snGradSchemes": {"default": "corrected"},
    }


def _solid_solution(case: Case) -> dict:
    return {
        # solids are solved to tolerance every outer iteration: with partial solves the
        # conjugate interface converges extremely slowly (several K drift per 100 iterations)
        "solvers": {"h": {"solver": "PCG", "preconditioner": "DIC", "tolerance": 1e-10, "relTol": 0}},
        "SIMPLE": {"nNonOrthogonalCorrectors": 0},
        "relaxationFactors": {"equations": {"h": 1.0}},
    }


def _decompose(n: int) -> dict:
    return {"numberOfSubdomains": n, "method": "scotch"}


def write_case_setup(case: Case, case_dir: Path) -> None:
    """Write constant/ and system/ files (not the mesh dicts)."""
    d = Path(case_dir)
    air = case.fluid_region
    solids = case.solid_regions
    write_foam_dict(
        d / "constant/regionProperties",
        {"regions": f"(fluid ({air}) solid ({' '.join(solids)}))"},
    )
    c = d / "constant" / air
    write_foam_dict(c / "thermophysicalProperties", _fluid_thermo(case))
    g = (0, 0, -9.81) if case.solver.gravity else (0, 0, 0)
    for gdir in (c, d / "constant"):  # some versions read the top-level g
        write_foam_dict(gdir / "g", {"dimensions": "[0 1 -2 0 0 0 0]",
                                     "value": f"({g[0]} {g[1]} {g[2]})"},
                        cls="uniformDimensionedVectorField")
    if case.solver.turbulence == "laminar":
        turb = {"simulationType": "laminar"}
    else:
        turb = {"simulationType": "RAS",
                "RAS": {"RASModel": case.solver.turbulence, "turbulence": "on", "printCoeffs": "on"}}
    write_foam_dict(c / "turbulenceProperties", turb)
    write_foam_dict(c / "radiationProperties", {"radiation": "off", "radiationModel": "none"})

    sysd = d / "system"
    write_foam_dict(sysd / "controlDict", _control_dict(case))
    # top-level fvSchemes/fvSolution are needed by mesh utilities
    write_foam_dict(sysd / "fvSchemes", _solid_schemes())
    write_foam_dict(sysd / "fvSolution", {})
    write_foam_dict(sysd / "decomposeParDict", _decompose(max(1, case.solver.cores)))
    write_foam_dict(sysd / air / "fvSchemes", _fluid_schemes())
    write_foam_dict(sysd / air / "fvSolution", _fluid_solution(case))
    write_foam_dict(sysd / air / "decomposeParDict", _decompose(max(1, case.solver.cores)))
    for comp in case.components:
        cs = d / "constant" / comp.name
        write_foam_dict(cs / "thermophysicalProperties", _solid_thermo(comp))
        write_foam_dict(cs / "radiationProperties", {"radiation": "off", "radiationModel": "none"})
        if comp.power > 0:
            write_foam_dict(cs / "fvOptions", _heat_source(comp))
        write_foam_dict(sysd / comp.name / "fvSchemes", _solid_schemes())
        write_foam_dict(sysd / comp.name / "fvSolution", _solid_solution(case))
        write_foam_dict(sysd / comp.name / "decomposeParDict", _decompose(max(1, case.solver.cores)))


# --------------------------------------------------------------------------- 0/
def _vol(cls: str, dims: str, internal: str, bf: dict) -> tuple[dict, str]:
    return {"dimensions": dims, "internalField": internal, "boundaryField": bf}, cls


def _inlet_vector(case: Case, name: str) -> str:
    o = next(o for o in case.openings if o.name == name)
    normal, at_max, _ = FACE_AXES[o.face]
    v = [0.0, 0.0, 0.0]
    v[normal] = -o.normal_velocity() if at_max else o.normal_velocity()
    return f"uniform ({v[0]:g} {v[1]:g} {v[2]:g})"


def _turbulence_inlet(case: Case) -> tuple[float, float]:
    """k and omega at inlets from 5 % intensity and a length scale of 7 % of the opening."""
    k, om = 1e-6, 1.0
    for o in case.openings:
        if o.kind == OpeningKind.inlet:
            u = abs(o.normal_velocity())
            dh = 2 * o.size[0] * o.size[1] / (o.size[0] + o.size[1])
            k = max(k, 1.5 * (0.05 * u) ** 2)
            om = max(om, math.sqrt(k) / (0.09**0.25 * 0.07 * dh))
    return k, om


def write_initial_fields(case: Case, case_dir: Path) -> None:
    """Write 0/<region>/ fields. Must be called AFTER splitMeshRegions."""
    d = Path(case_dir) / "0"
    air = case.fluid_region
    tamb = _tk(case.ambient_temperature)
    p0 = case.ambient_pressure
    inlets = [o for o in case.openings if o.kind == OpeningKind.inlet]
    outlets = [o for o in case.openings if o.kind == OpeningKind.outlet]
    iface = f"{air}_to_.*"
    k0, om0 = _turbulence_inlet(case)

    def bf(default: dict, inlet: dict | None, outlet: dict | None, interface: dict | None = None,
           per_inlet=None) -> dict:
        out: dict = {".*": default}
        if interface is not None:
            out[iface] = interface
        for o in inlets:
            out[o.name] = per_inlet(o) if per_inlet else inlet
        for o in outlets:
            out[o.name] = outlet
        return out

    wall_T = {"type": "zeroGradient"}
    coupled_fluid = {
        "type": "compressible::turbulentTemperatureCoupledBaffleMixed",
        "value": "$internalField", "Tnbr": "T", "kappaMethod": "fluidThermo",
    }
    fields = {
        "U": _vol("volVectorField", "[0 1 -1 0 0 0 0]", "uniform (0 0 0)", bf(
            {"type": "noSlip"}, None,
            {"type": "pressureInletOutletVelocity", "value": "uniform (0 0 0)"},
            per_inlet=lambda o: {"type": "fixedValue", "value": _inlet_vector(case, o.name)})),
        "p": _vol("volScalarField", "[1 -1 -2 0 0 0 0]", f"uniform {p0}", bf(
            {"type": "calculated", "value": "$internalField"}, None,
            {"type": "calculated", "value": "$internalField"},
            per_inlet=lambda o: {"type": "calculated", "value": "$internalField"})),
        "p_rgh": _vol("volScalarField", "[1 -1 -2 0 0 0 0]", f"uniform {p0}", bf(
            {"type": "fixedFluxPressure", "value": "$internalField"},
            {"type": "fixedFluxPressure", "value": "$internalField"},
            {"type": "prghPressure", "p": f"uniform {p0}", "value": "$internalField"})),
        "T": _vol("volScalarField", "[0 0 0 1 0 0 0]", f"uniform {tamb}", bf(
            wall_T, None,
            {"type": "inletOutlet", "inletValue": f"uniform {tamb}", "value": "$internalField"},
            coupled_fluid,
            per_inlet=lambda o: {"type": "fixedValue", "value": f"uniform {_tk(o.temperature if o.temperature is not None else case.ambient_temperature)}"})),
        "k": _vol("volScalarField", "[0 2 -2 0 0 0 0]", f"uniform {k0:.6g}", bf(
            {"type": "kqRWallFunction", "value": "$internalField"},
            {"type": "fixedValue", "value": f"uniform {k0:.6g}"},
            {"type": "inletOutlet", "inletValue": "$internalField", "value": "$internalField"})),
        "omega": _vol("volScalarField", "[0 0 -1 0 0 0 0]", f"uniform {om0:.6g}", bf(
            {"type": "omegaWallFunction", "value": "$internalField"},
            {"type": "fixedValue", "value": f"uniform {om0:.6g}"},
            {"type": "inletOutlet", "inletValue": "$internalField", "value": "$internalField"})),
        "nut": _vol("volScalarField", "[0 2 -1 0 0 0 0]", "uniform 0", bf(
            {"type": "nutkWallFunction", "value": "uniform 0"},
            {"type": "calculated", "value": "uniform 0"},
            {"type": "calculated", "value": "uniform 0"})),
        "alphat": _vol("volScalarField", "[1 -1 -1 0 0 0 0]", "uniform 0", bf(
            {"type": "compressible::alphatWallFunction", "Prt": 0.85, "value": "uniform 0"},
            {"type": "calculated", "value": "uniform 0"},
            {"type": "calculated", "value": "uniform 0"})),
    }
    if case.solver.turbulence == "laminar":
        for f in ("k", "omega", "nut", "alphat"):
            fields.pop(f)
    for name, (body, cls) in fields.items():
        write_foam_dict(d / air / name, body, cls=cls, obj=name)

    coupled_solid = {
        "type": "compressible::turbulentTemperatureCoupledBaffleMixed",
        "value": "$internalField", "Tnbr": "T", "kappaMethod": "solidThermo",
    }
    from ..thermal_estimate import estimate_temperatures

    t_guess = estimate_temperatures(case)
    for comp in case.components:
        t0 = _tk(t_guess.get(comp.name, case.ambient_temperature))
        body, cls = _vol("volScalarField", "[0 0 0 1 0 0 0]", f"uniform {t0}", {
            ".*": {"type": "zeroGradient"},
            f"{comp.name}_to_.*": coupled_solid,
        })
        write_foam_dict(d / comp.name / "T", body, cls=cls, obj="T")
        body, cls = _vol("volScalarField", "[1 -1 -2 0 0 0 0]", f"uniform {p0}", {
            ".*": {"type": "calculated", "value": "$internalField"},
        })
        write_foam_dict(d / comp.name / "p", body, cls=cls, obj="p")
