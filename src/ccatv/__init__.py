from __future__ import annotations

import importlib.metadata as importlib_metadata
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppInfo:
    """Application metadata sourced from pyproject.toml."""

    name: str
    version: str
    description: str


def _git_root() -> Path | None:
    """Return the git repository root, or None if unavailable."""
    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return None
    return Path(root)


def _app_info_from_installed_metadata() -> AppInfo | None:
    """Read app metadata from installed package metadata when available."""
    try:
        package_metadata = importlib_metadata.metadata("ccatv")
        package_version = importlib_metadata.version("ccatv")
    except importlib_metadata.PackageNotFoundError:
        return None
    except Exception:
        return None

    return AppInfo(
        name=str(package_metadata.get("Name", "ccatv")),
        version=str(package_version),
        description=str(package_metadata.get("Summary", "")),
    )


def _find_pyproject() -> Path | None:
    """Locate pyproject.toml from git root or by walking parent directories."""
    root = _git_root()
    if root is not None:
        path = root / "pyproject.toml"
        if path.exists():
            return path

    current = Path(__file__).resolve()
    for parent in current.parents:
        path = parent / "pyproject.toml"
        if path.exists():
            return path
    return None


def get_app_info() -> AppInfo:
    """Get app name, version, and description from pyproject.toml."""
    installed = _app_info_from_installed_metadata()
    if installed is not None:
        return installed

    pyproject_path = _find_pyproject()
    if pyproject_path is None:
        return AppInfo(name="ccatv", version="0.0.0", description="")

    try:
        with open(pyproject_path, "rb") as file_obj:
            data = tomllib.load(file_obj)
        project = data.get("project", {})
        return AppInfo(
            name=str(project.get("name", "ccatv")),
            version=str(project.get("version", "0.0.0")),
            description=str(project.get("description", "")),
        )
    except Exception:
        return AppInfo(name="ccatv", version="0.0.0", description="")


def get_version() -> str:
    """Get project version from pyproject.toml, falling back safely."""
    return get_app_info().version


_APP_INFO = get_app_info()

__app_name__ = _APP_INFO.name
__version__ = _APP_INFO.version
__description__ = _APP_INFO.description

__all__ = [
    "AppInfo",
    "__app_name__",
    "__description__",
    "__version__",
    "get_app_info",
    "get_version",
]
