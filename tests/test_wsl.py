import json
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from airconstruct import runner, wsl
from airconstruct.wsl import WslBackend, lightweight, windows_to_wsl, wsl_to_windows

FAKE = Path(__file__).with_name("fake_wsl.py")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    root = tmp_path / "linux"
    root.mkdir()
    log = tmp_path / "wsl_calls.jsonl"
    monkeypatch.setenv("FAKE_WSL_ROOT", str(root))
    monkeypatch.setenv("FAKE_WSL_LOG", str(log))
    monkeypatch.delenv("FAKE_WSL_PROBE", raising=False)
    monkeypatch.delenv("FAKE_WSL_FAIL", raising=False)

    class Fake:
        linux = root

        def calls(self):
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text().splitlines()]

        def backend(self, **kw):
            return WslBackend(wsl_command=[sys.executable, str(FAKE)], **kw)

    return Fake()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def case_dir(tmp_path):
    d = tmp_path / "win" / "box case"
    write(d / "system" / "controlDict", "endTime 3;\n")
    write(d / "0" / "air" / "T", "internalField uniform 300;\n")
    return d


# A fake solver: reads the case, writes time directories, decomposed data and a log.
SOLVER = textwrap.dedent("""
    import os, pathlib
    print("cwd", os.getcwd(), flush=True)
    print(pathlib.Path("system/controlDict").read_text().strip(), flush=True)
    for t in ("1", "2", "3"):
        pathlib.Path(t, "air").mkdir(parents=True, exist_ok=True)
        pathlib.Path(t, "air", "T").write_text("T at " + t)
    pathlib.Path("processor0", "3").mkdir(parents=True, exist_ok=True)
    pathlib.Path("processor0", "3", "T").write_text("big")
    pathlib.Path("log.solver").write_text("Time = 3\\nEnd\\n")
    print("Time = 3", flush=True)
""")


# ---------------------------------------------------------------- paths


@pytest.mark.parametrize("win, linux", [
    (r"C:\Users\me\runs\box", "/mnt/c/Users/me/runs/box"),
    ("D:\\", "/mnt/d"),
    (r"\\wsl.localhost\Ubuntu\home\me\box", "/home/me/box"),
    (r"\\wsl$\Ubuntu-24.04\tmp", "/tmp"),
])
def test_windows_to_wsl(win, linux):
    assert windows_to_wsl(win) == linux


def test_windows_to_wsl_custom_mount_root():
    assert windows_to_wsl(r"C:\x", mount_root="/") == "/c/x"


@pytest.mark.parametrize("bad", [r"relative\path", r"\\server\share\x", "C:no_root"])
def test_windows_to_wsl_rejects(bad):
    with pytest.raises(ValueError):
        windows_to_wsl(bad)


def test_wsl_to_windows():
    assert wsl_to_windows("/mnt/c/Users/me", "Ubuntu") == r"C:\Users\me"
    assert wsl_to_windows("/home/me/box", "Ubuntu") == r"\\wsl.localhost\Ubuntu\home\me\box"
    assert wsl_to_windows("/mnt/wsl/x", "Ubuntu") == r"\\wsl.localhost\Ubuntu\mnt\wsl\x"
    with pytest.raises(ValueError):
        wsl_to_windows("home/me", "Ubuntu")


def test_unc_distro():
    assert wsl.wsl_unc_distro(r"\\wsl.localhost\Ubuntu\home") == "Ubuntu"
    assert wsl.wsl_unc_distro(r"\\server\share\x") is None
    assert wsl.wsl_unc_distro(r"C:\x") is None


def test_lightweight():
    paths = ["0/air/T", "0.5/air/T", "1/air/T", "1/air/U", "processor0/1/air/T",
             "log.solver", "constant/air/polyMesh/points", "postProcessing/probes/0/T"]
    assert lightweight(paths) == ["0/air/T", "1/air/T", "1/air/U", "log.solver",
                                  "constant/air/polyMesh/points", "postProcessing/probes/0/T"]
    # first/latest are taken over the whole case, not only the changed files
    assert lightweight(["0.5/air/T"], all_paths=["0/T", "0.5/air/T", "1/T"]) == []


def test_utf16_decode():
    assert wsl._decode("no distro".encode("utf-16-le")) == "no distro"
    assert wsl._decode(b"plain") == "plain"


# ---------------------------------------------------------------- detection


def test_available_ok(fake):
    ok, msg = fake.backend().available()
    assert ok, msg
    assert "v2406" in msg and "WSL1" not in msg


def test_available_without_wsl(monkeypatch):
    monkeypatch.setattr(wsl, "find_wsl_exe", lambda: None)
    ok, msg = WslBackend().available()
    assert not ok and "wsl --install" in msg


def test_available_without_distro(fake, monkeypatch):
    monkeypatch.setenv("FAKE_WSL_FAIL", "There is no distribution installed.")
    ok, msg = fake.backend().available()
    assert not ok
    assert "There is no distribution installed." in msg and "wsl --install" in msg


def test_available_without_openfoam(fake, monkeypatch):
    monkeypatch.setenv("FAKE_WSL_PROBE", "kernel=5.15-microsoft-standard-WSL2\nhome=/h\n")
    ok, msg = fake.backend().available()
    assert not ok and "openfoam2406-default" in msg


def test_available_wrong_edition(fake, monkeypatch):
    monkeypatch.setenv("FAKE_WSL_PROBE", "kernel=x-microsoft-standard-WSL2\nversion=12\nsolver=\n")
    ok, msg = fake.backend().available()
    assert not ok and "ESI" in msg


