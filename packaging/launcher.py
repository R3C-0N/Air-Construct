"""Entry point of the frozen (PyInstaller) desktop app.

Owned by: workstream "packaging".

Besides starting the window like ``airconstruct-gui``, it handles two things a frozen
executable cannot do on its own:

- ``-c CODE``: some modules probe features in a child process with
  ``[sys.executable, "-c", code]`` (results.rendering_available). In a frozen app
  sys.executable is this program, so it must behave like ``python -c``.
- ``--self-test``: import every module, build the main window off screen, close it and
  exit 0. CI runs it on the built executable to catch missing modules or data files.
"""

from __future__ import annotations

import os
import sys


def _run_code(code: str, args: list[str]) -> int:
    sys.argv = ["-c", *args]
    try:
        exec(compile(code, "<string>", "exec"), {"__name__": "__main__"})  # noqa: S102
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


def self_test() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    report: list[str] = []

    import airconstruct
    from airconstruct import case, cli, estimate, materials, mesh, results, runner, wsl  # noqa: F401
    from airconstruct.foam import writer  # noqa: F401

    report.append(f"airconstruct {getattr(airconstruct, '__version__', '?')}")

    import numpy
    import pydantic
    import pyvista
    import vtkmodules

    report.append(f"numpy {numpy.__version__}, pydantic {pydantic.__version__}, "
                  f"pyvista {pyvista.__version__}, vtk {vtkmodules.__version__}")

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from airconstruct.ui import app as ui_app
    from airconstruct.ui.view import SceneView

    qapp = QApplication.instance() or QApplication([sys.argv[0]])
    case_obj = ui_app.default_case()
    report.append(f"default case: {case_obj.name} ({len(case_obj.components)} components)")
    import pyvista.plotting  # the rendering modules must be bundled

    from airconstruct.ui.view import geometry_items

    report.append(f"geometry: {len(geometry_items(case_obj))} scene items")
    win = ui_app.MainWindow()
    win.show()
    QTimer.singleShot(1500, qapp.quit)
    qapp.exec()
    view = win.findChild(SceneView)
    if view is not None:
        ph = getattr(view, "placeholder", None)
        state = ph.text() if ph is not None else "3D viewport"
        report.append(f"scene view: {state}")
    win.close()
    win.deleteLater()
    qapp.processEvents()

    # the matplotlib backend used for report images must be bundled
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure()
    plt.close(fig)

    report.append("self-test OK")
    _emit("\n".join(report))
    return 0


def _emit(text: str) -> None:
    """Print, or write to AIRCONSTRUCT_SELFTEST_LOG: a windowed exe has no stdout."""
    log = os.environ.get("AIRCONSTRUCT_SELFTEST_LOG")
    if log:
        with open(log, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if sys.stdout is not None:
        print(text, flush=True)


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["-c"] and len(argv) >= 2:
        return _run_code(argv[1], argv[2:])
    if argv[:1] == ["--self-test"]:
        return self_test()
    if argv[:1] in (["--version"], ["-V"]):
        from airconstruct import __version__

        _emit(f"Air-Construct {__version__}")
        return 0
    from airconstruct.ui.app import main as gui_main

    return gui_main([sys.argv[0], *argv])


def _exit_now(code: int) -> None:
    """Exit without interpreter teardown. Once the window is closed (MainWindow.closeEvent
    releases the VTK view), destroying the remaining Qt and VTK objects in arbitrary
    order can crash on Windows (0xC0000409), turning a clean exit into an error code."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.flush()
    os._exit(code)


if __name__ == "__main__":
    _exit_now(main())
