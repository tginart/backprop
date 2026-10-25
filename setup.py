"""
Purpose: Include the dashboard in wheels without duplicate source assets.
Input: The reviewed static web files from a source checkout or sdist.
Output: Dashboard resources inside the installed backend package.
"""
from pathlib import Path
import json
import shutil

from setuptools import find_packages, setup
from setuptools.command.build_py import build_py


ROOT = Path(__file__).resolve().parent


class BuildDashboard(build_py):
    """
    Purpose: Copy static assets into the package build output.
    Input: The web source directory.
    Output: A wheel that serves its dashboard without the source checkout.
    """

    def run(self) -> None:
        """
        Purpose: Build the SDK and copy only dashboard resource files.
        Input: The package build configuration and static web source.
        Output: An installed package resource directory without operator config.
        """
        super().run()
        destination = Path(self.build_lib) / "backprop_backend" / "dashboard"
        if destination.is_dir():
            shutil.rmtree(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for source in _asset_sources():
            target = destination / source.relative_to(ROOT / "web")
            target.parent.mkdir(parents=True, exist_ok=True)
            self.copy_file(str(source), str(target))
        if not (destination / "index.html").is_file():
            raise ValueError("Dashboard index.html is absent from the source archive")

    def get_source_files(self) -> list[str]:
        return [*super().get_source_files(),
                *[str(path.relative_to(ROOT)) for path in _asset_sources()]]


def _asset_sources() -> list[Path]:
    """
    Purpose: Include only the reviewed dashboard asset list in distributions.
    Input: The package asset manifest and web directory.
    Output: Regular source files without operator configuration or symlinks.
    """
    manifest = ROOT / "backend" / "dashboard_assets.json"
    names = json.loads(manifest.read_text())
    if names != sorted(set(names)):
        raise ValueError("Dashboard assets must be sorted and unique")
    sources = []
    for name in names:
        source = ROOT / "web" / name
        valid_name = (name.startswith("backends/") and name.endswith(".js")
                      or Path(name).name == name)
        if (not valid_name or Path(name).is_absolute() or name.startswith(".")
                or ".." in Path(name).parts or source.is_symlink()
                or not source.is_file()):
            raise ValueError("Dashboard assets must be regular source files")
        sources.append(source)
    return sources


setup(
    packages=[*find_packages("sdk", include=["backprop", "backprop.*"]),
              "backprop_backend",
              "backprop_backend.local", "backprop_backend.foundry"],
    package_dir={"": "sdk", "backprop_backend": "backend"},
    package_data={"backprop_backend": ["dashboard_assets.json"]},
    cmdclass={"build_py": BuildDashboard},
)
