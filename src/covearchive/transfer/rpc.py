"""Bounded, authenticated loopback JSON-RPC transport for an owned aria2 child."""

import http.client
import itertools
import json
import math
import socket
import threading
import time

from .types import RpcAuthError, RpcProtocolError, RpcResponseError, RpcTimeoutError


ALLOWED_METHODS = frozenset(("getVersion", "getGlobalOption", "changeGlobalOption", "addUri",
                           "tellStatus", "pause", "unpause", "remove", "shutdown", "forceShutdown"))
ALLOWED_ADD_URI_OPTIONS = frozenset(("dir", "out", "split", "max-connection-per-server", "max-download-limit",
                                    "header", "file-allocation", "allow-overwrite", "auto-file-renaming", "continue"))
ALLOWED_GLOBAL_OPTIONS = frozenset(("max-concurrent-downloads", "max-overall-download-limit"))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError
        result[key] = value
    return result


def _constant(_): raise ValueError


class Aria2RpcClient:
    def __init__(self, port, secret, *, host="127.0.0.1", timeout=2, max_response_bytes=1048576):
        if (host not in ("127.0.0.1", "::1") or type(port) is not int or not 1 <= port <= 65535
                or port == 6800 or not isinstance(secret, str) or not secret or len(secret) > 256
                or not secret.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in secret)
                or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600
                or type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 16 * 1024 * 1024):
            raise RpcProtocolError("rpc_configuration")
        self.host, self.port, self.timeout = host, port, timeout
        self.max_response_bytes, self._secret, self._ids = max_response_bytes, secret, itertools.count(1)

    def __repr__(self):
        return f"Aria2RpcClient(host={self.host!r}, port={self.port!r})"

    def call(self, method, params=()):
        if not isinstance(method, str): raise RpcProtocolError("rpc_method")
        method = method.removeprefix("aria2.")
        if method not in ALLOWED_METHODS or not isinstance(params, (tuple, list)):
            raise RpcProtocolError("rpc_method")
        if method in ("addUri", "changeGlobalOption"):
            index = 1 if method == "addUri" else 0
            allowed = ALLOWED_ADD_URI_OPTIONS if index else ALLOWED_GLOBAL_OPTIONS
            if (len(params) != index + 1 or not isinstance(params[index], dict)
                    or not params[index].keys() <= allowed):
                raise RpcProtocolError("rpc_options")
        request_id = str(next(self._ids))
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "aria2." + method,
                               "params": ["token:" + self._secret, *params]}, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise RpcProtocolError("rpc_parameters") from None
        deadline = time.monotonic() + self.timeout
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        expired = threading.Event()
        timer = None
        response = None
        owned_socket = None
        try:
            def expire():
                # Shutdown interrupts buffered header/body reads even with a slow drip.
                expired.set()
                try:
                    target = owned_socket or connection.sock
                    if target is not None: target.shutdown(socket.SHUT_RDWR)
                except OSError: pass

            timer = threading.Timer(max(0, deadline - time.monotonic()), expire)
            timer.daemon = True; timer.start()
            connection.connect()
            owned_socket = connection.sock
            if expired.is_set(): raise RpcTimeoutError("rpc_timeout")
            connection.request("POST", "/jsonrpc", body, {"Content-Type": "application/json", "Connection": "close"})
            response = connection.getresponse()
            length = response.getheader("Content-Length")
            if length is not None and (len(length) > 19 or not length.isascii() or not length.isdecimal()
                                       or int(length) > self.max_response_bytes):
                raise RpcProtocolError("rpc_response_size")
            data = response.read(self.max_response_bytes + 1)
            if expired.is_set() or time.monotonic() >= deadline: raise RpcTimeoutError("rpc_timeout")
            if len(data) > self.max_response_bytes: raise RpcProtocolError("rpc_response_size")
            try: reply = json.loads(data, object_pairs_hook=_object, parse_constant=_constant)
            except (ValueError, UnicodeError, RecursionError): raise RpcProtocolError("rpc_json") from None
            if (not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0" or reply.get("id") != request_id
                    or ("result" in reply) == ("error" in reply)):
                raise RpcProtocolError("rpc_envelope")
            if "error" in reply:
                error = reply["error"]
                if not isinstance(error, dict) or type(error.get("code")) is not int or not isinstance(error.get("message"), str):
                    raise RpcProtocolError("rpc_error_object")
                kind = RpcAuthError if error["message"] == "Unauthorized" else RpcResponseError
                # Remote messages can echo tokens, URLs or private config paths. Never retain them.
                raise kind("rpc_auth" if kind is RpcAuthError else "rpc_response", aria2_code=error["code"])
            return reply["result"]
        except TimeoutError:
            raise RpcTimeoutError("rpc_timeout") from None
        except (OSError, http.client.HTTPException):
            error = RpcTimeoutError if expired.is_set() or time.monotonic() >= deadline else RpcProtocolError
            raise error("rpc_timeout" if error is RpcTimeoutError else "rpc_transport") from None
        finally:
            if timer is not None: timer.cancel(); timer.join()
            if response is not None: response.close()
            connection.close()
