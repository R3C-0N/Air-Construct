"""Run history, run queue and comparison of two runs.

Owned by: workstream "runner" (run control).

Every run gets its own directory ``<root>/<run id>/`` (so earlier runs of the same case
are kept) and a row in ``<root>/history.sqlite`` (standard library sqlite3):

    store = RunStore("runs")
    queue = RunQueue(store)
    rid = queue.submit(case, label="fan at 80 %")   # status "queued"
    queue.start()                                  # background thread, one run at a time
    queue.stop_current()                           # clean stop: the run can be resumed
    queue.resume(rid)                              # back in the queue, continues
    diff = compare(store.get(a), store.get(b))     # case changes + result deltas

Statuses: queued, running, stopped, converged, finished (budget used, not converged),
failed, cancelled. A run left "running" by a crash of the application is marked
"stopped" when the store is opened again, so it can be resumed.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .case import Case

STATUSES = ("queued", "running", "stopped", "converged", "finished", "failed", "cancelled")
RESUMABLE = ("stopped", "finished", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    case_yaml TEXT NOT NULL,
    case_dir TEXT NOT NULL,
    iterations INTEGER NOT NULL DEFAULT 0,
    wall_time_s REAL NOT NULL DEFAULT 0,
    converged INTEGER NOT NULL DEFAULT 0,
    resume INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT,
    diagnosis_json TEXT,
    errors_json TEXT
);
"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class RunRecord:
    id: str
    name: str
    label: str
    status: str
    created_at: str
    started_at: str | None
    ended_at: str | None
    case_yaml: str
    case_dir: str
    iterations: int = 0
    wall_time_s: float = 0.0
    converged: bool = False
    resume: bool = False  # queued to continue an earlier, stopped run
    summary: dict | None = None  # results.Summary.to_dict()
    diagnosis: list[dict] = field(default_factory=list)  # diagnose.Diagnosis.to_dict()
    errors: list[str] = field(default_factory=list)

    @property
    def case(self) -> Case:
        import yaml

        return Case.model_validate(yaml.safe_load(self.case_yaml))

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


class RunStore:
    """The history: one sqlite file plus one directory per run under `root`."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "history.sqlite"
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(_SCHEMA)
            c.execute("UPDATE runs SET status = 'stopped' WHERE status = 'running'")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def _record(self, row: sqlite3.Row) -> RunRecord:
        d = dict(row)
        return RunRecord(
            id=d["id"], name=d["name"], label=d["label"], status=d["status"],
            created_at=d["created_at"], started_at=d["started_at"], ended_at=d["ended_at"],
            case_yaml=d["case_yaml"], case_dir=d["case_dir"], iterations=d["iterations"],
            wall_time_s=d["wall_time_s"], converged=bool(d["converged"]), resume=bool(d["resume"]),
            summary=json.loads(d["summary_json"]) if d["summary_json"] else None,
            diagnosis=json.loads(d["diagnosis_json"]) if d["diagnosis_json"] else [],
            errors=json.loads(d["errors_json"]) if d["errors_json"] else [])

    def add(self, case: Case, label: str = "", status: str = "queued") -> RunRecord:
        import yaml

        rid = f"{datetime.now().astimezone():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        case_dir = self.root / rid
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO runs (id, name, label, status, created_at, case_yaml, case_dir)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rid, case.name, label, status, _now(),
                 yaml.safe_dump(case.model_dump(mode="json"), sort_keys=False), str(case_dir)))
        return self.get(rid)

    def get(self, rid: str) -> RunRecord:
        with self._conn() as c:
            row = c.execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()
        if row is None:
            raise KeyError(f"no run {rid!r}")
        return self._record(row)

    def list(self, name: str | None = None, status: str | None = None) -> list[RunRecord]:
        """Runs, newest first; filter by case name and/or status."""
        q, args = "SELECT * FROM runs WHERE 1 = 1", []
        if name:
            q, args = q + " AND name = ?", args + [name]
        if status:
            q, args = q + " AND status = ?", args + [status]
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY rowid DESC", args).fetchall()
        return [self._record(r) for r in rows]

    def queued(self) -> list[RunRecord]:
        """The queue, first to run first."""
        return list(reversed(self.list(status="queued")))

    def update(self, rid: str, **fields) -> None:
        cols = {"summary": "summary_json", "diagnosis": "diagnosis_json", "errors": "errors_json"}
        sets, args = [], []
        for k, v in fields.items():
            if k in cols:
                k, v = cols[k], json.dumps(v, default=float) if v is not None else None
            if k == "status" and v not in STATUSES:
                raise ValueError(f"unknown status {v!r}")
            sets.append(f"{k} = ?")
            args.append(int(v) if isinstance(v, bool) else v)
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", args + [rid])

    def delete(self, rid: str, files: bool = True) -> None:
        import shutil

        rec = self.get(rid)
        if rec.status == "running":
            raise RuntimeError("stop the run before deleting it")
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM runs WHERE id = ?", (rid,))
        if files:
            shutil.rmtree(rec.case_dir, ignore_errors=True)


