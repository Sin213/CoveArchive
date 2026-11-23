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
    elif "--health-worker" in sys.argv:
        from covearchive.health_worker import main
        os._exit(main())
    elif "--discover-worker" in sys.argv:
        from covearchive.discover.worker import main
        os._exit(main())
    elif "--package-selftest" in sys.argv:
        from covearchive.ui.package_selftest import main
        raise SystemExit(main(LAUNCHER))
    elif "--smoke-test" in sys.argv:
        import tempfile, time
        temporary = tempfile.TemporaryDirectory(); os.environ["LOCALAPPDATA"] = temporary.name
        if sys.platform != "win32":
            os.environ["XDG_CONFIG_HOME"] = str(Path(temporary.name)/"config")
            os.environ["XDG_STATE_HOME"] = str(Path(temporary.name)/"state")
        from PySide6.QtWidgets import QApplication
        from covearchive.ui.window import MainWindow
        app = QApplication(sys.argv)
        window = MainWindow(LAUNCHER); window.show(); app.processEvents(); window.close()
        deadline = time.monotonic()+8
        while not window.bridge.closed and time.monotonic()<deadline: app.processEvents(); time.sleep(0.005)
        closed = window.bridge.closed; temporary.cleanup()
        raise SystemExit(0 if closed else 1)
    else:
        from covearchive.ui.window import main
        raise SystemExit(main(launcher=LAUNCHER))
