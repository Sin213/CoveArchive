"""Source-tree launcher; worker dispatch deliberately precedes all Qt imports."""

import os
from pathlib import Path
import sys

LAUNCHER = Path(__file__).resolve()
SOURCE = LAUNCHER.parent / "src"
if SOURCE.is_dir():
    sys.path.insert(0, str(SOURCE))

if __name__ == "__main__":
    if "--scan-worker" in sys.argv:
        from covearchive.scan_worker import main
        os._exit(main())
    elif "--smoke-test" in sys.argv:
        from PySide6.QtWidgets import QApplication
        from covearchive.ui.window import MainWindow
        app = QApplication(sys.argv)
        window = MainWindow(LAUNCHER); window.show(); app.processEvents(); window.close()
        raise SystemExit(0)
    else:
        from covearchive.ui.window import main
        raise SystemExit(main(launcher=LAUNCHER))
