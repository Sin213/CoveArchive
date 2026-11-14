"""CA-S6 R01-R10 loopback protocol and deadline tests (no aria2 child)."""

from contextlib import contextmanager
import json
import logging
import secrets
import socketserver
import threading
import time
import pytest

from covearchive.transfer.rpc import Aria2RpcClient
from covearchive.transfer.types import RpcAuthError, RpcProtocolError, RpcResponseError, RpcTimeoutError


@contextmanager
def server(mode="valid"):
    requests, stopped = [], threading.Event()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(1)
            try:
                self.rfile.readline()
                headers = {}
                while (line := self.rfile.readline()) not in (b"\r\n", b"\n", b""):
                    key, value = line.decode().split(":", 1); headers[key.lower()] = value.strip()
                request = json.loads(self.rfile.read(int(headers["content-length"])))
                requests.append(request)
                reply = {"jsonrpc": "2.0", "id": request["id"], "result": {"version": "1.37.0"}}
                status, extra = 200, b""
                if mode == "wrong_id": reply["id"] = "other"
                if mode == "missing": del reply["result"]
                if mode == "both": reply["error"] = {"code": 1, "message": "bad"}
                if mode == "version": reply["jsonrpc"] = "1.0"
                if mode in ("error", "auth", "echo"):
                    reply.pop("result"); status = 400
                    reply["error"] = {"code": 1, "message": "Unauthorized" if mode == "auth" else
                                      "bad " + request["params"][0] + " https://host/private?token=SECRET"}
                body = json.dumps(reply).encode()
                if mode in ("NaN", "Infinity", "-Infinity"): body = body.replace(b'{"version": "1.37.0"}', mode.encode())
                if mode == "malformed": body = b"not JSON"
                if mode == "list": body = b"[]"
                if mode == "duplicate": body = body.replace(b'"jsonrpc": "2.0"', b'"jsonrpc": "2.0", "jsonrpc": "2.0"')
                if mode == "huge_length": extra = b"Content-Length: 10000000\r\n"
                elif mode == "decimal_length": extra = b"Content-Length: " + b"9" * 5000 + b"\r\n"
                elif mode not in ("streamed", "slow_header"): extra = f"Content-Length: {len(body)}\r\n".encode()
                if mode == "streamed": body = b"x" * 2048
                header = f"HTTP/1.1 {status} Test\r\nConnection: close\r\n".encode() + extra + b"\r\n"
                if mode == "slow_header":
                    for byte in header:
                        if stopped.wait(.03): return
                        self.wfile.write(bytes([byte])); self.wfile.flush()
                else: self.wfile.write(header); self.wfile.flush()
                if mode in ("timeout", "slow_body"):
                    for byte in body:
                        if stopped.wait(.03): return
                        self.wfile.write(bytes([byte])); self.wfile.flush()
                else: self.wfile.write(body)
            except (OSError, ValueError): pass

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = False
        block_on_close = True

    instance = Server(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=instance.serve_forever, kwargs={"poll_interval": .01})
    worker.start()
    try: yield instance.server_address[1], requests
    finally:
        stopped.set(); instance.shutdown(); instance.server_close(); worker.join(2)
        assert not worker.is_alive()


def test_R01_valid_auth_and_unique_ids(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    secret = secrets.token_urlsafe(32)
    with server() as (port, requests):
        client = Aria2RpcClient(port, secret)
        assert client.call("getVersion") == {"version": "1.37.0"}
        client.call("aria2.getGlobalOption")
        assert len(requests) == 2
        assert requests[0]["method"] == "aria2.getVersion"
        assert bool(requests[0]["params"] == ["token:" + secret])
        assert all(isinstance(r["id"], str) for r in requests)
        assert requests[0]["id"] != requests[1]["id"]


@pytest.mark.parametrize("mode", ["malformed", "wrong_id", "missing", "both", "version", "list", "NaN", "Infinity", "-Infinity", "duplicate"])
def test_R02_R03_R04_protocol_rejection(mode):
    with server(mode) as (port, _):
        with pytest.raises(RpcProtocolError): Aria2RpcClient(port, secrets.token_urlsafe(32)).call("getVersion")


@pytest.mark.parametrize("mode, error", [("error", RpcResponseError), ("auth", RpcAuthError)])
def test_R05_HTTP400_body_parsed(mode, error):
    with server(mode) as (port, _):
        with pytest.raises(error) as caught: Aria2RpcClient(port, secrets.token_urlsafe(32)).call("getVersion")
        assert caught.value.aria2_code == 1


@pytest.mark.parametrize("mode", ["huge_length", "streamed", "decimal_length"])
def test_R06_response_caps(mode):
    with server(mode) as (port, _):
        with pytest.raises(RpcProtocolError):
            Aria2RpcClient(port, secrets.token_urlsafe(32), max_response_bytes=256).call("getVersion")


@pytest.mark.parametrize("mode", ["timeout", "slow_body", "slow_header"])
def test_R07_total_deadline(mode):
    with server(mode) as (port, _):
        started = time.monotonic()
        with pytest.raises(RpcTimeoutError):
            Aria2RpcClient(port, secrets.token_urlsafe(32), timeout=.15).call("getVersion")
        assert time.monotonic() - started < .8


def test_R08_no_secret_in_diagnostics(caplog):
    secret = secrets.token_urlsafe(32)
    with server("echo") as (port, _), caplog.at_level(logging.DEBUG):
        client = Aria2RpcClient(port, secret)
        with pytest.raises(RpcResponseError) as caught: client.call("getVersion")
        diagnostics = repr(client) + str(client) + repr(caught.value) + str(caught.value) + repr(vars(caught.value)) + caplog.text
        assert bool(secret not in diagnostics)
        assert "private" not in diagnostics and "SECRET" not in diagnostics


def test_R07_connect_and_read_share_deadline(monkeypatch):
    import http.client
    original = http.client.HTTPConnection.connect
    def delayed(connection): original(connection); threading.Event().wait(.1)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", delayed)
    with server("slow_body") as (port, _):
        started = time.monotonic()
        with pytest.raises(RpcTimeoutError): Aria2RpcClient(port, secrets.token_urlsafe(32), timeout=.2).call("getVersion")
        assert time.monotonic() - started < .3


@pytest.mark.parametrize("host", ["localhost", "0.0.0.0", "example.com", "127.0.0.2", "::", "::ffff:127.0.0.1"])
def test_R09_only_literal_loopback_hosts(host):
    with pytest.raises(RpcProtocolError): Aria2RpcClient(49152, secrets.token_urlsafe(32), host=host)


@pytest.mark.parametrize("method, params", [("system.multicall", []), ("tellActive", []),
    ("addUri", [["https://host/a"], {"bad": "value"}]),
    ("changeGlobalOption", [{"rpc-secret": "other"}]), ("addUri", [["https://host/a"], [], 0])])
def test_R10_disallowed_before_network(method, params):
    with server() as (port, requests):
        with pytest.raises(RpcProtocolError): Aria2RpcClient(port, secrets.token_urlsafe(32)).call(method, params)
        assert requests == []
