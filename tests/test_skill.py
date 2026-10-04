from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from remote_exec.skill import DEFAULT_SKILL_DIR, SkillExistsError, install_skill, skill_text

CLIENT = str(Path(sys.executable).parent / "remote-exec-client")


def test_default_location():
    assert DEFAULT_SKILL_DIR == Path("~/.claude/commands")


def test_skill_content_is_client_only():
    text = skill_text()
    assert text.startswith("---\ndescription:")
    assert "remote-exec-client --list" in text and "$ARGUMENTS" in text
    assert "remote-exec-server" not in text


def test_install_update_and_force(tmp_path):
    path, status = install_skill(tmp_path / "commands")
    assert status == "installed" and path == tmp_path / "commands" / "remote-exec.md"
    assert path.read_text() == skill_text()
    assert install_skill(tmp_path / "commands")[1] == "unchanged"

    path.write_text("user edits\n")
    with pytest.raises(SkillExistsError, match="--force"):
        install_skill(tmp_path / "commands")
    assert path.read_text() == "user edits\n"
    assert install_skill(tmp_path / "commands", force=True)[1] == "updated"
    assert path.read_text() == skill_text()


def test_cli_install_skill_default_dir(tmp_path):
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    r = subprocess.run([CLIENT, "--install-skill"], capture_output=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    target = tmp_path / ".claude" / "commands" / "remote-exec.md"
    assert target.read_text() == skill_text()
    assert str(target).encode() in r.stdout

    target.write_text("changed")
    r = subprocess.run([CLIENT, "--install-skill"], capture_output=True, env=env, timeout=30)
    assert r.returncode == 1 and b"--force" in r.stderr
    r = subprocess.run([CLIENT, "--install-skill", "--force"], capture_output=True, env=env, timeout=30)
    assert r.returncode == 0 and target.read_text() == skill_text()


def test_cli_install_skill_custom_dir(tmp_path):
    r = subprocess.run([CLIENT, "--install-skill", str(tmp_path / "x")], capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "x" / "remote-exec.md").exists()
