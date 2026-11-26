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
- `airconstruct/ui/`: PySide6 desktop window: a shell with five step tabs (Modèle, Air,
  Volume d'air, Simulation, Résultats) over a shared 3D view, see
  [docs/ui-architecture.md](docs/ui-architecture.md).

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

## Download (Windows / Linux)

Every push to `main` (finished work merged from `develop`, see
[CONTRIBUTING.md](CONTRIBUTING.md)) builds the desktop app in GitHub Actions
([Build desktop app](../../actions/workflows/build.yml)): open the latest run and download
the `Air-Construct-windows-x64` artifact (installer `...-setup.exe`, or a portable `.zip`)
or `Air-Construct-linux-x64` (`.tar.gz`). Tagging a version `v*` attaches the same
files to a GitHub release. The installer is per-user and needs no admin rights;
OpenFOAM still has to be installed in WSL2 (see above).

To build it yourself: `pip install ".[ui,results]" pyinstaller` then
`pyinstaller packaging/airconstruct.spec --noconfirm`; the app is in
`dist/Air-Construct/`, and `Air-Construct --self-test` checks that it starts.

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

## Fans, grilles and vents

Each opening says what drives the air through it:

| opening | OpenFOAM boundary (air region) |
|---|---|
| inlet with `flow_rate` / `velocity`, or `fan: {flow_rate: ...}` | fixed velocity |
| inlet with `fan: {curve: [[Q, dp], ...]}` (m3/s, Pa) | `fanPressure`, direction in |
| inlet with nothing: free vent | `prghTotalPressure` at ambient |
| outlet | `prghPressure` at ambient |
| outlet with `fan: {flow_rate: ...}` or `fan: {curve: ...}` | fixed velocity outwards / `fanPressure` out |

With a curve, the operating point is a result: `results.summarize(...).fans` gives the
flow rate and pressure rise of every fan, `airconstruct check` prints a 1D estimate
(`fans.estimate_flow`). `grille: {open_area: 0.5}` (or `loss_coefficient: K`) on any
opening adds a porous layer two cells deep just inside it (cellZone `grille_<name>`,
Darcy-Forchheimer) that drops K rho u^2 / 2; K comes from the open-area ratio (Idelchik,
thin perforated plate). See `examples/fan_box.yaml`.

## Enclosure walls

Walls are adiabatic by default. `walls: {mode: exchange, material: steel, thickness: 0.001,
h_external: 5, emissivity: 0.9}` makes heat cross them (1D conduction through the wall,
then convection and radiation to `t_external`, default ambient), with OpenFOAM's
`externalWallHeatFluxTemperature` on both the air and the solid side. `faces: {zmin:
{mode: adiabatic}}` overrides single faces (the walls are then split into
`walls_<face>` patches). `Summary.heat_through_walls_w` gives the heat they carry away,
and the energy balance includes it.

## Probes

`probes: [{name: cpu_top, point: {x: 0.075, y: 0.055, z: 0.012}}]` records the
temperature (and the air velocity, for a point in the air) every
`solver.probe_interval` iterations, through OpenFOAM `probes` function objects (one
per region). `probes.read_probes(case, case_dir)` returns the whole history, during the
run or after; `Summary.probes` holds the last values.

## Local mesh refinement

`refinements: [{target: cpu, factor: 3, margin: 0.01}]` uses cells 3 times smaller
(or `cell_size`) over the component, growing back to the bulk size over `margin`
(default 3 bulk cells) with graded blockMesh segments. The grid stays structured, so
the refinement is a slab through the whole enclosure along each axis.

## Conventions

Units are SI, temperatures in °C in the case file. z is up. The enclosure spans
`[0, x] × [0, y] × [0, z]`; components are boxes inside it; openings are rectangles on
an enclosure face. The fluid region is named `air`, each component is a solid region
named after it, interfaces are named `<a>_to_<b>`.
