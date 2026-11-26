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

## Quick start

```bash
pip install -e ".[dev,results,ui]"
airconstruct check examples/electronics_box.yaml
airconstruct run examples/electronics_box.yaml
pytest -q                 # unit tests
pytest -q -m openfoam     # simulation tests (needs OpenFOAM)
```

## Conventions

Units are SI, temperatures in °C in the case file. z is up. The enclosure spans
`[0, x] × [0, y] × [0, z]`; components are boxes inside it; openings are rectangles on
an enclosure face. The fluid region is named `air`, each component is a solid region
named after it, interfaces are named `<a>_to_<b>`.
