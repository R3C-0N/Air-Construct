"""MCP server (stdio) that lets Claude drive the open Air-Construct window.

Owned by: workstream "assistant". Start it with ``airconstruct-mcp`` (or
``Air-Construct-MCP.exe`` in the desktop install); Claude Desktop / Claude Code start
it for you once it is in their MCP configuration (see docs/claude-mcp.md).

The server holds no state of its own: each tool is a command sent to the app's bridge
(:mod:`airconstruct.assistant.protocol`), so everything happens in the window the
user is looking at. When no window is reachable the first tool call starts the app.

The MCP protocol part is the small subset tools need (initialize, tools/list,
tools/call, ping), newline-delimited JSON-RPC 2.0 on stdin/stdout, with no
dependency so the frozen desktop app can ship it. Nothing but protocol messages
may go to stdout: logs go to stderr.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .. import __version__
from .protocol import BridgeClient, BridgeError, NotRunning, read_bridge_file

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LAUNCH_TIMEOUT_S = 90.0
MAX_WAIT_S = 600.0
APP_COMMAND_ENV = "AIRCONSTRUCT_APP_COMMAND"

INSTRUCTIONS = """\
Air-Construct simulates air flow and heat in an enclosure (OpenFOAM, CHT). These tools
drive the Air-Construct window the user has open: they watch every action happen
(tab switch, selected object, "Claude · ..." step in the undo history, the "Claude"
pill in the top bar, which also lets them pause you). Work in small visible steps
and describe what you do.

Model: units are SI (metres, °C, W, m³/s, Pa). The enclosure is the box
[0, x] × [0, y] × [0, z], z up. Parts (components) are axis-aligned boxes given by
origin (min corner) and size, with a material, a dissipated power and an optional
max_temperature limit. Openings are rectangles on an enclosure face (xmin, xmax,
ymin, ymax, zmin, zmax): center and size are the 2 in-plane coordinates in axis order
(xmin/xmax: (y, z); ymin/ymax: (x, z); zmin/zmax: (x, y)). An inlet has an imposed
flow_rate or velocity, a fan, or nothing (free vent); an outlet is at ambient pressure
or has an exhaust fan. At least one inlet and one outlet are needed, and not every
opening may impose its flow.

Workflow: get_state / get_model to read, screenshot to look (the user sees the same
window), edit with add_part / update_part / add_opening / update_case..., check the
returned alerts, quick_estimate for a fast analytical answer, run_simulation then
simulation_status(wait_seconds=...) to follow the CFD run (minutes to hours),
get_results and screenshot(tab="results") to read and show the result. Edits are
undoable (undo). Never discard unsaved changes or overwrite files without the
user's agreement.

