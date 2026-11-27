"""What went wrong with a run, in words a user can act on.

Owned by: thread "Simulation". Pure functions (no widgets), tested on their own:

- :func:`openfoam_help`: the "OpenFOAM est introuvable" dialog content for the message
  of ``Backend.available()`` (Windows without WSL, WSL without a distribution, WSL
  without OpenFOAM, Linux without OpenFOAM, a foreign OpenFOAM edition);
- :func:`failure_report`: title, probable cause and offending part of a failed run, from
  ``RunResult.diagnosis`` (``airconstruct.diagnose``) and the solver errors.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

OPENFOAM_VERSION = "v2406"
OPENFOAM_COMMANDS = [
    "curl -s https://dl.openfoam.com/add-debian-repo.sh | sudo bash",
    "sudo apt-get install -y openfoam2406-default",
]
WSL_COMMANDS = ["wsl --install -d Ubuntu"]


@dataclass
class OpenFoamHelp:
    title: str
    subtitle: str  # platform and what was found
    intro: str  # what to do with the commands
    commands: list[str] = field(default_factory=list)
    after: str = ""  # what to do once the commands ran
    detail: str = ""  # the raw message

    def as_text(self) -> str:
        """The whole message, to paste in a forum or a ticket."""
        parts = [self.title, self.subtitle, "", self.intro]
        parts += [f"    {c}" for c in self.commands]
        if self.after:
            parts += ["", self.after]
        if self.detail:
            parts += ["", "Détail : " + self.detail]
        return "\n".join(parts)


def openfoam_help(message: str, platform: str | None = None) -> OpenFoamHelp:
    """Dialog content for why OpenFOAM could not be reached (``message`` is the text of
    ``Backend.available()``, ``platform`` defaults to ``sys.platform``)."""
    platform = platform or sys.platform
    msg = message or ""
    low = msg.lower()
    windows = platform.startswith("win")
    if windows and "wsl2 is not installed" in low:
        return OpenFoamHelp(
            "WSL n’est pas installé",
            "Windows · le calcul tourne dans Linux (WSL2), absent de ce PC.",
            "Ouvrez PowerShell en administrateur et lancez :", WSL_COMMANDS,
            "Redémarrez Windows, ouvrez « Ubuntu » une fois pour créer votre utilisateur "
            "Linux, puis installez OpenFOAM (ce message vous donnera les commandes).", msg)
    if windows and "no linux distribution" in low:
        return OpenFoamHelp(
            "Aucune distribution Linux ne démarre dans WSL",
            "Windows · WSL est installé mais aucune distribution n’a pu être lancée.",
            "Ouvrez PowerShell et lancez :", WSL_COMMANDS,
            "Ouvrez ensuite « Ubuntu » une fois pour terminer sa configuration, puis "
            "revenez ici.", msg)
    edition = re.search(r"OpenFOAM (\S+) found .*chtMultiRegionSimpleFoam is missing", msg)
    foreign = edition or "chtmultiregionsimplefoam is missing" in low
    where = "Windows · WSL est installé mais" if windows else "Linux ·"
    if foreign:
        version = f" {edition.group(1)}" if edition else ""
        return OpenFoamHelp(
            "Cette version d’OpenFOAM ne convient pas",
            f"{where} OpenFOAM{version} est installé sans le solveur "
            "chtMultiRegionSimpleFoam (édition openfoam.org ?).",
            "Air-Construct a besoin de l’édition openfoam.com (ESI) "
            f"{OPENFOAM_VERSION}. " + ("Ouvrez Ubuntu et collez ces deux commandes :"
                                        if windows else "Dans un terminal, lancez :"),
            OPENFOAM_COMMANDS, "Puis cliquez sur « Vérifier à nouveau ».", msg)
    return OpenFoamHelp(
        "OpenFOAM est introuvable",
        f"{where} OpenFOAM n’y est pas." if windows else
        "Linux · OpenFOAM n’est pas installé (ou pas trouvé).",
        f"Le calcul a besoin d’OpenFOAM {OPENFOAM_VERSION}. "
        + ("Ouvrez Ubuntu et collez ces deux commandes, puis revenez ici :" if windows
           else "Dans un terminal, lancez ces deux commandes (Ubuntu / Debian), puis "
                "revenez ici :"),
        OPENFOAM_COMMANDS, "", msg)


# --------------------------------------------------------------------------- failures

MESH_CODES = {"mesh_failed", "bad_mesh", "part_not_meshed", "parts_overlap"}
TITLES = {
    "openfoam_missing": "OpenFOAM n’a pas pu être lancé",
    "mesh_failed": "Le calcul a échoué au maillage",
    "bad_mesh": "Le calcul a échoué au maillage",
    "part_not_meshed": "Le calcul a échoué au maillage",
    "parts_overlap": "Le calcul a échoué au maillage",
    "heat_trapped": "Une pièce chauffe sans pouvoir refroidir",
    "solver_diverged": "Le solveur a divergé",
    "temperature_runaway": "Une température s’est emballée",
    "setup_error": "OpenFOAM a refusé le cas",
    "mpi_error": "Le calcul parallèle n’a pas démarré",
    "disk_full": "Le disque est plein",
    "out_of_memory": "Mémoire insuffisante",
    "not_converged": "Le calcul n’a pas convergé",
    "stopped": "Calcul arrêté",
}
_FAILED_CMD = re.compile(r"^(\S+).*failed \(exit (-?\d+)\)")


@dataclass
class FailureReport:
    title: str
    subtitle: str  # failing command and when, e.g. "splitMeshRegions failed (exit 1) · après 40 s"
    cause: str  # "Cause probable : ..." (empty when unknown)
    hint: str
    part: str | None  # component to show ("Aller à la pièce fautive"), if any
    code: str = ""
    others: list[str] = field(default_factory=list)  # further diagnoses, one line each


def _get(d, name, default=None):
    return d.get(name, default) if isinstance(d, dict) else getattr(d, name, default)


def failure_report(diagnosis: list, errors: list[str], elapsed: str = "",
                   components: set[str] | None = None) -> FailureReport:
    """Summary of a failed (or not converged) run for the error dialog.

    ``diagnosis``: ``diagnose.Diagnosis`` items (or their dicts), most important first;
    ``errors``: ``RunResult.errors`` / ``RunOutcome.errors``; ``components``: the
    component names, to only offer "Aller à la pièce fautive" for a real part."""
    first_err = next((e for e in errors if e.strip()), "")
    head = first_err.splitlines()[0] if first_err else ""
    m = _FAILED_CMD.match(head)
    subtitle = f"{m.group(1)} failed (exit {m.group(2)})" if m else head[:120]
    if elapsed:
        subtitle = f"{subtitle} · après {elapsed}" if subtitle else f"après {elapsed}"
    items = sorted(diagnosis or [], key=lambda d: {"error": 0, "warning": 1}.get(
        _get(d, "severity", ""), 2))
    if not items:
        return FailureReport("Le calcul a échoué", subtitle,
                             "Cause inconnue : le journal ci-dessous donne le détail.",
                             "Copiez les dernières lignes pour les signaler.", None)
    main = items[0]
    code = _get(main, "code", "")
    part = _get(main, "part")
    if part is None:
        part = next((_get(d, "part") for d in items if _get(d, "part")), None)
    if components is not None and part not in components:
        part = None
    title = TITLES.get(code, "Le calcul a échoué")
    if code in ("parts_overlap",) and any(_get(d, "code") in MESH_CODES - {"parts_overlap"}
                                          for d in items):
        title = TITLES["mesh_failed"]
    cause = "Cause probable : " + _get(main, "message", "")
    hint = _get(main, "hint", "") or ""
    if part:
        hint = (hint + " " if hint else "") + "Corrigez-la dans l’étape Modèle puis relancez."
    others = [_get(d, "message", "") for d in items[1:]]
    return FailureReport(title, subtitle, cause, hint, part, code, others)
