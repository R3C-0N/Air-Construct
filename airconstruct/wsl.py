"""Run OpenFOAM on Windows through WSL2.

Owned by: workstream "wsl".

``WslBackend`` implements ``runner.Backend``. The runner keeps writing the case on the
Windows side (e.g. ``C:\\Users\\me\\runs\\box``) and calls ``run(argv, cwd)`` as with any
backend. Behind the scenes:

1. The case directory is mirrored into the WSL Linux filesystem
   (``~/.cache/airconstruct/cases/<name>-<hash>``), because OpenFOAM doing its I/O on
   ``/mnt/c`` is several times slower. Only files changed since the last sync are sent
   (tar streamed over ``wsl.exe`` stdin).
2. The command runs there with ``wsl.exe -e bash -c ...``, after sourcing the OpenFOAM
   bashrc found in the distro. Output lines are streamed to ``on_line``.
3. Lightweight results are copied back: everything new or changed except ``processor*``
   directories and intermediate time directories (only the first and latest times
   come back). ``pull(cwd, everything=True)`` fetches all of it.

A case that already lives in the WSL filesystem (``\\\\wsl.localhost\\Ubuntu\\...``)
is run in place, without copying.

Every WSL-side operation is a plain command (``tar``, ``find``, ``rm``, ``mkdir``,
``xargs``, ``bash -c <fixed script> <args>``) so ``tests/fake_wsl.py`` can emulate
``wsl.exe`` in the tests.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from .runner import Backend, LogCallback

WSL_UNC_HOSTS = ("wsl$", "wsl.localhost")

INSTALL_WSL_HELP = (
    "WSL2 is not installed. Open PowerShell as administrator, run\n"
    "    wsl --install -d Ubuntu\n"
    "then restart Windows and open 'Ubuntu' once to create your Linux user."
)
INSTALL_OPENFOAM_HELP = (
    "OpenFOAM was not found in WSL. Open the Ubuntu terminal and run\n"
    "    curl -s https://dl.openfoam.com/add-debian-repo.sh | sudo bash\n"
    "    sudo apt-get install -y openfoam2406-default\n"
    "(or set AIRCONSTRUCT_FOAM_BASHRC to the etc/bashrc of your OpenFOAM install)."
)

# Prints key=value lines describing the distro and its OpenFOAM install.
# $1: bashrc to use instead of searching ("" to search).
PROBE_SCRIPT = (
    "# airconstruct-probe\n"
    'echo "kernel=$(uname -r)"; echo "home=$HOME"; b="$1"; '
    'if [ -z "$b" ] && ! command -v chtMultiRegionSimpleFoam >/dev/null 2>&1; then '
    "b=$(ls -d /usr/lib/openfoam/openfoam*/etc/bashrc /opt/openfoam*/etc/bashrc "
    '"$HOME"/OpenFOAM/OpenFOAM-v*/etc/bashrc 2>/dev/null | sort -V | tail -n 1); fi; '
    'if [ -n "$b" ]; then . "$b" >/dev/null 2>&1; fi; '
    'echo "bashrc=$b"; echo "version=${WM_PROJECT_VERSION:-}"; '
    'echo "solver=$(command -v chtMultiRegionSimpleFoam)"'
)

# $1 case dir, $2 OpenFOAM bashrc ("" if already on PATH), $3 pid file, $4.. argv.
RUN_SCRIPT = (
    'd="$1"; b="$2"; p="$3"; shift 3; '
    'if [ -n "$b" ]; then . "$b" >/dev/null 2>&1; fi; '
    'cd "$d" || exit 127; echo $$ > "$p"; exec "$@"'
)

# $1 pid file.
STOP_SCRIPT = '[ -f "$1" ] && kill -TERM "$(cat "$1")" 2>/dev/null; rm -f "$1"'

# $1 pid file. SIGUSR1 = write the current state and stop (stopAtWriteNowSignal=10).
WRITE_NOW_SCRIPT = '[ -f "$1" ] && kill -USR1 "$(cat "$1")" 2>/dev/null'


# ---------------------------------------------------------------- path translation


def windows_to_wsl(path: str | os.PathLike, mount_root: str = "/mnt/") -> str:
    """``C:\\a\\b`` -> ``/mnt/c/a/b``; ``\\\\wsl.localhost\\Ubuntu\\home\\u`` -> ``/home/u``."""
    p = PureWindowsPath(path)
    rest = [part for part in p.parts[1:]]
    if p.drive.startswith("\\\\"):
        host, _, _share = p.drive.lstrip("\\").partition("\\")
        if host.lower() not in WSL_UNC_HOSTS:
            raise ValueError(f"network share not reachable from WSL: {path}")
        return "/" + "/".join(rest)
    if len(p.drive) == 2 and p.drive[1] == ":" and p.root:
        root = mount_root.rstrip("/") + "/" + p.drive[0].lower()
        return "/".join([root, *rest])
    raise ValueError(f"not an absolute Windows path: {path}")


def wsl_to_windows(path: str, distro: str, mount_root: str = "/mnt/") -> str:
    """``/mnt/c/a`` -> ``C:\\a``; ``/home/u`` -> ``\\\\wsl.localhost\\<distro>\\home\\u``."""
    p = PurePosixPath(path)
    if not p.is_absolute():
        raise ValueError(f"not an absolute WSL path: {path}")
    mount = PurePosixPath(mount_root)
    try:
        rel = p.relative_to(mount)
    except ValueError:
        rel = None
    if rel is not None and rel.parts and re.fullmatch(r"[a-zA-Z]", rel.parts[0]):
        return str(PureWindowsPath(rel.parts[0].upper() + ":\\", *rel.parts[1:]))
    return "\\\\wsl.localhost\\" + distro + "".join("\\" + part for part in p.parts[1:])


def wsl_unc_distro(path: str | os.PathLike) -> str | None:
    """Distro name if ``path`` is inside a WSL filesystem share, else None."""
    drive = PureWindowsPath(path).drive
    if not drive.startswith("\\\\"):
        return None
    host, _, share = drive.lstrip("\\").partition("\\")
    return share if host.lower() in WSL_UNC_HOSTS and share else None


def find_wsl_exe() -> str | None:
    found = shutil.which("wsl.exe") or shutil.which("wsl")
    if found:
        return found
    root = os.environ.get("SystemRoot", r"C:\Windows")
    for sub in ("System32", "Sysnative"):  # Sysnative: 32-bit Python on 64-bit Windows
        candidate = os.path.join(root, sub, "wsl.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def _decode(data: bytes) -> str:
    """wsl.exe's own messages are UTF-16LE unless WSL_UTF8=1 is honoured."""
    if len(data) >= 2 and data.count(b"\x00") > len(data) // 4:
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- sync bookkeeping

