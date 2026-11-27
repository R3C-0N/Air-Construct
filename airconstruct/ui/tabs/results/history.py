"""Saved runs for « Comparer »: each run finished in the window is recorded in the run
history of ``airconstruct.runs`` (``<dossier des calculs>/history.sqlite``), and runs
saved earlier (by the window or by the command line queue) can be compared again.

Owned by: thread "Résultats" (see docs/ui-architecture.md).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from ....results import ComponentResult, Summary

STORE_LABEL = "interface"  # label of the runs recorded by the window
DONE = ("converged", "finished")


def open_store(root: Path):
    """The run history in the runs folder, or None when it cannot be opened."""
    try:
        from ....runs import RunStore

        return RunStore(root)
    except Exception:  # noqa: BLE001 - history is a convenience
        return None


def record_run(store, record) -> str | None:
    """Save a run finished in the window (project.RunRecord); returns its id."""
    out = record.outcome
    res = out.result
    if store is None or out.summary is None:
        return None
    try:
        rec = store.add(record.case, label=STORE_LABEL, status="running")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        converged = bool(getattr(res, "converged", False))
        store.update(rec.id, status="converged" if converged else "finished",
                     started_at=rec.created_at, ended_at=now,
                     case_dir=str(getattr(res, "case_dir", "") or ""),
                     iterations=int(getattr(res, "iterations", 0) or 0),
                     wall_time_s=float(getattr(res, "wall_time_s", 0.0) or 0.0),
                     converged=converged, summary=out.summary.to_dict())
        return rec.id
    except Exception:  # noqa: BLE001
        return None


def summary_from_dict(d: dict) -> Summary:
    """Summary with the numbers a comparison needs (parts, air, pressure drop)."""
    names = {f.name for f in dataclasses.fields(Summary)}
    cnames = {f.name for f in dataclasses.fields(ComponentResult)}
    comps = [ComponentResult(**{k: v for k, v in c.items() if k in cnames})
             for c in d.get("components") or []]
    simple = {k: v for k, v in d.items()
              if k in names and k not in ("components", "energy", "fans", "openings", "probes")}
    for k in ("air_t_max_c", "air_v_max", "pressure_drop_pa", "outlet_t_mean_c"):
        simple.setdefault(k, float("nan"))
    return Summary(components=comps, **simple)


def saved_runs(store, case_name: str, skip: set[str], limit: int = 15) -> list:
    """Finished runs of this case in the history, newest first, minus ``skip`` ids."""
    if store is None:
        return []
    try:
        runs = store.list(name=case_name)
    except Exception:  # noqa: BLE001
        return []
    return [r for r in runs if r.status in DONE and r.summary and r.id not in skip][:limit]


def run_date(rec) -> datetime:
    for s in (rec.ended_at, rec.created_at):
        try:
            return datetime.fromisoformat(s).replace(tzinfo=None)
        except (TypeError, ValueError):
            continue
    return datetime.now()


def as_record(rec, load_regions=None):
    """A saved run shaped like the window's project.RunRecord (outcome, case, data).

    Fields are read back only from a folder that belongs to this run alone (runs of
    the queue live in ``<root>/<id>``; the window reuses one folder per case)."""
    case = rec.case
    regions = {}
    case_dir = Path(rec.case_dir) if rec.case_dir else None
    if (load_regions is not None and case_dir is not None and case_dir.name == rec.id
            and case_dir.is_dir()):
        try:
            regions = load_regions(case_dir) or {}
        except Exception:  # noqa: BLE001
            regions = {}
    outcome = SimpleNamespace(summary=summary_from_dict(rec.summary), regions=regions,
                              result=SimpleNamespace(case_dir=case_dir, success=True),
                              errors=list(rec.errors), success=True)
    return SimpleNamespace(outcome=outcome, case=case, data=case.model_dump(mode="json"),
                           store_id=rec.id, label=rec.label)
