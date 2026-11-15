"""Package-relative application branding."""
from pathlib import Path
from PySide6.QtGui import QIcon


def app_icon():
    try: return QIcon(str(Path(__file__).resolve().parents[1] / "assets/covearchive-icon.png"))
    except (OSError, RuntimeError): return QIcon()
