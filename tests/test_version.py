"""One CalVer string, four places that used to disagree.

`--version` reported v26.08.22.3 while the package was at .21 and the PWA's
status endpoint said something else again, so nobody could tell what was
actually running on a host.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from click.testing import CliRunner

import agy_remote
import agy_remote.cli  # noqa: F401
from agy_remote.version import VERSION
from agy_remote.version import __version__ as pkg_version

cli_mod = sys.modules["agy_remote.cli"]

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def _numeric(version: str) -> tuple[int, ...]:
    """CalVer as digits, so `26.08.22.22` and PEP 440's `26.8.22.22` compare."""
    return tuple(int(part) for part in version.lstrip("v").split("."))


def test_version_is_calver():
    assert re.fullmatch(r"\d{2}\.\d{2}\.\d{2}\.\d+", VERSION), VERSION
    assert pkg_version == f"v{VERSION}"


def test_pyproject_agrees_with_the_package():
    declared = re.search(r'^version = "([^"]+)"', PYPROJECT.read_text(), re.M).group(1)
    assert _numeric(declared) == _numeric(VERSION)


def test_package_exports_one_version():
    assert agy_remote.__version__ == pkg_version


def test_cli_reports_the_package_version():
    res = CliRunner().invoke(cli_mod.cli, ["--version"])
    assert res.output.strip() == f"agy-remote {pkg_version}"


def test_server_reports_the_package_version():
    from agy_remote.server import VERSION as SERVER_VERSION

    assert SERVER_VERSION == VERSION


def test_changelog_documents_this_version():
    changelog = (Path(__file__).parent.parent / "CHANGELOG.md").read_text()
    assert f"## v{VERSION}" in changelog