# --------------------------------------------------------------------------- queue

Event = Callable[[str, str, object], None]  # (run id, kind, payload)


def execute(store: RunStore, rid: str, backend=None, on_log=None, on_progress=None,
            should_stop: Callable[[], bool] | None = None, summarize: bool = True):
    """Run (or resume) one stored run now, in this thread. Returns runner.RunResult."""
    from .runner import resume_case, run_case

    rec = store.get(rid)
    case = rec.case
    store.update(rid, status="running", started_at=rec.started_at or _now(), ended_at=None)
    kwargs = {"on_log": on_log, "on_progress": on_progress, "should_stop": should_stop}
    try:
        if rec.resume:
            res = resume_case(Path(rec.case_dir), backend, case=case, **kwargs)
        else:
            res = run_case(case, store.root, backend, case_dir=Path(rec.case_dir), **kwargs)
    except Exception as e:
        store.update(rid, status="failed", ended_at=_now(), errors=[str(e)], resume=False)
        raise
    status = ("failed" if not res.success else "converged" if res.converged
              else "stopped" if res.stopped else "finished")
    summary = None
    if summarize and res.success:
        try:
            from .results import summarize as summarize_fn

            summary = summarize_fn(case, res.case_dir).to_dict()
        except Exception:  # noqa: BLE001 (pyvista missing or no result yet)
            summary = None
    from .runner import read_state

    state = read_state(res.case_dir)
    store.update(rid, status=status, ended_at=_now(), iterations=res.iterations,
                 wall_time_s=state.get("wall_time_s", res.wall_time_s), converged=res.converged,
                 resume=False, summary=summary, errors=res.errors,
                 diagnosis=[d.to_dict() for d in res.diagnosis])
    return res


class RunQueue:
    """Runs the queued runs of a store one after the other, in a background thread.

    on_event(run_id, kind, payload): kind is "started", "log" (a line), "progress"
    (monitor.Progress), "ended" (runner.RunResult) or "error" (an exception)."""

    def __init__(self, store: RunStore, backend_factory: Callable | None = None,
                 on_event: Event | None = None):
        self.store = store
        self.backend_factory = backend_factory
        self.on_event = on_event
        self.current: str | None = None
        self._stop_current = threading.Event()
        self._quit = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # -- queue editing
    def submit(self, case: Case, label: str = "") -> str:
        rid = self.store.add(case, label).id
        self._wake.set()
        return rid

    def cancel(self, rid: str) -> None:
        """Take a queued run out of the queue (a running one is stopped instead)."""
        if rid == self.current:
            self.stop_current()
        elif self.store.get(rid).status == "queued":
            self.store.update(rid, status="cancelled")

    def resume(self, rid: str, case: Case | None = None) -> None:
        """Put a stopped (or finished, or failed after it started) run back in the queue;
        it continues where it ended. `case` may change solver settings (iterations)."""
        rec = self.store.get(rid)
        if rec.status not in RESUMABLE:
            raise ValueError(f"run {rid} is {rec.status}, it cannot be resumed")
        fields: dict = {"status": "queued", "resume": True}
        if case is not None:
            import yaml

            fields["case_yaml"] = yaml.safe_dump(case.model_dump(mode="json"), sort_keys=False)
        self.store.update(rid, **fields)
        self._wake.set()

    def stop_current(self) -> None:
        self._stop_current.set()

    # -- worker
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._quit.clear()
        self._thread = threading.Thread(target=self._loop, name="airconstruct-queue", daemon=True)
        self._thread.start()

    def shutdown(self, wait: bool = True) -> None:
        """Stop the worker; the current run is stopped cleanly (it can be resumed)."""
        self._quit.set()
        self._stop_current.set()
        self._wake.set()
        if wait and self._thread:
            self._thread.join()

    def run_pending(self) -> list[str]:
        """Run every queued run now, in this thread (CLI). Returns their ids."""
        done = []
        while not self._quit.is_set():
            q = self.store.queued()
            if not q:
                break
            self._run_one(q[0].id)
            done.append(q[0].id)
        return done

    def _emit(self, rid: str, kind: str, payload) -> None:
        if self.on_event:
            self.on_event(rid, kind, payload)

    def _run_one(self, rid: str) -> None:
        self.current = rid
        self._stop_current.clear()
        self._emit(rid, "started", None)
        try:
            backend = self.backend_factory() if self.backend_factory else None
            res = execute(self.store, rid, backend,
                          on_log=lambda line: self._emit(rid, "log", line),
                          on_progress=lambda p: self._emit(rid, "progress", p),
                          should_stop=self._stop_current.is_set)
            self._emit(rid, "ended", res)
        except Exception as e:  # noqa: BLE001
            self._emit(rid, "error", e)
        finally:
            self.current = None

    def _loop(self) -> None:
        while not self._quit.is_set():
            q = self.store.queued()
            if not q:
                self._wake.wait(timeout=2.0)
                self._wake.clear()
                continue
            self._run_one(q[0].id)

    @property
    def running(self) -> bool:
        return self.current is not None


