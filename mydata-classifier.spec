from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


datas = [("templates", "templates"), ("static", "static"), ("xsd", "xsd"), ("pyproject.toml", "."), ("docs/syndiasmoi_xaraktirismwn_v*.xlsx", "docs")]
datas += collect_data_files("tzdata") + copy_metadata("mcp")

a = Analysis(
    ["windows_launcher.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("uvicorn"),  # MCP server (windows_launcher --mcp)
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["gunicorn", "tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="myDATA Classifier",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon="static/app.ico",
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="myDATA Classifier",
)
