"""Check the built sdist and wheel before they go to PyPI.

Both must carry the same version, that version must be the `git describe` one for this checkout,
it must be a plain <X>.<Y>.<N> (no `+dirty` or other local part, which PyPI refuses), and the wheel
must contain the skill text the guide tool reads. Run: uv run python scripts/check_dist.py dist
"""

import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(dist: Path) -> int:
    wheels = sorted(dist.glob("fpgas_online_mcp-*.whl"))
    sdists = sorted(dist.glob("fpgas_online_mcp-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        print(f"expected one wheel and one sdist in {dist}, found {wheels + sdists}", file=sys.stderr)
        return 1
    wheel_version = wheels[0].name.split("-")[1]
    sdist_version = sdists[0].name.removesuffix(".tar.gz").split("-")[1]
    described = subprocess.run(
        ["git", "-C", str(ROOT), "describe", "--tags", "--long", "--match", "v[0-9]*", "--dirty"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    m = re.fullmatch(r"v(\d+)\.(\d+)-(\d+)-g[0-9a-f]+", described)
    problems = []
    if not m:
        problems.append(f"git describe gave {described!r}: a dirty tree or no v<X>.<Y> tag")
    else:
        expected = f"{int(m[1])}.{int(m[2])}.{int(m[3])}"
        for kind, got in (("wheel", wheel_version), ("sdist", sdist_version)):
            if got != expected:
                problems.append(f"{kind} version {got} is not {expected} (from {described})")
    with zipfile.ZipFile(wheels[0]) as z:
        if "fpgas_online_mcp/skill/SKILL.md" not in z.namelist():
            problems.append("the wheel has no fpgas_online_mcp/skill/SKILL.md")
    with tarfile.open(sdists[0]) as t:
        names = {n.split("/", 1)[1] for n in t.getnames() if "/" in n}
        for needed in ("build_version.py", "PKG-INFO", "skills/fpgas-online/SKILL.md"):
            if needed not in names:
                problems.append(f"the sdist has no {needed}")
    for p in problems:
        print(p, file=sys.stderr)
    if not problems:
        print(f"ok: {wheels[0].name} and {sdists[0].name}, version {wheel_version} from {described}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "dist")))
