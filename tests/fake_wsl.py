"""Stand-in for wsl.exe in the tests: emulates the few commands WslBackend issues.

The Linux filesystem is a directory: Linux path /a/b is $FAKE_WSL_ROOT/a/b.
Environment:
  FAKE_WSL_ROOT   root of the emulated filesystem (required)
  FAKE_WSL_LOG    file where each call's argv is appended as a JSON line
  FAKE_WSL_PROBE  replaces the output of the probe script
  FAKE_WSL_FAIL   "message": every call fails with that message, like wsl.exe without
                  any distro installed
Run commands: "python3"/"python" are replaced by this interpreter.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile

ROOT = os.environ["FAKE_WSL_ROOT"]
HOME = "/home/tester"
DEFAULT_PROBE = (
    "kernel=5.15.153.1-microsoft-standard-WSL2\n"
    f"home={HOME}\n"
    "bashrc=/usr/lib/openfoam/openfoam2406/etc/bashrc\n"
    "version=v2406\n"
    "solver=/usr/lib/openfoam/openfoam2406/platforms/bin/chtMultiRegionSimpleFoam\n"
)


def host(p: str) -> str:
    assert p.startswith("/"), p
    return os.path.join(ROOT, *p.strip("/").split("/"))


def main(args: list[str]) -> int:
    if os.environ.get("FAKE_WSL_LOG"):
        with open(os.environ["FAKE_WSL_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps(args) + "\n")
    if os.environ.get("FAKE_WSL_FAIL"):
        # wsl.exe writes its own errors in UTF-16LE.
        sys.stderr.buffer.write(os.environ["FAKE_WSL_FAIL"].encode("utf-16-le"))
        return 1
    if args[:1] == ["-d"]:
        args = args[2:]
    assert args[0] == "-e", args
    prog, *rest = args[1:]
    stdin = sys.stdin.buffer
    out = sys.stdout.buffer

    if prog == "rm" and rest[0] == "-rf":
        shutil.rmtree(host(rest[1]), ignore_errors=True)
        return 0
    if prog == "mkdir" and rest[0] == "-p":
        os.makedirs(host(rest[1]), exist_ok=True)
        return 0
    if prog == "xargs" and rest[:4] == ["-0", "rm", "-f", "--"]:
        for name in stdin.read().split(b"\0"):
            if name:
                try:
                    os.unlink(host(name.decode()))
                except FileNotFoundError:
                    pass
        return 0
    if prog == "tar" and rest[0] == "-xf":
        with tarfile.open(fileobj=io.BytesIO(stdin.read()), mode="r:") as tar:
            kw = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tar.extractall(host(rest[3]), **kw)
        return 0
    if prog == "tar" and rest[0] == "-cf":
        base = host(rest[3])
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for name in stdin.read().decode().splitlines():
                tar.add(os.path.join(base, name), arcname=name, recursive=False)
        out.write(buf.getvalue())
        return 0
    if prog == "find":
        base = host(rest[0])
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                full = os.path.join(dirpath, name)
                st = os.stat(full)
                rel = os.path.relpath(full, base).replace(os.sep, "/")
                out.write(f"{rel}\t{st.st_mtime:.10f}\t{st.st_size}\n".encode())
        return 0 if os.path.isdir(base) else 1
    if prog in ("bash", "sh") and rest[0] == "-c":
        name, params = rest[2], rest[3:]
        if name == "airconstruct-probe":
            out.write(os.environ.get("FAKE_WSL_PROBE", DEFAULT_PROBE).encode())
            return 0
        if name == "airconstruct-run":
            case_dir, _bashrc, pidfile, *argv = params
            argv = [sys.executable if a in ("python3", "python") else a for a in argv]
            if not os.path.isdir(host(case_dir)):
                return 127
            out.flush()
            child = subprocess.Popen(argv, cwd=host(case_dir))
            os.makedirs(os.path.dirname(host(pidfile)), exist_ok=True)
            with open(host(pidfile), "w") as f:
                f.write(str(child.pid))
            return child.wait()
        if name == "airconstruct-stop":
            try:
                with open(host(params[0])) as f:
                    os.kill(int(f.read()), signal.SIGTERM)
            except (FileNotFoundError, ProcessLookupError):
                pass
            return 0
    sys.stderr.write(f"fake_wsl: unsupported command {args}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
