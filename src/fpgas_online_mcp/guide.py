"""The `guide` tool's text, condensed from the stage 1 skill files shipped in the package.

The wheel carries `skills/fpgas-online/**` as `fpgas_online_mcp/skill/` (pyproject force-include);
a source checkout reads the repo's `skills/fpgas-online/` directly. One source of truth.
"""

import re
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

TOPICS = ("boards", "ssh", "power", "in-use", "tinytapeout")
LIMIT = 4096
SOURCE_URL = "https://github.com/fpgas-online/fpgas.online-mcp/blob/main/skills/fpgas-online"
DOCS_URL = "https://docs.fpgas.online/en/latest/user/index.html"

TOOLS_NOTE = (
    "With the fpgas-online MCP tools: list_boards and board_status to look, claim_board before anything "
    "that disturbs a board, then connect / run / power_cycle with that claim, and release_board when done. "
    "The shell commands below are for reference; the tools do the same with shared connections and claims."
)

_DOCS = re.compile(r"https://docs\.fpgas\.online/\S*[\w/]")


def skill_dir() -> Traversable:
    packaged = resources.files("fpgas_online_mcp") / "skill"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "skills" / "fpgas-online"


def _strip_front_matter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + len("\n---\n") :].lstrip("\n")
    return text


def _blocks(text: str) -> list[str]:
    """Paragraphs split on blank lines, keeping each fenced code block whole."""
    blocks, current, fenced = [], [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not line.strip() and not fenced:
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _size(text: str) -> int:
    return len(text.encode())


def _fit(head: str, body: str, footer: str, trimmed_note: str) -> str:
    """head + as many whole blocks of body as fit + footer, at most LIMIT bytes."""
    parts = [head]
    blocks = _blocks(body)
    for i, block in enumerate(blocks):
        rest = footer if i == len(blocks) - 1 else trimmed_note + footer
        if _size("\n\n".join([*parts, block]) + rest) > LIMIT:
            text = "\n\n".join(parts) + trimmed_note + footer
            return text if _size(text) <= LIMIT else text.encode()[:LIMIT].decode(errors="ignore")
        parts.append(block)
    return "\n\n".join(parts) + footer


def text(topic: str | None = None) -> str:
    """The guide text for no topic (the skill summary) or one of TOPICS."""
    root = skill_dir()
    if topic is None:
        body = _strip_front_matter((root / "SKILL.md").read_text())
        footer = (
            f"\n\nFor one topic call guide(topic) with one of: {', '.join(TOPICS)}. Full skill: {SOURCE_URL}/SKILL.md"
        )
        return _fit(TOOLS_NOTE, body, footer, "\n\n[… trimmed]")
    if topic not in TOPICS:
        raise ValueError(f"unknown topic {topic!r}; topics are {', '.join(TOPICS)}")
    body = (root / "references" / f"{topic}.md").read_text()
    url = f"{SOURCE_URL}/references/{topic}.md"
    docs = _DOCS.search(body)
    footer = f"\n\nFull reference: {url}\nDocs: {docs[0] if docs else DOCS_URL}"
    return _fit(TOOLS_NOTE, body, footer, f"\n\n[… trimmed to fit; read the rest at {url}]")
