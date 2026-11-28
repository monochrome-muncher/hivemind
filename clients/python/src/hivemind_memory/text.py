"""The texts shipped with the package: the agent instructions, the end-of-task reminder and the
line for after a compaction."""

from __future__ import annotations

from importlib import resources

_FRONT_MATTER_END = "\n---\n"


def _read(name: str) -> str:
    return (resources.files("hivemind_memory") / "text" / name).read_text(encoding="utf-8")


def skill_body() -> str:
    """The skill without its YAML front matter."""
    skill = _read("skill.md")
    if not skill.startswith("---\n"):
        return skill.strip()
    _, _, body = skill.partition(_FRONT_MATTER_END)
    return body.strip()


REMINDER = _read("reminder.txt")
COMPACTED = _read("compacted.txt")


INSTRUCTIONS = f"{_read('instructions.txt').strip()}\n\n{skill_body()}"