The tabs: model (Modèle: parts), air (Air: openings, fans, walls), volume (Volume
d'air: mesh precision, refinements), simulation (Simulation: run, probes), results
(Résultats).
"""

# ----------------------------------------------------------------------------- schemas
NUM = {"type": "number"}
VEC3 = {"type": "array", "items": NUM, "minItems": 3, "maxItems": 3,
        "description": "[x, y, z] in metres"}
PAIR = {"type": "array", "items": NUM, "minItems": 2, "maxItems": 2}
TAB = {"type": "string", "enum": ["model", "air", "volume", "simulation", "results"]}
CAMERA = {"type": "string", "enum": ["iso", "top", "front", "side"]}
DISCARD = {"type": "boolean", "default": False,
           "description": "drop unsaved changes of the open project; only when the "
                          "user agreed"}
FAN = {"type": "object", "description":
       "fan on the opening: {flow_rate: m³/s} for a fixed flow, or {curve: [[Q m³/s, "
       "dp Pa], ...]} (Q increasing, dp not increasing) for a fan curve"}
GRILLE = {"type": "object", "description":
          "grille / perforated plate: {open_area: 0..1} or {loss_coefficient: K}"}


def _obj(props: dict, required: tuple[str, ...] = ()) -> dict:
    s = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        s["required"] = list(required)
    return s


def _tool(name: str, description: str, schema: dict | None = None, *,
          read_only: bool = False, title: str | None = None) -> dict:
    t = {"name": name, "description": description,
         "inputSchema": schema or _obj({})}
    ann = {"readOnlyHint": read_only}
    if title:
        ann["title"] = title
    t["annotations"] = ann
    return t


PART_FIELDS = {
    "name": {"type": "string", "description": "letters, digits, _; starts with a letter"},
    "size": VEC3, "origin": {**VEC3, "description": "min corner [x, y, z] in metres"},
    "material": {"type": "string", "description": "built-in (aluminium, copper, fr4, "
                 "silicon, steel, plastic_abs...) or a custom material (get_model lists "
                 "them)"},
    "power": {**NUM, "description": "dissipated power [W], > 0 makes it a heat source"},
    "max_temperature": {"type": ["number", "null"],
                        "description": "temperature limit [°C] for the results"},
}
OPENING_FIELDS = {
    "name": {"type": "string"},
    "kind": {"type": "string", "enum": ["inlet", "outlet"]},
    "face": {"type": "string", "enum": ["xmin", "xmax", "ymin", "ymax", "zmin", "zmax"]},
    "center": {**PAIR, "description": "in-plane centre in metres (see face axis order)"},
    "size": {**PAIR, "description": "in-plane size in metres"},
    "flow_rate": {"type": ["number", "null"], "description": "imposed inlet flow [m³/s]"},
    "velocity": {"type": ["number", "null"], "description": "imposed inlet speed [m/s]"},
    "temperature": {"type": ["number", "null"],
                    "description": "incoming air temperature [°C], default ambient"},
    "fan": {**FAN, "type": ["object", "null"]},
    "grille": {**GRILLE, "type": ["object", "null"]},
}

TOOLS: list[dict] = [
    _tool("get_state", "What the window shows now: project name, file, unsaved changes, "
          "open tab, selection, alerts (errors block the run), run and results state.",
          read_only=True, title="État de l’application"),
    _tool("screenshot", "Look at the app: a PNG of the whole window (default) or of the 3D "
          "view only, optionally after opening a tab and choosing a camera. The user sees "
          "the same thing.",
          _obj({"target": {"type": "string", "enum": ["window", "view"], "default": "window"},
                "tab": TAB, "camera": CAMERA}),
          read_only=True, title="Capture d’écran"),
    _tool("get_model", "The full case data (SI units): enclosure, parts, openings, walls, "
          "probes, refinements, mesh precision, solver settings; plus the material names "
          "and the validation alerts.",
          _obj({"include_ui": {"type": "boolean", "default": False,
                               "description": "also the UI metadata (groups, hidden...)"}}),
          read_only=True, title="Lire le modèle"),
    _tool("set_view", "Show something to the user: open a tab, select (and frame) objects "
          "by key ('enclosure', 'component:<name>', 'opening:<name>'), set the camera, or "
          "the result field (T temperature, U air speed).",
          _obj({"tab": TAB, "camera": CAMERA,
                "select": {"type": "array", "items": {"type": "string"}},
                "frame": {"type": "boolean", "default": True},
                "field": {"type": "string", "enum": ["T", "U"]}}),
          title="Changer la vue"),
    _tool("new_project", "Start a new project from a template (empty, board, rack, psu) or "
          "from full case data. Refused when the open project has unsaved changes, unless "
          "discard_changes.",
          _obj({"template": {"type": "string", "enum": ["empty", "board", "rack", "psu"]},
                "data": {"type": "object", "description": "full case data, as get_model"},
                "name": {"type": "string"}, "discard_changes": DISCARD}),
          title="Nouveau projet"),
    _tool("open_project", "Open a project file (.yaml), or a CAD file (.step/.stl) as a new "
          "project built around its parts.",
          _obj({"path": {"type": "string"}, "discard_changes": DISCARD}, ("path",)),
          title="Ouvrir un projet"),
    _tool("save_project", "Save the project (to its file, or to path, a .yaml file). Ask "
          "the user before overwriting a file you did not create.",
          _obj({"path": {"type": "string"}}), title="Enregistrer"),
    _tool("import_cad", "Add parts with their real shape from a STEP or STL file (one part "
          "per body, placed on the enclosure floor; the enclosure grows to hold them), or "
          "make a new project around them.",
          _obj({"path": {"type": "string"},
                "unit": {"type": "string", "enum": ["mm", "cm", "m", "in"], "default": "mm",
                         "description": "length unit of an STL file (STEP gives its own)"},
                "material": {"type": "string", "default": "aluminium"},
                "new_project": {"type": "boolean", "default": False},
                "discard_changes": DISCARD}, ("path",)),
          title="Importer une CAO"),
    _tool("update_case", "Change case settings, merged into the current ones: name, "
          "enclosure [x, y, z], ambient_temperature °C, ambient_pressure Pa, fluid, walls "
          "{mode: adiabatic|exchange, material, thickness, h_external, t_external, "
          "emissivity, faces: {<face>: {...}}}, precision {level 1-5, cell_size}, solver "
          "{iterations, turbulence: kOmegaSST|laminar, gravity, convergence_tol, cores}.",
          _obj({"changes": {"type": "object"}}, ("changes",)), title="Réglages du cas"),
    _tool("add_part", "Add a part (box). Without origin it is centred on the floor, or on "
          "top of the part named 'on'. Returns the name given (made unique) and its box.",
          _obj({**PART_FIELDS, "on": {"type": "string", "description": "put it on this part"}},
               ("size",)), title="Ajouter une pièce"),
    _tool("update_part", "Change a part: any of name (renames), origin, size, material, "
          "power, max_temperature. Only the given fields change.",
          _obj({"name": {"type": "string"},
                "changes": _obj(PART_FIELDS)}, ("name", "changes")),
          title="Modifier une pièce"),
    _tool("add_opening", "Add an inlet or outlet on an enclosure face. Without center/size "
          "it gets a free spot and a default size; a new inlet without fan or flow gets an "
          "imposed flow of 1 m/s over its area (set flow_rate/velocity to change it).",
          _obj({k: v for k, v in OPENING_FIELDS.items()}, ("kind", "face")),
          title="Ajouter une ouverture"),
    _tool("update_opening", "Change an opening: any of name, kind, face, center, size, "
          "flow_rate, velocity, temperature, fan, grille (null removes). Giving flow_rate "
          "or velocity removes the fan and the reverse. To make an inlet a free vent set "
          "flow_rate, velocity and fan to null.",
          _obj({"name": {"type": "string"}, "changes": _obj(OPENING_FIELDS)},
               ("name", "changes")), title="Modifier une ouverture"),
    _tool("add_probe", "Add a probe: a point where temperature (and air speed) is "
          "recorded during the run.",
          _obj({"name": {"type": "string"}, "point": VEC3}, ("name", "point")),
          title="Ajouter une sonde"),
    _tool("add_refinement", "Finer mesh around a part: cells factor times smaller (or "
          "cell_size m) over its extent, growing back over margin m.",
          _obj({"target": {"type": "string"},
                "factor": {"type": "integer", "minimum": 1, "maximum": 8, "default": 2},
                "cell_size": {"type": ["number", "null"]},
                "margin": {"type": ["number", "null"]}}, ("target",)),
          title="Affiner le maillage"),
    _tool("add_material", "Add (or replace) a custom solid: conductivity kappa W/m/K, "
          "density rho kg/m³, specific heat cp J/kg/K.",
          _obj({"name": {"type": "string"}, "kappa": NUM, "rho": NUM, "cp": NUM},
               ("name", "kappa", "rho", "cp")), title="Ajouter un matériau"),
    _tool("delete", "Delete a part, opening, probe, refinement (name = its part) or "
          "custom material.",
          _obj({"kind": {"type": "string",
                         "enum": ["part", "opening", "probe", "refinement", "material"]},
                "name": {"type": "string"}}, ("kind", "name")), title="Supprimer"),
    _tool("undo", "Undo the last edits (yours or the user's), like Ctrl+Z.",
          _obj({"steps": {"type": "integer", "minimum": 1, "default": 1}}), title="Annuler"),
    _tool("redo", "Redo undone edits.",
          _obj({"steps": {"type": "integer", "minimum": 1, "default": 1}}), title="Rétablir"),
    _tool("quick_estimate", "Fast analytical estimate of part temperatures (no CFD, about "
          "a second) and of the CFD run time. Good to compare options before a run.",
          read_only=True, title="Estimation rapide"),
    _tool("run_simulation", "Start the OpenFOAM simulation of the open model (the model is "
          "locked while it runs), or resume the last stopped/unconverged run. Returns at "
          "once; follow it with simulation_status.",
          _obj({"resume": {"type": "boolean", "default": False}}), title="Lancer le calcul"),
    _tool("simulation_status", "Progress of the run: phase, iteration, residuals, time "
          "left, log tail; the outcome once finished. wait_seconds waits (up to that long) "
          "for the run to end before answering.",
          _obj({"wait_seconds": {"type": "number", "minimum": 0, "default": 0},
                "log_lines": {"type": "integer", "minimum": 0, "default": 15}}),
          read_only=True, title="Suivre le calcul"),
    _tool("stop_simulation", "Stop the run cleanly (the partial result is kept and can be "
          "resumed).", title="Arrêter le calcul"),
    _tool("get_results", "Results of the last run: per part max/mean temperature with its "
          "limit, air temperatures and speed, pressure drop, flow, energy balance, fans, "
          "openings, probes; 'stale' when the model changed since. Opens the Résultats tab "
          "(screenshot(tab='results') shows the fields).",
          _obj({"show": {"type": "boolean", "default": True}}),
          read_only=True, title="Résultats"),
]


# ----------------------------------------------------------------------------- the app
def app_command() -> list[str]:
    """How to start the desktop app (same install as this server)."""
    env = os.environ.get(APP_COMMAND_ENV)
    if env:
        return json.loads(env) if env.lstrip().startswith("[") else [env]
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        name = "Air-Construct.exe" if sys.platform == "win32" else "Air-Construct"
        return [str(exe.with_name(name))]
    return [sys.executable, "-m", "airconstruct.ui"]


def launch_app() -> None:
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform == "win32":
        kw["creationflags"] = (subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
                               | subprocess.CREATE_NEW_PROCESS_GROUP)  # type: ignore[attr-defined]
    else:
        kw["start_new_session"] = True
    subprocess.Popen(app_command(), **kw)


class Tools:
    """Tool name -> handler returning MCP content blocks."""

    def __init__(self, client: BridgeClient | None = None,
                 launcher: Callable[[], None] = launch_app,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.client = client or BridgeClient()
        self.launcher = launcher
        self.sleep = sleep
        self._launched = False

    # -------------------------------------------------------------- connection
    def call(self, method: str, **params):
        try:
            return self.client.call(method, **params)
        except NotRunning:
            if self._launched or read_bridge_file(self.client.path) is not None:
                raise
        self._start_app()
        return self.client.call(method, **params)

    def _start_app(self) -> None:
        self._launched = True
        self.launcher()
        end = time.monotonic() + LAUNCH_TIMEOUT_S
        while time.monotonic() < end:
            self.sleep(1.0)
            if self.client.connected():
                return
        raise NotRunning("Air-Construct was started but its Claude control does not "
                         "answer: in the window, check Fichier > Autoriser le pilotage "
                         "par Claude")

    # -------------------------------------------------------------- dispatch
    def run(self, name: str, args: dict) -> list[dict]:
        special = {"screenshot": self.screenshot, "simulation_status": self.simulation_status}
        if name in special:
            return special[name](**args)
        return [_text(self.call(name, **args))]

    def screenshot(self, **args) -> list[dict]:
        r = self.call("screenshot", **args)
        meta = {k: v for k, v in r.items() if k != "png_base64"}
        return [{"type": "image", "data": r["png_base64"], "mimeType": "image/png"},
                _text(meta)]

    def simulation_status(self, wait_seconds: float = 0, log_lines: int = 15) -> list[dict]:
        end = time.monotonic() + min(max(float(wait_seconds), 0.0), MAX_WAIT_S)
        while True:
            st = self.call("run_status", log_lines=log_lines)
            if not st.get("running") or time.monotonic() >= end:
                return [_text(st)]
            self.sleep(min(5.0, max(0.2, end - time.monotonic())))


def _text(v) -> dict:
    text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1)
    return {"type": "text", "text": text}


# ----------------------------------------------------------------------------- MCP
class McpServer:
    def __init__(self, tools: Tools | None = None) -> None:
        self.tools = tools or Tools()
        self.names = {t["name"] for t in TOOLS}

    def handle(self, msg: dict) -> dict | None:
        """One JSON-RPC message -> its reply (None for notifications)."""
        mid = msg.get("id")
        method = msg.get("method")
        if method is None:  # a reply to us (we send no requests): ignore
            return None
        try:
            result = self._dispatch(method, msg.get("params") or {})
        except _RpcError as exc:
            if mid is None:
                return None
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": exc.code, "message": str(exc)}}
        if mid is None:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _dispatch(self, method: str, params: dict):
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return {"protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "air-construct", "title": "Air-Construct",
                                   "version": __version__},
                    "instructions": INSTRUCTIONS}
        if method == "ping":
            return {}
        if method.startswith("notifications/"):
            return None
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            name = params.get("name")
            if name not in self.names:
                raise _RpcError(-32602, f"unknown tool {name!r}")
            return self.call_tool(name, params.get("arguments") or {})
        raise _RpcError(-32601, f"method not found: {method}")

    def call_tool(self, name: str, args: dict) -> dict:
        try:
            content = self.tools.run(name, args)
        except NotRunning as exc:
            return _error(f"{exc}. Ask the user to open Air-Construct "
                          "(and allow Fichier > Autoriser le pilotage par Claude).")
        except BridgeError as exc:
            return _error(str(exc))
        except TypeError as exc:
            return _error(f"bad arguments: {exc}")
        except Exception as exc:  # noqa: BLE001 - a tool error, not a protocol error
            return _error(f"{type(exc).__name__}: {exc}")
        return {"content": content, "isError": False}

    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                reply = {"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32700, "message": "parse error"}}
            else:
                if isinstance(msg, list):  # batch (2025-03-26)
                    replies = [r for m in msg if (r := self.handle(m)) is not None]
                    reply = replies or None
                else:
                    reply = self.handle(msg) if isinstance(msg, dict) else None
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (["--version"], ["-V"]):
        print(f"airconstruct-mcp {__version__}")
        return 0
    if argv[:1] in (["--help"], ["-h"]):
        print("airconstruct-mcp: MCP server (stdio) driving the open Air-Construct window.\n"
              "Add it to Claude: claude mcp add air-construct -- airconstruct-mcp")
        return 0
    # stdout carries the protocol: UTF-8, no buffering surprises, nothing else on it
    for s in (sys.stdin, sys.stdout):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", newline="\n")
    McpServer().serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
