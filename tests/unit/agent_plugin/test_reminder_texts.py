"""Reminder texts: every copy, as its runtime sees it, is the canonical one."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from tests.unit.agent_plugin.common import (
    CLINE_SHORT_LINE,
    COMPACTED_TEXT,
    CONFIGURED_TEXT,
    INCOGNITO_TEXT,
    PLUGIN,
    STAY_AWARE_CORE,
    STOP_REMINDER_TEXT,
)

NOT_CONFIGURED_TEXT = (
    "HIVEMIND_API_KEY is not set in this environment, so Hivemind is "
    "probably not connected. If the hive_* tools are missing or fail, "
    "tell the user once and offer to run the hivemind-setup skill."
)


def _py_literals(src: str) -> list[str]:
    """Python: the parser already joins adjacent literals; walk every str."""
    return [
        node.value
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "`": "`"}


def _js_literals(src: str) -> list[str]:
    """JS/TS: quoted strings, with the literals a `+` chains into one value.
    Comments and regex literals (which may hold a quote) are skipped, and so
    is a template literal's `${ }` code; a template with one is not a text."""
    out: list[str] = []
    joined: list[str] = []  # the literals chained so far by `+`
    chained = False  # a `+` came since the last literal

    def string(i: int) -> tuple[str, int, bool]:
        """Read the literal that opens at src[i]; return its value, the index
        after it and whether it holds a `${ }`."""
        quote, buf, dynamic = src[i], [], False
        i += 1
        while src[i] != quote:
            if src[i] == "\\":
                buf.append(_JS_ESCAPES[src[i + 1]])
                i += 2
            elif quote == "`" and src.startswith("${", i):
                dynamic, depth, i = True, 1, i + 2
                while depth:
                    if src[i] in "\"'`":
                        i = string(i)[1]
                        continue
                    depth += {"{": 1, "}": -1}.get(src[i], 0)
                    i += 1
            else:
                buf.append(src[i])
                i += 1
        return "".join(buf), i + 1, dynamic

    def flush() -> None:
        nonlocal joined
        if joined:
            out.append("".join(joined))
            joined = []

    i, prev = 0, ""  # prev: the last significant character
    while i < len(src):
        c = src[i]
        if src.startswith("//", i):
            end = src.find("\n", i)
            i = len(src) if end < 0 else end
        elif src.startswith("/*", i):
            i = src.index("*/", i) + 2
        elif c in "\"'`":
            value, i, dynamic = string(i)
            if not chained:
                flush()
            if not dynamic:
                joined.append(value)
            chained, prev = False, c
        elif c == "/" and (prev == "" or prev in "(,=:[!&|?{};"):  # a regex literal
            i += 1
            in_class = False
            while in_class or src[i] != "/":
                in_class = {"[": True, "]": False}.get(src[i], in_class)
                i += 2 if src[i] == "\\" else 1
            i += 1
            prev = "/"
        else:
            if c == "+":
                chained = True
            elif not c.isspace():
                chained = False
                flush()
                prev = c
            i += 1
    flush()
    return out


def _mdc_literals(src: str) -> list[str]:
    """A Cursor rule (.mdc): the text after the frontmatter is the one literal."""
    _, front, body = src.split("---\n", 2)
    assert "alwaysApply: true" in front
    return [body.strip()]


def _sh_literals(src: str) -> list[str]:
    """POSIX shell: single quotes are verbatim; double quotes drop the
    backslash before $ ` " \\; `$( )` bodies are scanned for nested strings.
    Variable references stay in the text as written."""
    out: list[str] = []

    def scan(i: int, closer: str) -> int:
        while i < len(src) and src[i] != closer:
            c = src[i]
            if c == "#" and (i == 0 or src[i - 1] in " \t\n;"):
                end = src.find("\n", i)
                i = len(src) if end < 0 else end
            elif c == "\\":
                i += 2
            elif c == "'":
                end = src.index("'", i + 1)
                out.append(src[i + 1 : end])
                i = end + 1
            elif c == '"':
                i = dq(i + 1)
            elif src.startswith("$(", i):
                i = scan(i + 2, ")") + 1
            else:
                i += 1
        return i

    def dq(i: int) -> int:
        buf: list[str] = []
        while src[i] != '"':
            if src[i] == "\\" and src[i + 1] in '$`"\\':
                buf.append(src[i + 1])
                i += 2
            elif src.startswith("$(", i):
                i = scan(i + 2, ")") + 1  # nested strings are not part of this text
                buf.append("$(...)")
            else:
                buf.append(src[i])
                i += 1
        out.append("".join(buf))
        return i + 1

    scan(0, "\0")
    return out


