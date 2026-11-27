# Desktop UI: architecture and extension points

The window follows the "Studio" mockup: five step tabs (Modèle, Air, Volume d'air,
Simulation, Résultats) over one shared 3D viewport. This page says what the shell
provides and where each tab's work goes, so the tabs can be built in parallel without
touching the same files.

## Layout

```
┌ Fichier ▾  name  modifié │ Modèle ① │ Air │ Volume d'air │ Simulation │ Résultats │  ↶ ↷  ☐ Mode expert ┐
├───────────────┬──────────────────────────────────────────────┬────────────────────┤
│ left panel    │ [tab toolbar]            Dessus Face Côté Iso │ right panel        │
│ (tab)         │            shared 3D viewport                 │ (tab)              │
├───────────────┴──────────────────────────────────────────────┴────────────────────┤
│ ✓ Aucune alerte / 2 alertes ▾   messages…                     mm · °C ▾  Raccourcis ? │
└────────────────────────────────────────────────────────────────────────────────────┘
```

## Files

| File | Owner | What |
|---|---|---|
| `ui/app.py` | shell | `MainWindow`: top bar, tab switching, File menu and recent files, global shortcuts, scene refresh, close/save prompts |
| `ui/context.py` | shell | `AppContext` handed to every tab, `ExpertMode`, `Services` |
| `ui/project.py` | shell | `Project`: editable case data, undo/redo, file, dirty flag, last results, stale flag |
| `ui/selection.py` | shell | `Selection` and selection keys |
| `ui/alerts.py` | shell | `Alert`, `AlertCenter`, case validation alerts |
| `ui/units.py` | shell | `Units` (mm/m, °C/K) and formatting |
| `ui/viewport.py`, `ui/view.py` | shell | the one 3D viewport, scene items, picking, highlight, camera presets |
| `ui/run.py`, `ui/worker.py` | shell | `RunController` (start/stop/progress) around the background worker |
| `ui/statusbar.py` | shell | status card: messages, alerts menu, units menu, shortcuts dialog |
| `ui/theme.py`, `ui/fonts/` | shell | colours (`COLORS`), Inter font, the one style sheet |
| `ui/widgets/` | shared | widgets several tabs use (`Card`, `ObjectTree`, `StepNav`, `ToggleSwitch`); add new shared widgets here |
| `ui/tabs/base.py` | shell | `StepTab` base class |
| `ui/tabs/__init__.py` | shell | `TAB_CLASSES`, the tab order |
| `ui/tabs/model/` | thread Modèle | tab 1 |
| `ui/tabs/air/` | thread Air | tab 2 |
| `ui/tabs/volume/` | thread Volume d'air | tab 3 |
| `ui/tabs/simulation/` | thread Simulation | tab 4 |
| `ui/tabs/results/` | thread Résultats | tab 5 |
| `ui/form.py` | temporary | the old case form, shown by the Modèle tab until the tabs have their own editors |

A tab thread works inside its own `ui/tabs/<key>/` package (add as many modules as
needed there) and its tests in `tests/test_tab_<key>.py`. If it needs something
from the shell that is missing, add a small, generic hook to the shared module in
its own commit and say so in the PR, rather than reaching into another tab.

## Writing a tab

```python
from ..base import StepTab

class AirTab(StepTab):
    key = "air"          # used by alerts and ctx.goto_tab
    title = "Air"

    def __init__(self, ctx):
        super().__init__(ctx)
        ...build widgets once, connect to ctx signals...

    def left_panel(self): ...          # QWidget or None (hidden)
    def right_panel(self): ...         # QWidget or None (hidden)
    def viewport_toolbar(self): ...    # controls shown above the 3D view, or None
    def scene(self): ...               # list[SceneItem] to draw, or None = model geometry
    def alerts(self, project): ...     # this step's checks -> Iterable[Alert]
    def actions(self): ...             # QActions with shortcuts, active while the tab is open
    def activated(self): ...           # tab opened
    def deactivated(self): ...         # another tab opened
```

