"""CA-S6 T19-T21 and pure contract/FakeEngine security checks."""

from dataclasses import FrozenInstanceError, replace
import pytest

from covearchive.transfer import types as t


@pytest.mark.parametrize("name", ["", "../x", "..\\x", "C:\\x.part", "/x.part", "a:b.part",
    "a/b.part", "a\\b.part", "a.part ", "é.part", "a" * 64 + ".part", "a.PART",
    "CON.part", "nul.part", "COM1.part", "lpt9.part", "aux.part", "prn.part", ".part"])
def test_T19_reject_output_names(name):
    with pytest.raises(t.InvalidRequestError): t.validate_output_name(name)


@pytest.mark.parametrize("name", ["a.part", "A_0-z.part", "a" * 63 + ".part", "com10.part"])
def test_T19_accept_output_names(name):
    assert t.validate_output_name(name) == name


@pytest.mark.parametrize("uri", ["ftp://host/a", "https://u:p@host/a", "https:///a", "https://host:0/a",
    "https://host:65536/a", "https://host:bad/a", "https://host/a#x", "https://host/a#",
    "https://host/a\n", " https://host/a", "https://host/é", "https://host/" + "a" * 8192,
    "https://[invalid]/a", "https://host\\evil/a", "https://host/%0d%0aX", "https://host:/a"])
def test_security_URL_rejection(uri):
    with pytest.raises(t.InvalidRequestError): t.DownloadRequest(uri, "x.part")


@pytest.mark.parametrize("uri, redacted", [("https://example.com/private?token=SECRET", "https://example.com/..."),
    ("http://127.0.0.1:1234/x?q=y", "http://127.0.0.1:1234/..."),
    ("https://[::1]:443/x", "https://[::1]:443/...")])
def test_security_URL_redaction(uri, redacted):
    request = t.DownloadRequest(uri, "x.part")
    assert t.validate_uri(uri) == uri
    assert t.redact_uri(uri) == redacted
    assert "SECRET" not in repr(request) and "private" not in repr(request)
    assert t.redact_uri("invalid?secret") == "<redacted>"


@pytest.mark.parametrize("changes", [{"max_active": 0}, {"max_active": 17}, {"max_active": True},
    {"connections_per_download": 17}, {"global_speed_limit": -1}, {"global_speed_limit": 2**63},
    {"connect_timeout": 0}, {"io_timeout": 3601}, {"io_timeout": float("nan")}])
def test_security_settings_bounds(changes):
    with pytest.raises(t.InvalidSettingsError): t.TransferSettings(**changes)


@pytest.mark.parametrize("changes", [{"expected_size": -1}, {"expected_size": True}, {"connections": 0},
    {"connections": 17}, {"speed_limit": -1}, {"resume": 1}, {"resume": True},
    {"validator": 'W/"weak"'}, {"validator": '"x"\r\nX: injected'}, {"validator": "yesterday"}])
def test_security_request_bounds(changes):
    with pytest.raises(t.InvalidRequestError): t.DownloadRequest("https://host/a", "x.part", **changes)


@pytest.mark.parametrize("validator", ['"etag-123"', 'Wed, 21 Oct 2015 07:28:00 GMT'])
def test_resume_limit_validator(validator):
    assert t.DownloadRequest("https://host/a", "x.part", expected_size=1, resume=True,
                             validator=validator).validator == validator


def test_T20_T21_status_invariants():
    progress = t.TransferProgress(3, 3)
    failure = t.TransferFailure(t.FailureKind.NETWORK, 1, detail="https://host/private?token=SECRET\n" + "x" * 300)
    assert len(failure.detail) <= 200 and "SECRET" not in failure.detail and "\n" not in failure.detail
    for state, error in [(t.TransferState.COMPLETED, None), (t.TransferState.FAILED, failure)]:
        status = t.TransferStatus("0000000000000001", state, progress, error)
        assert status.verified is False and status.publisher_verified is False
        for field in ("verified", "publisher_verified"):
            with pytest.raises(t.InvalidRequestError): replace(status, **{field: True})
        with pytest.raises(FrozenInstanceError): status.verified = True
    with pytest.raises(t.InvalidRequestError): t.TransferStatus("x", t.TransferState.FAILED, progress)
    with pytest.raises(t.InvalidRequestError): t.TransferProgress(-1)
    with pytest.raises(t.InvalidRequestError): t.TransferProgress(4, 3)
    with pytest.raises(t.InvalidRequestError): t.TransferStatus("0000000000000001", t.TransferState.PAUSED,
                                                             t.TransferProgress(1, 3, 1))


def test_fake_engine_lifecycle_and_scripting():
    engine = t.FakeEngine()
    assert isinstance(engine, t.DownloadEngine) and engine.state is t.EngineState.NEW
    request = t.DownloadRequest("https://host/private?token=SECRET", "a.part", expected_size=10)
    with pytest.raises(t.InvalidJobStateError): engine.submit(request)
    with engine:
        first = engine.submit(request)
        second = engine.submit(replace(request, output_name="b.part"))
        third = engine.submit(replace(request, output_name="c.part"))
        assert engine.status(first).state is t.TransferState.ACTIVE
        assert engine.status(second).state is t.TransferState.ACTIVE
        assert engine.status(third).state is t.TransferState.WAITING
        with pytest.raises(t.TargetExistsError): engine.submit(replace(request, output_name="A.part"))
        assert engine.advance(first, 3, speed_bps=5).progress.completed_bytes == 3
        assert engine.pause(first).progress.speed_bps == 0
        with pytest.raises(t.InvalidJobStateError): engine.advance(first, 4)
        assert engine.resume(first).state in (t.TransferState.ACTIVE, t.TransferState.WAITING)
        engine.complete(second)
        assert engine.status(second).verified is False
        engine.cancel(third)
        assert engine.status(third).state is t.TransferState.REMOVED
        with pytest.raises(t.InvalidJobStateError): engine.resume(third)
        failure = t.TransferFailure(t.FailureKind.NETWORK, 1)
        assert engine.fail(first, failure).failure == failure
        with pytest.raises(t.InvalidJobError): engine.cancel("ffffffffffffffff")
        engine.update_settings(t.TransferSettings(max_active=1, connections_per_download=1))
        with pytest.raises(t.InvalidRequestError): engine.submit(replace(request, output_name="d.part", connections=2))
    assert engine.state is t.EngineState.STOPPED
    engine.stop()


def test_fake_size_mismatch_and_stopped_jobs():
    engine = t.FakeEngine(); engine.start()
    job = engine.submit(t.DownloadRequest("https://host/a", "a.part", expected_size=5))
    status = engine.advance(job, 6, total_bytes=6)
    assert status.state is t.TransferState.FAILED and status.failure.kind is t.FailureKind.SIZE_MISMATCH
    other = engine.submit(t.DownloadRequest("https://host/a", "b.part"))
    engine.stop()
    assert engine.status(other).state is t.TransferState.REMOVED
