"""Automatic installation of WSL2 / Ubuntu / OpenFOAM (airconstruct.foam_setup), with a
fake shell: nothing is installed, the commands issued are checked."""

from __future__ import annotations

import threading

import pytest

from airconstruct import foam_setup as fs
from airconstruct.foam_setup import FoamSetup, SetupCancelled, Shell, plan_for

WSL = r"C:\Windows\System32\wsl.exe"
NO_WSL = "WSL2 is not installed. Open PowerShell as administrator, run"
NO_DISTRO = "WSL is installed but no Linux distribution could be started (x)."
NO_FOAM = "OpenFOAM was not found in WSL. Open the Ubuntu terminal and run"
FOREIGN = "OpenFOAM 11 found in WSL but chtMultiRegionSimpleFoam is missing."
LINUX_NO_FOAM = "OpenFOAM not found (install openfoam.com, or set AIRCONSTRUCT_FOAM_BASHRC)"


class FakeShell(Shell):
    """Records every command; answers from ``codes`` (first matching substring)."""

    def __init__(self, codes=None, elevated=0, distro_after_polls=0, output=()):
        self.calls: list[list[str]] = []
        self.codes = codes or {}
        self.elevated_code = elevated
        self.elevated: list[list[str]] = []
        self.consoles: list[list[str]] = []
        self.polls = 0
        self.distro_after_polls = distro_after_polls
        self.output = list(output)

    def run(self, argv, on_line=None, cancel=None):
        self.calls.append(argv)
        joined = " ".join(argv)
        if argv[-4:] == ["-u", "root", "-e", "true"]:  # does the distro start?
            self.polls += 1
            return 0 if self.polls > self.distro_after_polls else 1
        if "bash" in argv and argv[-1] == fs.INSTALL_SCRIPT:
            for line in self.output:
                if on_line:
                    on_line(line)
        for key, code in self.codes.items():
            if key in joined:
                return code
        return 0

    def run_elevated(self, exe, args):
        self.elevated.append([exe, *args])
        if self.elevated_code == "cancel":
            raise SetupCancelled()
        return self.elevated_code

    def open_console(self, argv):
        self.consoles.append(argv)

    def sleep(self, seconds, cancel):
        if cancel.wait(0.01):
            raise SetupCancelled()


def make(plan, shell, check=lambda: (True, "OpenFOAM v2406 in WSL"), **kw):
    steps, lines, progress = [], [], []
    scheduled = []
    setup = FoamSetup(plan, check, shell=shell, wsl_exe=WSL, platform=kw.pop("platform", "win32"),
                      on_step=lambda s, st: steps.append((s, st)), on_line=lines.append,
                      on_progress=progress.append, schedule=lambda: scheduled.append(1) or True,
                      unschedule=lambda: scheduled.append(0), **kw)
    return setup, steps, lines, progress, scheduled


# --------------------------------------------------------------------------- plan


@pytest.mark.parametrize(("message", "kind", "steps", "admin", "reboot"), [
    (NO_WSL, "wsl", ["wsl", "reboot", "distro", "openfoam", "check"], True, True),
    (NO_DISTRO, "distro", ["distro", "openfoam", "check"], False, False),
    (NO_FOAM, "openfoam", ["openfoam", "check"], False, False),
    (FOREIGN, "openfoam", ["openfoam", "check"], False, False),
])
def test_plan_on_windows(message, kind, steps, admin, reboot):
    p = plan_for(message, "win32")
    assert (p.kind, p.steps, p.needs_admin, p.may_reboot) == (kind, steps, admin, reboot)
    assert p.automatic and p.windows
    text = "\n".join(p.consent_lines())
    assert "OpenFOAM" in text and ("administrateur" in text) == admin
    assert ("redémarrage" in text) == reboot


def test_plan_on_linux():
    has = {"apt-get": "/usr/bin/apt-get", "pkexec": "/usr/bin/pkexec"}
    p = plan_for(LINUX_NO_FOAM, "linux", has.get)
    assert p.kind == "openfoam" and p.needs_admin and not p.windows
    assert "mot de passe administrateur" in "\n".join(p.consent_lines())
    p = plan_for(LINUX_NO_FOAM, "linux", {"pkexec": "x"}.get)
    assert not p.automatic and "apt-get" in p.reason
    p = plan_for(LINUX_NO_FOAM, "linux", {"apt-get": "x"}.get)
    assert not p.automatic and "pkexec" in p.reason
    assert not plan_for(LINUX_NO_FOAM, "darwin").automatic


# --------------------------------------------------------------------------- flows


def test_fresh_windows_installs_wsl_then_asks_for_a_restart():
    shell = FakeShell(codes={"--status": 1})  # WSL2 not running before the restart
    setup, steps, _, _, scheduled = make(plan_for(NO_WSL, "win32"), shell)
    result = setup.run()
    assert result.status == "reboot"
    assert shell.elevated == [[WSL, "--install", "-d", "Ubuntu", "--no-launch"]]
    assert steps == [("wsl", "running"), ("wsl", "done"), ("reboot", "running")]
    assert scheduled == [1]  # resumes at the next logon
    assert not any("bash" in c for c in shell.calls)  # OpenFOAM waits for the restart