Call `self.scene_changed.emit()` when what `scene()` returns changes: the shell
redraws if the tab is open. Panels are built once and kept, so keep them in sync with
the project through signals, not only in `activated()`.

## Shared state (`self.ctx`)

**`ctx.project`** (`Project`): the source of truth.

- `project.data` returns a copy of the case data, a dict shaped like
  `Case.model_dump(mode="json")`, in SI units (metres, °C, m³/s, W).
  `project.get("components", 0, "power")` reads one value without copying everything.
- Every change goes through `project.edit(label, fn, merge_key=None)` (`fn` changes a
  copy in place) or `project.set_data(label, data, merge_key=None)`. Each call is one
  undo step named `label` (shown in the undo tooltip). Edits with the same `merge_key`
  less than a second apart merge into one step: use it for typing and dragging, for
  example `merge_key=f"move:{name}"`.
- The data may be invalid while the user edits. `project.case` is the validated
  `Case` or `None`; `project.validation_error` says why; `project.last_valid_case`
  keeps something to draw.
- Keys the `Case` model does not know are kept and saved. UI-only metadata (hidden,
  locked, groups, per-tab preferences) goes under `data["ui"]` (for example
  `data["ui"]["hidden"] = ["ram"]`); changes there do not make results stale.
  New physics fields (fan, grille, walls, per-part max temperature) go into
  `airconstruct/case.py` with the solver support, in a core PR.
- Signals: `changed` (any edit, undo, redo, open), `replaced` (new or opened
  project, history cleared), `dirty_changed`, `path_changed`, `results_changed`,
  `stale_changed`.
- Results: `project.results` is the last `RunRecord(outcome, case, data)` or
  `None`. `project.stale` is True when the model changed since that run (the
  Résultats tab shows the "version précédente du modèle" banner).

**`ctx.selection`** (`Selection`): shared by the trees, the 3D view, inspectors and
alerts. Keys are `"enclosure"`, `"component:<name>"`, `"opening:<name>"`; new object
kinds use `"<kind>:<name>"`. `selection.keys`, `selection.current`,
`selection.set(keys)`, `selection.select(key, additive)`, `selection.rename(old, new)`
(call it when renaming an object), signal `changed(list)`. Deleted objects are
dropped from the selection automatically.

**`ctx.viewport`** (`Viewport`): the only 3D view. Tabs do not create plotters.
A tab describes the scene with `SceneItem`s (`view.py`); set `key` on an item to make
it clickable and highlightable. `view.geometry_items(case)` and
`view.result_items(case, regions, field)` build the standard scenes. Picking emits
the item's key into the selection (Ctrl or Shift adds). `ctx.viewport.frame(keys=None)`
points the camera at those objects (default: the selection) and returns their bounds.
`ctx.viewport.scene.plotter` is the pyvistaqt `QtInteractor` (None when 3D is disabled, as in tests) for widgets
such as move/resize handles.

**`ctx.alerts`** (`AlertCenter`): `Alert(level, message, tab, target)`. Level
`"error"` blocks the run, `"warning"` does not. A tab's `alerts()` is registered
automatically and re-run on every project change; the badge on the tab and the
status bar count follow. Clicking an alert opens its tab and selects its `target`.

**`ctx.units`** (`Units`): display units chosen in the status bar.
`units.length_to_display(m)`, `units.length_from_display(v)`, `units.fmt_length(m)`,
`units.fmt_temperature(c)`, `units.length_symbol`, signal `changed`. Data stays SI.

**`ctx.runner`** (`RunController`): `start()`, `stop()`, `running`,
`progress_value / progress_max / progress_text`, `last_residuals`, `log_lines`;
signals `started`, `log(str)`, `stage(str)`, `residual(int, dict)`, `progress`,
`finished(outcome)`, `running_changed(bool)`. The outcome is stored in
`project.results`.

