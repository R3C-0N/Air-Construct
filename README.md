# Air-Construct

Open source (GPL v3) desktop tool to simulate air flow and temperature, inspired by
what SimScale does, built on [OpenFOAM](https://www.openfoam.com). First target:
**electronics cooling** (heated components in a ventilated box, conjugate heat transfer).

Status: early prototype.

## How it works

```
case.yaml ──> mesh.py ──> foam/writer.py ──> runner.py ──> results.py ──> report / UI
 (Case)     blockMesh,     chtMultiRegion-     local or      PyVista: max T per
            topoSet,       SimpleFoam setup    WSL2          component, slices,
            splitMesh-                                       streamlines
            Regions
```

- `airconstruct/case.py`: the case model (YAML), single source of truth.
- `airconstruct/materials.py`: material library.
- `airconstruct/mesh.py`: multi-region mesh from axis-aligned boxes.
- `airconstruct/foam/`: OpenFOAM dictionary writer and case setup.
- `airconstruct/runner.py`: execution (Linux native, Windows through WSL2).
- `airconstruct/results.py`: post-processing and report.
- `airconstruct/ui/`: PySide6 desktop window.

## Requirements

- Python 3.10+
- OpenFOAM (openfoam.com, v2406 or newer). On Windows, install it inside WSL2 (Ubuntu).

### Windows (WSL2)

Air-Construct runs OpenFOAM through `wsl.exe`. Cases are copied into the Linux
filesystem (`~/.cache/airconstruct/cases`) to avoid slow `/mnt/c` I/O, and results come
back next to the case on the Windows side. Setup:

```powershell
wsl --install -d Ubuntu          # admin PowerShell, then reboot and open Ubuntu once
```
```bash
# inside Ubuntu
curl -s https://dl.openfoam.com/add-debian-repo.sh | sudo bash
sudo apt-get install -y openfoam2406-default
```

Optional: `AIRCONSTRUCT_WSL_DISTRO` picks a distro other than the default one,
`AIRCONSTRUCT_FOAM_BASHRC` points at a specific OpenFOAM `etc/bashrc`.

## Quick start

```bash
pip install -e ".[dev,results,ui]"
airconstruct check examples/electronics_box.yaml
airconstruct run examples/electronics_box.yaml
pytest -q                 # unit tests
pytest -q -m openfoam     # simulation tests (needs OpenFOAM)
```

## Solver strategy

Steady conjugate heat transfer (`chtMultiRegionSimpleFoam`, k-omega SST, gravity on).
The partitioned fluid/solid coupling makes solid temperatures converge slowly, so
`runner.run_case` works in phases:

1. **flow**: flow and energy together until the flow residuals converge;
2. **energy**: frozen flow, energy only (about 4x cheaper per iteration) until every
   region's maximum temperature is stable;
3. **check**: 200 coupled iterations so buoyancy reacts to the new temperatures; back to
   2 if temperatures still move.

Solid temperatures start from an instant lumped estimate (`thermal_estimate.py`).
Phases end cleanly through SIGUSR1 (`-opt-switch stopAtWriteNowSignal=10`).

Precision levels 1 to 5 set the mesh; `airconstruct check` prints the cell count and a
run time estimate (`estimate.py`).

## Conventions

Units are SI, temperatures in °C in the case file. z is up. The enclosure spans
`[0, x] × [0, y] × [0, z]`; components are boxes inside it; openings are rectangles on
an enclosure face. The fluid region is named `air`, each component is a solid region
named after it, interfaces are named `<a>_to_<b>`.
