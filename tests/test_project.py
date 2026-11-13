"""Offline packaging, text hygiene, and import-isolation checks."""

import ast
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from covearchive import __version__


ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = (
    "pyproject.toml",
    ".gitignore",
    ".gitattributes",
    "LICENSE",
    "README.md",
    "src/covearchive/__init__.py",
    "src/covearchive/formats.py",
    "src/covearchive/github/__init__.py",
    "src/covearchive/github/urls.py",
    "tests/test_urls.py",
    "tests/test_formats.py",
    "tests/test_project.py",
)


def test_version():
    assert __version__ == "0.1.0.dev0"


def test_pyproject_bootstrap_contract():
    with (ROOT / "pyproject.toml").open("rb") as stream:
        config = tomllib.load(stream)
    assert config["build-system"] == {
        "requires": ["setuptools>=77"],
        "build-backend": "setuptools.build_meta",
    }
    project = config["project"]
    assert project["name"] == "covearchive"
    assert project["version"] == __version__
    assert project["requires-python"] == ">=3.12"
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert project["dependencies"] == ["requests>=2.32,<3"]
    assert project["optional-dependencies"]["test"] == ["pytest>=8"]
    assert project["readme"] == "README.md"
    assert isinstance(project["description"], str)
    assert 0 < len(project["description"]) < 120
    assert config["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]
    assert config["tool"]["pytest"]["ini_options"] == {
        "pythonpath": ["src"],
        "testpaths": ["tests"],
    }


def test_git_text_settings_are_exact():
    assert (ROOT / ".gitattributes").read_bytes() == b"* text=auto eol=lf\n"
    assert (ROOT / ".gitignore").read_bytes() == (
        b"__pycache__/\n"
        b".pytest_cache/\n"
        b"*.pyc\n"
        b"build/\n"
        b"dist/\n"
        b".venv/\n"
        b"*.egg-info/\n"
    )


@pytest.mark.parametrize("relative_path", ALLOWLIST)
def test_allowlisted_files_exist_are_utf8_and_use_lf(relative_path):
    path = ROOT / relative_path
    assert path.is_file()
    data = path.read_bytes()
    assert b"\r" not in data
    assert data.endswith(b"\n")
    assert data.decode("utf-8")


@pytest.mark.parametrize(
    "statement",
    [
        "local preservation of GitHub project releases",
        "pre-alpha, slice CA-S1",
        "bootstrap",
        "URL classifier",
        "format matcher",
        ".appimage",
        ".tar.gz",
        ".tar",
        ".dmg",
        ".zip",
        ".7z",
        "case-insensitively",
        "FUTURE",
        "not implemented",
        "official GitHub REST API",
        "per-release folders",
        "Resume",
        "Rescan",
        "without duplicating",
        "not a complete Git history backup",
        "downloads are never automatically executed or extracted",
        "python -m pytest",
    ],
)
def test_readme_required_statements(statement):
    assert statement in (ROOT / "README.md").read_text(encoding="utf-8")


def test_mit_license():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License\n")
    assert "Copyright (c) 2026 Cove" in text
    assert "Permission is hereby granted, free of charge" in text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in text


def test_imports_succeed_when_optional_third_party_modules_are_blocked():
    script = "\n".join(
        [
            "import sys",
            "from pathlib import Path",
            "assert sys.flags.isolated == 1",
            "sys.dont_write_bytecode = True",
            "src = Path(sys.argv[1]).resolve()",
            "sys.path.insert(0, str(src))",
            "sys.modules['requests'] = None",
            "sys.modules['PySide6'] = None",
            "import covearchive",
            "assert 'covearchive.formats' not in sys.modules",
            "assert 'covearchive.github' not in sys.modules",
            "import covearchive.github",
            "assert 'covearchive.github.urls' not in sys.modules",
            "import covearchive.formats",
            "import covearchive.github.urls",
            "for module in (covearchive, covearchive.formats, covearchive.github.urls):",
            "    assert Path(module.__file__).resolve().is_relative_to(src)",
            "assert sys.modules['requests'] is None",
            "assert sys.modules['PySide6'] is None",
            "assert covearchive.__version__ == '0.1.0.dev0'",
            "print('imports isolated')",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(ROOT / "src")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "imports isolated"


def test_source_imports_are_stdlib_or_own_package_only():
    paths = sorted((ROOT / "src").rglob("*.py"))
    assert paths
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    assert node.level <= 2, (path, node.lineno)
                    continue
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                root = module.split(".")[0]
                assert (
                    root in sys.stdlib_module_names
                    or root == "covearchive"
                    or (
                        root == "requests"
                        and path.relative_to(ROOT).as_posix()
                        == "src/covearchive/github/client.py"
                    )
                ), (
                    path.relative_to(ROOT), node.lineno, module
                )
