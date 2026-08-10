# -*- mode: python ; coding: utf-8 -*-

import _tkinter
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, copy_metadata
from PyInstaller.utils.hooks.tcl_tk import tcltk_info


ROOT = Path(SPECPATH).resolve().parent


datas = [
    (str(ROOT / "README.md"), "."),
    (str(ROOT / "README.pl.md"), "."),
    (str(ROOT / "LICENSE.txt"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.txt"), "."),
    (str(ROOT / "THIRD_PARTY_LICENSES" / "Apache-2.0.txt"), "THIRD_PARTY_LICENSES"),
    (str(ROOT / "THIRD_PARTY_LICENSES" / "CTranslate2-LICENSE.txt"), "THIRD_PARTY_LICENSES"),
    (str(ROOT / "THIRD_PARTY_LICENSES" / "ONNXRuntime-LICENSE.txt"), "THIRD_PARTY_LICENSES"),
    (str(ROOT / "config.example.json"), "."),
    (str(ROOT / "slownik.example.txt"), "."),
    (str(ROOT / "assets" / "Mowik.ico"), "assets"),
]

# PyInstaller discovers these directories by initializing Tcl in an isolated
# subprocess. A restricted build worker can block that probe even though the
# complete Tcl/Tk payload is installed with CPython. In that case hook-_tkinter
# still enables its runtime hook, so omitting the payload creates an executable
# that fails before mowik.py starts.
if not tcltk_info.available:
    tcl_root = Path(sys.base_prefix) / "tcl"
    tcl_data_dir = tcl_root / f"tcl{_tkinter.TCL_VERSION}"
    tk_data_dir = tcl_root / f"tk{_tkinter.TK_VERSION}"
    tcl_major_dir = tcl_root / f"tcl{_tkinter.TCL_VERSION.split('.', 1)[0]}"

    missing_tcl_tk_payload = []
    if not (tcl_data_dir / "init.tcl").is_file():
        missing_tcl_tk_payload.append(tcl_data_dir / "init.tcl")
    if not (tk_data_dir / "tk.tcl").is_file():
        missing_tcl_tk_payload.append(tk_data_dir / "tk.tcl")
    if not tcl_major_dir.is_dir():
        missing_tcl_tk_payload.append(tcl_major_dir)
    if missing_tcl_tk_payload:
        missing = ", ".join(str(path) for path in missing_tcl_tk_payload)
        raise FileNotFoundError(
            "PyInstaller could not discover Tcl/Tk and the CPython fallback "
            f"payload is incomplete: {missing}"
        )

    datas += [
        (str(tcl_data_dir), tcltk_info.TCL_ROOTNAME),
        (str(tk_data_dir), tcltk_info.TK_ROOTNAME),
        (str(tcl_major_dir), tcl_major_dir.name),
    ]
binaries = []
hiddenimports = []

# Preserve distribution metadata, including the full license files shipped in
# the wheels that are redistributed inside the frozen application. Keep this
# list explicit so a dependency change is reviewed together with its notices.
redistributed_distributions = (
    "anyio",
    "av",
    "certifi",
    "cffi",
    "click",
    "colorama",
    "ctranslate2",
    "faster-whisper",
    "filelock",
    "flatbuffers",
    "fsspec",
    "h11",
    "hf-xet",
    "httpcore",
    "httpx",
    "huggingface-hub",
    "idna",
    "numpy",
    "nvidia-cublas-cu12",
    "nvidia-cuda-nvrtc-cu12",
    "onnxruntime",
    "packaging",
    "Pillow",
    "protobuf",
    "pycparser",
    "pynput",
    "pyperclip",
    "pystray",
    "pywin32",
    "PyYAML",
    "setuptools",
    "six",
    "sounddevice",
    "tokenizers",
    "tqdm",
    "typing-extensions",
)
for distribution in redistributed_distributions:
    datas += copy_metadata(distribution)

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
    "tkinter",
    "_tkinter",
    "pythoncom",
    "pywintypes",
    "win32com.client",
]

# Bibliotek CUDA celowo nie pakujemy: w rozpakowanej postaci zajmują prawie
# gigabajt, a używają ich wyłącznie posiadacze karty NVIDIA. Mówik pobiera je
# przy pierwszym uruchomieniu modelu na GPU (mowik_cuda.py). Metadane pakietów
# zostają powyżej, bo to my doprowadzamy te biblioteki na komputer użytkownika
# i ich licencje muszą podróżować z aplikacją.

a = Analysis(
    [str(ROOT / "mowik.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(ROOT / "packaging" / "hooks")],
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
