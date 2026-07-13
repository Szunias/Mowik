# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs


ROOT = Path(SPECPATH).resolve().parent


datas = [
    (str(ROOT / "README.md"), "."),
    (str(ROOT / "README.pl.md"), "."),
    (str(ROOT / "LICENSE.txt"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.txt"), "."),
    (str(ROOT / "config.example.json"), "."),
    (str(ROOT / "slownik.example.txt"), "."),
    (str(ROOT / "assets" / "Mowik.ico"), "assets"),
]
binaries = []
hiddenimports = []

for package in (
    "faster_whisper",
    "ctranslate2",
    "av",
    "sounddevice",
    "pystray",
    "pyperclip",
    "PIL",
):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

hiddenimports += [
    "pythoncom",
    "pywintypes",
    "win32com.client",
]

for package in ("nvidia.cublas", "nvidia.cuda_nvrtc", "nvidia.cudnn"):
    binaries += collect_dynamic_libs(package)

a = Analysis(
    [str(ROOT / "mowik.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Mowik",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "Mowik.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
    manifest=str(ROOT / "packaging" / "Mowik.manifest"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Mowik",
)
