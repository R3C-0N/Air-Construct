"""Install what the calculation needs, with the user's consent: WSL2, a Linux
distribution and OpenFOAM on Windows; OpenFOAM on Debian/Ubuntu.

Owned by: workstream "wsl".

The Simulation step shows "OpenFOAM est introuvable" with the message of
``Backend.available()``. :func:`plan_for` turns that message into a
:class:`SetupPlan` (what is missing, the steps, whether admin rights or a restart may
be needed). :class:`FoamSetup` then runs the steps, one after the other:

``wsl``       ``wsl.exe --install -d Ubuntu --no-launch`` in an elevated process (one
              UAC prompt). Turns on the Windows features and downloads Ubuntu.
``reboot``    Windows must restart before WSL2 works. Air-Construct registers itself
              in ``HKCU\\...\\RunOnce`` with ``--setup-openfoam`` so the installation
              resumes after the restart (see :func:`schedule_resume`).
``distro``    first start of Ubuntu: ``wsl.exe --install -d Ubuntu`` in its own
              console window, where Ubuntu asks for a Linux user name and password.
              Air-Construct waits until the distribution starts.
``openfoam``  the openfoam.com repository and ``openfoam2406-default``, installed as
              root (``wsl.exe -u root``: no Linux password needed; ``pkexec`` on Linux).
``check``     ``Backend.available()`` again.

Every external action goes through :class:`Shell`, which tests replace.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

DISTRO = "Ubuntu"
OPENFOAM_PACKAGE = "openfoam2406-default"
OPENFOAM_VERSION = "v2406"
RESUME_FLAG = "--setup-openfoam"
RUNONCE_KEY = r"Software\Microsoft\Windows\CurrentVersion\RunOnce"
RUNONCE_VALUE = "AirConstructSetup"
DISTRO_WAIT_S = 20 * 60  # the user creates their Linux account meanwhile
POLL_S = 3.0

# Run as root inside the distribution (or through pkexec on Linux). Non interactive.
INSTALL_SCRIPT = (
    "set -e\n"
    "export DEBIAN_FRONTEND=noninteractive\n"
    "if ! command -v apt-get >/dev/null 2>&1; then\n"
    "  echo 'airconstruct: apt-get not found (Debian or Ubuntu needed)' >&2; exit 3\n"
    "fi\n"
    "echo '== airconstruct: step 1/4 apt-get update'\n"
    "apt-get update\n"
    "echo '== airconstruct: step 2/4 curl'\n"
    "apt-get install -y curl ca-certificates\n"
    "echo '== airconstruct: step 3/4 openfoam.com repository'\n"
    "curl -fsSL https://dl.openfoam.com/add-debian-repo.sh | bash\n"
    "echo '== airconstruct: step 4/4 " + OPENFOAM_PACKAGE + "'\n"
    "apt-get install -y " + OPENFOAM_PACKAGE + "\n"
    "echo '== airconstruct: done'\n"
)

STEP_LABELS = {
    "wsl": "Activer WSL2 et télécharger Ubuntu",
    "reboot": "Redémarrer Windows",
    "distro": "Premier démarrage d’Ubuntu",
    "openfoam": f"Installer OpenFOAM {OPENFOAM_VERSION}",
    "check": "Vérifier l’installation",
}

# progress markers printed by INSTALL_SCRIPT -> fraction of the "openfoam" step
_MARKERS = {"step 1/4": 0.05, "step 2/4": 0.15, "step 3/4": 0.25, "step 4/4": 0.35,
            "done": 1.0}


class SetupCancelled(Exception):
    """The user cancelled (the dialog's "Annuler" or the UAC prompt)."""


class SetupFailed(RuntimeError):
    """A step failed; the message says which and why, in French."""


# ---------------------------------------------------------------- plan


@dataclass
class SetupPlan:
    kind: str  # "wsl" | "distro" | "openfoam" | "manual"
    steps: list[str] = field(default_factory=list)
    needs_admin: bool = False  # a UAC (or pkexec) prompt will appear
    may_reboot: bool = False
    download: str = ""  # what will be downloaded, for the consent text
    reason: str = ""  # why it cannot be automatic (kind "manual")
    windows: bool = False

    @property
    def automatic(self) -> bool:
        return self.kind != "manual"

    def consent_lines(self) -> list[str]:
        """What will happen, one line each, shown before the user agrees."""
        return self.consent_steps() + self.consent_notes()

    def consent_steps(self) -> list[str]:
        return [STEP_LABELS[s] for s in self.steps if s != "check"]

    def consent_notes(self) -> list[str]:
        out: list[str] = []
        if self.needs_admin:
            out.append("Windows demandera l’autorisation administrateur (une fois)."
                       if self.windows else
                       "Votre mot de passe administrateur sera demandé (une fois).")
        if self.may_reboot:
            out.append("Un redémarrage de Windows sera probablement nécessaire : "
                       "Air-Construct reprendra l’installation tout seul ensuite.")
        if self.download:
            out.append(f"Téléchargement : {self.download}.")
        return out


def plan_for(message: str, platform: str | None = None,
             which: Callable[[str], str | None] = shutil.which) -> SetupPlan:
    """What to install, from the ``Backend.available()`` message."""
    platform = platform or sys.platform
    low = (message or "").lower()
    foam_dl = "OpenFOAM (≈ 1 Go)"
    if platform.startswith("win"):
        if "wsl2 is not installed" in low:
            p = SetupPlan("wsl", ["wsl", "reboot", "distro", "openfoam", "check"],
                          needs_admin=True, may_reboot=True, windows=True,
                          download="Ubuntu (≈ 500 Mo) puis " + foam_dl)
        elif "no linux distribution" in low:
            p = SetupPlan("distro", ["distro", "openfoam", "check"], windows=True,
                          download="Ubuntu (≈ 500 Mo) puis " + foam_dl)
        else:  # the distro runs, OpenFOAM is missing or the wrong edition
            p = SetupPlan("openfoam", ["openfoam", "check"], windows=True, download=foam_dl)
        return p
    if not platform.startswith("linux"):
        return SetupPlan("manual", reason="Installation automatique possible seulement sous "
                                          "Windows et Linux (Debian, Ubuntu).")
    if which("apt-get") is None:
        return SetupPlan("manual", reason="Installation automatique possible seulement sous "
                                          "Debian ou Ubuntu (apt-get introuvable).")
    if which("pkexec") is None:
        return SetupPlan("manual", reason="pkexec introuvable : Air-Construct ne peut pas "
                                          "demander les droits administrateur.")
    return SetupPlan("openfoam", ["openfoam", "check"], needs_admin=True, download=foam_dl)


# ---------------------------------------------------------------- shell


class Shell:
    """The few system actions the setup needs. Tests subclass it."""

    def env(self) -> dict[str, str]:
        return {**os.environ, "WSL_UTF8": "1"}

    def run(self, argv: list[str], on_line: Callable[[str], None] | None = None,
            cancel: threading.Event | None = None) -> int:
        """Run ``argv``, stream its merged output lines, return its exit code."""
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=self.env(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        watcher = None
        if cancel is not None:
            def watch() -> None:
                while proc.poll() is None:
                    if cancel.wait(0.5):
                        proc.terminate()
                        return
            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
        assert proc.stdout is not None
        for raw in proc.stdout:
            if on_line is not None:
                line = raw.replace(b"\x00", b"").decode("utf-8", errors="replace")
                line = line.rstrip("\r\n")
                if line.strip():
                    on_line(line)
        return proc.wait()

    def run_elevated(self, exe: str, args: list[str]) -> int:
        """Windows: run with administrator rights (UAC prompt), wait, return the exit code.
        Raises SetupCancelled when the user refuses the prompt."""
        return _shell_execute_runas(exe, args)

    def open_console(self, argv: list[str]) -> None:
        """Windows: start ``argv`` in a new, visible console window."""
        subprocess.Popen(argv, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))

    def sleep(self, seconds: float, cancel: threading.Event) -> None:
        if cancel.wait(seconds):
            raise SetupCancelled()


def _quote_arg(a: str) -> str:
    """One argument of a Windows command line (CommandLineToArgvW rules)."""
    return subprocess.list2cmdline([a])


def _shell_execute_runas(exe: str, args: list[str]) -> int:  # pragma: no cover - Windows
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x40
    SW_SHOWNORMAL = 1
    ERROR_CANCELLED = 1223
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = " ".join(_quote_arg(a) for a in args)
    info.nShow = SW_SHOWNORMAL
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            raise SetupCancelled()
        raise SetupFailed(f"Impossible d’obtenir les droits administrateur (erreur {err}).")
    try:
        kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


# ---------------------------------------------------------------- resume after restart


def resume_command() -> str:
    """Command line that starts Air-Construct and resumes the setup."""
    if getattr(sys, "frozen", False):
        return f"{_quote_arg(sys.executable)} {RESUME_FLAG}"
    exe = sys.executable
    w = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(w):
        exe = w
    return f"{_quote_arg(exe)} -m airconstruct.ui {RESUME_FLAG}"


def schedule_resume(command: str | None = None) -> bool:  # pragma: no cover - Windows
    """Start Air-Construct once at the next Windows logon. False if not possible."""
    if not sys.platform.startswith("win"):
        return False
    import winreg

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUNONCE_KEY) as key:
            winreg.SetValueEx(key, RUNONCE_VALUE, 0, winreg.REG_SZ, command or resume_command())
    except OSError:
        return False
    return True


