"""One explicitly started history worker; queued signals are the GUI boundary."""
from pathlib import Path
import re
import threading
from PySide6.QtCore import QObject, Signal
from covearchive.archive import githistory
from covearchive.ui.queue_panel import size_text


ERRORS = {
    "git_missing": "Git is not installed or not on PATH.",
    "git_unsupported": "Git 2.45 or newer is required.",
    "invalid_repository": "Choose a supported public repository.",
    "not_found": "Public repository was not found.",
    "network": "Network error while archiving Git history; retry later.",
    "timeout": "Git history operation timed out.",
    "redirect": "Repository moved or renamed; scan its new canonical URL.",
    "cancelled": "Git history cancelled.",
    "too_many_refs": "Repository exceeds the Git history ref limit.",
    "too_large": "Git history exceeds the size or output limit.",
    "disk_full": "Not enough free disk space for Git history.",
    "invalid_objects": "Repository contains invalid Git objects.",
    "empty_repository": "Repository has no branches or tags to archive.",
    "busy": "Another Git history operation is running; retry later.",
    "conflict": "Git history destination conflicts with an existing file.",
    "corrupt": "Git history snapshot is damaged; existing files were retained.",
    "unsafe_path": "Choose a valid, existing archive root directory.",
    "internal_error": "Git history could not be archived; retry later."}
STAGES = {"checking": "Checking Git history...", "downloading": "Downloading Git history...",
    "verifying": "Verifying Git history...", "publishing": "Publishing Git history (cannot be cancelled now)..."}


def result_text(result):
    path = str(result.snapshot)
    if re.search(r"://|[?&](?:token|sig|secret)=", path, re.I): path = "[redacted destination]"
    path = "".join(c for c in path if c.isprintable())
    if result.status == "UNCHANGED":
        text = f"Git history already up to date (snapshot {Path(path).name}) at {Path(path).parent}; nothing downloaded"
    else: text = f"Archived Git history: {result.ref_count} refs, {size_text(result.size)}; {path}"
    if result.damaged: text += f"; Warning: {len(result.damaged)} earlier snapshot(s) failed verification and were kept untouched"
    return text


class HistoryController(QObject):
    finished = Signal(object)
    failed = Signal(object)
    progress = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None
        self._cancel = threading.Event()

    @property
    def running(self): return self._thread is not None and self._thread.is_alive()

    @property
    def is_active(self): return self.running

    def start(self, repository, archive_root):
        if self.running: return False
        if self._thread is not None: self._thread.join(0)
        self._cancel = threading.Event(); cancel = self._cancel
        def work():
            try:
                result = githistory.archive_history(repository, archive_root,
                    cancel=cancel.is_set, progress=self.progress.emit)
            except Exception as error:
                code = error.code if isinstance(error, githistory.HistoryError) and error.code in ERRORS else "internal_error"
                self.failed.emit(githistory.HistoryError(code))
            else: self.finished.emit(result)
        self._thread = threading.Thread(target=work, name="covearchive-git-history", daemon=False)
        self._thread.start()
        return True

    def cancel(self):
        self._cancel.set()

    request_cancel = cancel

    def shutdown(self):
        if self.running:
            self.cancel(); return False
        if self._thread is not None: self._thread.join(0)
        return True
