"""Validate the immutable commit and release version before any build job."""
import os
from pathlib import Path
import re
import runpy
import tomllib

expected = os.environ["EXPECTED_COMMIT"]
if not re.fullmatch(r"[0-9a-fA-F]{40}", expected) or os.environ["GITHUB_SHA"] != expected:
    raise SystemExit("Expected commit does not match github.sha")
if os.environ["GITHUB_REF"] != "refs/heads/" + os.environ["DEFAULT_BRANCH"]:
    raise SystemExit("Releases must run from the default branch")
if (runpy.run_path("src/covearchive/__init__.py")["__version__"] != "1.0.1"
        or tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"] != "1.0.1"):
    raise SystemExit("Release version mismatch")
