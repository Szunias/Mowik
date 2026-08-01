import sys
from pathlib import Path

from PyInstaller.utils.hooks.tcl_tk import tcltk_info


def pre_find_module_path(hook_api):
    """Keep stdlib tkinter discoverable when only PyInstaller's Tcl probe fails."""

    if tcltk_info.available:
        return

    stdlib_dir = Path(sys.base_prefix) / "Lib"
    tkinter_init = stdlib_dir / "tkinter" / "__init__.py"
    if not tkinter_init.is_file():
        raise FileNotFoundError(
            "PyInstaller could not discover Tcl/Tk and the CPython tkinter "
            f"package is missing: {tkinter_init}"
        )

    hook_api.search_dirs = [str(stdlib_dir)]
