"""The frozen-app launcher (packaging/launcher.py), tested from source."""

import importlib.util
import sys
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).parent.parent / "packaging" / "launcher.py"


@pytest.fixture
def launcher():
    spec = importlib.util.spec_from_file_location("ac_launcher", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dash_c_behaves_like_python(launcher, monkeypatch, capsys):
    # results.rendering_available runs [sys.executable, "-c", code]: in the frozen app
    # sys.executable is the launcher, so it must run the code and return its exit code
    monkeypatch.setattr(sys, "argv", ["Air-Construct", "-c", "import sys; print('hi'); sys.exit(3)"])
    assert launcher.main() == 3
    assert capsys.readouterr().out == "hi\n"
    monkeypatch.setattr(sys, "argv", ["Air-Construct", "-c", "x = 1"])
    assert launcher.main() == 0


def test_version(launcher, monkeypatch, capsys, tmp_path):
    log = tmp_path / "v.log"
    monkeypatch.setenv("AIRCONSTRUCT_SELFTEST_LOG", str(log))
    monkeypatch.setattr(sys, "argv", ["Air-Construct", "--version"])
    assert launcher.main() == 0
    assert "Air-Construct 0." in capsys.readouterr().out
    assert log.read_text().startswith("Air-Construct ")


def test_self_test(launcher, monkeypatch, capsys):
    pytest.importorskip("PySide6")
    pytest.importorskip("pyvista")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("AIRCONSTRUCT_NO_3D", "1")
    monkeypatch.delenv("AIRCONSTRUCT_SELFTEST_LOG", raising=False)
    monkeypatch.setattr(sys, "argv", ["Air-Construct", "--self-test"])
    assert launcher.main() == 0
    assert "self-test OK" in capsys.readouterr().out
