"""Launch MKX Character Studio with visible startup diagnostics."""
import sys
import traceback
from pathlib import Path

if __name__ == '__main__':
    try:
        import tkinter
        import numpy
        import PIL
        from studio_gui import run_gui
        run_gui(selftest='--selftest' in sys.argv)
    except Exception:
        details = traceback.format_exc()
        Path(__file__).with_name('startup_error.log').write_text(details, encoding='utf-8')
        print(details)
        print('Run Setup Dependencies.cmd. Python must include Tcl/Tk.')
        sys.exit(1)
