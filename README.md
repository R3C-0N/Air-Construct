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
- `airconstruct/mesh.py`: multi-region mesh from axis-aligned boxes (and imported shapes).
- `airconstruct/cad.py`: CAD import (STL, STEP), shapes stored in the case, voxelization.
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
back next to the case on the Windows side.

Automatic setup: when WSL2, Ubuntu or OpenFOAM is missing, the "OpenFOAM est
introuvable" dialog of the Simulation step offers **Installer automatiquement**
(`airconstruct/foam_setup.py`): `wsl --install` with one UAC prompt, a Windows restart
after which Air-Construct reopens and resumes (`--setup-openfoam`, registered in
`RunOnce`), the first start of Ubuntu (where you create your Linux user), then OpenFOAM
installed as root inside the distro. The Windows installer starts the app with
`--setup-openfoam`, so a fresh PC is offered this right away. On Debian/Ubuntu the same
dialog installs OpenFOAM through `pkexec`. Manual setup:

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
the app then offers to install WSL2 and OpenFOAM (see above).

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

## CAD import (STEP, STL)

Modèle tab > *Importer un fichier CAO…*, the *Importer un fichier CAO* template of
*Nouveau projet*, or Fichier > Ouvrir on a `.step` / `.stp` / `.stl` file. Each body
of the file becomes a solid part, at its place (an assembly keeps its layout). STL has
no unit: the dialog asks (mm by default) and can split a single STL into its closed
shells; STEP gives its own unit and needs the `cascadio` package (OpenCascade,
installed with the `ui` extra, or `pip install "airconstruct[cad]"`).

The shape is stored in the case file (`shape.data`: float32 triangles, zlib, base64)
and fitted onto the part's `box`, so moving or resizing the box moves or stretches it.
The mesh stays the structured grid: the part gets the cells of its box whose centre
is inside the surface (a staircase of the real shape, finer with the precision; see the
mesh cut in the Volume d'air tab). A surface that is not closed may be filled wrongly:
the import dialog says which. *Remplacer par sa boîte* in the inspector turns the part
back into a plain block.

## Claude control (MCP)

Claude can drive the open window through an MCP server shipped with the app
(`Air-Construct-MCP.exe` next to `Air-Construct.exe`, or `airconstruct-mcp` from
source): look at the 3D view, build the model, set up the air and the mesh, run and
read the results, while you watch. A "Claude" pill in the top bar shows each action
and lets you pause it; every change is an undo step. Setup and tool list:
[docs/claude-mcp.md](docs/claude-mcp.md).

## Run control and live results

- **Live monitors**: OpenFOAM function objects (`foam/monitors.py`) print, every
  iteration, the mean temperature of each region, the heat each part gives to the air
  and the flow, temperature and pressure of each opening. `monitor.RunMonitor` turns
  them, with the residuals of every field in every region and the solver's min/max
  temperatures, into one `Progress` snapshot per iteration (`run_case(on_progress=...)`,
  also appended to `<case>/monitor.jsonl` for charts after the fact).
- **Energy balance and pressure drop**: `results.summarize` gives where each part's
  heat goes (air, other parts, walls, openings; `Summary.energy.to_sankey()`) and the
  static and total pressure drop with the flow through each opening.
- **Stop and resume**: a stopped run (or one out of iterations) continues from its last
  iteration: `airconstruct resume runs/<case> [--iterations N]` or
  `runner.resume_case`. Its state is in `<case>/run.json`.
- **Queue and history**: `runs.RunStore` keeps every run in its own directory with a
  row in `runs/history.sqlite`; `runs.RunQueue` runs queued cases one after the other in
  the background; `runs.compare` lists what changed between two runs (case and
  results). CLI: `airconstruct queue add a.yaml b.yaml`, `airconstruct queue run`,
  `airconstruct history`, `airconstruct compare ID1 ID2`.
- **Diagnosis**: when a run fails or does not converge, `RunResult.diagnosis`
  (`diagnose.py`) names the usual cause (divergence, temperature runaway, heated part
  with no path to the air, mesh failure, disk full...) and the part involved.

## Conventions

Units are SI, temperatures in °C in the case file. z is up. The enclosure spans
`[0, x] × [0, y] × [0, z]`; components are boxes inside it; openings are rectangles on
an enclosure face. The fluid region is named `air`, each component is a solid region
named after it, interfaces are named `<a>_to_<b>`.
