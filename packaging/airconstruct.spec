# PyInstaller spec for the Air-Construct desktop app.
# Build from the repository root:  pyinstaller packaging/airconstruct.spec --noconfirm
# Output: dist/Air-Construct/ (Air-Construct.exe on Windows, Air-Construct on Linux).
# Owned by: workstream "packaging".

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH is set by PyInstaller

datas = [
    # app.py looks for <bundle>/examples/electronics_box.yaml (parents[2] of ui/app.py)
    (str(ROOT / "examples"), "examples"),
    (str(ROOT / "LICENSE"), "."),
    # Inter font of the UI theme (ui/theme.py loads <package>/ui/fonts/*.ttf)
    (str(ROOT / "airconstruct" / "ui" / "fonts"), "airconstruct/ui/fonts"),
]
datas += collect_data_files("pyvista")
datas += collect_data_files("pyvistaqt")

hiddenimports = (
    collect_submodules("airconstruct")
    + collect_submodules("pyvistaqt")
    + [
        "vtkmodules.qt.QVTKRenderWindowInteractor",
        "vtkmodules.util.numpy_support",
        "matplotlib.backends.backend_agg",
    ]
)

# Not used by the app; leaving them out keeps the bundle a few hundred MB smaller.
EXCLUDES = [
    "tkinter", "PyQt5", "PyQt6", "PySide2", "IPython", "pytest", "trame", "jupyter_server",
    "notebook", "ipywidgets",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.Qt3DExtras", "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQml", "PySide6.QtQuickWidgets",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech", "PySide6.QtHttpServer",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

icon = ROOT / "packaging" / "airconstruct.ico"
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Air-Construct",
    console=False,
    icon=str(icon) if icon.exists() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Air-Construct")  # noqa: F821
