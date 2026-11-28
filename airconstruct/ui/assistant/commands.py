"""What Claude can do in the open window: one method per bridge command.

Owned by: workstream "assistant".

Every command runs in the GUI thread on the window the user is looking at, through
the same code paths as the panels: edits are ``project.set_data`` steps named
"Claude · ..." (the user undoes them with Ctrl+Z), the step tab concerned is opened
and the object touched is selected and framed, so the user sees each action happen.
Commands return plain JSON-able dicts (SI units: metres, °C, W, m³/s).
"""

from __future__ import annotations

import base64
import copy
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, QRect, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from ...case import Face
from ...materials import SOLIDS
from ..project import physics_part
from ..selection import ENCLOSURE, component_key, opening_key
from ..tabs.air import ops as air_ops
from ..tabs.model import geometry as geo

MAX_SHOT_WIDTH = 1600  # px, screenshots are scaled down to this
FIELDS = ("T", "U")
CAMERA_NAMES = ("iso", "top", "front", "side")
TOP_LEVEL = {"name", "enclosure", "fluid", "ambient_temperature", "ambient_pressure",
             "walls", "precision", "solver"}


class CommandError(Exception):
    """Refused or impossible command: the message goes back to Claude."""


@dataclass
class Command:
    fn: Callable
    describe: Callable[[dict], str]  # French text shown to the user while it runs
    acts: bool = True  # changes something (refused while the user paused Claude)