# --------------------------------------------------------------------------- compare


@dataclass
class Comparison:
    a: str
    b: str
    case_changes: dict[str, tuple] = field(default_factory=dict)  # "components.cpu.power": (a, b)
    metrics: dict[str, tuple] = field(default_factory=dict)  # name: (a, b, b - a)

    def to_dict(self) -> dict:
        return {"a": self.a, "b": self.b,
                "case_changes": {k: list(v) for k, v in self.case_changes.items()},
                "metrics": {k: list(v) for k, v in self.metrics.items()}}


def _flatten(d, prefix: str = "") -> dict:
    """Nested case dict -> {"a.b.c": value}; lists of named items are keyed by name."""
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_flatten(v, f"{prefix}{k}."))
    elif isinstance(d, list) and all(isinstance(x, dict) and "name" in x for x in d):
        for x in d:
            out.update(_flatten({k: v for k, v in x.items() if k != "name"},
                                f"{prefix}{x['name']}."))
        if not d:
            out[prefix[:-1]] = []
    else:
        out[prefix[:-1]] = d
    return out


def _metrics(rec: RunRecord) -> dict[str, float]:
    m: dict[str, float] = {"iterations": rec.iterations, "wall_time_s": rec.wall_time_s}
    s = rec.summary or {}
    for k in ("air_t_max_c", "air_v_max", "outlet_t_mean_c", "pressure_drop_pa",
              "pressure_drop_total_pa", "mass_flow_kg_s", "heat_to_air_w"):
        if k in s:
            m[k] = s[k]
    for c in s.get("components", []):
        for k in ("t_max_c", "t_mean_c"):
            m[f"{c['name']}.{k}"] = c.get(k, float("nan"))
    e = s.get("energy") or {}
    for p in e.get("parts", []):
        m[f"{p['name']}.to_air_w"] = p.get("to_air_w", float("nan"))
    return m


def compare(a: RunRecord, b: RunRecord) -> Comparison:
    """What changed between two runs: case parameters, then key results (b - a)."""
    fa = _flatten(a.case.model_dump(mode="json"))
    fb = _flatten(b.case.model_dump(mode="json"))
    changes = {k: (fa.get(k), fb.get(k)) for k in sorted(set(fa) | set(fb))
               if fa.get(k) != fb.get(k)}
    ma, mb = _metrics(a), _metrics(b)
    metrics = {}
    for k in list(dict.fromkeys(list(ma) + list(mb))):
        va, vb = ma.get(k), mb.get(k)
        delta = (vb - va) if isinstance(va, (int, float)) and isinstance(vb, (int, float)) else None
        metrics[k] = (va, vb, delta)
    return Comparison(a.id, b.id, changes, metrics)
