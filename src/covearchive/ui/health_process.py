"""Health protocol adapter for the existing killable ScanController supervisor."""
from covearchive import health_worker as wire
from covearchive.ui.scan_process import ScanController, worker_command as scan_command


def worker_command(launcher):
    command = scan_command(launcher)
    return command[:-1] + ['--health-worker']


class HealthController(ScanController):
    response_limit = wire.MAX_RESPONSE_BYTES

    def __init__(self, launcher=None, *, command_factory=worker_command, deadline_ms=180_000, parent=None):
        # Deliberately no auth argument or saved-settings access.
        super().__init__(launcher, command_factory=command_factory, deadline_ms=deadline_ms, parent=parent)

    def start(self, repository, root, *, prepare=False):
        if self.job is not None: return False
        self._repository, self._root = repository, root
        self._prepare = prepare
        return super().start(repository.url)

    def _encode_request(self, request_id, url, token):
        return wire.encode_request(request_id, self._repository, self._root, prepare=self._prepare)

    def _decode_response(self, output, request_id):
        return wire.decode_response(output, request_id, self._repository.identity)
