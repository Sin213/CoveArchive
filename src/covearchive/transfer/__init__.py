"""Lazy transfer interfaces with no engine startup at import time."""

from importlib import import_module


_EXPORTS = {
    name: module
    for module, names in {
        "types": ("EngineState", "TransferState", "FailureKind", "TransferSettings",
                  "DownloadRequest", "TransferProgress", "TransferFailure", "TransferStatus",
                  "DownloadEngine", "FakeEngine", "TransferError", "EngineUnavailableError",
                  "EngineStartError", "RpcAuthError", "RpcTimeoutError", "RpcProtocolError",
                  "RpcResponseError", "EngineExitedError", "InvalidJobError", "InvalidJobStateError",
                  "InvalidRequestError", "InvalidSettingsError", "TargetExistsError",
                  "ResumeNotAllowedError", "ShutdownError", "validate_output_name", "validate_uri", "redact_uri"),
        "rpc": ("Aria2RpcClient",),
        "aria2": ("Aria2Engine", "find_aria2"),
    }.items()
    for name in names
}
__all__ = tuple(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{_EXPORTS[name]}", __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