Manifest = dict[str, tuple[float, int]]  # relative posix path -> (mtime s, size)


@dataclass
class _Mirror:
    wsl_dir: str
    win_files: Manifest = field(default_factory=dict)  # Windows side after last sync
    wsl_files: Manifest = field(default_factory=dict)  # WSL side after last sync
    pushed: set[str] = field(default_factory=set)
    pulled: set[str] = field(default_factory=set)


def _scan_windows(root: Path) -> Manifest:
    out: Manifest = {}
    if not root.is_dir():
        return out
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = Path(dirpath) / name
            st = full.stat()
            out[full.relative_to(root).as_posix()] = (st.st_mtime, st.st_size)
    return out


def _is_time(name: str) -> bool:
    try:
        float(name)
    except ValueError:
        return False
    return True


def lightweight(paths: list[str], all_paths: list[str] | None = None) -> list[str]:
    """Drop ``processor*`` data and every time directory but the first and the latest
    (among ``all_paths``, default ``paths``)."""
    pool = paths if all_paths is None else all_paths
    times = sorted({p.split("/", 1)[0] for p in pool if "/" in p and _is_time(p.split("/")[0])},
                   key=float)
    keep_times = {times[0], times[-1]} if times else set()
    out = []
    for p in paths:
        top = p.split("/", 1)[0]
        if "/" in p and top.startswith("processor"):
            continue
        if "/" in p and _is_time(top) and top not in keep_times:
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------- backend


@dataclass
class ProbeInfo:
    kernel: str = ""
    home: str = ""
    bashrc: str = ""
    version: str = ""
    solver: str = ""

    @property
    def wsl2(self) -> bool:
        k = self.kernel.lower()
        return "wsl2" in k or "microsoft" not in k  # custom kernels: assume WSL2


