"""The update skill's drift check: the installed plugin and the copies setup
wrote outside it, compared with the latest release (hermetic: the "latest
release" is a local checkout or a local git repository)."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "hivemind"
DRIFT = Path("skills") / "hivemind-update" / "scripts" / "hivemind-drift.sh"
SETUP = PLUGIN / "skills" / "hivemind-setup" / "SKILL.md"

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _block() -> str:
    lines = SETUP.read_text().splitlines()
    start = lines.index("<!-- hivemind:begin -->")
    end = lines.index("<!-- hivemind:end -->")
    return "\n".join(lines[start : end + 1]) + "\n"


def _release(at: Path) -> Path:
    """A checkout of the repository: the two plugin folders, as released."""
    shutil.copytree(PLUGIN, at / "plugins" / "hivemind", symlinks=True)
    shutil.copytree(ROOT / "plugins" / "antigravity", at / "plugins" / "antigravity", symlinks=True)
    return at


def _install(at: Path) -> Path:
    shutil.copytree(PLUGIN, at)
    return at


def _set_version(plugin: Path, version: str) -> None:
    manifest = plugin / "package.json"
    data = json.loads(manifest.read_text())
    data["version"] = version
    manifest.write_text(json.dumps(data, indent=2) + "\n")


def _drift(tmp_path: Path, installed: Path, *args: str | Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HIVEMIND_", "GIT_", "XDG_"))}
    env.update(HOME=str(tmp_path), TMPDIR=str(tmp_path), GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(
        ["sh", str(installed / DRIFT), *map(str, args)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_current_install_has_no_drift(tmp_path: Path) -> None:
    latest = _release(tmp_path / "latest")
    installed = _install(tmp_path / "installed")
    out = _drift(tmp_path, installed, "--latest", latest)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "Installed plugin: ~/installed, version" in out.stdout
    assert ": current" in out.stdout and out.stdout.endswith("No drift.\n")


def test_an_older_install_is_behind_and_lists_the_changed_instructions(tmp_path: Path) -> None:
    latest = _release(tmp_path / "latest")
    installed = _install(tmp_path / "installed")
    _set_version(installed, "1.0.0")
    with (installed / "skills" / "hivemind" / "SKILL.md").open("a") as skill:
        skill.write("an older rule\n")
    (installed / "skills" / "hivemind-setup" / "references" / "gone.md").write_text("x\n")
    (installed / "guidelines" / "hivemind.md").unlink()
    shutil.rmtree(latest / "plugins" / "hivemind" / "skills" / "hivemind-update" / "references")
    out = _drift(tmp_path, installed, "--latest", latest)
    assert out.returncode == 1
    lines = out.stdout.splitlines()
    assert "version 1.0.0: behind" in lines[1]
    assert "  differs: skills/hivemind/SKILL.md" in lines
    assert "  only in your copy: skills/hivemind-setup/references/gone.md" in lines
    assert "  only in your copy: skills/hivemind-update/references" in lines
    assert not any("guidelines" in line for line in lines)  # not installed, not compared


def test_a_newer_install_is_not_drift(tmp_path: Path) -> None:
    latest = _release(tmp_path / "latest")
    installed = _install(tmp_path / "installed")
    _set_version(installed, "99.0.0")
    out = _drift(tmp_path, installed, "--latest", latest)
    assert out.returncode == 0
    assert "newer than the release compared with" in out.stdout


def test_the_antigravity_rule_is_compared_with_its_own_folder(tmp_path: Path) -> None:
    latest = _release(tmp_path / "latest")
    installed = _install(tmp_path / "agy")
    (installed / "rules").mkdir()
    shutil.copy(ROOT / "plugins" / "antigravity" / "rules" / "AGENTS.md", installed / "rules")
    assert _drift(tmp_path, installed, "--latest", latest).returncode == 0
    (installed / "rules" / "AGENTS.md").write_text("old rule\n")
    out = _drift(tmp_path, installed, "--latest", latest)
    assert out.returncode == 1 and "  differs: rules/AGENTS.md" in out.stdout


def test_instruction_blocks_are_compared_with_the_latest_setup_skill(tmp_path: Path) -> None:
    latest = _release(tmp_path / "latest")
    installed = _install(tmp_path / "installed")
    current = tmp_path / "current.md"
    current.write_text("# Mine\n\n" + _block().replace("\n", "\r\n") + "\nmore\n")
    old = tmp_path / "old.md"
    old.write_text(_block().replace("long-term memory", "memory"))
    doubled = tmp_path / "doubled.md"
    doubled.write_text(_block() + _block())
    plain = tmp_path / "plain.md"
    plain.write_text("no block\n")
    out = _drift(
        tmp_path,
        installed,
        "--latest",
        latest,
        *("--block", current, "--block", old, "--block", doubled),
        *("--block", plain, "--block", tmp_path / "missing.md"),
    )
    assert out.returncode == 1
    text = out.stdout
    assert "Instruction block in ~/current.md: current" in text
    assert "Instruction block in ~/old.md: differs" in text
    assert "-This organization's Hivemind (the hive_* MCP tools) is my memory:" in text
    assert "Instruction block in ~/doubled.md: 2 begin and 2 end markers" in text
    assert "Instruction block in ~/plain.md: none" in text
    assert "Instruction block in ~/missing.md: no such file" in text
    assert "Drift: 2 difference(s)." in text


def test_print_block_gives_the_block_to_write(tmp_path: Path) -> None:
    out = _drift(
        tmp_path, _install(tmp_path / "i"), "--latest", _release(tmp_path / "l"), "--print-block"
    )
    assert out.returncode == 0 and out.stdout == _block()


def test_hand_copied_skills_and_hooks_are_compared(tmp_path: Path) -> None:
    latest = _release(tmp_path / "latest")
    installed = _install(tmp_path / "installed")
    skills = tmp_path / "cline-skills"
    shutil.copytree(PLUGIN / "skills" / "hivemind", skills / "hivemind")
    shutil.copytree(PLUGIN / "skills" / "hivemind-setup", skills / "hivemind-setup")
    shutil.copytree(PLUGIN / "skills" / "hivemind-update", skills / "hivemind-update")
    (skills / "someone-elses-skill").mkdir()
    hooks = tmp_path / "Hooks"
    hooks.mkdir()
    shutil.copy(PLUGIN / "cline" / "TaskStart", hooks)
    (hooks / "PostToolUse").write_text("old hook\n")
    args = ("--latest", latest, "--skills", skills)
    copies = (
        *("--copy", f"{hooks}/TaskStart=cline/TaskStart"),
        *("--copy", f"{hooks}/PostToolUse=cline/PostToolUse"),
        *("--copy", f"{hooks}/UserPromptSubmit=cline/UserPromptSubmit"),
    )
    out = _drift(tmp_path, installed, *args, *copies)
    assert out.returncode == 1
    lines = out.stdout.splitlines()
    skills_at = lines.index("Skill copies in ~/cline-skills:")
    assert lines[skills_at + 1] == "  current"
    assert "Copy ~/Hooks/TaskStart of cline/TaskStart:" in lines
    assert "  differs: cline/PostToolUse" in lines and "-old hook" in lines
    assert "  missing: cline/UserPromptSubmit" in lines
    assert not any("someone-elses-skill" in line for line in lines)

    shutil.rmtree(skills / "hivemind-update")
    out = _drift(tmp_path, installed, *args)
    assert "  missing: hivemind-update" in out.stdout.splitlines()


def test_copied_skills_alone_are_not_taken_for_an_installed_plugin(tmp_path: Path) -> None:
    latest = _release(tmp_path / "latest")
    home_skills = tmp_path / "home" / "skills"
    for name in ("hivemind", "hivemind-setup", "hivemind-update"):
        shutil.copytree(PLUGIN / "skills" / name, home_skills / name)
    out = _drift(tmp_path, tmp_path / "home", "--latest", latest, "--skills", home_skills)
    assert out.returncode == 0, out.stdout
    assert "Installed plugin: none around this script" in out.stdout


def test_it_refuses_what_it_cannot_compare(tmp_path: Path) -> None:
    installed = _install(tmp_path / "installed")
    out = _drift(tmp_path, installed, "--latest", tmp_path)
    assert out.returncode == 2 and "is not a checkout of the Hivemind repository" in out.stderr
    out = _drift(tmp_path, installed, "--latest", _release(tmp_path / "l"), "--copy", "nothing")
    assert out.returncode == 2 and "--copy needs FILE=PATH" in out.stderr


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull},
    )


def _mirror(at: Path) -> Path:
    """A release repository somewhere other than GitHub, tagged v2.22.0."""
    repo = _release(at)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "release")
    _git(repo, "tag", "v2.22.0")
    return repo


@needs_git
def test_a_cloned_plugin_is_compared_with_the_place_it_was_cloned_from(tmp_path: Path) -> None:
    mirror = _mirror(tmp_path / "mirror")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", mirror.as_uri(), str(clone)], check=True)
    out = _drift(tmp_path, clone / "plugins" / "hivemind")
    assert out.returncode == 0, out.stderr
    assert (
        f"of {mirror.as_uri()} (from the installed plugin's git remote)"
        in out.stdout.splitlines()[0]
    )


@needs_git
def test_a_saved_source_is_used_when_the_plugin_is_not_a_hivemind_clone(tmp_path: Path) -> None:
    mirror = _mirror(tmp_path / "mirror")
    dotfiles = tmp_path / "dotfiles"
    installed = _install(dotfiles / "plugin")
    _git(dotfiles, "init", "-q")
    _git(dotfiles, "remote", "add", "origin", "https://example.invalid/dotfiles.git")
    source = tmp_path / ".config" / "hivemind" / "plugin-source"
    source.parent.mkdir(parents=True)
    source.write_text(f"# our mirror\n\n  {mirror.as_uri()}  \n")
    out = _drift(tmp_path, installed)
    assert out.returncode == 0, out.stderr
    assert f"of {mirror.as_uri()} (from ~/.config/hivemind/plugin-source)" in out.stdout


@needs_git
def test_a_failed_fetch_never_prints_credentials(tmp_path: Path) -> None:
    installed = _install(tmp_path / "installed")
    out = _drift(tmp_path, installed, "--repo", "https://agent:s3cret@127.0.0.1:9/hivemind.git")
    assert out.returncode == 2
    assert "s3cret" not in out.stdout + out.stderr
    assert "https://127.0.0.1:9/hivemind.git (--repo)" in out.stderr
    assert "--ref <branch>" in out.stderr


@needs_git
def test_it_fetches_the_newest_release_tag_and_can_keep_it(tmp_path: Path) -> None:
    repo = _release(tmp_path / "repo")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "2.9.0")
    _git(repo, "tag", "v2.9.0")
    _set_version(repo / "plugins" / "hivemind", "2.10.0")
    _git(repo, "commit", "-qam", "2.10.0")
    _git(repo, "tag", "-a", "v2.10.0", "-m", "release")
    _set_version(repo / "plugins" / "hivemind", "3.0.0")
    _git(repo, "commit", "-qam", "next")
    _git(repo, "tag", "v3.0.0-rc1")
    installed = _install(tmp_path / "installed")
    _set_version(installed, "2.9.0")
    kept = tmp_path / "kept"
    out = _drift(tmp_path, installed, "--repo", repo.as_uri(), "--keep", kept)
    assert out.returncode == 1, out.stderr
    assert out.stdout.startswith(
        f"Latest: version 2.10.0, v2.10.0 of {repo.as_uri()} (from --repo)\n"
    )
    assert "version 2.9.0: behind" in out.stdout
    assert (
        json.loads((kept / "plugins" / "hivemind" / "package.json").read_text())["version"]
        == "2.10.0"
    )

    out = _drift(tmp_path, installed, "--repo", (tmp_path / "nowhere").as_uri())
    assert out.returncode == 2 and "found no vX.Y.Z tag" in out.stderr
