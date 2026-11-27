"""Exports of the Résultats tab: picture of the view (PNG), full report (PDF),
3D fields and streamlines (VTK, for ParaView).

Owned by: thread "Résultats" (see docs/ui-architecture.md). The per-part CSV is in
``data.py``; the HTML report is the core ``results.write_report`` (``ctx.services``).
"""

from __future__ import annotations

import math
import tempfile
from datetime import datetime
from pathlib import Path

from .data import NA, PartRow, finite, fr

MONTHS = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.",
          "nov.", "déc.")


def fr_date(d: datetime) -> str:
    """'26 nov. 09:40'"""
    return f"{d.day} {MONTHS[d.month - 1]} {d:%H:%M}"


# --------------------------------------------------------------------------- PNG


def save_view_png(plotter, items, path: Path, window_size=(1600, 1000)) -> Path:
    """Screenshot of the live 3D view, else an off-screen render of ``items``."""
    path = Path(path)
    if plotter is not None:
        plotter.screenshot(str(path), window_size=None)
        return path
    from ....results import rendering_available

    if not rendering_available():
        raise RuntimeError("La vue 3D n’est pas disponible sur cet ordinateur.")
    import pyvista as pv

    p = pv.Plotter(off_screen=True, window_size=window_size)
    p.set_background("#E7EAEF", top="#FFFFFF")
    for it in items:
        kw = dict(it.kwargs)
        if it.scalars:
            kw["scalars"] = it.scalars
        else:
            kw["color"] = it.color
        p.add_mesh(it.mesh, style=it.style, opacity=it.opacity, **kw)
    p.view_isometric()
    p.screenshot(str(path))
    p.close()
    return path


# --------------------------------------------------------------------------- VTK


def write_fields_vtk(regions: dict, path: Path) -> Path:
    """Every region of the run in one multiblock file (.vtm + its folder)."""
    import pyvista as pv

    path = Path(path).with_suffix(".vtm")
    mb = pv.MultiBlock({name: mesh for name, mesh in regions.items()})
    mb.save(str(path))
    return path


def write_streamlines_vtk(lines, path: Path) -> Path:
    if lines is None or not lines.n_points:
        raise RuntimeError("Pas de lignes de courant à exporter.")
    path = Path(path).with_suffix(".vtp")
    lines.save(str(path))
    return path


# --------------------------------------------------------------------------- PDF


def _kpis(summary) -> list[tuple[str, str]]:
    hot = summary.hottest() if hasattr(summary, "hottest") else None
    out = [
        ("Pièce la plus chaude", f"{hot.name} · {fr(hot.t_max_c)} °C" if hot else NA),
        ("Air max", f"{fr(summary.air_t_max_c)} °C"),
        ("Sortie moyenne", f"{fr(summary.outlet_t_mean_c)} °C"),
        ("Vitesse max", f"{fr(summary.air_v_max, 2)} m/s"),
        ("Perte de charge", f"{fr(summary.pressure_drop_pa, 2)} Pa"),
    ]
    err = getattr(summary, "energy_balance_error", math.nan)
    if finite(err):
        out.append(("Bilan d’énergie", f"écart {fr(100 * err, 1, sign=True)} %"))
    return out


def _cover(pdf, plt, title: str, subtitle: str, summary, rows: list[PartRow]) -> None:
    fig = plt.figure(figsize=(8.27, 11.69))  # A4 portrait
    fig.text(0.07, 0.95, title, fontsize=18, weight="bold")
    fig.text(0.07, 0.925, subtitle, fontsize=9, color="#4B5563")
    y = 0.88
    for label, value in _kpis(summary):
        fig.text(0.07, y, label, fontsize=10, color="#4B5563")
        fig.text(0.40, y, value, fontsize=10, weight="bold")
        y -= 0.025
    ko = [r for r in rows if r.status == "ko"]
    for r in ko:
        fig.text(0.07, y, f"{r.name} dépasse sa limite ({fr(r.t_max)} / {fr(r.limit, 0)} °C)",
                 fontsize=10, color="#B91C1C")
        y -= 0.022
    ax = fig.add_axes([0.07, 0.08, 0.86, max(0.1, y - 0.12)])
    ax.axis("off")
    ax.set_title("Par pièce", loc="left", fontsize=12, weight="bold")
    cells = [[r.name, fr(r.power_w), fr(r.t_min), fr(r.t_mean), fr(r.t_max),
              fr(r.limit, 0) if r.limit is not None else NA,
              {"ok": "OK", "ko": "KO"}.get(r.status, NA)] for r in rows]
    if cells:
        t = ax.table(cellText=cells, colLabels=["Pièce", "P (W)", "T min", "T moy.", "T max",
                                                "Limite", "État"],
                     loc="upper left", cellLoc="right", colLoc="right")
        t.auto_set_font_size(False)
        t.set_fontsize(9)
        t.scale(1, 1.4)
        for (i, j), cell in t.get_celld().items():
            cell.set_edgecolor("#E5E7EB")
            if i > 0 and j == 6 and cells[i - 1][6] == "KO":
                cell.get_text().set_color("#B91C1C")
    pdf.savefig(fig)
    plt.close(fig)


