"""The package version, from `git describe` (read by hatch at build time; see pyproject.toml).

`git describe --tags --long` on main gives `v<X>.<Y>-<N>-g<hash>`, N being the number of commits
since the `v<X>.<Y>` tag (the `v0.0` tag sits on the first commit). The version is `<X>.<Y>.<N>`:
every commit on main has more commits behind it than the one before, so every push to main gets a
new, larger number, which is what PyPI needs (it never accepts the same version twice). A tree with
uncommitted changes gets `+dirty` (a local version, which PyPI refuses: such builds are never
published).

An sdist has no .git: the wheel built from it takes the version its PKG-INFO recorded.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DESCRIBE = re.compile(r"v(?P<x>\d+)\.(?P<y>\d+)-(?P<n>\d+)-g[0-9a-f]+(?P<dirty>-dirty)?")


def from_describe(described: str) -> str:
    """'v0.0-80-g29a5a1a' -> '0.0.80'; 'v0.0-80-g29a5a1a-dirty' -> '0.0.80+dirty'."""
    m = DESCRIBE.fullmatch(described.strip())
    if not m:
        raise ValueError(f"git describe gave {described.strip()!r}, not v<X>.<Y>-<N>-g<hash>")
    return f"{int(m['x'])}.{int(m['y'])}.{int(m['n'])}" + ("+dirty" if m["dirty"] else "")


def from_pkg_info(root: Path) -> str | None:
    pkg_info = root / "PKG-INFO"
    if not pkg_info.is_file():
        return None
    for line in pkg_info.read_text().splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    return None


def get_version(root: Path = ROOT) -> str:
    if (recorded := from_pkg_info(root)) is not None:
        return recorded
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "describe", "--tags", "--long", "--match", "v[0-9]*", "--dirty"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, "stderr", "") or str(e)
        raise RuntimeError(
            "the version comes from `git describe`, which needs the git history and its v0.0 tag "
            f"(a shallow clone has neither; fetch with tags, or install from PyPI): {detail.strip()}"
        ) from e
    return from_describe(out)


__version__ = get_version()
