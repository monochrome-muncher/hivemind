"""Copy the plugin's `hivemind` skill and Stop reminder into the hivemind-memory packages.

Run from the repository root: ``make sync-client-text``.

An app built on the packages has no plugin, no hivemind-setup skill and no incognito launcher,
so the skill copy drops the lines about them (ADR 0163). Each replaced passage must appear in the
plugin's skill exactly once: when the skill changes under one, this script fails rather than ship
a copy that still names the plugin.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins" / "hivemind" / "skills" / "hivemind" / "SKILL.md"
STOP_HOOK = (
    ROOT / "plugins" / "hivemind" / "skills" / "hivemind-setup" / "scripts" / "hivemind-stop.sh"
)
TEXT_DIRS = (
    ROOT / "clients" / "python" / "src" / "hivemind_memory" / "text",
    ROOT / "clients" / "typescript" / "text",
)

PLUGIN_ONLY = "\n## 6. Incognito sessions\n"

APP_WORDING = (
    (
        "| No tool, or connection errors | Incognito session (§6)? Say nothing. Otherwise tell "
        "the user once, offer **hivemind-setup**, and work on. |",
        "| No tool, or connection errors | Tell the user once, and work on. |",
    ),
    (
        '| `key_kind: "org"` | Not registered or awaiting activation. Follow hivemind-setup, '
        '"Register": same name and owner alias as before; on `already_registered`, act on its '
        "status. No reads or writes yet. |",
        '| `key_kind: "org"` | Not an agent key, or the agent awaits activation. Tell the user '
        "that the app needs an activated agent key. No reads or writes yet. |",
    ),
    (
        'Stop calling `hive_*`, tell the user once, follow hivemind-setup, "Key rejected". '
        "Do not register again. |",
        "Stop calling `hive_*` and tell the user once: the app needs a new key. |",
    ),
    (
        "(tell the user; move the notes in and delete them once you can), and\n"
        "incognito notes. Never move a note marked\n"
        "`[hivemind: incognito, never upload]` into Hivemind.",
        "(tell the user; move the notes in and delete them once you can).",
    ),
    (
        "**Unavailable** (and not incognito): mention it once, keep helping, keep\n"
        "notes locally, write them when it is back. If no\n"
        "`HIVEMIND:` reminder starts your context and you have no plugin, offer\n"
        'hivemind-setup\'s "Stay aware" step.',
        "**Unavailable**: mention it once, keep helping, keep notes locally, write\n"
        "them when it is back.",
    ),
)


def client_skill(skill: str) -> str:
    """The plugin's skill as the packages ship it: without the plugin-only lines."""
    head, found, _ = skill.partition(PLUGIN_ONLY)
    if not found:
        raise ValueError(f"the skill has no {PLUGIN_ONLY.strip()!r} section")
    text = head.rstrip("\n") + "\n"
    for plugin, app in APP_WORDING:
        if text.count(plugin) != 1:
            raise ValueError(f"the skill no longer has, exactly once: {plugin[:60]!r}...")
        text = text.replace(plugin, app)
    return text


def reminder() -> str:
    match = re.search(r"^text='([^']*)'$", STOP_HOOK.read_text(), re.MULTILINE)
    if match is None:
        raise ValueError("the Stop hook's reminder line moved")
    return match.group(1)


def main() -> None:
    skill = client_skill(SKILL.read_text())
    for text_dir in TEXT_DIRS:
        (text_dir / "skill.md").write_text(skill)
        (text_dir / "reminder.txt").write_text(reminder())


if __name__ == "__main__":
    main()
