"""Simulation step: live progress, stop/resume, queue, probes, lock and error states.

Offscreen Qt, 3D disabled, solver mocked (tests/ui_support.py)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from ui_support import EXAMPLE, fake_run, fake_summary, make_window, run_and_wait, wait_until

from airconstruct.case import Case
from airconstruct.monitor import Progress
from airconstruct.runner import RunResult


class Diag:
    def __init__(self, code, severity, message, part=None, hint=""):
        self.code, self.severity, self.message, self.part, self.hint = (
            code, severity, message, part, hint)


def phased_run(flow=40, energy=60, stop_at=None, fail=None, delay=0.0, start=0):
    """A fake run_case that reports monitor snapshots through flow then energy phases.
    stop_at: keep going (slowly) until should_stop(); fail: return a failed result."""
    calls = []

    def run(case, work_dir, on_log=None, on_residual=None, should_stop=None, on_progress=None,
            case_dir=None, resume=False, **_kw):
        calls.append({"resume": resume, "case_dir": case_dir})
        case_dir = Path(case_dir) if case_dir else Path(work_dir) / case.name
        case_dir.mkdir(parents=True, exist_ok=True)
        if not resume:
            on_log("$ blockMesh")
        it, stopped = start, False
        plan = [("flow", flow), ("energy", energy)]
        if resume:  # continue in energy
            plan = [("energy", energy)]
        for phase, n in plan:
            on_log(f"--- phase {phase}: iterations {it}..{it + n}")
            for _ in range(n):
                if should_stop and should_stop():
                    stopped = True
                    break
                it += 1
                res = {"Ux": 1.0 / it, "p_rgh": 0.5 / it, "h": 0.1 / it}
                on_residual(it, res)
                on_progress(Progress(it, phase, {"air": res, "cpu": {"h": 0.2 / it}},
                                     {"cpu": {"min_c": 30.0, "mean_c": 40.0, "max_c": 40.0 + it / 10},
                                      "air": {"min_c": 25.0, "mean_c": 27.0, "max_c": 31.0}}))
                if delay:
                    time.sleep(delay)
                if stop_at is not None and it >= stop_at:
                    while should_stop and not should_stop():
                        time.sleep(0.005)
            if stopped:
                break
        if fail:
            return RunResult(case_dir, False, False, it, 2.0,
                             errors=["splitMeshRegions failed (exit 1)\nFOAM FATAL ERROR"],
                             diagnosis=fail)
        res = RunResult(case_dir, True, not stopped, it, 2.0,
                        region_temperatures={"cpu": (30.0, 40.0 + it / 10)})
        res.stopped = stopped
        return res

    run.calls = calls
    return run


def window(tmp_path, settings, run_fn, **kw):
    kw.setdefault("summarize_fn", fake_summary)
    kw.setdefault("load_regions_fn", lambda d: {})
    win = make_window(tmp_path, settings, run_fn=run_fn, **kw)
    return win, win.tabs["simulation"]


# --------------------------------------------------------------------- live run


def test_live_progress_temperatures_and_lock(qapp, tmp_path, settings):
    win, sim = window(tmp_path, settings, phased_run(stop_at=30))
    r = win.runner
    assert sim.card.headline.text() == "Prêt à lancer"
    assert win.start_run()
    assert wait_until(qapp, lambda: r.iteration >= 30)
    qapp.processEvents()
    # locked while running: physics edits refused, ui metadata still fine, undo off
    p = win.project
    assert p.locked and sim.solver_card.lock_note.isVisibleTo(sim.solver_card)
    assert not p.edit("power", lambda d: d["components"][0].__setitem__("power", 99.0))
    assert p.edit("hide", lambda d: d.setdefault("ui", {}).__setitem__("hidden", ["ram"]))
    assert not win.undo_action.isEnabled()
    assert "verrouillé" in win.status.message_label.text() if hasattr(win.status,
                                                                       "message_label") else True
    assert "Verrouillé" in win.tab_bar.buttons[0].toolTip()
    assert not sim.solver_card.iterations.isEnabled()
    assert not sim.live_card.add_probe_button.isEnabled()
    # live numbers
    assert sim.card.state_pill.text() == "En cours"
    assert sim.card.headline.text().startswith("Progression globale")
    assert "phase Écoulement" in sim.card.detail.text()
    assert "itération 30" in sim.card.detail.text()
    assert sim.live_card.part_rows()[0] == ("cpu", "43,0 °C")
    assert sim.live_temperatures["cpu"] == pytest.approx(43.0)
    sim._chart_timer.timeout.emit()
    assert set(sim.chart.curves) == {"Ux", "p_rgh", "h"}
    assert sim.stepper.texts()[:2] == ["Maillage ✓", "Écoulement 30 it."]
    assert sim.stop_button.isVisible() is False or sim.stop_button.isEnabled()
    r.stop()
    assert wait_until(qapp, lambda: not r.running)
    assert not p.locked and win.undo_action.isEnabled()
    win.close()


def test_stop_and_resume_keep_one_iteration_count(qapp, tmp_path, settings):
    run = phased_run(flow=20, energy=200, stop_at=55)
    win, sim = window(tmp_path, settings, run)
    r = win.runner
    r.resume_problem_fn = lambda case, case_dir: None
    assert win.start_run()
    assert wait_until(qapp, lambda: r.iteration >= 55)
    sim.stop_button.click()
    assert wait_until(qapp, lambda: not r.running and win.project.results is not None)
    outcome = win.project.results.outcome
    assert outcome.stopped and outcome.success
    # one count everywhere: headline, stepper, result
    assert sim.card.headline.text() == "Arrêté par vous après 55 itérations (phase Températures)"
    assert outcome.result.iterations == 55
    texts = sim.stepper.texts()
    assert texts[1] == "Écoulement ✓ 20 it." and texts[2] == "Températures 35 it."
    assert sim.card.state_pill.text() == "Arrêté"
    assert "non convergé" in sim.card.note.text()
    assert sim.resume_button.isVisibleTo(sim.card) and sim.resume_button.isEnabled()
    assert sim.restart_button.isVisibleTo(sim.card)
    assert not sim.run_button.isVisibleTo(sim.card)
    assert sim.card.results_button.text() == "Voir le résultat partiel"

    history = list(r.history)
    r.read_history_fn = lambda case_dir: history
    run_resumed = phased_run(energy=10, start=55)
    r._run_fn = run_resumed
    assert sim.resume()
    assert r.resuming and r.iteration == 55  # chart and counts start from the history
    assert wait_until(qapp, lambda: not r.running and win.project.results is not None)
    assert run_resumed.calls[0]["resume"] is True
    assert Path(run_resumed.calls[0]["case_dir"]) == outcome.result.case_dir
    out2 = win.project.results.outcome
    assert out2.resumed and out2.result.converged and out2.result.iterations == 65
    assert sum(r.phase_iterations().values()) == 65
    assert sim.card.headline.text() == "Convergé après 65 itérations"
    win.close()


def test_resume_refused_when_the_model_changed(qapp, tmp_path, settings):
    win, sim = window(tmp_path, settings, phased_run(stop_at=5))
    r = win.runner
    assert win.start_run()
    assert wait_until(qapp, lambda: r.iteration >= 5)
    r.stop()
    assert wait_until(qapp, lambda: not r.running)
    r.resume_problem_fn = lambda case, d: "the case changed since the run started (components)"
    ok, why = r.can_resume()
    assert not ok and "changed" in why
    assert not sim.resume_button.isEnabled()
    assert not sim.resume()
    win.close()


# --------------------------------------------------------------------- errors


def test_failed_run_shows_cause_and_goes_to_the_part(qapp, tmp_path, settings):
    diag = [Diag("parts_overlap", "warning", "Les pièces « ram » et « regulator » se "
                 "chevauchent.", "regulator", "Déplacez l'une des deux pièces."),
            Diag("mesh_failed", "error", "Le maillage a échoué (splitMeshRegions) : Cell 18234 "
                 "belongs to more than one region")]
    win, sim = window(tmp_path, settings, phased_run(flow=3, energy=0, fail=diag))
    outcome = run_and_wait(qapp, win)
    assert not outcome.success
    dlg = sim.failure_dialog
    assert dlg is not None and dlg.isVisible()
    assert dlg.report.title == "Le calcul a échoué au maillage"
    assert dlg.report.subtitle.startswith("splitMeshRegions failed (exit 1) · après")
    assert "Cause probable : Le maillage a échoué" in dlg.cause.text()
    assert dlg.report.part == "regulator"  # the first part named, from the overlap
    assert dlg.goto_button.isVisibleTo(dlg)
    assert sim.card.state_pill.text() == "Échoué"
    assert sim.card.details_button.isVisibleTo(sim.card)
    dlg.goto_button.click()
    assert win.current_tab is win.tabs["model"]
    assert win.selection.keys == ["component:regulator"]
    win.close()


def test_openfoam_missing_dialog(qapp, tmp_path, settings, monkeypatch):
    from airconstruct import runner

    class NoFoam(runner.Backend):
        def available(self):
            return False, "OpenFOAM not found (install openfoam.com, or set AIRCONSTRUCT_FOAM_BASHRC)"

    monkeypatch.setattr(runner, "detect_backend", lambda: NoFoam())
    win, sim = window(tmp_path, settings, None)
    outcome = run_and_wait(qapp, win)
    assert outcome.backend_missing.startswith("OpenFOAM not found")
    dlg = sim.openfoam_dialog
    assert dlg is not None and dlg.isVisible()
    assert [row.command for row in dlg.command_rows][-1] == \
        "sudo apt-get install -y openfoam2406-default"
    assert sim.card.state_pill.text() == "OpenFOAM absent"
    sim.check_backend = lambda: (True, "OpenFOAM v2406")
    assert dlg.check_again()
    assert "Trouvé" in dlg.status.text()
    win.close()


@pytest.mark.parametrize(("message", "platform", "title", "first_cmd"), [
    ("WSL2 is not installed. Open PowerShell as administrator", "win32",
     "WSL n’est pas installé", "wsl --install -d Ubuntu"),
    ("WSL is installed but no Linux distribution could be started (x)", "win32",
     "Aucune distribution Linux ne démarre dans WSL", "wsl --install -d Ubuntu"),
    ("OpenFOAM was not found in WSL. Open the Ubuntu terminal", "win32",
     "OpenFOAM est introuvable", "curl -s https://dl.openfoam.com/add-debian-repo.sh | sudo bash"),
    ("OpenFOAM not found (install openfoam.com, or set AIRCONSTRUCT_FOAM_BASHRC)", "linux",
     "OpenFOAM est introuvable", "curl -s https://dl.openfoam.com/add-debian-repo.sh | sudo bash"),
    ("OpenFOAM 11 found in WSL but chtMultiRegionSimpleFoam is missing. Air-Construct", "win32",
     "Cette version d’OpenFOAM ne convient pas", "curl -s https://dl.openfoam.com/add-debian-repo.sh | sudo bash"),
    ("OpenFOAM found but chtMultiRegionSimpleFoam is missing", "linux",
     "Cette version d’OpenFOAM ne convient pas", "curl -s https://dl.openfoam.com/add-debian-repo.sh | sudo bash"),
])
def test_openfoam_help_per_platform(message, platform, title, first_cmd):
    from airconstruct.ui.tabs.simulation.errors import openfoam_help

    h = openfoam_help(message, platform)
    assert h.title == title
    assert h.commands[0] == first_cmd
    assert ("Windows" in h.subtitle) == platform.startswith("win")
    assert message in h.as_text()


def test_failure_report_without_diagnosis():
    from airconstruct.ui.tabs.simulation.errors import failure_report

    r = failure_report([], ["chtMultiRegionSimpleFoam failed (exit -9)\n..."], "3 min")
    assert r.title == "Le calcul a échoué"
    assert r.subtitle == "chtMultiRegionSimpleFoam failed (exit -9) · après 3 min"
    assert r.part is None
    r = failure_report([{"code": "solver_diverged", "severity": "error",
                         "message": "Le solveur a divergé pendant le calcul de « ghost ».",
                         "part": "ghost", "hint": ""}], [], "", {"cpu"})
    assert r.title == "Le solveur a divergé" and r.part is None  # not a component


# --------------------------------------------------------------------- queue


def test_queue_runs_cases_one_after_the_other(qapp, tmp_path, settings):
    run = phased_run(flow=5, energy=5)
    win, sim = window(tmp_path, settings, run)
    r, q = win.runner, sim.queue_card
    other = Case.load(EXAMPLE)
    other.name = "rack_1U"
    other_path = tmp_path / "rack.yaml"
    other.save(other_path)
    name = win.project.name
    variant = q.add_current()
    filed = q.add_file(other_path)
    assert [i.status for i in r.queue] == ["queued", "queued"]
    assert q.rows()[0][0].endswith("variante 1") and q.rows()[1][0] == "rack_1U"
    assert q.start_button.isVisibleTo(q)
    # the user runs the model; the queue follows on its own
    assert win.start_run()
    assert q.rows()[0] == (f"{name} · modèle actuel", "en cours")
    assert wait_until(qapp, lambda: all(i.status == "converged" for i in r.queue) and
                      not r.running, 20)
    dirs = [Path(c["case_dir"]) if c["case_dir"] else None for c in run.calls]
    assert dirs[0] is None and dirs[1].name == f"{name}-q{variant.id}"
    assert dirs[2].name == f"rack_1U-file{filed.id}"
    # the file's result did not replace the project's
    assert win.project.results.case.name == name
    assert filed.outcome is not None and not filed.own
    win.confirm_discard = lambda: True
    q.list.setCurrentItem(q.list.topLevelItem(1))
    assert q.current_item() is filed and q.open_button.isVisibleTo(q)
    assert q.open_selected()
    assert win.project.name == "rack_1U" and win.project.results is not None
    assert not win.project.stale
    win.close()


def test_stop_pauses_the_queue_and_reorder(qapp, tmp_path, settings):
    win, sim = window(tmp_path, settings, phased_run(stop_at=3))
    r, q = win.runner, sim.queue_card
    a, b = q.add_current(), q.add_current()
    r.move_in_queue(b, -1)
    assert r.waiting() == [b, a]
    assert win.start_run()
    assert wait_until(qapp, lambda: r.iteration >= 3)
    r.stop()
    assert wait_until(qapp, lambda: not r.running)
    assert r.queue_paused and [i.status for i in r.waiting()] == ["queued", "queued"]
    assert q.start_button.text() == "Reprendre la file"
    r.remove_from_queue(a)
    assert r.waiting() == [b]
    win.close()


# --------------------------------------------------------------------- probes, solver


def test_place_and_move_a_probe(qapp, tmp_path, settings):
    win, sim = window(tmp_path, settings, fake_run)
    live = sim.live_card
    win.selection.set(["component:cpu"])
    name = live.add_probe()
    assert name == "S1"
    case = win.project.case
    probe = case.probes[0]
    cpu = next(c for c in case.components if c.name == "cpu")
    assert probe.point.x == pytest.approx((cpu.box.min[0] + cpu.box.max[0]) / 2)
    assert probe.point.z > cpu.box.max[2]
    assert win.selection.keys == ["probe:S1"]
    assert live.coords.isVisibleTo(live)
    live.coord_spins[0].setValue(20.0)  # mm
    assert win.project.case.probes[0].point.x == pytest.approx(0.020)
    assert sim.live_card.probes.topLevelItem(0).text(0).startswith("S1 · (20,")
    assert sim.probe_action.shortcut().toString() == "P"
    win.goto_tab("simulation")
    assert "probe_S1" in [i.name for i in win.viewport.items] if hasattr(win.viewport, "items") \
        else True
    live.remove_probe()
    assert win.project.case.probes == []
    win.close()


def test_solver_settings_are_undoable(qapp, tmp_path, settings):
    win, sim = window(tmp_path, settings, fake_run)
    s = sim.solver_card
    s.cores.setValue(4)
    s.turbulence.setCurrentIndex(s.turbulence.findData("laminar"))
    assert win.project.case.solver.cores == 4
    assert win.project.case.solver.turbulence == "laminar"
    win.project.undo_stack.undo()
    assert s.turbulence.currentData() == "kOmegaSST"
    win.close()


def test_residual_lines_and_phase_starts():
    from airconstruct.ui.tabs.simulation.chart import phase_starts, residual_lines

    h = [Progress(1, "flow", {"air": {"Ux": 1.0, "h": 0.1}, "cpu": {"h": 0.3}}),
         Progress(2, "energy", {"cpu": {"h": 0.2}, "air": {"h": float("nan")}})]
    lines = residual_lines(h)
    assert lines["h"] == ([1, 2], [0.3, 0.2])
    assert lines["Ux"] == ([1], [1.0])
    assert phase_starts(h) == [(1, "flow"), (2, "energy")]