class WslBackend(Backend):
    """Run OpenFOAM inside a WSL2 distro from Windows. See module docstring."""

    def __init__(
        self,
        distro: str | None = None,
        foam_bashrc: str | None = None,
        wsl_command: list[str] | None = None,
        mirror_root: str | None = None,
        mount_root: str = "/mnt/",
    ):
        self.distro = distro or os.environ.get("AIRCONSTRUCT_WSL_DISTRO") or None
        self.foam_bashrc = foam_bashrc or os.environ.get("AIRCONSTRUCT_FOAM_BASHRC") or None
        self._wsl_command = wsl_command
        self._mirror_root = mirror_root  # default: $HOME/.cache/airconstruct/cases
        self.mount_root = mount_root
        self._probe: ProbeInfo | None = None
        self._mirrors: dict[str, _Mirror] = {}
        self._lock = threading.Lock()
        self._current_pidfile: tuple[str | None, str] | None = None  # (distro, path)

    # -- low level

    def _base(self, distro: str | None = None) -> list[str]:
        cmd = self._wsl_command
        if cmd is None:
            exe = find_wsl_exe()
            if exe is None:
                raise RuntimeError(INSTALL_WSL_HELP)
            cmd = [exe]
        d = distro or self.distro
        return [*cmd, *(["-d", d] if d else []), "-e"]

    @staticmethod
    def _env() -> dict[str, str]:
        return {**os.environ, "WSL_UTF8": "1"}

    @staticmethod
    def _flags() -> int:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console popup from the GUI

    def _call(self, args: list[str], data: bytes | None = None, distro: str | None = None,
              check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            self._base(distro) + args, input=data, capture_output=True, check=False,
            env=self._env(), creationflags=self._flags(),
        )
        if check and proc.returncode != 0:
            msg = _decode(proc.stderr).strip() or _decode(proc.stdout).strip()
            raise RuntimeError(f"wsl {args[0]} failed ({proc.returncode}): {msg}")
        return proc

    # -- detection

    def probe(self, refresh: bool = False) -> ProbeInfo:
        if self._probe is not None and not refresh:
            return self._probe
        proc = self._call(["bash", "-c", PROBE_SCRIPT, "airconstruct-probe",
                           self.foam_bashrc or ""], check=False)
        if proc.returncode != 0:
            raise RuntimeError(_decode(proc.stderr).strip() or _decode(proc.stdout).strip()
                               or f"wsl.exe exited with {proc.returncode}")
        info = ProbeInfo()
        for line in _decode(proc.stdout).splitlines():
            key, sep, value = line.partition("=")
            if sep and hasattr(info, key):
                setattr(info, key, value.strip())
        self._probe = info
        return info

    def available(self) -> tuple[bool, str]:
        if self._wsl_command is None and find_wsl_exe() is None:
            return False, INSTALL_WSL_HELP
        try:
            info = self.probe(refresh=True)
        except (RuntimeError, OSError) as e:
            return False, (f"WSL is installed but no Linux distribution could be started "
                           f"({e}).\nRun 'wsl --install -d Ubuntu' in PowerShell, then open "
                           "Ubuntu once to finish its setup.")
        if not info.solver:
            if info.version:
                return False, (f"OpenFOAM {info.version} found in WSL but "
                               "chtMultiRegionSimpleFoam is missing. Air-Construct needs the "
                               "openfoam.com (ESI) edition.\n" + INSTALL_OPENFOAM_HELP)
            return False, INSTALL_OPENFOAM_HELP
        where = f" ({self.distro})" if self.distro else ""
        msg = f"OpenFOAM {info.version or '(unknown version)'} in WSL{where}"
        if not info.wsl2:
            msg += (". Warning: this distro runs under WSL1, which is much slower; convert it "
                    "with 'wsl --set-version <distro> 2'")
        return True, msg

    # -- mirroring

    def wsl_dir(self, cwd: Path) -> str:
        """Linux path where the case in ``cwd`` is run."""
        if wsl_unc_distro(cwd):
            return windows_to_wsl(cwd, self.mount_root)
        root = self._mirror_root or (self.probe().home.rstrip("/") + "/.cache/airconstruct/cases")
        key = str(Path(cwd).absolute()).lower()
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(cwd).name) or "case"
        return f"{root}/{name}-{hashlib.sha1(key.encode()).hexdigest()[:10]}"

    def _wsl_manifest(self, wsl_dir: str) -> Manifest:
        proc = self._call(["find", wsl_dir, "-type", "f", "-printf", "%P\\t%T@\\t%s\\n"],
                          check=False)
        out: Manifest = {}
        for line in _decode(proc.stdout).splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                out[parts[0]] = (float(parts[1]), int(parts[2]))
        return out

    def _wsl_delete(self, wsl_dir: str, rel: list[str]) -> None:
        if rel:
            data = b"".join(f"{wsl_dir}/{p}".encode() + b"\0" for p in rel)
            self._call(["xargs", "-0", "rm", "-f", "--"], data=data)

    def push(self, cwd: Path) -> list[str]:
        """Send files changed on the Windows side to the WSL mirror. Returns them."""
        cwd = Path(cwd)
        key = str(cwd.absolute())
        local = _scan_windows(cwd)
        mirror = self._mirrors.get(key)
        fresh = mirror is None or (
            mirror.win_files and not any(local.get(p) == s for p, s in mirror.win_files.items())
        )  # nothing left from the last sync: the case was rewritten from scratch
        if fresh:
            mirror = _Mirror(wsl_dir=self.wsl_dir(cwd))
            self._call(["rm", "-rf", mirror.wsl_dir])
            self._call(["mkdir", "-p", mirror.wsl_dir])
            self._mirrors[key] = mirror
        changed = sorted(p for p, s in local.items() if mirror.win_files.get(p) != s)
        removed = sorted(p for p in mirror.win_files if p not in local)
        self._wsl_delete(mirror.wsl_dir, removed)
        if changed:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for rel in changed:
                    tar.add(cwd / rel, arcname=rel, recursive=False)
            self._call(["tar", "-xf", "-", "-C", mirror.wsl_dir], data=buf.getvalue())
        mirror.win_files = local
        mirror.pushed |= set(changed)
        mirror.wsl_files = self._wsl_manifest(mirror.wsl_dir)
        return changed

    def pull(self, cwd: Path, everything: bool = False) -> list[str]:
        """Copy files created or changed in the WSL mirror back to ``cwd``. Returns them."""
        cwd = Path(cwd)
        mirror = self._mirrors.get(str(cwd.absolute()))
        if mirror is None:
            return []
        remote = self._wsl_manifest(mirror.wsl_dir)
        wanted = sorted(p for p, s in remote.items()
                        if everything or mirror.wsl_files.get(p) != s)
        if not everything:
            wanted = lightweight(wanted, list(remote))
        # Results we brought back earlier and that are gone in WSL (e.g. time dirs removed).
        for rel in mirror.pulled - set(remote):
            (cwd / rel).unlink(missing_ok=True)
            mirror.pulled.discard(rel)
        if wanted:
            proc = self._call(["tar", "-cf", "-", "-C", mirror.wsl_dir, "-T", "-"],
                              data="".join(p + "\n" for p in wanted).encode())
            with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tar:
                _safe_extract(tar, cwd)
        mirror.wsl_files = remote
        mirror.pulled |= set(wanted)
        mirror.win_files = _scan_windows(cwd)
        return wanted

    def reset(self, cwd: Path) -> None:
        """Forget and delete the WSL mirror of ``cwd`` (the next run copies everything)."""
        mirror = self._mirrors.pop(str(Path(cwd).absolute()), None)
        if mirror is not None:
            self._call(["rm", "-rf", mirror.wsl_dir])

    # -- execution

    def run(self, argv: list[str], cwd: Path, on_line: LogCallback | None = None) -> int:
        cwd = Path(cwd)
        in_place = wsl_unc_distro(cwd)
        distro = in_place or self.distro
        if in_place:
            target = windows_to_wsl(cwd, self.mount_root)
        else:
            self.push(cwd)
            target = self._mirrors[str(cwd.absolute())].wsl_dir
        info = self.probe()
        pidfile = f"/tmp/airconstruct-{hashlib.sha1(target.encode()).hexdigest()[:10]}.pid"
        bashrc = self.foam_bashrc or info.bashrc
        proc = subprocess.Popen(
            self._base(distro) + ["bash", "-c", RUN_SCRIPT, "airconstruct-run",
                                  target, bashrc, pidfile, *argv],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=self._env(), creationflags=self._flags(),
        )
        with self._lock:
            self._current_pidfile = (distro, pidfile)
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                if on_line is not None:
                    on_line(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            code = proc.wait()
        finally:
            with self._lock:
                self._current_pidfile = None
        if not in_place:
            self.pull(cwd)
        return code

    def stop(self) -> None:
        """Ask the command started by ``run`` (from another thread) to terminate."""
        with self._lock:
            current = self._current_pidfile
        if current is not None:
            distro, pidfile = current
            self._call(["sh", "-c", STOP_SCRIPT, "airconstruct-stop", pidfile],
                       distro=distro, check=False)


    def write_now(self) -> None:
        """Ask the running solver to write its current state and exit cleanly."""
        with self._lock:
            current = self._current_pidfile
        if current is not None:
            distro, pidfile = current
            self._call(["sh", "-c", WRITE_NOW_SCRIPT, "airconstruct-write-now", pidfile],
                       distro=distro, check=False)


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    dest = Path(dest).resolve()
    members = []
    for m in tar.getmembers():
        target = (dest / m.name).resolve()
        if not (m.isfile() or m.isdir()) or dest not in (target, *target.parents):
            continue  # skip links, devices and anything escaping dest
        members.append(m)
    if hasattr(tarfile, "data_filter"):
        tar.extractall(dest, members=members, filter="data")
    else:  # Python < 3.10.12 / 3.11.4
        tar.extractall(dest, members=members)
