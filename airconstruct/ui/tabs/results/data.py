"""Plain data helpers of the Résultats tab (no Qt widgets): French number formatting,
per-part rows with their temperature limit, what changed in the model since a run,
comparison of two runs, CSV.

Owned by: thread "Résultats" (see docs/ui-architecture.md).
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from pathlib import Path

UI_KEY = "ui"
LIMITS_KEY = "limits"  # data["ui"]["limits"] = {part name: max temperature in °C}
NA = "—"


# --------------------------------------------------------------------------- format


def finite(v) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(v)


def fr(v, digits: int = 1, sign: bool = False) -> str:
    """French decimal comma: fr(-8.5) -> '−8,5', fr(3.2, sign=True) -> '+3,2'."""
    if not finite(v):
        return NA
    s = f"{v:+.{digits}f}" if sign else f"{v:.{digits}f}"
    if s.lstrip("+-").strip("0,.") == "":  # -0.0
        s = s.replace("-", "+" if sign else "")
    return s.replace(".", ",").replace("-", "−")


def parse_fr(text: str) -> float | None:
    """'85', '85,5', ' 85.5 °C' -> float; '' or '—' -> None."""
    t = text.replace("°C", "").replace("−", "-").replace(",", ".").strip()
    if t in ("", NA, "-"):
        return None
    return float(t)


# --------------------------------------------------------------------------- limits


def part_limit(data: dict, name: str) -> float | None:
    """Maximum allowed temperature of a part (°C), or None.

    A ``max_temperature`` on the component (case model, when the core supports it)
    wins over the one set in this tab (``data["ui"]["limits"]``)."""
    for c in data.get("components") or []:
        if isinstance(c, dict) and c.get("name") == name and finite(c.get("max_temperature")):
            return float(c["max_temperature"])
    v = ((data.get(UI_KEY) or {}).get(LIMITS_KEY) or {}).get(name)
    return float(v) if finite(v) else None


def set_part_limit(data: dict, name: str, value: float | None) -> None:
    """Edit function for ``project.edit``: sets (or removes, value None) a limit."""
    limits = data.setdefault(UI_KEY, {}).setdefault(LIMITS_KEY, {})
    if value is None:
        limits.pop(name, None)
    else:
        limits[name] = float(value)


@dataclass
class PartRow:
    name: str
    power_w: float
    t_min: float
    t_mean: float
    t_max: float
    limit: float | None = None

    @property
    def status(self) -> str:
        """'ok', 'ko' or '' (no limit or no temperature)."""
        if self.limit is None or not finite(self.t_max):
            return ""
        return "ok" if self.t_max <= self.limit else "ko"

    @property
    def margin(self) -> float:
        return self.limit - self.t_max if self.limit is not None and finite(self.t_max) else math.nan


def _hottest_first(rows: list[PartRow]) -> list[PartRow]:
    return sorted(rows, key=lambda r: -r.t_max if finite(r.t_max) else math.inf)


def part_rows(summary, data: dict) -> list[PartRow]:
    """One row per part of a finished run, hottest first."""
    return _hottest_first([
        PartRow(c.name, c.power_w, getattr(c, "t_min_c", math.nan), c.t_mean_c, c.t_max_c,
                part_limit(data, c.name))
        for c in summary.components])


def estimate_rows(case, temps: dict[str, float], data: dict) -> list[PartRow]:
    """Rows from the quick estimate (only a max temperature per part)."""
    power = {c.name: c.power for c in case.components}
    return _hottest_first([PartRow(n, power.get(n, 0.0), math.nan, math.nan, t,
                                   part_limit(data, n)) for n, t in temps.items()])


def over_limit(rows: list[PartRow]) -> list[PartRow]:
    return [r for r in rows if r.status == "ko"]


# --------------------------------------------------------------------------- CSV


CSV_HEADER = ["Pièce", "Puissance (W)", "T min (°C)", "T moy. (°C)", "T max (°C)",
              "Limite (°C)", "Marge (°C)", "État"]


def rows_csv(rows: list[PartRow]) -> str:
    """Per-part table as CSV for a French spreadsheet: ';' separator, decimal comma."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(CSV_HEADER)
    for r in rows:
        w.writerow([r.name, fr(r.power_w, 2), fr(r.t_min, 2), fr(r.t_mean, 2), fr(r.t_max, 2),
                    fr(r.limit, 1) if r.limit is not None else "", fr(r.margin, 2)
                    if finite(r.margin) else "", {"ok": "OK", "ko": "KO"}.get(r.status, "")])
    return buf.getvalue().replace(NA, "")


