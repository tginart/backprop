"""
Purpose: Share the reviewed dashboard asset boundary across both backends.
Input: The asset manifest and optional source checkout root.
Output: Public static files from a source checkout or installed package.
Note: Connection files, credentials, and operator state are never static assets.
"""
from __future__ import annotations

import json
from pathlib import Path


def asset_names() -> list[str]:
    names = json.loads(Path(__file__).with_name("dashboard_assets.json").read_text())
    if names != sorted(set(names)):
        raise ValueError("Dashboard assets must be sorted and unique")
    return names


def source_asset(name: str, root: Path) -> Path:
    return root / "web" / name


def asset_path(name: str) -> Path:
    """
    Purpose: Resolve an allowlisted file without an installed checkout dependency.
    Input: One public asset path from the reviewed manifest.
    Output: A packaged resource or its source development file.
    """
    if name not in asset_names():
        raise FileNotFoundError("Unknown dashboard asset")
    resources = Path(__file__).resolve().parent
    packaged = resources.joinpath("dashboard", name)
    if packaged.is_file():
        return packaged
    source = source_asset(name, Path(__file__).resolve().parents[1])
    if source.is_file() and not source.is_symlink():
        return source
    raise FileNotFoundError("Dashboard assets are absent; reinstall the Backprop package")
