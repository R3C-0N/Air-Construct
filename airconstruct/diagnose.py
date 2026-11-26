"""Recognise the usual reasons a run fails or does not converge, and name the part.

Owned by: workstream "runner" (run control).

``diagnose(case, case_dir, result)`` reads the logs and the live history of a finished,
stopped or failed run; ``check_case(case)`` looks at the case before meshing;
``check_mesh(case, case_dir)`` at the regions after meshing. Each returns a list of
:class:`Diagnosis`, most important first. ``code`` is stable (for the UI and tests),
``message`` and ``hint`` are user-facing text (French, like the UI), ``part`` names the
offending component, region or opening when there is one.

Codes:
  openfoam_missing     OpenFOAM or one of its tools could not be started
  mesh_failed          a meshing tool failed (bad_mesh: cells with negative volume)
  part_not_meshed      a component got no cell (thinner than the mesh can resolve)
  parts_overlap        two components overlap (the first one listed keeps the overlap)
  heat_trapped         a heated part has no path to the air (nothing can cool it)
  solver_diverged      floating point exception / NaN in the solver, region named
  temperature_runaway  a region's temperature went out of any physical range
  setup_error          OpenFOAM rejected a case file (missing keyword, unknown type...)
  mpi_error            parallel run could not start
  disk_full            no space left on the disk
  out_of_memory        the solver was killed (usually by the system, out of memory)
  not_converged        the iteration budget ran out, with the part still moving most
  stopped              the user stopped the run (it can be resumed)
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .case import Case

MAX_T_C = 1000.0  # above this, a temperature is a numerical runaway, not physics
MIN_T_C = -100.0


@dataclass
class Diagnosis:
    code: str
    severity: str  # "error" | "warning" | "info"
    message: str
    part: str | None = None
    hint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- logs


def _tail(path: Path, n: int = 400) -> list[str]:
    try:
        with open(path, errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    return [line.rstrip("\n") for line in lines[-n:]]


def _logs(case_dir: Path) -> dict[str, list[str]]:
    """{log name: last lines}, most recent file last."""
    files = sorted(Path(case_dir).glob("log.*"), key=lambda p: p.stat().st_mtime)
    return {p.name[4:]: _tail(p) for p in files}


def _fatal_text(lines: list[str]) -> str:
    """The message of the last FOAM FATAL (IO) ERROR block, on one line."""
    idx = [i for i, ln in enumerate(lines) if "FOAM FATAL" in ln]
    if not idx:
        return ""
    out = []
    for ln in lines[idx[-1] + 1:idx[-1] + 12]:
        s = ln.strip()
        if s.startswith(("From function", "From ", "in file", "FOAM exiting", "FOAM aborting")):
            break
        if s:
            out.append(s)
    return " ".join(out)


_REGION = re.compile(r"^Solving for (?:fluid|solid) region (\w+)")
_MINMAX = re.compile(r"^Min/max T:\s*(\S+)\s+(\S+)")


def _last_region(lines: list[str]) -> str | None:
    for ln in reversed(lines):
        m = _REGION.match(ln)
        if m:
            return m.group(1)
    return None


def _wild_region(lines: list[str]) -> tuple[str, float] | None:
    """(region, temperature degC) of the last out-of-range Min/max T, if any."""
    region = None
    hit = None
    for ln in lines:
        m = _REGION.match(ln)
        if m:
            region = m.group(1)
            continue
        m = _MINMAX.match(ln)
        if m and region:
            try:
                lo, hi = float(m.group(1)) - 273.15, float(m.group(2)) - 273.15
            except ValueError:
                continue
            if not (math.isfinite(lo) and math.isfinite(hi)):
                hit = (region, float("nan"))
            elif hi > MAX_T_C or lo < MIN_T_C:
                hit = (region, hi if hi > MAX_T_C else lo)
    return hit


def _part_label(case: Case, region: str | None) -> str | None:
    if region is None:
        return None
    return region if region in {c.name for c in case.components} | {case.fluid_region} else None


# --------------------------------------------------------------------------- checks


def check_case(case: Case) -> list[Diagnosis]:
    """Problems visible on the case alone, before meshing."""
    out: list[Diagnosis] = []
    comps = case.components
    for i, a in enumerate(comps):
        for b in comps[:i]:
            if all(a.box.min[k] < b.box.max[k] - 1e-12 and b.box.min[k] < a.box.max[k] - 1e-12
                   for k in range(3)):
                out.append(Diagnosis(
                    "parts_overlap", "warning",
                    f"Les pièces « {b.name} » et « {a.name} » se chevauchent : le volume commun "
                    f"est attribué à « {b.name} ».", a.name,
                    "Déplacez ou redimensionnez l'une des deux pièces."))
    return out


def _interfaces(case_dir: Path, region: str) -> list[str]:
    f = Path(case_dir) / "constant" / region / "polyMesh" / "boundary"
    try:
        txt = f.read_text(errors="replace")
    except OSError:
        return []
    return re.findall(rf"^\s*{re.escape(region)}_to_(\w+)\s*$", txt, flags=re.MULTILINE)


def check_mesh(case: Case, case_dir: Path) -> list[Diagnosis]:
    """Problems visible once the regions are split: parts without cells, and heated
    parts with no conduction path to the air."""
    out: list[Diagnosis] = []
    case_dir = Path(case_dir)
    air = case.fluid_region
    meshed = {c.name for c in case.components
              if (case_dir / "constant" / c.name / "polyMesh" / "boundary").exists()}
    if not (case_dir / "constant" / air / "polyMesh" / "boundary").exists():
        return out  # not split yet
    for c in case.components:
        if c.name not in meshed:
            out.append(Diagnosis(
                "part_not_meshed", "error",
                f"La pièce « {c.name} » n'a reçu aucune maille.", c.name,
                "Elle est trop fine pour le maillage ou recouverte par une autre pièce : "
                "augmentez la précision ou épaississez-la."))
    links = {name: set(_interfaces(case_dir, name)) for name in meshed}
    reach, todo = {air}, [air]
    while todo:  # parts connected to the air, directly or through other parts
        cur = todo.pop()
        for name, nbrs in links.items():
            if name not in reach and (cur in nbrs or (cur in links and name in links[cur])):
                reach.add(name)
                todo.append(name)
    for c in case.components:
        if c.name in meshed and c.power > 0 and c.name not in reach:
            out.append(Diagnosis(
                "heat_trapped", "error",
                f"La pièce « {c.name} » dissipe {c.power:g} W mais ne touche ni l'air ni une "
                "pièce en contact avec l'air : rien ne peut la refroidir.", c.name,
                "Vérifiez sa position : elle est sans doute enfermée dans une autre pièce."))
    return out


def diagnose(case: Case, case_dir: Path, result=None) -> list[Diagnosis]:
    """Why a run failed, did not converge or stopped (empty for a clean converged run)."""
    case_dir = Path(case_dir)
    logs = _logs(case_dir)
    out: list[Diagnosis] = []
    errors = "\n".join(getattr(result, "errors", []) or [])
    everything = "\n".join("\n".join(v) for v in logs.values()) + "\n" + errors
    solver = logs.get("chtMultiRegionSimpleFoam", [])

    if "cannot resume" in errors:
        out.append(Diagnosis("setup_error", "error", errors.split("\n")[0]))
        return out
    if re.search(r"No space left on device", everything):
        out.append(Diagnosis("disk_full", "error", "Le disque est plein.", None,
                             "Libérez de la place (anciens calculs) puis reprenez le calcul."))
    if re.search(r"command not found|No such file or directory.*(?:Foam|Mesh|mpirun)|"
                 r"OpenFOAM not found|cannot execute", everything):
        out.append(Diagnosis("openfoam_missing", "error",
                             "OpenFOAM ou l'un de ses outils n'a pas pu être lancé.", None,
                             "Vérifiez l'installation d'OpenFOAM (WSL2 sous Windows)."))
    if re.search(r"not enough slots|mpirun (?:was unable|noticed|detected)|MPI_ABORT", everything):
        out.append(Diagnosis("mpi_error", "error", "Le calcul parallèle n'a pas pu démarrer.",
                             None, "Réduisez le nombre de cœurs ou passez à 1 cœur."))

    # meshing
    mesh_logs = [k for k in logs if k.split(".")[0] in
                 ("blockMesh", "topoSet", "createPatch", "splitMeshRegions", "decomposePar")]
    for name in mesh_logs:
        fatal = _fatal_text(logs[name])
        if fatal:
            bad = re.search(r"negative (?:cell )?volume|Zero or negative", fatal, re.IGNORECASE)
            out.append(Diagnosis(
                "bad_mesh" if bad else "mesh_failed", "error",
                f"Le maillage a échoué ({name.split('.')[0]}) : {fatal}", None,
                "Vérifiez que les pièces et les ouvertures sont dans l'enceinte et ne sont pas "
                "plus fines que les mailles."))
    out.extend(check_mesh(case, case_dir))

    # solver
    fatal = _fatal_text(solver)
    region = _last_region(solver)
    wild = _wild_region(solver)
    if wild:
        reg, t = wild
        temp = "non définie (NaN)" if math.isnan(t) else f"{t:.0f} °C"
        out.append(Diagnosis(
            "temperature_runaway", "error",
            f"La température de « {reg} » est sortie de toute plage physique ({temp}).",
            _part_label(case, reg),
            "Vérifiez sa puissance et son matériau, et qu'elle est bien en contact avec l'air."))
    if re.search(r"Floating point exception|sigFpe|nan\b.*residual|Initial residual = nan",
                 "\n".join(solver), re.IGNORECASE):
        out.append(Diagnosis(
            "solver_diverged", "error",
            "Le solveur a divergé" + (f" pendant le calcul de « {region} »." if region else "."),
            _part_label(case, region),
            "Baissez la précision pour un premier calcul, vérifiez les débits et les puissances."))
    elif fatal:
        if re.search(r"Maximum number of iterations exceeded", fatal):
            out.append(Diagnosis(
                "temperature_runaway", "error",
                f"La température de « {region} » n'a pas pu être calculée (valeur hors limites).",
                _part_label(case, region),
                "Vérifiez la puissance de la pièce et qu'elle touche l'air."))
        else:
            out.append(Diagnosis(
                "setup_error", "error", f"OpenFOAM a refusé le cas : {fatal}",
                _part_label(case, region), "Signalez-le avec le journal du calcul."))
    if result is not None and not getattr(result, "success", True) and not out:
        m = re.search(r"failed \(exit (-?\d+)\)", errors)
        code = int(m.group(1)) if m else None
        if code in (-9, 137):
            out.append(Diagnosis("out_of_memory", "error",
                                 "Le solveur a été arrêté par le système (mémoire insuffisante ?).",
                                 None, "Baissez la précision ou fermez d'autres applications."))

    # ended without converging
    if result is not None and getattr(result, "success", False) and not out:
        if getattr(result, "stopped", False):
            out.append(Diagnosis("stopped", "info",
                                 f"Calcul arrêté à l'itération {result.iterations}.", None,
                                 "Il peut être repris là où il s'est arrêté."))
        elif not getattr(result, "converged", True):
            out.append(_not_converged(case, case_dir, result))
    return out


def _not_converged(case: Case, case_dir: Path, result) -> Diagnosis:
    """Name the part whose max temperature still moves the most at the end."""
    from .monitor import read_history

    hist = read_history(case_dir)
    part, drift = None, 0.0
    if len(hist) > 10:
        a, b = hist[-max(10, len(hist) // 10)], hist[-1]
        for reg, t in b.temperatures.items():
            old = a.temperatures.get(reg, {}).get("max_c", math.nan)
            d = abs(t.get("max_c", math.nan) - old)
            if math.isfinite(d) and d > drift and reg != case.fluid_region:
                part, drift = reg, d
    msg = f"Pas de convergence en {result.iterations} itérations"
    msg += (f" : la température de « {part} » bouge encore ({drift:.2f} K sur la fin)."
            if part and drift > 0 else ".")
    return Diagnosis("not_converged", "warning", msg, part,
                     "Reprenez le calcul avec plus d'itérations.")
