"""Relocation adapter for the existing deadline/cancel/process supervisor."""
from covearchive import relocation_worker as wire
from covearchive.ui.scan_process import ScanController, worker_command as scan_command


def worker_command(launcher):
    return scan_command(launcher)[:-1] + ['--relocation-worker']


class RelocationController(ScanController):
    response_limit = wire.MAX_RESPONSE_BYTES

    def __init__(self, launcher=None, *, command_factory=worker_command, deadline_ms=30_000, parent=None):
        super().__init__(launcher, command_factory=command_factory, deadline_ms=deadline_ms, parent=parent)

    def start(self, repository, path):
        if self.job is not None: return False
        self._repository, self._path = repository, path
        return super().start(repository.url)

    def _encode_request(self, request_id, url, token):
        return wire.encode_request(request_id, self._repository, self._path)

    def _decode_response(self, output, request_id):
        return wire.decode_response(output, request_id, self._repository)