def write_csv(rows: list[PartRow], path: Path) -> Path:
    path = Path(path)
    path.write_text(rows_csv(rows), encoding="utf-8-sig")  # BOM: Excel reads UTF-8
    return path


# --------------------------------------------------------------------------- changes


def flatten(d, prefix: str = "") -> dict:
    """Nested case dict -> {"components.cpu.box.origin.x": value}; lists of named
    items are keyed by name."""
    out: dict = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(flatten(v, f"{prefix}{k}."))
    elif isinstance(d, list) and d and all(isinstance(x, dict) and "name" in x for x in d):
        for x in d:
            out.update(flatten({k: v for k, v in x.items() if k != "name"},
                               f"{prefix}{x['name']}."))
    else:
        out[prefix[:-1]] = d
    return out


_GROUPS = {"components": "pièce", "openings": "ouverture", "probes": "sonde",
           "refinements": "raffinement"}
_WHAT = {  # (group, first key under the item) -> words
    "box.origin": "déplacé", "box.size": "redimensionné", "power": "puissance modifiée",
    "material": "matériau modifié", "max_temperature": "limite modifiée",
    "flow_rate": "débit modifié", "velocity": "vitesse modifiée", "center": "déplacée",
    "size": "redimensionnée", "face": "changée de face", "temperature": "température modifiée",
    "kind": "type modifié", "fan": "ventilateur modifié", "grille": "grille modifiée",
}
_TOP = {
    "enclosure": "enceinte redimensionnée", "ambient_temperature": "température ambiante modifiée",
    "ambient_pressure": "pression ambiante modifiée", "fluid": "fluide modifié",
    "precision": "finesse du maillage modifiée", "solver": "réglages du calcul modifiés",
    "walls": "parois modifiées", "name": "nom modifié", "gravity": "gravité modifiée",
    "turbulence": "turbulence modifiée",
}


def _names(data: dict, group: str) -> list[str]:
    return [x.get("name") for x in data.get(group) or [] if isinstance(x, dict)]


@dataclass
class Change:
    text: str  # short, e.g. "cpu déplacé"
    details: list[str] = field(default_factory=list)  # "components.cpu.box.origin.x : 0,06 → 0,07"


def _val(v) -> str:
    if isinstance(v, float):
        return fr(v, 4).rstrip("0").rstrip(",") if finite(v) else str(v)
    return "—" if v is None else str(v)


