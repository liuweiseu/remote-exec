"""Install the Claude Code skill (custom slash command) for remote-exec-client."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

SKILL_NAME = "remote-exec"
DEFAULT_SKILL_DIR = Path("~/.claude/commands")


class SkillExistsError(Exception):
    pass


def skill_text() -> str:
    return resources.files("remote_exec").joinpath("claude_skill.md").read_text(encoding="utf-8")


def install_skill(directory: str | Path | None = None, force: bool = False) -> tuple[Path, str]:
    """Write ``<directory>/remote-exec.md``.

    Returns the path and one of ``"installed"``, ``"updated"``, ``"unchanged"``.
    An existing file with different content (e.g. edited by the user) is only
    replaced when ``force`` is true.
    """
    target = Path(directory or DEFAULT_SKILL_DIR).expanduser() / f"{SKILL_NAME}.md"
    text = skill_text()
    if target.exists():
        if target.read_text(encoding="utf-8") == text:
            return target, "unchanged"
        if not force:
            raise SkillExistsError(f"{target} already exists with different content; use --force to overwrite it")
        status = "updated"
    else:
        status = "installed"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target, status