**`ctx.expert`** (`ExpertMode`): `expert.on`, signal `changed(bool)`. Show advanced
settings only when on.

**`ctx.services`**: `estimate_runtime(case)`, `quick_temperatures(case)`,
`write_report(...)`, `open_report`. Injected so tests run without OpenFOAM.

**`ctx.goto_tab(key)`**, **`ctx.show_message(text)`**, **`ctx.work_dir()`**,
**`ctx.settings`** (QSettings: prefix your keys with the tab key, e.g. `"results/cmap"`).

## Look and feel (theme)

The window follows the mockup's look: grey-blue ground (`#E7EAEF`), white rounded
cards, Inter font, blue accent (`#1D4ED8`). It is set once for the whole app by
`ui/theme.py` (`apply_theme`, called by the window). Tabs never hard-code colours or
write their own style sheets, so every tab looks the same and the theme can change in
one place:

- Build each side panel from one or more `Card("Titre")` (`widgets/card.py`) and put
  the content in `card.body`; a panel that is not a `Card` is wrapped in one.
- Mark widgets with properties instead of styles: `set_prop(button, kind="primary")`
  (blue), `kind="danger"`, `kind="flat"`; `set_prop(label, role="section")` (small
  uppercase title such as "PLACEMENT"), `role="muted"`, `role="title"`,
  `role="pill"` with `tone="warning" | "error" | "success" | "info"`;
  `QToolButton` with `kind="segment"` for segmented choices.
- When a colour is needed in code (a chart, a 3D item, a painted widget), take it from
  `theme.COLORS` (`COLORS["accent"]`, `COLORS["error"]`, ...).
- A new kind of control the sheet does not cover goes into `theme.style_sheet()`, not
  into the tab.

The panels sit beside the 3D view rather than floating over it as in the mockup:
widgets drawn over a VTK OpenGL view are unreliable (they flicker or turn black on
some Windows drivers). The ground, the cards and the gradient behind the 3D scene
give the same look.

## Libraries to use

Use these rather than writing widgets from scratch; they are already dependencies
(`pip install -e ".[ui]"`) and bundled in the desktop app.

| Need | Library |
|---|---|
| Widgets, dialogs, item models, undo/redo (`QUndoStack` is behind `Project`) | PySide6 (Qt 6) |
| 3D view, picking, widgets in the scene (box/plane/line widgets for move, resize, cut planes, probe lines) | pyvista + pyvistaqt (`ctx.viewport.scene.plotter`) |
| Collapsible sections, sliders with labels and ranges, searchable combo boxes, icon fonts, throttled/debounced signals | superqt (`QCollapsible`, `QLabeledDoubleSlider`, `QLabeledDoubleRangeSlider`, `QSearchableComboBox`, `superqt.utils.qthrottled` / `qdebounced`, `superqt.fonticon`) |
| 2D charts: convergence (log scale), temperature bars with limits, line plots, live updates | pyqtgraph (`pyqtgraph.PlotWidget`) |
| Report images | matplotlib (already used by `results.py`) |

## Tests

UI tests run offscreen with the 3D view disabled (`AIRCONSTRUCT_NO_3D=1`). Use the
`qapp` and `settings` fixtures (`tests/conftest.py`) and the helpers of
`tests/ui_support.py`: `make_window(tmp_path, settings, **kw)` builds the window on the
example case with throw-away settings and no "save changes?" prompt (inject
`run_fn`, `summarize_fn`, `load_regions_fn`, `estimate_fn`, `preview_fn`),
`run_and_wait(qapp, win)` runs it with the mocked solver (`fake_run`, `fake_summary`),
`wait_until(qapp, cond)` waits for a condition. A tab is reached with
`win.tabs["<key>"]`, the shared state with `win.ctx`. Examples: `tests/test_ui.py`.

## Language

The interface is in French, like the mockup. Code, comments and tests stay in English.