def test_available_wsl1_warns(fake, monkeypatch):
    monkeypatch.setenv("FAKE_WSL_PROBE",
                       "kernel=4.4.0-19041-Microsoft\nhome=/h\nversion=v2406\nsolver=/x\n")
    ok, msg = fake.backend().available()
    assert ok and "WSL1" in msg


def test_distro_and_bashrc_forwarded(fake, case_dir):
    b = fake.backend(distro="Ubuntu-24.04", foam_bashrc="/opt/of/etc/bashrc")
    b.run(["python3", "-c", "pass"], case_dir)
    calls = fake.calls()
    assert all(c[:2] == ["-d", "Ubuntu-24.04"] for c in calls)
    probe = next(c for c in calls if "airconstruct-probe" in c)
    assert probe[-1] == "/opt/of/etc/bashrc"
    run = next(c for c in calls if "airconstruct-run" in c)
    assert run[run.index("airconstruct-run") + 2] == "/opt/of/etc/bashrc"


def test_detect_backend_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert isinstance(runner.detect_backend(), WslBackend)


# ---------------------------------------------------------------- run and sync


def test_run_in_linux_filesystem_and_pull_results(fake, case_dir):
    b = fake.backend()
    lines: list[str] = []
    code = b.run(["python3", "-c", SOLVER], case_dir, on_line=lines.append)
    assert code == 0
    assert lines[1:] == ["endTime 3;", "Time = 3"]

    # ran inside the WSL home, not on /mnt/c
    mirror = b.wsl_dir(case_dir)
    assert mirror.startswith("/home/tester/.cache/airconstruct/cases/box_case-")
    assert Path(lines[0].split(" ", 1)[1]).resolve() == Path(fake.linux, mirror.lstrip("/")).resolve()
    assert (fake.linux / mirror.lstrip("/") / "processor0" / "3" / "T").exists()

    # lightweight results came back; decomposed data and intermediate times did not
    assert (case_dir / "3" / "air" / "T").read_text() == "T at 3"
    assert (case_dir / "log.solver").exists()
    assert not (case_dir / "1").exists() and not (case_dir / "2").exists()
    assert not (case_dir / "processor0").exists()
    assert (case_dir / "0" / "air" / "T").exists()

    # everything=True fetches the rest
    pulled = b.pull(case_dir, everything=True)
    assert "processor0/3/T" in pulled and (case_dir / "2" / "air" / "T").exists()


def test_incremental_push(fake, case_dir):
    b = fake.backend()
    b.run(["python3", "-c", SOLVER], case_dir)
    assert b.push(case_dir) == []  # pulled results are not sent back

    write(case_dir / "system" / "fvSolution", "solvers {}\n")
    assert b.push(case_dir) == ["system/fvSolution"]

    (case_dir / "system" / "fvSolution").unlink()
    b.push(case_dir)
    assert not (fake.linux / b.wsl_dir(case_dir).lstrip("/") / "system" / "fvSolution").exists()


def test_rewritten_case_resets_mirror(fake, case_dir):
    b = fake.backend()
    b.run(["python3", "-c", SOLVER], case_dir)
    mirror = fake.linux / b.wsl_dir(case_dir).lstrip("/")
    assert (mirror / "processor0").exists()

    time.sleep(0.05)  # new mtimes for every file
    for p in list(case_dir.rglob("*")):
        if p.is_file():
            p.write_text(p.read_text())
    b.push(case_dir)
    assert not (mirror / "processor0").exists()


def test_results_removed_in_wsl_are_removed_on_windows(fake, case_dir):
    b = fake.backend()
    b.run(["python3", "-c", SOLVER], case_dir)
    assert (case_dir / "3").exists()
    b.run(["python3", "-c", "import shutil; shutil.rmtree('3')"], case_dir)
    assert not (case_dir / "3" / "air" / "T").exists()


def test_exit_code_passthrough(fake, case_dir):
    b = fake.backend()
    lines: list[str] = []
    code = b.run(["python3", "-c", "import sys; print('FOAM FATAL ERROR'); sys.exit(3)"],
                 case_dir, on_line=lines.append)
    assert code == 3 and lines == ["FOAM FATAL ERROR"]


def test_stop(fake, case_dir):
    b = fake.backend()
    result = {}
    t = threading.Thread(target=lambda: result.update(
        code=b.run(["python3", "-c", "import time; time.sleep(60)"], case_dir)))
    start = time.monotonic()
    t.start()
    pidfiles = fake.linux / "tmp"
    while not (pidfiles.exists() and any(pidfiles.iterdir())):
        assert time.monotonic() - start < 30, "solver did not start"
        time.sleep(0.05)
    b.stop()
    t.join(30)
    assert not t.is_alive()
    assert result["code"] != 0
    assert time.monotonic() - start < 30


def test_reset(fake, case_dir):
    b = fake.backend()
    b.run(["python3", "-c", "pass"], case_dir)
    mirror = fake.linux / b.wsl_dir(case_dir).lstrip("/")
    assert mirror.exists()
    b.reset(case_dir)
    assert not mirror.exists()


def test_safe_extract_skips_escapes(tmp_path):
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in ("ok/T", "../evil"):
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    buf.seek(0)
    dest = tmp_path / "dest"
    dest.mkdir()
    with tarfile.open(fileobj=buf, mode="r:") as tar:
        wsl._safe_extract(tar, dest)
    assert (dest / "ok" / "T").exists() and not (tmp_path / "evil").exists()