_PS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0"}


def _ps_literals(src: str) -> list[str]:
    """PowerShell: single quotes verbatim with '' for a quote; double quotes
    with `` ` `` escapes and "" for a quote; `#` and `<# #>` comments."""
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("<#", i):
            i = src.index("#>", i) + 2
        elif c == "#":
            end = src.find("\n", i)
            i = n if end < 0 else end
        elif c in "'\"":
            j, buf = i + 1, []
            while True:
                if src[j] == c and src[j + 1 : j + 2] == c:
                    buf.append(c)
                    j += 2
                elif src[j] == c:
                    break
                elif c == '"' and src[j] == "`":
                    buf.append(_PS_ESCAPES.get(src[j + 1], src[j + 1]))
                    j += 2
                else:
                    buf.append(src[j])
                    j += 1
            out.append("".join(buf))
            i = j + 1
        else:
            i += 1
    return out


def _md_paragraphs(src: str) -> list[str]:
    """Markdown: each paragraph is one text (the Junie files are plain prose)."""
    return [p.strip() for p in src.split("\n\n") if p.strip()]


def _json_leaves(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [leaf for v in value.values() for leaf in _json_leaves(v)]
    return []


def _runtime_texts(path: Path) -> list[str]:
    """Every string a file's code builds, as its runtime sees it, with a JSON
    document inside a literal (a hook's stdout) decoded to its string values
    and trailing variable references dropped."""
    extract = {
        ".py": _py_literals,
        ".js": _js_literals,
        ".ts": _js_literals,
        ".ps1": _ps_literals,
        ".sh": _sh_literals,
        ".mdc": _mdc_literals,
        ".md": _md_paragraphs,  # the Junie guidelines and incognito files
        "": _sh_literals,  # the Cline Unix hooks have no extension
    }[path.suffix]
    texts: list[str] = []
    for literal in extract(path.read_text()):
        try:  # a literal that is no whole document (a template half) stays text
            doc = json.JSONDecoder().raw_decode(literal.removesuffix("\\n"))[0]
        except json.JSONDecodeError:
            texts.append(literal)
        else:
            texts += _json_leaves(doc) if isinstance(doc, dict) else [literal]
    return [re.sub(r"(\s?\$\{?\w+\}?)+$", "", t) for t in texts]


# reminder -> (canonical text, words every copy opens with, files holding a copy)
_SKILL_SCRIPTS = Path("skills", "hivemind-setup", "scripts")


_PI_TS = Path("extensions", "hivemind.ts")


_JUNIE_GUIDELINES = Path("guidelines", "hivemind.md")


_JUNIE_INCOGNITO = Path("junie", "incognito.md")


_OPENCODE = Path("opencode", "hivemind.js")


REMINDER_COPIES: dict[str, tuple[str, str, tuple[Path, ...]]] = {
    "session line": (
        STAY_AWARE_CORE,
        "HIVEMIND: your organization",
        (
            _PI_TS,
            _OPENCODE,
            Path("__init__.py"),
            _SKILL_SCRIPTS / "hivemind-session-start.sh",
            Path("cline", "UserPromptSubmit"),
            Path("cline", "UserPromptSubmit.ps1"),
            Path("cursor", "rules", "hivemind.mdc"),
            _JUNIE_GUIDELINES,
        ),
    ),
    "incognito line": (
        INCOGNITO_TEXT,
        "HIVEMIND: this is an incognito",
        (
            _PI_TS,
            _OPENCODE,
            Path("__init__.py"),
            _SKILL_SCRIPTS / "hivemind-session-start.sh",
            Path("cline", "UserPromptSubmit"),
            Path("cline", "UserPromptSubmit.ps1"),
            _JUNIE_INCOGNITO,
        ),
    ),
    "stop reminder": (
        STOP_REMINDER_TEXT,
        "HIVEMIND: before finishing",
        (
            _PI_TS,
            _OPENCODE,
            Path("dsh", "hivemind-reminders.js"),
            _SKILL_SCRIPTS / "hivemind-stop.sh",
            Path("cline", "PostToolUse"),
            Path("cline", "PostToolUse.ps1"),
        ),
    ),
    "compaction line": (
        COMPACTED_TEXT,
        "Context was just compacted",
        (_PI_TS, _OPENCODE, _SKILL_SCRIPTS / "hivemind-session-start.sh"),
    ),
    "configured line": (
        CONFIGURED_TEXT,
        "Hivemind is configured. Your first action",
        (_PI_TS, _OPENCODE, Path("__init__.py"), _SKILL_SCRIPTS / "hivemind-session-start.sh"),
    ),
    "not-configured line": (
        NOT_CONFIGURED_TEXT,
        "HIVEMIND_API_KEY is not set in this",
        (
            _PI_TS,
            _OPENCODE,
            Path("__init__.py"),
            _SKILL_SCRIPTS / "hivemind-session-start.sh",
            Path("cline", "UserPromptSubmit"),
            Path("cline", "UserPromptSubmit.ps1"),
        ),
    ),
    # Cline's second and later prompts of a task (its CONFIGURED line is the
    # short "Hivemind is configured." by design, so it is not a copy of the above).
    "short line": (
        CLINE_SHORT_LINE,
        "HIVEMIND: follow the hivemind skill",
        (Path("cline", "UserPromptSubmit"), Path("cline", "UserPromptSubmit.ps1")),
    ),
}


_REMINDER_CASES = [
    pytest.param(canonical, opening, rel, id=f"{name}-{rel.as_posix()}")
    for name, (canonical, opening, files) in REMINDER_COPIES.items()
    for rel in files
]


@pytest.mark.parametrize(("canonical", "opening", "rel"), _REMINDER_CASES)
def test_every_copy_of_a_reminder_text_is_the_canonical_text(
    canonical: str, opening: str, rel: Path
) -> None:
    """The copy in each file, evaluated as its runtime would, is the canonical
    text: not just contained in the file's source (a split literal, a
    concatenation or a quoting slip would hide from that)."""
    copies = [t for t in _runtime_texts(PLUGIN / rel) if opening in t]
    assert copies, f"{rel} has no string starting {opening!r}"
    assert all(t == canonical for t in copies), (rel, copies)


def test_no_plugin_file_holds_an_unchecked_copy_of_a_reminder_text() -> None:
    """A new file that carries a reminder's opening words must be added to
    REMINDER_COPIES, so a future copy cannot go unchecked. Markdown is prose
    that quotes the openers, not a copy a harness shows, unless a harness reads
    the whole file (the Junie guidelines), which REMINDER_COPIES then lists."""
    listed = {(opening, rel) for _, opening, files in REMINDER_COPIES.values() for rel in files}
    found = set()
    for path in PLUGIN.rglob("*"):
        parts = path.relative_to(PLUGIN).parts
        listed_md = path.relative_to(PLUGIN) in {rel for _, rel in listed}
        if not path.is_file() or (path.suffix == ".md" and not listed_md):
            continue
        if "node_modules" in parts:
            continue
        if "__pycache__" in parts:
            continue
        text = path.read_text(errors="ignore")
        for _, opening, _ in REMINDER_COPIES.values():
            if opening in text:
                found.add((opening, path.relative_to(PLUGIN)))
    assert found == listed