def describe_changes(old: dict, new: dict) -> list[Change]:
    """What changed in the physics between two project data dicts, in French."""
    old = {k: v for k, v in old.items() if k != UI_KEY}
    new = {k: v for k, v in new.items() if k != UI_KEY}
    out: dict[str, Change] = {}

    def add(text: str, detail: str | None = None) -> None:
        ch = out.setdefault(text, Change(text))
        if detail:
            ch.details.append(detail)

    for group, word in _GROUPS.items():
        a, b = _names(old, group), _names(new, group)
        for n in b:
            if n not in a:
                add(f"{word} {n} ajoutée" if word in ("pièce", "ouverture", "sonde")
                    else f"{word} {n} ajouté")
        for n in a:
            if n not in b:
                add(f"{word} {n} supprimée" if word in ("pièce", "ouverture", "sonde")
                    else f"{word} {n} supprimé")
    fa, fb = flatten(old), flatten(new)
    for key in sorted(set(fa) | set(fb)):
        va, vb = fa.get(key), fb.get(key)
        if va == vb:
            continue
        parts = key.split(".")
        detail = f"{key} : {_val(va)} → {_val(vb)}"
        if parts[0] in _GROUPS and len(parts) >= 2:
            name = parts[1]
            if name not in _names(old, parts[0]) or name not in _names(new, parts[0]):
                continue  # added / removed: already said
            rest = ".".join(parts[2:])
            what = next((w for k, w in _WHAT.items() if rest == k or rest.startswith(k + ".")),
                        "modifiée" if parts[0] in ("openings", "probes") else "modifié")
            add(f"{name} {what}", detail)
        else:
            add(_TOP.get(parts[0], f"{parts[0]} modifié"), detail)
    return list(out.values())


def changes_line(changes: list[Change], limit: int = 3) -> str:
    """'cpu déplacé, débit modifié' (+ 'et 2 autres')."""
    texts = [c.text for c in changes]
    line = ", ".join(texts[:limit])
    if len(texts) > limit:
        line += f" et {len(texts) - limit} autre{'s' if len(texts) - limit > 1 else ''}"
    return line


# --------------------------------------------------------------------------- compare


@dataclass
class CompareRow:
    label: str
    a: float
    b: float
    unit: str = "°C"
    digits: int = 1

    @property
    def delta(self) -> float:
        return self.b - self.a if finite(self.a) and finite(self.b) else math.nan

    def cells(self) -> tuple[str, str, str]:
        u = f" {self.unit}" if self.unit else ""
        return (fr(self.a, self.digits) + u if finite(self.a) else NA,
                fr(self.b, self.digits) + u if finite(self.b) else NA,
                fr(self.delta, self.digits, sign=True) + u if finite(self.delta) else NA)


def compare_rows(sa, sb) -> list[CompareRow]:
    """Max temperature per part (parts of either run), then key air numbers."""
    ta = {c.name: c.t_max_c for c in sa.components}
    tb = {c.name: c.t_max_c for c in sb.components}
    names = sorted(dict.fromkeys([*ta, *tb]),
                   key=lambda n: -max(v for v in (ta.get(n, -math.inf), tb.get(n, -math.inf))))
    rows = [CompareRow(n, ta.get(n, math.nan), tb.get(n, math.nan)) for n in names]
    rows.append(CompareRow("Air sortie", sa.outlet_t_mean_c, sb.outlet_t_mean_c))
    rows.append(CompareRow("Air max", sa.air_t_max_c, sb.air_t_max_c))
    rows.append(CompareRow("Vitesse max", sa.air_v_max, sb.air_v_max, "m/s", 2))
    rows.append(CompareRow("Perte de charge", sa.pressure_drop_pa, sb.pressure_drop_pa, "Pa", 2))
    return rows


def changed_parameters(data_a: dict, data_b: dict) -> list[tuple[str, str, str]]:
    """[(parameter, value in run A, value in run B)] for the physics that differ."""
    fa = flatten({k: v for k, v in data_a.items() if k != UI_KEY})
    fb = flatten({k: v for k, v in data_b.items() if k != UI_KEY})
    out = []
    for key in sorted(set(fa) | set(fb)):
        if fa.get(key) != fb.get(key):
            label = key
            if key.endswith("flow_rate"):  # m³/s -> m³/h, as the Air tab shows it
                va, vb = fa.get(key), fb.get(key)
                out.append((key.rsplit(".", 1)[0] + " · débit (m³/h)",
                            fr(va * 3600, 1) if finite(va) else NA,
                            fr(vb * 3600, 1) if finite(vb) else NA))
                continue
            out.append((label, _val(fa.get(key)), _val(fb.get(key))))
    return out
