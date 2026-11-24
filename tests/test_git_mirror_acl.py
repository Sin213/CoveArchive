"""Inject Windows security descriptors on Arch; native ACL verification is pending."""
from pathlib import Path
import importlib

import pytest

from covearchive.archive import githistory as gh
from test_git_history import REPO, local

USER = "S-1-5-21-10-20-30-1000"
OTHER = "S-1-5-21-10-20-30-1001"
ADMIN = "S-1-5-32-544"
SYSTEM = "S-1-5-18"


def descriptor(owner=USER, aces=()):
    return dict(owner=owner, user=USER, dacl=aces)


@pytest.mark.parametrize("target", ["root", "project", "history", "mirror", "repo", "config", "identity", "captures", "capture"])
def test_repair3_windows_acl_blocks_foreign_write_before_git(local, monkeypatch, target):
    first = gh.update_mirror(REPO, local[1])
    paths = dict(root=local[1], project=first.path.parent.parent, history=first.path.parent,
        mirror=first.path, repo=first.path / "repository.git", config=first.path / "repository.git" / "config",
        identity=first.path / "identity.json", captures=first.path / "captures", capture=first.capture)
    def reader(path):
        return descriptor(aces=[dict(type=0, flags=0, mask=2, sid=OTHER)] if Path(path) == paths[target] else [])
    monkeypatch.setattr(gh, "_test_windows_acl_reader", reader, raising=False)
    monkeypatch.setattr(gh, "_run_git", lambda *a, **kw: pytest.fail("Git ran before rejecting Windows permissions"))
    with pytest.raises(gh.HistoryError) as error: gh.update_mirror(REPO, local[1])
    assert error.value.code == "unsafe_permissions"


@pytest.mark.parametrize("mask", [2, 4, 0x10, 0x40, 0x100, 0x10000, 0x40000, 0x80000, 0x40000000, 0x10000000])
def test_repair3_windows_dacl_rejects_foreign_mutation_rights(mask):
    security = importlib.import_module("covearchive.archive.gitsecurity")
    with pytest.raises(security.SecurityError) as error:
        security.check_path(Path("unused"), reader=lambda _: descriptor(aces=[dict(type=0, flags=0, mask=mask, sid=OTHER)]))
    assert error.value.code == "unsafe_permissions"


@pytest.mark.parametrize("owner", [USER, ADMIN, SYSTEM])
def test_repair3_windows_acl_accepts_trusted_owner_and_read_only_foreign_ace(owner):
    security = importlib.import_module("covearchive.archive.gitsecurity")
    aces = [dict(type=0, flags=0, mask=0x1f01ff, sid=sid) for sid in (USER, ADMIN, SYSTEM)]
    aces += [dict(type=0, flags=0, mask=0x120089, sid=OTHER), dict(type=1, flags=0, mask=2, sid=OTHER)]
    security.check_path(Path("unused"), reader=lambda _: descriptor(owner=owner, aces=aces))


@pytest.mark.parametrize("kind", ["foreign_owner", "null_dacl", "unknown_ace", "api_failure"])
def test_repair3_windows_acl_fails_closed(kind):
    security = importlib.import_module("covearchive.archive.gitsecurity")
    def reader(_):
        if kind == "api_failure": raise OSError("security API failed")
        if kind == "foreign_owner": return descriptor(owner=OTHER)
        if kind == "null_dacl": return descriptor(aces=None)
        return descriptor(aces=[dict(type=42, flags=0, mask=0, sid=USER)])
    with pytest.raises(security.SecurityError) as error: security.check_path(Path("unused"), reader=reader)
    assert error.value.code == ("unsafe_permissions" if kind in ("foreign_owner", "null_dacl") else "permissions_unavailable")


def test_repair3_windows_api_error_blocks_git_with_clear_code(local, monkeypatch):
    gh.update_mirror(REPO, local[1])
    def failed(_): raise OSError("API unavailable")
    monkeypatch.setattr(gh, "_test_windows_acl_reader", failed)
    monkeypatch.setattr(gh, "_run_git", lambda *a, **kw: pytest.fail("Git ran after an ACL API failure"))
    with pytest.raises(gh.HistoryError) as error: gh.verify_mirror(REPO, local[1])
    assert error.value.code == "permissions_unavailable"


def test_repair3_windows_inherit_only_foreign_write_is_rejected():
    security = importlib.import_module("covearchive.archive.gitsecurity")
    with pytest.raises(security.SecurityError):
        security.check_path(Path("unused"), reader=lambda _: descriptor(aces=[dict(type=0, flags=8, mask=2, sid=OTHER)]))


@pytest.mark.parametrize("sid", ["S-1-3-0", "S-1-3-1"])
def test_repair4_creator_placeholders_accept_inherit_only(sid):
    security = importlib.import_module("covearchive.archive.gitsecurity")
    security.check_path(Path("unused"), reader=lambda _: descriptor(aces=[dict(type=0, flags=0x0b, mask=0x1f01ff, sid=sid)]))


def test_repair4_creator_owner_on_object_resolves_to_trusted_owner():
    security = importlib.import_module("covearchive.archive.gitsecurity")
    security.check_path(Path("unused"), reader=lambda _: descriptor(aces=[dict(type=0, flags=0, mask=2, sid="S-1-3-0")]))


def test_repair4_creator_group_on_object_and_foreign_owner_fail_closed():
    security = importlib.import_module("covearchive.archive.gitsecurity")
    for value in (descriptor(aces=[dict(type=0, flags=0, mask=2, sid="S-1-3-1")]),
                  descriptor(owner=OTHER, aces=[dict(type=0, flags=8, mask=2, sid="S-1-3-0")])):
        with pytest.raises(security.SecurityError): security.check_path(Path("unused"), reader=lambda _: value)


@pytest.mark.parametrize("flags", [0, 8])
def test_windows_acceptance_owner_rights_resolves_to_trusted_owner(flags):
    # pytest and hardened NTFS directories carry OWNER RIGHTS (S-1-3-4); it grants
    # the owner only, so it is safe exactly when the already-checked owner is.
    security = importlib.import_module("covearchive.archive.gitsecurity")
    aces = [dict(type=0, flags=flags, mask=0x1f01ff, sid="S-1-3-4"), dict(type=0, flags=0, mask=0x1f01ff, sid=SYSTEM)]
    security.check_path(Path("unused"), reader=lambda _: descriptor(aces=aces))
    with pytest.raises(security.SecurityError) as error:
        security.check_path(Path("unused"), reader=lambda _: descriptor(owner=OTHER, aces=aces))
    assert error.value.code == "unsafe_permissions"
