"""Scheduling blocker: release Auto-Archive never opts into mirror downloads."""
from covearchive.archive import githistory as gh
from test_scheduled import rig


def test_release_auto_archive_never_updates_mirrors(rig, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("release scheduling must not download Git history")
    monkeypatch.setattr(gh, "update_mirror", forbidden)
    monkeypatch.setattr(gh, "archive_history", forbidden)
    assert rig.run() == 0
    assert not list(rig.root.rglob("git-history"))
