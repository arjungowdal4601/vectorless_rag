"""Reproducibility and repository-policy checks for project metadata."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]
EXACT_REQUIREMENT = re.compile(r"(?P<name>[A-Za-z0-9_-]+)==(?P<version>[^;\s]+)\Z")


def _load_toml(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_runtime_dependencies_are_exact_and_match_lock() -> None:
    project = _load_toml(ROOT / "pyproject.toml")["project"]
    assert isinstance(project, dict)
    dependencies = project["dependencies"]
    assert isinstance(dependencies, list)

    expected: dict[str, str] = {}
    for requirement in dependencies:
        assert isinstance(requirement, str)
        match = EXACT_REQUIREMENT.fullmatch(requirement)
        assert match is not None, f"runtime dependency is not exact: {requirement}"
        expected[match.group("name").replace("_", "-").lower()] = match.group("version")

    lock = _load_toml(ROOT / "uv.lock")
    packages = lock["package"]
    assert isinstance(packages, list)
    locked = {
        package["name"]: package["version"]
        for package in packages
        if isinstance(package, dict) and "version" in package
    }
    assert {name: locked[name] for name in expected} == expected
    assert expected["deepagents"] == "0.7.9"


def test_package_targets_python_312_and_installs_a_cli() -> None:
    metadata = _load_toml(ROOT / "pyproject.toml")
    build_system = metadata["build-system"]
    project = metadata["project"]
    assert isinstance(build_system, dict)
    assert isinstance(project, dict)
    assert build_system["requires"] == ["hatchling==1.32.0"]
    assert project["requires-python"] == ">=3.12,<3.13"
    assert project["scripts"] == {"document-processing": "document_processing.__main__:main"}


def test_make_commands_use_the_committed_lock() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "$(UV) sync --locked --all-groups" in makefile
    run_commands = [line.strip() for line in makefile.splitlines() if "$(UV) run" in line]
    assert run_commands
    assert all(command.startswith("$(UV) run --locked ") for command in run_commands)


def test_code_and_configuration_files_stay_within_line_limit() -> None:
    paths = [
        *ROOT.glob("src/**/*.py"),
        *ROOT.glob("tests/**/*.py"),
        ROOT / "pyproject.toml",
        ROOT / "Makefile",
        ROOT / ".env.example",
    ]
    oversized = {
        str(path.relative_to(ROOT)): len(path.read_text(encoding="utf-8").splitlines())
        for path in paths
        if len(path.read_text(encoding="utf-8").splitlines()) > 350
    }
    assert oversized == {}
