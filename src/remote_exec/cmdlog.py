"""Command log: every executed command together with its output.

One text file shared by all executions (each line carries the execution id,
so concurrent commands can be told apart)::

    2026-10-04 10:00:00.120 [3f2a9c1d0b7e] START client=ipv4:10.0.0.7:51234 cwd="/proj" timeout=0 env_keys="" command="make"
    2026-10-04 10:00:00.250 [3f2a9c1d0b7e] OUT| gcc -c main.c
    2026-10-04 10:00:00.260 [3f2a9c1d0b7e] ERR| warning: unused variable
    2026-10-04 10:00:01.900 [3f2a9c1d0b7e] END status=exited exit_code=0 duration=1.780s

Output is split into lines per stream (undecodable bytes are replaced).
When the file would exceed ``max_bytes`` it is renamed to
``<name>.<timestamp><suffix>`` and a new file is started; ``keep`` limits how
many rotated files are retained (0 = keep all). Files are created with mode
600 because command output may contain sensitive data. Environment variable
values are never written.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_COMMAND_LOG = "remote-exec-commands.log"
DEFAULT_MAX_MB = 10.0
MAX_LINE_BYTES = 64 * 1024  # longer output without newline is written in pieces


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


class CommandLog:
    def __init__(self, path: str | os.PathLike, max_bytes: int = int(DEFAULT_MAX_MB * 1024 * 1024), keep: int = 0):
        if max_bytes < 1024:
            raise ValueError("command log size limit must be at least 1 KiB")
        if keep < 0:
            raise ValueError("number of rotated command logs to keep must be >= 0")
        self.path = Path(path).expanduser().absolute()
        self.max_bytes = max_bytes
        self.keep = keep
        self._pending: dict[tuple[str, str], bytearray] = {}
        self._error_reported = False
        self._file = None
        self._open()

    # ------------------------------------------------------------ public API

    def start(self, execution_id: str, client: str, cwd: str, command: str, timeout: float, env_keys) -> None:
        self._write(self._line(
            execution_id,
            f"START client={client} cwd={_q(cwd)} timeout={timeout:g} "
            f"env_keys={_q(','.join(sorted(env_keys)))} command={_q(command)}",
        ))

    def output(self, execution_id: str, stream: str, data: bytes) -> None:
        """Record a chunk of ``stream`` ("stdout"/"stderr"); complete lines are written."""
        key = (execution_id, stream)
        buf = self._pending.setdefault(key, bytearray())
        buf.extend(data)
        lines = []
        while (nl := buf.find(b"\n")) >= 0:
            lines.append(bytes(buf[:nl]))
            del buf[:nl + 1]
        while len(buf) > MAX_LINE_BYTES:
            lines.append(bytes(buf[:MAX_LINE_BYTES]))
            del buf[:MAX_LINE_BYTES]
        if lines:
            self._write("".join(self._output_line(execution_id, stream, line) for line in lines))

    def end(self, execution_id: str, status: str, exit_code, duration: float) -> None:
        text = []
        for stream in ("stdout", "stderr"):
            rest = self._pending.pop((execution_id, stream), None)
            if rest:
                text.append(self._output_line(execution_id, stream, bytes(rest)))
        text.append(self._line(execution_id, f"END status={status} exit_code={exit_code} duration={duration:.3f}s"))
        self._write("".join(text))

    def rejected(self, client: str, command: str, reason: str) -> None:
        self._write(self._line("-" * 12, f"REJECTED client={client} reason={_q(reason)} command={_q(command)}"))

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None

    # ------------------------------------------------------------ internals

    @staticmethod
    def _line(execution_id: str, message: str) -> str:
        ts = datetime.now().isoformat(sep=" ", timespec="milliseconds")
        return f"{ts} [{execution_id}] {message}\n"

    def _output_line(self, execution_id: str, stream: str, raw: bytes) -> str:
        text = raw.decode("utf-8", errors="replace").rstrip("\r")
        return self._line(execution_id, f"{'OUT' if stream == 'stdout' else 'ERR'}| {text}")

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._file = os.fdopen(fd, "a", encoding="utf-8")
        self._size = os.fstat(fd).st_size

    def _write(self, text: str) -> None:
        if self._file is None:
            return
        data_len = len(text.encode("utf-8"))
        try:
            if self._size > 0 and self._size + data_len > self.max_bytes:
                self._rotate()
            self._file.write(text)
            self._file.flush()
            self._size += data_len
        except OSError as exc:
            # Never let logging problems (e.g. disk full) break command execution.
            if not self._error_reported:
                log.error("cannot write command log %s: %s (further errors suppressed)", self.path, exc)
                self._error_reported = True

    def _rotated_name(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate = self.path.with_name(f"{self.path.stem}.{stamp}{self.path.suffix}")
        n = 1
        while candidate.exists():
            candidate = self.path.with_name(f"{self.path.stem}.{stamp}-{n}{self.path.suffix}")
            n += 1
        return candidate

    def rotated_files(self) -> list[Path]:
        """Rotated files, oldest first (by last write time, then name)."""
        pattern = f"{self.path.stem}.*{self.path.suffix}"
        files = []
        for p in self.path.parent.glob(pattern):
            if p != self.path:
                try:
                    files.append((p.stat().st_mtime, p.name, p))
                except OSError:
                    pass
        return [p for _, _, p in sorted(files)]

    def _rotate(self) -> None:
        self._file.close()
        self._file = None
        try:
            os.replace(self.path, self._rotated_name())
            if self.keep:
                for old in self.rotated_files()[:-self.keep]:
                    try:
                        old.unlink()
                    except OSError:
                        pass
        finally:
            self._open()  # if the rename failed, keep appending to the current file