def test_after_the_restart_ubuntu_is_set_up_then_openfoam():
    shell = FakeShell(distro_after_polls=3,
                      output=["== airconstruct: step 1/4 apt-get update", "Get:1 ...",
                              "== airconstruct: step 4/4 openfoam2406-default",
                              "== airconstruct: done"])
    setup, steps, lines, progress, scheduled = make(plan_for(NO_DISTRO, "win32"), shell)
    result = setup.run()
    assert result.status == "done", result.message
    # first start in its own console, where Ubuntu asks for the Linux user
    assert shell.consoles == [[WSL, "--install", "-d", "Ubuntu"]]
    assert setup.distro == "Ubuntu"
    install = shell.calls[-1]
    assert install[:7] == [WSL, "-d", "Ubuntu", "-u", "root", "-e", "bash"]
    assert install[-1] == fs.INSTALL_SCRIPT and "openfoam2406-default" in install[-1]
    assert [s for s, st in steps if st == "done"] == ["distro", "openfoam", "check"]
    assert progress[0] == pytest.approx(0.05) and progress[-1] == 1.0
    assert any(line.startswith("✓ OpenFOAM v2406") for line in lines)
    assert scheduled == [0]  # nothing left to resume


def test_wsl_without_restart_goes_on_to_openfoam():
    shell = FakeShell(elevated=0)  # wsl --status works right away
    setup, steps, *_ = make(plan_for(NO_WSL, "win32"), shell)
    assert setup.run().status == "done"
    assert ("reboot", "skipped") in steps
    assert shell.consoles == []  # Ubuntu already starts as root


def test_reboot_required_exit_code():
    shell = FakeShell(elevated=3010)
    setup, *_ = make(plan_for(NO_WSL, "win32"), shell)
    assert setup.run().status == "reboot"


def test_uac_refused_or_failed():
    setup, *_ = make(plan_for(NO_WSL, "win32"), FakeShell(elevated="cancel"))
    assert setup.run().status == "cancelled"
    setup, *_ = make(plan_for(NO_WSL, "win32"), FakeShell(elevated=1))
    r = setup.run()
    assert r.status == "failed" and "code 1" in r.message


def test_openfoam_into_the_default_distro_and_failures():
    shell = FakeShell(codes={"bash": 100}, output=["E: Unable to locate package"])
    setup, *_ = make(plan_for(NO_FOAM, "win32"), shell)
    r = setup.run()
    assert r.status == "failed" and "code 100" in r.message and "Unable to locate" in r.message
    assert shell.calls[-1][:4] == [WSL, "-u", "root", "-e"]  # no -d: the default distro
    setup, *_ = make(plan_for(NO_FOAM, "win32"), FakeShell(codes={"bash": 3}))
    assert "apt-get" in setup.run().message


def test_still_missing_after_install():
    setup, *_ = make(plan_for(FOREIGN, "win32"), FakeShell(),
                     check=lambda: (False, "OpenFOAM 11 found in WSL but ..."))
    r = setup.run()
    assert r.status == "failed" and "reste introuvable" in r.message


def test_linux_uses_pkexec():
    shell = FakeShell()
    has = {"apt-get": "x", "pkexec": "x"}
    setup, *_ = make(plan_for(LINUX_NO_FOAM, "linux", has.get), shell, platform="linux")
    assert setup.run().status == "done"
    assert shell.calls[-1][:3] == ["pkexec", "bash", "-c"]
    setup, *_ = make(plan_for(LINUX_NO_FOAM, "linux", has.get), FakeShell(codes={"bash": 126}),
                     platform="linux")
    assert "refusés" in setup.run().message


def test_cancel_while_waiting_for_ubuntu():
    shell = FakeShell(distro_after_polls=10**6)
    setup, *_ = make(plan_for(NO_DISTRO, "win32"), shell)
    setup.cancel_event.set()
    assert setup.run().status == "cancelled"
    shell = FakeShell(distro_after_polls=10**6)
    setup, *_ = make(plan_for(NO_DISTRO, "win32"), shell)
    t = threading.Timer(0.2, setup.cancel)
    t.start()
    assert setup.run().status == "cancelled"
    t.join()


def test_manual_plan_does_nothing():
    plan = plan_for(LINUX_NO_FOAM, "darwin")
    shell = FakeShell()
    setup, *_ = make(plan, shell, platform="darwin")
    r = setup.run()
    assert r.status == "failed" and shell.calls == []


def test_real_shell_streams_lines_and_exit_code():
    import sys

    lines = []
    script = "print('a'); print(''); print('b'); raise SystemExit(4)"
    code = Shell().run([sys.executable, "-c", script], lines.append)
    assert code == 4 and lines == ["a", "b"]


def test_resume_command_starts_the_gui_with_the_flag():
    cmd = fs.resume_command()
    assert cmd.endswith(fs.RESUME_FLAG) and "airconstruct.ui" in cmd