def cancel_resume() -> None:  # pragma: no cover - Windows
    if not sys.platform.startswith("win"):
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUNONCE_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUNONCE_VALUE)
    except OSError:
        pass


def restart_windows() -> None:  # pragma: no cover - Windows
    """Restart now (the user clicked "Redémarrer maintenant")."""
    root = os.environ.get("SystemRoot", r"C:\Windows")
    subprocess.Popen([os.path.join(root, "System32", "shutdown.exe"), "/r", "/t", "5",
                      "/c", "Air-Construct : redémarrage pour terminer l’installation de WSL2"])


# ---------------------------------------------------------------- runner


@dataclass
class SetupResult:
    status: str  # "done" | "reboot" | "failed" | "cancelled"
    message: str = ""


StepCallback = Callable[[str, str], None]  # (step, "running" | "done" | "skipped")


class FoamSetup:
    """Run a :class:`SetupPlan`. ``check()`` is ``Backend.available()``; ``distro`` the
    WSL distribution to install into (None: the default one, or Ubuntu when created)."""

    def __init__(self, plan: SetupPlan, check: Callable[[], tuple[bool, str]],
                 shell: Shell | None = None, distro: str | None = None,
                 wsl_exe: str | None = None, platform: str | None = None,
                 on_step: StepCallback | None = None,
                 on_line: Callable[[str], None] | None = None,
                 on_progress: Callable[[float], None] | None = None,
                 schedule: Callable[[], bool] = schedule_resume,
                 unschedule: Callable[[], None] = cancel_resume) -> None:
        self.plan = plan
        self.check = check
        self.shell = shell or Shell()
        self.distro = distro or os.environ.get("AIRCONSTRUCT_WSL_DISTRO") or None
        self._wsl_exe = wsl_exe
        self.platform = platform or sys.platform
        self.on_step = on_step or (lambda s, st: None)
        self.on_line = on_line or (lambda line: None)
        self.on_progress = on_progress or (lambda f: None)
        self.schedule = schedule
        self.unschedule = unschedule
        self.cancel_event = threading.Event()

    # -- helpers

    def cancel(self) -> None:
        self.cancel_event.set()

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise SetupCancelled()

    @property
    def wsl_exe(self) -> str:
        if self._wsl_exe is None:
            from .wsl import find_wsl_exe

            self._wsl_exe = find_wsl_exe() or os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"), "System32", "wsl.exe")
        return self._wsl_exe

    def _wsl(self, *args: str, distro: str | None = None) -> list[str]:
        d = distro or self.distro
        return [self.wsl_exe, *(["-d", d] if d else []), *args]

    def distro_starts(self, distro: str | None = None) -> bool:
        return self.shell.run(self._wsl("-u", "root", "-e", "true", distro=distro)) == 0

    # -- steps

    def step_wsl(self) -> bool:
        """Returns True when Windows must restart before going on."""
        self.on_line(f"> wsl --install -d {DISTRO} --no-launch (administrateur)")
        code = self.shell.run_elevated(self.wsl_exe, ["--install", "-d", DISTRO, "--no-launch"])
        if code not in (0, 3010):  # 3010: ERROR_SUCCESS_REBOOT_REQUIRED
            raise SetupFailed(f"L’installation de WSL a échoué (code {code}). Lancez "
                              f"« wsl --install -d {DISTRO} » dans un PowerShell "
                              "administrateur pour voir le détail.")
        self.distro = self.distro or DISTRO
        # Without a restart the WSL2 platform is not running yet: wsl --status fails.
        status = self.shell.run([self.wsl_exe, "--status"], self.on_line)
        return code == 3010 or status != 0

    def step_distro(self) -> None:
        distro = self.distro or DISTRO
        if self.distro_starts(distro):
            self.distro = distro
            return
        self.on_line(f"> wsl --install -d {distro} (nouvelle fenêtre)")
        self.on_line("Ubuntu s’ouvre dans une fenêtre noire : choisissez un nom "
                     "d’utilisateur et un mot de passe Linux, puis revenez ici.")
        self.shell.open_console([self.wsl_exe, "--install", "-d", distro])
        waited = 0.0
        while waited < DISTRO_WAIT_S:
            self.shell.sleep(POLL_S, self.cancel_event)
            waited += POLL_S
            if self.distro_starts(distro):
                self.distro = distro
                self.on_line(f"{distro} démarre.")
                return
        raise SetupFailed(f"{distro} n’a pas démarré après {DISTRO_WAIT_S // 60} minutes. "
                          "Terminez sa configuration dans la fenêtre Ubuntu, puis relancez "
                          "l’installation.")

    def step_openfoam(self) -> None:
        windows = self.platform.startswith("win")
        if windows:
            argv = self._wsl("-u", "root", "-e", "bash", "-c", INSTALL_SCRIPT)
        else:
            argv = ["pkexec", "bash", "-c", INSTALL_SCRIPT]
        tail: list[str] = []

        def line(text: str) -> None:
            tail.append(text)
            del tail[:-5]
            for marker, frac in _MARKERS.items():
                if f"airconstruct: {marker}" in text:
                    self.on_progress(frac)
            self.on_line(text)

        code = self.shell.run(argv, line, self.cancel_event)
        self._check_cancel()
        if code == 3:
            raise SetupFailed("Cette distribution Linux n’utilise pas apt-get : installez "
                              f"OpenFOAM {OPENFOAM_VERSION} (openfoam.com) à la main.")
        if code in (126, 127) and not windows:
            raise SetupFailed("Les droits administrateur ont été refusés.")
        if code != 0:
            last = tail[-1] if tail else ""
            raise SetupFailed(f"L’installation d’OpenFOAM a échoué (code {code})."
                              + (f" Dernière ligne : {last}" if last else ""))

    def step_check(self) -> None:
        ok, msg = self.check()
        if not ok:
            raise SetupFailed("OpenFOAM reste introuvable après l’installation : "
                              + (msg.splitlines()[0] if msg else ""))
        self.on_line(f"✓ {msg}")

    # -- all

    def run(self) -> SetupResult:
        if not self.plan.automatic:
            return SetupResult("failed", self.plan.reason)
        steps = list(self.plan.steps)
        try:
            for i, step in enumerate(steps):
                self._check_cancel()
                if step == "reboot":
                    continue  # handled by step_wsl
                self.on_step(step, "running")
                if step == "wsl":
                    must_restart = self.step_wsl()
                    self.on_step(step, "done")
                    if must_restart:
                        self.schedule()
                        self.on_step("reboot", "running")
                        return SetupResult("reboot", "Redémarrez Windows pour terminer : "
                                           "Air-Construct reprendra l’installation ensuite.")
                    if i + 1 < len(steps) and steps[i + 1] == "reboot":
                        self.on_step("reboot", "skipped")
                    continue
                getattr(self, f"step_{step}")()
                self.on_step(step, "done")
        except SetupCancelled:
            return SetupResult("cancelled", "Installation annulée.")
        except SetupFailed as e:
            return SetupResult("failed", str(e))
        except OSError as e:
            return SetupResult("failed", f"Commande impossible à lancer : {e}")
        self.unschedule()
        return SetupResult("done", "OpenFOAM est prêt : vous pouvez lancer le calcul.")