def _bars(pdf, plt, rows: list[PartRow]) -> None:
    rows = [r for r in rows if finite(r.t_max)]
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(8.27, 1.8 + 0.45 * len(rows)))
    for i, r in enumerate(rows):
        lo = r.t_min if finite(r.t_min) else (r.t_mean if finite(r.t_mean) else r.t_max)
        ax.barh(i, max(r.t_max - lo, 0.15), left=lo, height=0.45,
                color="#E0445A" if r.status == "ko" else "#F97316")
        if finite(r.t_mean):
            ax.plot([r.t_mean], [i], "o", color="white", mec="#111827", ms=5)
        if r.limit is not None:
            ax.plot([r.limit, r.limit], [i - 0.35, i + 0.35], color="#111827", lw=2)
    ax.set_yticks(range(len(rows)), [r.name for r in rows])
    ax.invert_yaxis()
    ax.set_xlabel("Température (°C) · barre min → max, point = moyenne, trait = limite")
    ax.set_title("Températures par pièce", loc="left", weight="bold")
    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)


def _energy(pdf, plt, summary) -> None:
    b = getattr(summary, "energy", None)
    if b is None:
        return
    fig = plt.figure(figsize=(8.27, 11.69))
    fig.text(0.07, 0.95, "Bilan d’énergie", fontsize=14, weight="bold")
    ax = fig.add_axes([0.07, 0.4, 0.86, 0.5])
    ax.axis("off")
    cells = [[p.name, fr(p.power_w, 2), fr(p.to_air_w, 2), fr(p.to_walls_w, 2),
              fr(p.residual_w, 2)] for p in b.parts]
    if cells:
        t = ax.table(cellText=cells, colLabels=["Pièce", "Puissance (W)", "Vers l’air (W)",
                                                "Vers les parois (W)", "Non équilibré (W)"],
                     loc="upper left", cellLoc="right", colLoc="right")
        t.auto_set_font_size(False)
        t.set_fontsize(9)
        t.scale(1, 1.4)
    out = " · ".join(f"{n} {fr(w, 2)} W" for n, w in b.air_to_openings_w.items())
    fig.text(0.07, 0.36, f"Sorti par l’air : {out or NA}", fontsize=10)
    fig.text(0.07, 0.34, f"Écart : {fr(b.residual_w, 2)} W ({fr(100 * b.error, 1)} % de "
                         f"{fr(b.total_power_w, 2)} W)", fontsize=10)
    pdf.savefig(fig)
    plt.close(fig)


def _image_page(pdf, plt, image: Path, title: str) -> None:
    img = plt.imread(str(image))
    fig = plt.figure(figsize=(11.69, 8.27))  # A4 landscape
    fig.text(0.04, 0.95, title, fontsize=13, weight="bold")
    ax = fig.add_axes([0.04, 0.04, 0.92, 0.88])
    ax.imshow(img)
    ax.axis("off")
    pdf.savefig(fig)
    plt.close(fig)


IMAGE_TITLES = {
    "temperature_slice_z": "Température, coupe horizontale",
    "temperature_slice_y": "Température, coupe verticale",
    "velocity_slice_z": "Vitesse de l’air, coupe horizontale",
    "velocity_slice_y": "Vitesse de l’air, coupe verticale",
    "component_temperatures": "Températures par pièce",
    "components_3d": "Pièces en 3D",
    "streamlines_3d": "Lignes de courant",
}


def write_pdf(path: Path, case, summary, rows: list[PartRow], case_dir: Path | None = None,
              view_png: Path | None = None, extra_images: list[Path] | None = None) -> Path:
    """Full report as a PDF: key numbers, per-part table with limits, temperature
    chart, energy balance, then the field pictures (cuts, streamlines) and the view."""
    import matplotlib

    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    path = Path(path).with_suffix(".pdf")
    images: list[Path] = list(extra_images or [])
    with tempfile.TemporaryDirectory() as tmp:
        if case_dir is not None:
            try:
                from ....results import render_images

                images = render_images(case, Path(case_dir), Path(tmp), summary=summary) + images
            except Exception:  # noqa: BLE001, S110 - a report without pictures is still useful
                pass
        with PdfPages(path) as pdf:
            sub = f"Air-Construct · {fr_date(datetime.now().astimezone())}"
            _cover(pdf, plt, f"{case.name} : résultats", sub, summary, rows)
            _bars(pdf, plt, rows)
            _energy(pdf, plt, summary)
            if view_png is not None and Path(view_png).exists():
                _image_page(pdf, plt, Path(view_png), "Vue 3D")
            for im in images:
                if Path(im).stem == "component_temperatures":
                    continue  # already drawn with the limits
                _image_page(pdf, plt, Path(im), IMAGE_TITLES.get(Path(im).stem, Path(im).stem))
            info = pdf.infodict()
            info["Title"] = f"{case.name} : résultats"
            info["Creator"] = "Air-Construct"
    return path
