"""Structure tests for the fpgas-online Claude Code plugin and its skill."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = ROOT / "skills" / "fpgas-online"
SKILL_MD = SKILL_DIR / "SKILL.md"
REFERENCES = SKILL_DIR / "references"
EXPECTED_REFERENCES = {"boards.md", "ssh.md", "power.md", "in-use.md", "tinytapeout.md"}
REFERENCE_LINK = re.compile(r"references/([\w.-]+\.md)")


def split_skill():
    """Return (front matter dict, body text); front matter is parsed by hand."""
    lines = SKILL_MD.read_text().splitlines()
    assert lines[0] == "---", "SKILL.md must start with front matter"
    end = lines.index("---", 1)
    meta = {}
    for line in lines[1:end]:
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, "\n".join(lines[end + 1 :])


def skill_files():
    return [p for p in (ROOT / "skills").rglob("*") if p.is_file()]


def test_plugin_manifest():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "fpgas-online"
    assert manifest["description"]
    assert manifest["version"]
    assert manifest["license"] == "Apache-2.0"


def test_marketplace_lists_plugin():
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert len(marketplace["plugins"]) == 1
    plugin = marketplace["plugins"][0]
    assert plugin["name"] == "fpgas-online"
    assert plugin["source"] == "./"


def test_skill_front_matter():
    meta, _ = split_skill()
    assert meta["name"] == "fpgas-online"
    assert 1 <= len(meta["description"]) <= 1024
    assert "fpgas.online" in meta["description"]


def test_skill_body_is_short():
    _, body = split_skill()
    assert len(body.splitlines()) <= 150


def test_references_all_linked_and_present():
    _, body = split_skill()
    linked = set(REFERENCE_LINK.findall(body))
    present = {p.name for p in REFERENCES.glob("*.md")}
    assert linked == present == EXPECTED_REFERENCES


def test_references_do_not_chain():
    for path in REFERENCES.glob("*.md"):
        assert not REFERENCE_LINK.search(path.read_text()), f"{path.name} links to another reference"


def shipped_files():
    """Skill files plus the package sources: both are scanned for forbidden strings."""
    return skill_files() + [p for p in (ROOT / "src").rglob("*") if p.is_file() and "__pycache__" not in p.parts]


def test_no_stderr_discard():
    for path in shipped_files():
        assert "2>/dev/null" not in path.read_text(), path


def test_no_operator_paths():
    pattern = re.compile(r"10\.21\.|tweed|:8765")
    for path in shipped_files():
        assert not pattern.search(path.read_text()), path


def markdown_files():
    return [SKILL_MD, *sorted(REFERENCES.glob("*.md"))]


SH_BLOCK = re.compile(r"```sh\n(.*?)```", re.S)
ALIAS_USE = re.compile(r"(?<![\w.-])fo-[A-Za-z0-9<>$_-]*[A-Za-z0-9<>$_]")
ALIAS_DEF = re.compile(r"^Host (fo-[A-Za-z0-9_-]+)$", re.M)
# the per-host rule in ssh.md: fo-ps1-pi<N>, fo-welland-pi-sw<S>-p<P>, written out or as placeholders
ALIAS_RULE = re.compile(r"fo-(ps1-pi(\d+|<N>|\$N)|welland-pi-sw(\d+|<S>)-p(\d+|<P>)|<site>-<host>)")


def test_aliases_follow_ssh_md():
    defined = set(ALIAS_DEF.findall((REFERENCES / "ssh.md").read_text()))
    assert defined, "ssh.md defines no fo- alias"
    for path in markdown_files():
        for alias in ALIAS_USE.findall(path.read_text()):
            assert ALIAS_RULE.fullmatch(alias), f"{path.name}: {alias} does not follow the alias rule"
            if not re.search(r"[<$]", alias):
                assert alias in defined, f"{path.name}: {alias} is used but not defined in ssh.md"


def test_curl_examples_send_user_agent():
    lines = []
    for path in markdown_files():
        text = path.read_text()
        for block in SH_BLOCK.findall(text):
            lines += [(path.name, line) for line in block.splitlines() if re.search(r"\bcurl\b", line)]
        lines += [(path.name, span) for span in re.findall(r"`(curl [^`]*https?://[^`]*)`", text)]
    assert lines
    for name, line in lines:
        assert "-A fpgas-online-skill" in line, f"{name}: {line}"
