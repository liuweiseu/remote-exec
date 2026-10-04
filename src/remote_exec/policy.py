"""Server-side allow/deny policy for the programs a command may run (tools.yaml).

``tools.yaml``::

    allow: ["*"]      # "*" = every program (glob patterns are supported)
    deny: [ssh]       # deny wins over allow

Defaults (no file, or a key missing/empty): ``allow: ["*"]``, ``deny: ["ssh"]``.
``deny: []`` explicitly denies nothing (e.g. to permit ssh). All values are
read as strings, so names like ``true`` or ``yes`` stay program names.

Commands are shell strings, so the policy looks at every program the command
line would start: each simple command in pipelines/lists (``|``, ``&&``,
``;``, newlines), subshells, ``$(...)`` / backticks / ``<(...)``, the target
of wrappers such as ``sudo``, ``env``, ``timeout``, ``nohup``, and scripts
passed to ``bash -c`` / ``eval``. Patterns without "/" match the program's
base name (``ssh`` matches ``/usr/bin/ssh``).

This is a guardrail against mistakes, not a sandbox: programs started
indirectly (a script or ``python -c`` calling ssh, ``X=ssh; $X``) cannot be
seen in the command line. Use OS-level controls for real isolation.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

DEFAULT_TOOLS_FILE = Path("~/.config/remote-exec/tools.yaml")
DEFAULT_ALLOW = ("*",)
DEFAULT_DENY = ("ssh",)

# Shell builtins that do not start another program: never need to be allowed
# (they can still be denied explicitly).
BUILTINS = frozenset({
    "cd", "echo", "printf", "pwd", "export", "unset", "set", "true", "false", "test", "[", "[[",
    "read", "exit", "return", "shift", "local", "declare", "typeset", "wait", ":", "let", "umask",
    "ulimit", "trap", "type", "hash", "shopt", "alias", "unalias", "pushd", "popd", "dirs",
    "break", "continue", "getopts", "readonly", "jobs",
})
# Reserved words that may precede a command.
_KEYWORD_PREFIXES = frozenset({"!", "{", "}", "if", "then", "else", "elif", "do", "while", "until", "time"})
# Words starting a construct whose remaining words are not a program.
_NON_COMMANDS = frozenset({"for", "select", "case", "esac", "fi", "done", "in", "function", "((", "]]"})
# Commands that run another command given as their arguments.
WRAPPERS = frozenset({
    "sudo", "doas", "env", "nohup", "time", "exec", "nice", "timeout", "command", "builtin",
    "xargs", "stdbuf", "setsid", "ionice", "chrt", "unbuffer", "watch", "strace", "ltrace",
    "numactl", "srun", "mpirun", "mpiexec",
})
# Wrapper options that consume the following word (e.g. ``sudo -u user``).
_WRAPPER_OPTS_WITH_ARG = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T", "--user", "--group", "--host",
             "--prompt", "--close-from", "--chdir", "--role", "--type", "--other-user", "--command-timeout"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "nice": {"-n", "--adjustment"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "ionice": {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata"},
    "xargs": {"-I", "-i", "-n", "-P", "-L", "-l", "-d", "-E", "-e", "-s", "-a", "--arg-file", "--delimiter",
              "--max-args", "--max-procs", "--max-lines", "--max-chars", "--replace"},
    "watch": {"-n", "--interval", "-d"},
    "numactl": {"-N", "-m", "-C", "-p", "-i", "--cpunodebind", "--membind", "--physcpubind",
                "--preferred", "--interleave"},
    "srun": {"-n", "-N", "-p", "-c", "-t", "-J", "-A", "-w", "--partition", "--account", "--time"},
    "mpirun": {"-n", "-np", "-N", "-H", "--host", "--hostfile", "-hostfile", "-machinefile", "--map-by"},
    "mpiexec": {"-n", "-np", "-N", "-H", "--host", "--hostfile", "-hostfile", "-machinefile", "--map-by"},
    "strace": {"-o", "-e", "-p", "-s", "-u", "-E"},
    "ltrace": {"-o", "-e", "-p", "-s", "-u", "-E"},
    "chrt": {"-p"},
}
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})

_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_WRAPPER_ARG_RE = re.compile(r"^(\d+(\.\d+)?[smhd]?|0x[0-9a-fA-F]+)$")  # durations, priorities, masks


class PolicyError(Exception):
    pass


# ---------------------------------------------------------------- shell parsing


class _Parser:
    """Small bash-like lexer that collects simple commands (lists of words).

    Expansions it cannot evaluate are kept as a literal ``$`` in the word.
    It is deliberately lenient: malformed input (e.g. an unterminated quote)
    is parsed as far as possible; bash will reject it anyway.
    """

    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.commands: list[list[str]] = []

    def parse(self, until: str | None = None) -> None:
        s = self.s
        words: list[str] = []
        word: list[str] | None = None
        skip_next_word = False  # redirection target
        heredocs: list[tuple[str, bool]] = []  # (delimiter, strip tabs)
        pending_heredoc: bool | None = None  # next word is a heredoc delimiter
        depth = 0

        def end_word() -> None:
            nonlocal word, skip_next_word, pending_heredoc
            if word is None:
                return
            value = "".join(word)
            word = None
            if pending_heredoc is not None:
                heredocs.append((value, pending_heredoc))
                pending_heredoc = None
            if skip_next_word:
                skip_next_word = False
            else:
                words.append(value)

        def end_command() -> None:
            end_word()
            if words:
                self.commands.append(list(words))
                words.clear()

        def start_word() -> list[str]:
            nonlocal word
            if word is None:
                word = []
            return word

        while self.i < len(s):
            c = s[self.i]
            if until == "`" and c == "`":
                self.i += 1
                end_command()
                return
            if c in " \t\r":
                end_word()
                self.i += 1
            elif c == "\n":
                end_command()
                self.i += 1
                for delim, strip in heredocs:
                    self._skip_heredoc(delim, strip)
                heredocs.clear()
            elif c in ";&|":
                end_command()
                self.i += 1
            elif c == "(":
                end_command()
                depth += 1
                self.i += 1
            elif c == ")":
                end_command()
                self.i += 1
                if until == ")" and depth == 0:
                    return
                depth = max(0, depth - 1)
            elif c in "<>":
                if word is not None and "".join(word).isdigit():
                    word = None  # file descriptor number, e.g. 2>&1
                else:
                    end_word()
                if self.s.startswith("(", self.i + 1):  # process substitution <(...) >(...)
                    self.i += 2
                    self.parse(until=")")
                    continue
                op_start = self.i
                self.i += 1
                while self.i < len(s) and s[self.i] in "<>&|-":
                    self.i += 1
                op = s[op_start:self.i]
                if op in ("<<", "<<-"):
                    pending_heredoc = op == "<<-"
                skip_next_word = True
            elif c == "#" and word is None:
                while self.i < len(s) and s[self.i] != "\n":
                    self.i += 1
            elif c == "\\":
                if self.s.startswith("\n", self.i + 1):
                    self.i += 2  # line continuation
                else:
                    start_word().append(s[self.i + 1:self.i + 2])
                    self.i += 2
            elif c == "'":
                end = s.find("'", self.i + 1)
                end = len(s) if end < 0 else end
                start_word().extend(s[self.i + 1:end])
                self.i = end + 1
            elif c == '"':
                self.i += 1
                self._double_quoted(start_word())
            elif c == "`":
                self.i += 1
                self.parse(until="`")
                start_word().append("$")
            elif c == "$":
                self._dollar(start_word())
            else:
                start_word().append(c)
                self.i += 1
        end_command()

    def _double_quoted(self, word: list[str]) -> None:
        s = self.s
        while self.i < len(s):
            c = s[self.i]
            if c == '"':
                self.i += 1
                return
            if c == "\\" and self.i + 1 < len(s):
                word.append(s[self.i + 1])
                self.i += 2
            elif c == "`":
                self.i += 1
                self.parse(until="`")
                word.append("$")
            elif c == "$":
                self._dollar(word)
            else:
                word.append(c)
                self.i += 1

    def _dollar(self, word: list[str]) -> None:
        s = self.s
        word.append("$")
        if s.startswith("$((", self.i):  # arithmetic: no commands inside
            self.i = self._skip_balanced(self.i + 1, "(", ")")
        elif s.startswith("$(", self.i):
            self.i += 2
            self.parse(until=")")
        elif s.startswith("${", self.i):
            self.i = self._skip_balanced(self.i + 1, "{", "}")
        else:
            self.i += 1

    def _skip_balanced(self, start: int, open_: str, close: str) -> int:
        depth, i = 0, start
        while i < len(self.s):
            if self.s[i] == open_:
                depth += 1
            elif self.s[i] == close:
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return i

    def _skip_heredoc(self, delim: str, strip_tabs: bool) -> None:
        s = self.s
        while self.i < len(s):
            end = s.find("\n", self.i)
            end = len(s) if end < 0 else end
            line = s[self.i:end]
            self.i = end + 1
            if (line.lstrip("\t") if strip_tabs else line) == delim:
                return


def _programs_in_words(words: list[str], out: list[str], depth: int) -> None:
    """Find the program of one simple command, following wrappers/shells."""
    i = 0
    while i < len(words) and (words[i] in _KEYWORD_PREFIXES or _ASSIGNMENT_RE.match(words[i])):
        i += 1
    if i >= len(words) or words[i] in _NON_COMMANDS:
        return
    program, args = words[i], words[i + 1:]
    out.append(program)
    name = os.path.basename(program)

    if name in SHELLS:
        for j, arg in enumerate(args):
            if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
                if j + 1 < len(args):
                    _collect(args[j + 1], out, depth + 1)
                return
        return
    if name == "eval":
        _collect(" ".join(args), out, depth + 1)
        return
    if name in WRAPPERS:
        if name == "command" and any(a in ("-v", "-V") for a in args):
            return  # only looks the name up
        takes_arg = _WRAPPER_OPTS_WITH_ARG.get(name, set())
        j = 0
        while j < len(args) and (
            args[j].startswith("-") or _ASSIGNMENT_RE.match(args[j]) or _WRAPPER_ARG_RE.match(args[j])
        ):
            j += 2 if args[j] in takes_arg else 1
        if j < len(args):
            _programs_in_words(args[j:], out, depth)


def _collect(command: str, out: list[str], depth: int) -> None:
    if depth > 20:
        raise PolicyError("command is nested too deeply to analyse")
    parser = _Parser(command)
    parser.parse()
    for words in parser.commands:
        _programs_in_words(words, out, depth)


def programs_in(command: str) -> list[str]:
    """Programs (as written, e.g. ``ssh`` or ``/usr/bin/ssh``) that ``command`` would run."""
    out: list[str] = []
    _collect(command, out, 0)
    return out


# ---------------------------------------------------------------- policy


def _matches(program: str, patterns: tuple[str, ...]) -> str | None:
    base = os.path.basename(program)
    for pattern in patterns:
        target = os.path.normpath(program) if "/" in pattern else base
        if fnmatchcase(target, pattern):
            return pattern
    return None


@dataclass(frozen=True)
class ToolPolicy:
    allow: tuple[str, ...] = DEFAULT_ALLOW
    deny: tuple[str, ...] = DEFAULT_DENY
    source: str = "built-in default"

    def check(self, command: str) -> str | None:
        """Return a reason if ``command`` is not allowed, else None."""
        try:
            programs = programs_in(command)
        except PolicyError as exc:
            return str(exc)
        for program in programs:
            if pattern := _matches(program, self.deny):
                return f"program '{program}' is denied (deny pattern '{pattern}')"
            if os.path.basename(program) in BUILTINS:
                continue
            if not _matches(program, self.allow):
                return f"program '{program}' is not in the allow list"
        return None

    def describe(self) -> str:
        return f"allow={list(self.allow)} deny={list(self.deny)} ({self.source})"


def _pattern_list(value, key: str, path: Path) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise PolicyError(f"{path}: '{key}' must be a list of program names or patterns")
    return tuple(v.strip() for v in value)


def load_policy(path: str | os.PathLike) -> ToolPolicy:
    p = Path(path).expanduser()
    try:
        # BaseLoader keeps every scalar a string: program names such as
        # "true", "yes" or "7z" must not turn into booleans/numbers.
        data = yaml.load(p.read_text(), Loader=yaml.BaseLoader)
    except OSError as exc:
        raise PolicyError(f"cannot read tools file {p}: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise PolicyError(f"invalid YAML in {p}: {exc}") from exc
    data = data or {}
    if not isinstance(data, dict):
        raise PolicyError(f"{p}: top level must be a mapping with 'allow' and/or 'deny'")
    unknown = set(data) - {"allow", "deny"}
    if unknown:
        raise PolicyError(f"{p}: unknown key(s): {', '.join(sorted(unknown))}")
    # A missing or empty key keeps its default; an explicit empty list
    # ("deny: []") means "nothing" (e.g. to allow ssh).
    allow = _pattern_list(data["allow"], "allow", p) if data.get("allow") not in (None, "") else DEFAULT_ALLOW
    deny = _pattern_list(data["deny"], "deny", p) if data.get("deny") not in (None, "") else DEFAULT_DENY
    return ToolPolicy(allow=allow, deny=deny, source=str(p))


class PolicyProvider:
    """Serves the current policy, reloading tools.yaml when it changes.

    If a reload fails (invalid YAML), the last good policy stays active.
    """

    def __init__(self, path: str | os.PathLike | None = None, required: bool = False):
        self.path = Path(path or DEFAULT_TOOLS_FILE).expanduser()
        self._mtime: float | None = None
        self._policy = ToolPolicy()
        if self.path.exists():
            # Fail fast at startup on an invalid file (later reloads keep the last good policy).
            self._mtime = self.path.stat().st_mtime
            self._policy = load_policy(self.path)
        elif required:
            raise PolicyError(f"tools file not found: {self.path}")

    def current(self) -> ToolPolicy:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            if self._mtime is not None:
                log.warning("tools file %s removed; using built-in default policy", self.path)
                self._mtime, self._policy = None, ToolPolicy()
            return self._policy
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                self._policy = load_policy(self.path)
                log.info("tools policy loaded: %s", self._policy.describe())
            except PolicyError as exc:
                log.error("%s; keeping previous policy: %s", exc, self._policy.describe())
        return self._policy
