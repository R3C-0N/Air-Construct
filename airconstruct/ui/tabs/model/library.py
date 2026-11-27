"""Parts library (left panel) and the starting templates of "Nouveau".

Owned by: thread "Modèle" (see docs/ui-architecture.md).

A library entry is a ready-made part: material, typical size, power and maximum
temperature. Adding one puts it on the selected part or on the floor
(:func:`geometry.add_part`). A fan here is a fan INSIDE the box: for now it is a
solid block (its air flow is not simulated yet); a fan mounted on an enclosure
opening is set in the Air tab.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from ....materials import SOLIDS


@dataclass(frozen=True)
class PartPreset:
    kind: str  # stable id, stored in data["ui"]["model"]["kinds"]
    label: str  # French name shown in the library and the inspector
    name: str  # base of the part name
    material: str
    size_mm: tuple[float, float, float]
    power: float = 0.0
    max_temperature: float | None = None
    hint: str = ""

    def part(self) -> dict:
        d = {"name": self.name, "material": self.material, "power": self.power,
             "box": {"origin": {"x": 0.0, "y": 0.0, "z": 0.0},
                     "size": {a: v / 1000 for a, v in zip("xyz", self.size_mm)}}}
        if self.max_temperature is not None:
            d["max_temperature"] = self.max_temperature
        return d


PRESETS: list[PartPreset] = [
    PartPreset("board", "Carte", "carte", "fr4", (160, 100, 1.6),
               hint="circuit imprimé FR4, 160 × 100 × 1,6 mm"),
    PartPreset("chip", "Puce", "puce", "silicon", (20, 20, 2), power=5.0, max_temperature=85.0,
               hint="composant chauffant, 5 W, 85 °C max"),
    PartPreset("heatsink", "Dissipateur", "dissipateur", "aluminium", (40, 40, 20),
               hint="bloc d’aluminium posé sur une puce"),
    PartPreset("psu", "Alimentation", "alimentation", "aluminium", (60, 40, 30), power=10.0,
               max_temperature=100.0, hint="bloc d’alimentation, 10 W"),
    PartPreset("fan", "Ventilateur", "ventilateur", "plastic_abs", (40, 40, 10),
               hint="ventilateur interne (bloc solide pour l’instant)"),
    PartPreset("block", "Bloc libre", "bloc", "aluminium", (20, 20, 20),
               hint="boîte à régler soi-même"),
]
PRESET_BY_KIND = {p.kind: p for p in PRESETS}

MATERIAL_LABELS = {"aluminium": "Aluminium", "copper": "Cuivre", "fr4": "FR4",
                   "silicon": "Silicium", "steel": "Acier", "plastic_abs": "Plastique ABS"}


def material_label(name: str) -> str:
    return MATERIAL_LABELS.get(name, name)


def material_props(data: dict, name: str) -> tuple[float, float, float] | None:
    """(k, rho, cp) of a built-in or custom material, None if unknown."""
    for m in data.get("materials") or []:
        if m.get("name") == name:
            try:
                return float(m["kappa"]), float(m["rho"]), float(m["cp"])
            except (KeyError, TypeError, ValueError):
                return None
    s = SOLIDS.get(name)
    return (s.kappa, s.rho, s.cp) if s else None


# --------------------------------------------------------------------- templates
def _box(name, material, origin_mm, size_mm, power=0.0, tmax=None) -> dict:
    d = {"name": name, "material": material, "power": power,
         "box": {"origin": {a: v / 1000 for a, v in zip("xyz", origin_mm)},
                 "size": {a: v / 1000 for a, v in zip("xyz", size_mm)}}}
    if tmax is not None:
        d["max_temperature"] = tmax
    return d


def _opening(name, kind, face, center_mm, size_mm, **kw) -> dict:
    return {"name": name, "kind": kind, "face": face,
            "center": [v / 1000 for v in center_mm], "size": [v / 1000 for v in size_mm], **kw}


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    subtitle: str
    data: dict = field(repr=False)
    enabled: bool = True

    def build(self, name: str | None = None) -> dict:
        d = copy.deepcopy(self.data)
        if name:
            d["name"] = name
        return d


def _empty_box() -> dict:
    return {"name": "nouveau_projet", "enclosure": {"x": 0.2, "y": 0.12, "z": 0.06},
            "ambient_temperature": 25.0, "components": [], "openings": []}


def _rack_1u() -> dict:
    kinds = {"carte": "board", "cpu": "chip", "dissipateur": "heatsink", "alimentation": "psu"}
    return {
        "name": "rack_1U", "enclosure": {"x": 0.44, "y": 0.30, "z": 0.044},
        "ambient_temperature": 25.0,
        "components": [
            _box("carte", "fr4", (20, 20, 5), (280, 260, 1.6)),
            _box("cpu", "silicon", (120, 120, 6.6), (35, 35, 2.5), 25.0, 90.0),
            _box("dissipateur", "aluminium", (110, 110, 9.1), (55, 55, 30)),
            _box("alimentation", "aluminium", (320, 40, 0), (100, 150, 40), 20.0, 100.0),
        ],
        "openings": [
            _opening("ventilateur_1", "inlet", "ymin", (140, 22), (40, 40),
                     fan={"flow_rate": 0.004}),
            _opening("ventilateur_2", "inlet", "ymin", (300, 22), (40, 40),
                     fan={"flow_rate": 0.004}),
            _opening("sortie", "outlet", "ymax", (220, 22), (300, 30)),
        ],
        "precision": {"level": 2},
        "ui": {"model": {"groups": {"carte_mere": ["carte", "cpu", "dissipateur"]},
                         "hidden": [], "locked": [], "kinds": kinds}},
    }


def _power_supply() -> dict:
    kinds = {"carte": "board", "transfo": "psu", "regulateur_1": "chip",
             "regulateur_2": "chip"}
    return {
        "name": "boitier_alim", "enclosure": {"x": 0.16, "y": 0.10, "z": 0.07},
        "ambient_temperature": 25.0,
        "components": [
            _box("carte", "fr4", (10, 10, 5), (140, 80, 1.6)),
            _box("transfo", "steel", (20, 25, 6.6), (50, 50, 40), 8.0, 110.0),
            _box("regulateur_1", "aluminium", (95, 20, 6.6), (15, 10, 20), 3.0, 125.0),
            _box("regulateur_2", "aluminium", (95, 65, 6.6), (15, 10, 20), 3.0, 125.0),
        ],
        "openings": [
            _opening("aeration_bas", "inlet", "xmin", (50, 15), (60, 10)),
            _opening("aeration_haut", "outlet", "xmax", (50, 55), (60, 10)),
        ],
        "precision": {"level": 2},
        "ui": {"model": {"groups": {}, "hidden": [], "locked": [], "kinds": kinds}},
    }


def templates(example: dict) -> list[Template]:
    """The starting points of "Nouveau projet"; ``example`` is the current example
    case (the electronics board)."""
    return [
        Template("empty", "Boîte vide", "enceinte seule, à remplir", _empty_box()),
        Template("board", "Carte électronique", "l’exemple : carte, cpu, dissipateur", example),
        Template("rack", "Rack 1U", "440 × 44 mm, 2 ventilateurs", _rack_1u()),
        Template("psu", "Boîtier d’alimentation", "transfo + régulateurs, convection",
                 _power_supply()),
        Template("cad", "Importer un fichier CAO", "STEP / STL · plus tard", {}, enabled=False),
    ]