# ----------------------------------------------------------------------------- helpers
def json_safe(v):
    """NaN/inf -> None, tuples -> lists, recursively (strict JSON for MCP clients)."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {str(k): json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [json_safe(x) for x in v]
    if isinstance(v, Path):
        return str(v)
    if hasattr(v, "value") and not isinstance(v, (int, str, bool)):  # Enum
        return v.value
    return v


def deep_merge(target: dict, changes: dict) -> None:
    """Merge ``changes`` into ``target`` in place: nested dicts merge, the rest replaces."""
    for k, v in changes.items():
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            deep_merge(target[k], v)
        else:
            target[k] = copy.deepcopy(v)


def vec3(v, what: str) -> dict:
    """[x, y, z] or {"x", "y", "z"} -> {"x", "y", "z"} floats."""
    if isinstance(v, dict):
        try:
            return {a: float(v[a]) for a in "xyz"}
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandError(f"{what}: expected {{x, y, z}} in metres") from exc
    if isinstance(v, (list, tuple)) and len(v) == 3:
        return {a: float(x) for a, x in zip("xyz", v)}
    raise CommandError(f"{what}: expected [x, y, z] in metres")


def pair(v, what: str) -> list[float]:
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return [float(v[0]), float(v[1])]
    raise CommandError(f"{what}: expected [a, b] in metres")


def _find(items: list[dict], name: str, what: str) -> dict:
    for it in items:
        if it.get("name") == name:
            return it
    known = ", ".join(str(i.get("name")) for i in items) or "none"
    raise CommandError(f"no {what} named {name!r} (known: {known})")


def _part_changes(p: dict, changes: dict) -> dict:
    """Accept the short forms origin/size [x, y, z] next to box: {origin, size}."""
    changes = dict(changes)
    box = dict(changes.pop("box", None) or {})
    for key in ("origin", "size"):
        if key in changes:
            box[key] = changes.pop(key)
    for key in list(box):
        box[key] = vec3(box[key], key)
    if box:
        changes["box"] = box
    return changes


def png_base64(img: QImage) -> str:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return base64.b64encode(bytes(ba.data())).decode("ascii")


class Commands:
    """The bridge's command table, bound to one MainWindow."""

    def __init__(self, win) -> None:
        self.win = win
        d = self._describe
        self.table: dict[str, Command] = {
            "ping": Command(lambda: {"app": "Air-Construct"}, lambda p: "", acts=False),
            "get_state": Command(self.get_state, d("Lit l’état du projet"), acts=False),
            "get_model": Command(self.get_model, d("Lit le modèle"), acts=False),
            "screenshot": Command(self.screenshot, d("Regarde l’écran"), acts=False),
            "set_view": Command(self.set_view, d("Change la vue")),
            "new_project": Command(self.new_project, d("Nouveau projet")),
            "open_project": Command(self.open_project,
                                    lambda p: f"Ouvre {Path(str(p.get('path'))).name}"),
            "save_project": Command(self.save_project, d("Enregistre le projet")),
            "update_case": Command(self.update_case, d("Modifie les réglages du cas")),
            "add_part": Command(self.add_part,
                                lambda p: f"Ajoute la pièce « {p.get('name', 'piece')} »"),
            "update_part": Command(self.update_part,
                                   lambda p: f"Modifie la pièce « {p.get('name')} »"),
            "add_opening": Command(self.add_opening, lambda p: "Ajoute une ouverture "
                                   f"« {p.get('name') or p.get('kind', '')} »"),
            "update_opening": Command(self.update_opening,
                                      lambda p: f"Modifie l’ouverture « {p.get('name')} »"),
            "add_probe": Command(self.add_probe,
                                 lambda p: f"Ajoute la sonde « {p.get('name')} »"),
            "add_refinement": Command(self.add_refinement, lambda p: "Affine le maillage "
                                      f"autour de « {p.get('target')} »"),
            "add_material": Command(self.add_material,
                                    lambda p: f"Ajoute le matériau « {p.get('name')} »"),
            "import_cad": Command(self.import_cad, lambda p: "Importe "
                                  f"{Path(str(p.get('path'))).name}"),
            "delete": Command(self.delete, lambda p: f"Supprime « {p.get('name')} »"),
            "undo": Command(self.undo, d("Annule")),
            "redo": Command(self.redo, d("Rétablit")),
            "quick_estimate": Command(self.quick_estimate, d("Estimation rapide")),
            "run_simulation": Command(self.run_simulation, d("Lance le calcul")),
            "run_status": Command(self.run_status, d("Suit le calcul"), acts=False),
            "stop_simulation": Command(self.stop_simulation, d("Arrête le calcul")),
            "get_results": Command(self.get_results, d("Lit les résultats"), acts=False),
        }

    @staticmethod
    def _describe(text: str) -> Callable[[dict], str]:
        return lambda _p: text

    # ------------------------------------------------------------------ shared
    @property
    def project(self):
        return self.win.project

    def _show(self, tab: str | None = None, keys: list[str] | None = None,
              frame: bool = False) -> None:
        """Make the action visible: open its step tab, select (and frame) objects."""
        if tab:
            self.win.goto_tab(tab)
        if keys is not None:
            self.win.selection.set(keys)
            if frame:
                self.win.refresh_scene()
                self.win.viewport.frame(keys)

    def _validation(self) -> dict:
        from ..app import validation_message

        p = self.project
        self.win.alerts.refresh()
        err = p.validation_error
        return {
            "valid": p.case is not None,
            "validation_error": validation_message(err) if err is not None else None,
            "alerts": [{"level": a.level, "message": a.message, "tab": a.tab,
                        "target": a.target} for a in self.win.alerts.alerts],
        }

    def _edit(self, label: str, fn: Callable[[dict], object]) -> dict:
        """One undoable step named "Claude · label"; returns validation and alerts."""
        p = self.project
        if p.locked:
            raise CommandError(f"the model is locked: {p.lock_reason or 'a run is going on'}")
        new = p.data
        out = fn(new)
        changed = p.set_data(f"Claude · {label}", new)
        res = {"changed": changed, **self._validation()}
        if isinstance(out, dict):
            res.update(out)
        return res

    # ------------------------------------------------------------------ reading
    def get_state(self) -> dict:
        p, r, win = self.project, self.win.runner, self.win
        rec = p.results
        return json_safe({
            "project": {"name": p.name, "path": p.path, "dirty": p.dirty,
                        "locked": p.locked},
            **self._validation(),
            "tab": win.current_tab.key if win.current_tab else None,
            "tabs": {k: win.tabs[k].title for k in win.tab_order},
            "selection": win.selection.keys,
            "run": {"running": r.running, "phase": r.phase if r.running else None,
                    "progress": str(r.progress_text or "").replace("%v",
                                                                   str(r.progress_value))},
            "results": None if rec is None else {
                "success": rec.outcome.success, "stopped": rec.outcome.stopped,
                "stale": p.stale},
            "display_units": {"length": win.units.length, "temperature": win.units.temperature},
        })

    def get_model(self, include_ui: bool = False) -> dict:
        data = self.project.data if include_ui else physics_part(self.project.data)
        for c in data.get("components") or []:
            shape = c.get("shape")
            if isinstance(shape, dict):  # the encoded triangles are useless to read
                c["shape"] = {"source": shape.get("source"),
                              "triangles": shape.get("triangles"),
                              "note": "imported CAD shape fitted in the box"}
        custom = [m.get("name") for m in data.get("materials") or []]
        return json_safe({
            "case": data,
            "built_in_materials": sorted(SOLIDS),
            "custom_materials": custom,
            **self._validation(),
        })

    def screenshot(self, target: str = "window", tab: str | None = None,
                   camera: str | None = None) -> dict:
        """PNG of the whole window (default) or of the 3D view only."""
        if tab:
            self._tab_key(tab)
            self.win.goto_tab(tab)
        if camera:
            self._camera(camera)
        self.win.refresh_scene()
        QApplication.processEvents()
        if target == "view":
            img = self._view_image()
            if img is None:
                img = self.win.viewport.grab().toImage()
        elif target == "window":
            img = self._window_image()
        else:
            raise CommandError("target: 'window' or 'view'")
        if img.width() > MAX_SHOT_WIDTH:
            img = img.scaledToWidth(MAX_SHOT_WIDTH, Qt.TransformationMode.SmoothTransformation)
        return {"png_base64": png_base64(img), "width": img.width(), "height": img.height(),
                "tab": self.win.current_tab.key if self.win.current_tab else None}

    def _view_image(self) -> QImage | None:
        """The 3D view rendered by VTK (QWidget.grab gives black pixels for OpenGL)."""
        plotter = self.win.viewport.scene.plotter
        if plotter is None:
            return None
        try:
            plotter.render()
            arr = plotter.screenshot(return_img=True)
        except Exception:  # noqa: BLE001 - no OpenGL context yet: fall back to grab()
            return None
        import numpy as np

        arr = np.ascontiguousarray(arr[:, :, :3])
        h, w = arr.shape[:2]
        return QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()

    def _window_image(self) -> QImage:
        win = self.win
        pix = win.grab()
        view = self._view_image()
        plotter = win.viewport.scene.plotter
        if view is not None and plotter is not None:
            w = plotter.interactor
            top_left = w.mapTo(win, QPoint(0, 0))
            painter = QPainter(pix)
            painter.drawImage(QRect(top_left, w.size()), view)
            painter.end()
        return pix.toImage()

    # ------------------------------------------------------------------ view
    def _tab_key(self, tab: str) -> str:
        if tab not in self.win.tabs:
            raise CommandError(f"unknown tab {tab!r}, known: {', '.join(self.win.tab_order)}")
        return tab

    def _camera(self, camera: str) -> None:
        buttons = self.win.viewport.camera_buttons
        if camera not in buttons:
            raise CommandError(f"camera: one of {', '.join(CAMERA_NAMES)}")
        buttons[camera].click()

    def set_view(self, tab: str | None = None, camera: str | None = None,
                 select: list[str] | None = None, frame: bool = True,
                 field: str | None = None) -> dict:
        if tab:
            self.win.goto_tab(self._tab_key(tab))
        if field:
            self._show_field(field)
        if select is not None:
            self._show(keys=list(select), frame=frame)
        if camera:
            self._camera(camera)
        return {"tab": self.win.current_tab.key if self.win.current_tab else None,
                "selection": self.win.selection.keys}

    def _show_field(self, field: str) -> None:
        if field not in FIELDS:
            raise CommandError("field: 'T' (temperature) or 'U' (air speed)")
        res = self.win.tabs.get("results")
        combo = getattr(res, "field_combo", None)
        if combo is None:
            return
        self.win.goto_tab("results")
        i = combo.findData(field)
        if i >= 0:
            combo.setCurrentIndex(i)

    # ------------------------------------------------------------------ files
    def _may_drop(self, discard_changes: bool) -> None:
        if self.win.runner.running:
            raise CommandError("a run is going on: stop it first")
        if self.project.dirty and not discard_changes:
            raise CommandError("the open project has unsaved changes: save_project first, "
                               "or pass discard_changes=true if the user agrees to lose them")

    def new_project(self, template: str | None = None, data: dict | None = None,
                    name: str | None = None, discard_changes: bool = False) -> dict:
        """Start from a template of "Nouveau projet" or from full case data."""
        from ..app import default_case
        from ..tabs.model.library import templates

        self._may_drop(discard_changes)
        if data is None:
            temps = {t.key: t for t in templates(default_case().model_dump(mode="json"))
                     if t.enabled}
            key = template or "empty"
            if key not in temps:
                raise CommandError(f"template: one of {', '.join(temps)}")
            data = temps[key].build(name)
        else:
            data = copy.deepcopy(data)
            if name:
                data["name"] = name
        self.project.reset(data)
        self._show(tab=self.win.tab_order[0], keys=[])
        return {"name": self.project.name, **self._validation()}

    def open_project(self, path: str, discard_changes: bool = False) -> dict:
        from ... import cad

        p = Path(path).expanduser()
        if p.suffix.lower() in cad.SUFFIXES:
            return self.import_cad(str(p), new_project=True, discard_changes=discard_changes)
        self._may_drop(discard_changes)
        try:
            self.project.load(p)
        except Exception as exc:
            raise CommandError(f"cannot open {p}: {exc}") from exc
        self.win._add_recent(p)
        self._show(tab=self.win.tab_order[0], keys=[])
        return {"name": self.project.name, "path": str(p), **self._validation()}

    def save_project(self, path: str | None = None) -> dict:
        target = Path(path).expanduser() if path else self.project.path
        if target is None:
            raise CommandError("the project has no file yet: give a path ending in .yaml")
        if target.suffix.lower() not in (".yaml", ".yml"):
            target = target.with_suffix(".yaml")
        if not self.win.write_case(target):
            raise CommandError(f"cannot save to {target}")
        return {"path": str(self.project.path)}

    # ------------------------------------------------------------------ model edits
    def update_case(self, changes: dict) -> dict:
        """Top-level settings: enclosure, ambient, fluid, walls, precision, solver, name."""
        bad = set(changes) - TOP_LEVEL
        if bad:
            raise CommandError(f"update_case cannot change {sorted(bad)}; it takes "
                               f"{sorted(TOP_LEVEL)} (use the part/opening/probe tools "
                               "for the lists)")
        if "enclosure" in changes:
            changes = {**changes, "enclosure": vec3(changes["enclosure"], "enclosure")}
        tab = ("volume" if set(changes) & {"precision", "solver"} else
               "air" if set(changes) & {"walls"} else "model")
        res = self._edit("réglages du cas", lambda d: deep_merge(d, changes))
        self._show(tab=tab, keys=[ENCLOSURE] if "enclosure" in changes else None)
        return res

    def add_part(self, size, name: str = "piece", origin=None, material: str = "aluminium",
                 power: float = 0.0, max_temperature: float | None = None,
                 on: str | None = None) -> dict:
        size = vec3(size, "size")
        part = {"name": name, "material": material, "power": float(power),
                "max_temperature": max_temperature,
                "box": {"origin": {"x": 0.0, "y": 0.0, "z": 0.0}, "size": size}}
        made: dict = {}

        def fn(d: dict) -> dict:
            made["name"] = geo.add_part(d, part, on=on)
            if origin is not None:
                geo.comp(d, made["name"])["box"]["origin"] = vec3(origin, "origin")
            c = geo.comp(d, made["name"])
            return {"name": made["name"], "box": copy.deepcopy(c["box"])}

        res = self._edit(f"ajout de « {name} »", fn)
        self._show("model", [component_key(made["name"])], frame=True)
        return res

    def update_part(self, name: str, changes: dict) -> dict:
        _find(self.project.get("components") or [], name, "part")
        changes = dict(changes)
        new_name = changes.pop("name", None)
        if new_name and new_name != name:
            data = self.project.data
            why = geo.rename_error(data, name, new_name)
            if why:
                raise CommandError(f"cannot rename to {new_name!r}: {why}")
        final = new_name or name

        def fn(d: dict) -> None:
            c = geo.comp(d, name)
            deep_merge(c, _part_changes(c, changes))
            if new_name and new_name != name:
                geo.rename(d, name, new_name)

        res = self._edit(f"modification de « {name} »", fn)
        if final != name:
            self.win.selection.rename(component_key(name), component_key(final))
        self._show("model", [component_key(final)], frame=True)
        return res

    def add_opening(self, kind: str, face: str, name: str | None = None, center=None,
                    size=None, flow_rate: float | None = None, velocity: float | None = None,
                    temperature: float | None = None, fan: dict | None = None,
                    grille: dict | None = None) -> dict:
        if kind not in ("inlet", "outlet"):
            raise CommandError("kind: 'inlet' or 'outlet'")
        if face not in {f.value for f in Face}:
            raise CommandError("face: xmin, xmax, ymin, ymax, zmin or zmax")
        made: dict = {}

        def fn(d: dict) -> dict:
            o = air_ops.new_opening(d, kind, face, fan=fan is not None)
            if name:
                if any(x.get("name") == name for x in d["openings"] if x is not o) \
                        or name in geo.taken_names({**d, "openings": []}):
                    raise CommandError(f"the name {name!r} is already taken")
                o["name"] = name
            if size is not None:
                o["size"] = pair(size, "size")
            if center is not None:
                o["center"] = pair(center, "center")
            if fan is not None:
                o["fan"] = copy.deepcopy(fan)
                o["flow_rate"] = o["velocity"] = None
            if flow_rate is not None or velocity is not None:
                o["flow_rate"], o["velocity"], o["fan"] = flow_rate, velocity, None
            elif kind == "inlet" and fan is None and size is not None and o["flow_rate"]:
                o["flow_rate"] = round(air_ops.DEFAULT_VELOCITY * o["size"][0] * o["size"][1], 6)
            if temperature is not None:
                o["temperature"] = float(temperature)
            if grille is not None:
                o["grille"] = copy.deepcopy(grille)
            made["name"] = o["name"]
            return {"name": o["name"], "center": o["center"], "size": o["size"]}

        res = self._edit(f"ajout de l’ouverture « {name or kind} »", fn)
        self._show("air", [opening_key(made["name"])], frame=True)
        return res

    def update_opening(self, name: str, changes: dict) -> dict:
        _find(self.project.get("openings") or [], name, "opening")
        changes = dict(changes)
        new_name = changes.pop("name", None)
        if new_name and new_name != name and new_name in geo.taken_names(self.project.data):
            raise CommandError(f"the name {new_name!r} is already taken")

        def fn(d: dict) -> None:
            o = air_ops.find_opening(d, name)
            if "face" in changes and changes["face"] != o.get("face"):
                air_ops.move_to_face(d, o, changes.pop("face"))
            for key in ("center", "size"):
                if key in changes:
                    changes[key] = pair(changes[key], key)
            if any(k in changes for k in ("flow_rate", "velocity")) and "fan" not in changes:
                o["fan"] = None
            if changes.get("fan") is not None and "flow_rate" not in changes:
                o["flow_rate"] = o["velocity"] = None
            for k, v in changes.items():
                o[k] = copy.deepcopy(v)
            if new_name:
                o["name"] = new_name

        res = self._edit(f"modification de « {name} »", fn)
        final = new_name or name
        if final != name:
            self.win.selection.rename(opening_key(name), opening_key(final))
        self._show("air", [opening_key(final)], frame=True)
        return res

    def import_cad(self, path: str, unit: str = "mm", material: str = "aluminium",
                   new_project: bool = False, discard_changes: bool = False) -> dict:
        """Parts with their real shape from a STEP or STL file."""
        from ... import cad
        from ..tabs.model import cadparts

        p = Path(path).expanduser()
        if unit not in cad.UNITS:
            raise CommandError(f"unit: one of {', '.join(cad.UNITS)}")
        if new_project:
            self._may_drop(discard_changes)
        try:
            bodies = cad.read(p, unit)
        except (cad.CadError, OSError) as exc:
            raise CommandError(f"cannot import {p}: {exc}") from exc
        if new_project:
            stem = re.sub(r"[^A-Za-z0-9_-]+", "_", p.stem).strip("_-")
            name = stem if re.match(r"^[A-Za-z][A-Za-z0-9_-]*$", stem) else "projet_cao"
            data = cadparts.new_project(bodies, name, str(p), material)
            self.project.reset(data)
            names = [c["name"] for c in data.get("components") or []]
            res = {"changed": True, "names": names, **self._validation()}
        else:
            made: dict = {}

            def fn(d: dict) -> dict:
                made["names"] = cadparts.add_bodies(d, bodies, str(p), material)
                return {"names": made["names"]}

            res = self._edit(f"import de {p.name}", fn)
            names = made.get("names", [])
        self._show("model", [component_key(n) for n in names], frame=True)
        return res

    def add_probe(self, name: str, point) -> dict:
        pt = vec3(point, "point")

        def fn(d: dict) -> None:
            probes = d.setdefault("probes", [])
            if any(p.get("name") == name for p in probes):
                raise CommandError(f"a probe named {name!r} already exists")
            probes.append({"name": name, "point": pt})

        res = self._edit(f"sonde « {name} »", fn)
        self._show("simulation")
        return res

    def add_refinement(self, target: str, factor: int = 2, cell_size: float | None = None,
                       margin: float | None = None) -> dict:
        _find(self.project.get("components") or [], target, "part")

        def fn(d: dict) -> None:
            refs = [r for r in d.get("refinements") or [] if r.get("target") != target]
            refs.append({"target": target, "factor": int(factor), "cell_size": cell_size,
                         "margin": margin})
            d["refinements"] = refs

        res = self._edit(f"maillage affiné autour de « {target} »", fn)
        self._show("volume", [component_key(target)])
        return res

    def add_material(self, name: str, kappa: float, rho: float, cp: float) -> dict:
        def fn(d: dict) -> None:
            mats = [m for m in d.get("materials") or [] if m.get("name") != name]
            mats.append({"name": name, "kappa": float(kappa), "rho": float(rho),
                         "cp": float(cp)})
            d["materials"] = mats

        return self._edit(f"matériau « {name} »", fn)

    def delete(self, kind: str, name: str) -> dict:
        lists = {"part": "components", "opening": "openings", "probe": "probes",
                 "material": "materials"}
        if kind == "refinement":
            items = [{"name": r.get("target")} for r in self.project.get("refinements") or []]
            _find(items, name, "refinement")
            return self._edit(f"suppression de l’affinage « {name} »", lambda d: d.update(
                refinements=[r for r in d["refinements"] if r.get("target") != name]))
        if kind not in lists:
            raise CommandError("kind: part, opening, probe, refinement or material")
        _find(self.project.get(lists[kind]) or [], name, kind)
        if kind == "part":
            fn = lambda d: geo.delete(d, [name])
        elif kind == "opening":
            fn = lambda d: air_ops.delete_opening(d, name)
        else:
            fn = lambda d: d.update({lists[kind]: [
                x for x in d[lists[kind]] if x.get("name") != name]})
        res = self._edit(f"suppression de « {name} »", fn)
        self._show({"part": "model", "opening": "air"}.get(kind))
        return res

    def undo(self, steps: int = 1) -> dict:
        return self._history(steps, undo=True)

    def redo(self, steps: int = 1) -> dict:
        return self._history(steps, undo=False)

    def _history(self, steps: int, undo: bool) -> dict:
        if self.project.locked:
            raise CommandError("undo/redo is off while a run is going on")
        stack = self.project.undo_stack
        done = []
        for _ in range(max(1, int(steps))):
            if not (stack.canUndo() if undo else stack.canRedo()):
                break
            done.append(stack.undoText() if undo else stack.redoText())
            stack.undo() if undo else stack.redo()
        return {"done": done, **self._validation()}

    # ------------------------------------------------------------------ simulation
    def quick_estimate(self) -> dict:
        case = self.project.case
        if case is None:
            raise CommandError("the case is not valid: " + str(self._validation()
                                                                ["validation_error"]))
        svc = self.win.ctx.services
        temps = svc.quick_temperatures(case)
        runtime = svc.estimate_runtime(case)
        limits = {c.name: c.max_temperature for c in case.components}
        return json_safe({
            "method": "fast analytical estimate (no CFD), to compare designs quickly",
            "temperatures_c": temps,
            "limits_c": {k: v for k, v in limits.items() if v is not None},
            "estimated_run_time_s": runtime,
        })

    def run_simulation(self, resume: bool = False) -> dict:
        r = self.win.runner
        if r.running:
            raise CommandError("a run is already going on (run_status follows it)")
        v = self._validation()
        errors = [a for a in v["alerts"] if a["level"] == "error"]
        if self.project.case is None or errors:
            raise CommandError("the case cannot run yet: "
                               + "; ".join(a["message"] for a in errors)
                               or str(v["validation_error"]))
        if resume:
            ok, why = r.can_resume()
            if not ok:
                raise CommandError(f"cannot resume: {why}")
            started = r.resume()
        else:
            started = self.win.start_run()
        if not started:
            raise CommandError("the run did not start (see the alerts)")
        self._show("simulation")
        return self.run_status()

    def run_status(self, log_lines: int = 15) -> dict:
        r = self.win.runner
        rec = self.project.results
        out = {
            "running": r.running,
            "phase": r.phase if r.running else None,
            "iteration": r.iteration,
            "progress": {"value": r.progress_value, "max": r.progress_max,
                         "text": str(r.progress_text or "").replace("%v",
                                                                    str(r.progress_value))},
            "elapsed_s": r.elapsed_s() if r.running else None,
            "remaining_s": r.remaining_s() if r.running else None,
            "last_residuals": dict(r.last_residuals or {}),
            "log_tail": list(r.log_lines)[-max(0, int(log_lines)):],
        }
        if not r.running and rec is not None:
            out["outcome"] = self._outcome(rec.outcome)
        return json_safe(out)

    @staticmethod
    def _outcome(o) -> dict:
        from ..run import outcome_status

        return {"status": outcome_status(o), "success": o.success, "stopped": o.stopped,
                "errors": list(o.errors), "backend_missing": o.backend_missing,
                "diagnosis": [str(getattr(x, "message", x)) for x in o.diagnosis]}

    def stop_simulation(self) -> dict:
        r = self.win.runner
        if not r.running:
            raise CommandError("no run is going on")
        r.stop()
        return {"stopping": True, "note": "the partial result is kept; run_simulation "
                "with resume=true continues it"}

    # ------------------------------------------------------------------ results
    def get_results(self, show: bool = True) -> dict:
        rec = self.project.results
        if rec is None:
            raise CommandError("no results yet: run_simulation first")
        o = rec.outcome
        out = {"outcome": self._outcome(o), "stale": self.project.stale,
               "stale_note": "the model changed since this run" if self.project.stale else None,
               "case_dir": getattr(o.result, "case_dir", None)}
        s = o.summary
        if s is not None:
            summary = s.to_dict()
            limits = {c.name: c.max_temperature for c in rec.case.components}
            for comp in summary.get("components", []):
                lim = limits.get(comp.get("name"))
                comp["max_temperature_c"] = lim
                t = comp.get("t_max_c")
                comp["over_limit"] = (lim is not None and t is not None
                                      and math.isfinite(t) and t > lim)
            out["summary"] = summary
        if show:
            self._show("results")
        return json_safe(out)

