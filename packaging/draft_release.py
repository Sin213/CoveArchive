"""Fail-closed draft creation and asset verification; never publishes a release."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

TAG = "v1.0.0"
NAMES = {"CoveArchive-1.0.0-Setup.exe", "CoveArchive-1.0.0-Portable.exe",
         "CoveArchive-1.0.0-x86_64.AppImage", "covearchive_1.0.0_amd64.deb"}


def verify_release(release, expected, commit, tag_exists, receipt_path=None):
    if tag_exists or release.get("tag_name") != TAG or release.get("draft") is not True:
        raise ValueError("tag exists or release is not the expected draft")
    if release.get("target_commitish") != commit:
        raise ValueError("draft targets another commit")
    assets = release.get("assets", [])
    if len(assets) != len(expected) or {a["name"] for a in assets} != set(expected):
        raise ValueError("draft asset set mismatch")
    for asset in assets:
        wanted = expected[asset["name"]]
        if asset.get("size") != wanted["size"] or asset.get("sha256") != wanted["sha256"]:
            raise ValueError("draft asset size or sha256 mismatch")
    receipt = {"url": release["html_url"], "commit": commit, "assets": expected}
    if receipt_path:
        Path(receipt_path).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def command(*args):
    return subprocess.run(args, check=True, capture_output=True).stdout


def api(endpoint):
    return json.loads(command("gh", "api", endpoint))


def checksums(directory, outputs):
    directory = Path(directory)
    files = {p.name: p for p in directory.iterdir() if p.is_file()}
    # Generated checksums are outputs only; reruns require a fresh input directory.
    if set(files) != NAMES | {"windows.sha256", "linux.sha256"}:
        raise ValueError("unexpected artifact input set")
    recorded = {}
    for label in ("windows", "linux"):
        text = files[label + ".sha256"].read_text()
        if not outputs.get(label) or text.splitlines() != outputs[label].splitlines():
            raise ValueError("build job output does not match checksum sidecar")
        for line in text.splitlines():
            digest, name = line.split(maxsplit=1); name = name.lstrip(" *")
            if name in recorded or name not in NAMES or not re.fullmatch("[a-fA-F0-9]{64}", digest):
                raise ValueError("invalid or duplicate build checksum")
            recorded[name] = digest.lower()
    if set(recorded) != NAMES:
        raise ValueError("partial checksum set")
    for name, digest in recorded.items():
        if hashlib.sha256(files[name].read_bytes()).hexdigest() != digest:
            raise ValueError("artifact checksum mismatch")
    sums = []
    for name in sorted(NAMES):
        checksum = directory / (name + ".sha256")
        checksum.write_bytes(f"{recorded[name]}  {name}\n".encode("utf-8"))
        sums.append(checksum)
    return {p.name: {"size": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in [*(files[n] for n in sorted(NAMES)), *sums]}


def create_or_reuse_draft(directory):
    expected = checksums(directory, {label: os.environ.get(label.upper() + "_SHA256", "") for label in ("windows", "linux")})
    repo, commit = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("invalid commit")
    prefix = f"repos/{repo}"
    def tag_exists():
        return any(r["ref"] == f"refs/tags/{TAG}" for r in api(prefix + f"/git/matching-refs/tags/{TAG}"))
    if tag_exists(): raise ValueError("remote tag already exists")
    pages = json.loads(command("gh", "api", "--paginate", "--slurp", prefix + "/releases"))
    matches = [r for page in pages for r in page if r["tag_name"] == TAG]
    if len(matches) > 1: raise ValueError("ambiguous release set")
    if matches:
        release = matches[0]
        if release["draft"] is not True or release["target_commitish"] != commit:
            raise ValueError("existing release is published or targets another commit")
        if release["assets"]:
            raise ValueError("delete the draft and rerun")
    else:
        command("gh", "release", "create", TAG, "--draft", "--target", commit,
                "--title", "CoveArchive 1.0.0", "--notes-file", "packaging/release-notes.md")
        pages = json.loads(command("gh", "api", "--paginate", "--slurp", prefix + "/releases"))
        matches = [r for page in pages for r in page if r["tag_name"] == TAG]
        if len(matches) != 1: raise ValueError("created draft not uniquely visible")
        release = matches[0]
    # A concurrent publisher can only receive new assets: uploads never replace
    # or delete, and the final draft check detects publication during upload.
    release = api(prefix + f"/releases/{release['id']}")
    if tag_exists() or release["draft"] is not True or release["target_commitish"] != commit:
        raise ValueError("draft changed before upload")
    if release["assets"]:
        raise ValueError("delete the draft and rerun")
    command("gh", "release", "upload", TAG, *(str(Path(directory) / n) for n in sorted(expected)))
    pages = json.loads(command("gh", "api", "--paginate", "--slurp", prefix + "/releases"))
    matches = [r for page in pages for r in page if r["tag_name"] == TAG]
    if len(matches) != 1 or matches[0]["id"] != release["id"]:
        raise ValueError("release identity changed after upload")
    release = matches[0]
    for asset in release["assets"]:
        content = command("gh", "api", prefix + f"/releases/assets/{asset['id']}", "-H", "Accept: application/octet-stream")
        if len(content) != asset["size"]: raise ValueError("downloaded asset size mismatch")
        asset["sha256"] = hashlib.sha256(content).hexdigest()
    receipt = verify_release(release, expected, commit, tag_exists(), "dist/release-receipt.json")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write("Draft verification receipt\n\n```json\n" + json.dumps(receipt, indent=2) + "\n```\n")


if __name__ == "__main__":
    create_or_reuse_draft(sys.argv[1])
