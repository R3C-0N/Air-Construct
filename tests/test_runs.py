"""Run history, queue and comparison, with the fake solver of test_resume."""

import threading

import pytest
from test_resume import DiskSolver, _case

from airconstruct.runs import RunQueue, RunStore, compare


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path / "runs")


def test_store_roundtrip(store, tmp_path):
    rec = store.add(_case(), label="first")
    assert rec.status == "queued" and rec.label == "first"
    assert rec.case.name == "electronics_box"
    assert store.queued()[0].id == rec.id
    store.update(rec.id, status="failed", errors=["boom"], converged=False)
    assert store.get(rec.id).errors == ["boom"]
    with pytest.raises(ValueError):
        store.update(rec.id, status="weird")
    with pytest.raises(KeyError):
        store.get("nope")


def test_crashed_runs_become_resumable(store):
    rid = store.add(_case()).id
    store.update(rid, status="running")
    again = RunStore(store.root)  # the application restarts
    assert again.get(rid).status == "stopped"


def test_queue_runs_in_order_and_keeps_history(store):
    events = []
    q = RunQueue(store, DiskSolver, lambda rid, kind, p: events.append((rid, kind)))
    a = q.submit(_case(200), "a")
    b = q.submit(_case(300), "b")
    c = q.submit(_case(300), "c")
    q.cancel(c)
    assert q.run_pending() == [a, b]
    ra, rb = store.get(a), store.get(b)
    assert ra.status in ("converged", "finished") and rb.status in ("converged", "finished")
    assert store.get(c).status == "cancelled"
    assert ra.case_dir != rb.case_dir  # each run keeps its own files
    assert ra.iterations > 0 and ra.ended_at
    starts = [rid for rid, kind in events if kind == "started"]
    assert starts == [a, b]
    assert any(kind == "progress" for _, kind in events)
    assert [r.id for r in store.list()] == [c, b, a]
    assert [r.id for r in store.list(status="cancelled")] == [c]


def test_stop_and_resume_through_the_queue(store):
    q = RunQueue(store, DiskSolver)
    seen = {"n": 0}

    def on_event(rid, kind, p):
        if kind == "progress":
            seen["n"] += 1
            if seen["n"] == 50:
                q.stop_current()

    q.on_event = on_event
    rid = q.submit(_case(600))
    q.run_pending()
    rec = store.get(rid)
    assert rec.status == "stopped" and 40 <= rec.iterations <= 60
    with pytest.raises(ValueError):
        q.resume(q.submit(_case()))  # queued: nothing to resume
    store.update(store.queued()[0].id, status="cancelled")
    q.resume(rid)
    assert store.get(rid).resume
    q.run_pending()
    rec = store.get(rid)
    assert rec.status in ("converged", "finished") and rec.iterations > 60
    assert not rec.resume


def test_background_worker(store):
    done = threading.Event()
    q = RunQueue(store, DiskSolver, lambda rid, kind, p: kind == "ended" and done.set())
    q.start()
    rid = q.submit(_case(150))
    assert done.wait(30)
    q.shutdown()
    assert store.get(rid).status in ("converged", "finished")


def test_compare(store):
    a = store.add(_case(100), "base")
    other = _case(100)
    other.components[1].power = 12.0  # cpu
    other.openings[0].flow_rate = 0.004
    b = store.add(other, "more power")
    store.update(a.id, iterations=100, summary={
        "pressure_drop_pa": 1.0, "components": [{"name": "cpu", "t_max_c": 80.0, "t_mean_c": 70.0}],
        "energy": {"parts": [{"name": "cpu", "to_air_w": 1.0}]}})
    store.update(b.id, iterations=120, summary={
        "pressure_drop_pa": 1.5, "components": [{"name": "cpu", "t_max_c": 95.0, "t_mean_c": 80.0}]})
    d = compare(store.get(a.id), store.get(b.id))
    assert d.case_changes == {"components.cpu.power": (8.0, 12.0),
                              "openings.inlet.flow_rate": (0.0032, 0.004)}
    assert d.metrics["cpu.t_max_c"] == (80.0, 95.0, 15.0)
    assert d.metrics["pressure_drop_pa"] == (1.0, 1.5, 0.5)
    assert d.metrics["iterations"][2] == 20
    assert d.metrics["cpu.to_air_w"] == (1.0, None, None)
    assert d.to_dict()["case_changes"]["components.cpu.power"] == [8.0, 12.0]


def test_cli(store, monkeypatch, capsys, tmp_path):
    from test_case import EXAMPLE

    from airconstruct import cli, runner

    monkeypatch.setattr(runner, "detect_backend", DiskSolver)
    root = str(store.root)
    assert cli.main(["queue", "add", str(EXAMPLE), "--root", root, "--label", "x"]) == 0
    assert cli.main(["queue", "run", "--root", root, "--quiet"]) == 0
    assert cli.main(["history", "--root", root]) == 0
    out = capsys.readouterr().out
    assert "electronics_box" in out
    rid = store.list()[0].id
    assert cli.main(["compare", rid, rid, "--root", root]) == 0
    assert cli.main(["resume", store.get(rid).case_dir, "--iterations", "30000", "--quiet"]) == 0
