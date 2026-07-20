"""Content-free per-turn performance telemetry.

The writer records durations, counters, and aggregate sizes only. It never
persists prompts, messages, tool arguments/results, local paths, base URLs, or
raw user/chat/session identifiers.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

_SCHEMA_VERSION = 1
_MAX_FILE_BYTES = 10 * 1024 * 1024
_LOCK_TIMEOUT_SECONDS = 0.1
_LABEL_RE = re.compile(r"^[A-Za-z0-9._:+/-]{1,80}$")
_EXIT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_PERSISTED_SIZE_RE = re.compile(
    r"This tool result was too large \(([0-9,]+) characters(?:,|\))"
)


def summarize_tool_messages(messages: list[dict[str, Any]]) -> dict[str, int]:
    """Return size/count metrics without retaining tool-result content."""
    tool_count = 0
    context_chars = 0
    original_chars = 0
    persisted_count = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        tool_count += 1
        content = message.get("content", "")
        if not isinstance(content, str):
            try:
                content = json.dumps(content, ensure_ascii=True, separators=(",", ":"))
            except Exception:
                content = str(content)
        context_size = len(content)
        context_chars += context_size
        match = (
            _PERSISTED_SIZE_RE.search(content)
            if "<persisted-output>" in content
            else None
        )
        if match:
            try:
                original_chars += int(match.group(1).replace(",", ""))
                persisted_count += 1
                continue
            except (TypeError, ValueError):
                pass
        original_chars += context_size
    return {
        "tool_count": tool_count,
        "context_chars": context_chars,
        "original_chars": original_chars,
        "persisted_count": persisted_count,
    }


def _safe_label(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith(("/", "~", "./")) or ".." in text:
        return "other"
    return text if _LABEL_RE.fullmatch(text) else "other"


def _safe_exit_reason(value: Any) -> str:
    # Several runtime reasons append dynamic details in parentheses. Keep only
    # the stable reason code so errors or provider text cannot enter telemetry.
    text = str(value or "unknown").strip().split("(", 1)[0]
    return text if _EXIT_RE.fullmatch(text) else "other"


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short telemetry write")
        view = view[written:]


def _open_nofollow(path: Path, flags: int, mode: int) -> int:
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)


def _ensure_metrics_dir() -> Path:
    directory = Path(get_hermes_home()) / "metrics"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    return directory


def _acquire_lock(directory: Path) -> int | None:
    lock_path = directory / ".turn-performance.lock"
    fd = _open_nofollow(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.005)


def _load_or_create_key(directory: Path) -> bytes:
    key_path = directory / ".telemetry-key"
    for _attempt in range(2):
        try:
            fd = _open_nofollow(key_path, os.O_RDONLY, 0o600)
        except FileNotFoundError:
            key = secrets.token_bytes(32)
            try:
                fd = _open_nofollow(
                    key_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                continue
            else:
                try:
                    os.fchmod(fd, 0o600)
                    _write_all(fd, key)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                return key

        try:
            os.fchmod(fd, 0o600)
            key = os.read(fd, 33)
        finally:
            os.close(fd)
        if len(key) == 32:
            return key

        # A process can die after O_EXCL creation but before the 32-byte write.
        # This code runs under the inter-process lock, so a short key is an
        # incomplete private artifact and can be safely recreated.
        try:
            key_path.unlink()
        except FileNotFoundError:
            pass

    raise ValueError("could not create telemetry key")


def _append_record(row: dict[str, Any]) -> None:
    directory = _ensure_metrics_dir()
    lock_fd = _acquire_lock(directory)
    if lock_fd is None:
        return
    try:
        metrics_path = directory / "turn-performance.jsonl"
        if metrics_path.exists() and metrics_path.stat().st_size >= _MAX_FILE_BYTES:
            rotated = directory / "turn-performance.jsonl.1"
            try:
                rotated.unlink()
            except FileNotFoundError:
                pass
            os.replace(metrics_path, rotated)
            os.chmod(rotated, 0o600)

        payload = (json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
        if metrics_path.exists() and metrics_path.stat().st_size > 0:
            read_fd = _open_nofollow(metrics_path, os.O_RDONLY, 0o600)
            try:
                last_byte = os.pread(read_fd, 1, metrics_path.stat().st_size - 1)
            finally:
                os.close(read_fd)
            if last_byte != b"\n":
                payload = b"\n" + payload
        fd = _open_nofollow(
            metrics_path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o600,
        )
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, payload)
        finally:
            os.close(fd)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


class TurnPerformanceTelemetry:
    """Accumulate one turn's content-free performance measurements."""

    def __init__(
        self,
        *,
        session_id: str,
        started_monotonic: float | None = None,
        started_at: str | None = None,
    ) -> None:
        self._started_monotonic = time.monotonic() if started_monotonic is None else float(started_monotonic)
        self._started_at = started_at or dt.datetime.now(dt.timezone.utc).isoformat()
        self._session_id = str(session_id or "")
        self._prologue_ms: int | None = None
        self._model_calls: list[dict[str, Any]] = []
        self._tool_batches: list[dict[str, int]] = []
        self._compression_ms = 0
        self._compression_count = 0
        self._finalized = False

    @property
    def is_finalized(self) -> bool:
        return self._finalized

    def mark_prologue_complete(self, *, now_monotonic: float | None = None) -> None:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        self._prologue_ms = _nonnegative_int(round((now - self._started_monotonic) * 1000))

    def record_model_call(
        self,
        *,
        duration_ms: int,
        ttft_ms: int | None,
        success: bool,
        provider: str,
        model: str,
        request_chars: int,
    ) -> None:
        self._model_calls.append({
            "duration_ms": _nonnegative_int(duration_ms),
            "ttft_ms": None if ttft_ms is None else _nonnegative_int(ttft_ms),
            "success": bool(success),
            "provider": _safe_label(provider),
            "model": _safe_label(model),
            "request_chars": _nonnegative_int(request_chars),
        })

    def record_tool_batch(
        self,
        *,
        duration_ms: int,
        tool_count: int,
        original_chars: int,
        context_chars: int,
        persisted_count: int,
    ) -> None:
        self._tool_batches.append({
            "duration_ms": _nonnegative_int(duration_ms),
            "tool_count": _nonnegative_int(tool_count),
            "original_chars": _nonnegative_int(original_chars),
            "context_chars": _nonnegative_int(context_chars),
            "persisted_count": _nonnegative_int(persisted_count),
        })

    def record_compression(self, *, duration_ms: int) -> None:
        self._compression_count += 1
        self._compression_ms += _nonnegative_int(duration_ms)

    def finalize(
        self,
        *,
        api_call_count: int,
        exit_reason: str,
        completed: bool,
        finalization_ms: int = 0,
        now_monotonic: float | None = None,
    ) -> dict[str, Any]:
        if self._finalized:
            raise RuntimeError("turn telemetry already finalized")
        self._finalized = True

        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        turn_duration_ms = _nonnegative_int(round((now - self._started_monotonic) * 1000))
        model_duration_ms = sum(row["duration_ms"] for row in self._model_calls)
        tool_work_ms = sum(row["duration_ms"] for row in self._tool_batches)
        finalization_ms = _nonnegative_int(finalization_ms)
        prologue_ms = self._prologue_ms or 0
        attributed_ms = prologue_ms + model_duration_ms + tool_work_ms + self._compression_ms + finalization_ms

        # Derive a stable profile-local pseudonym without storing the raw ID.
        directory = _ensure_metrics_dir()
        lock_fd = _acquire_lock(directory)
        if lock_fd is None:
            key = b""
        else:
            try:
                key = _load_or_create_key(directory)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
        session_ref = hmac.new(
            key, self._session_id.encode("utf-8"), hashlib.sha256
        ).hexdigest()[:24] if key else "unavailable"

        row = {
            "schema_version": _SCHEMA_VERSION,
            "started_at": self._started_at,
            "session_ref": session_ref,
            "turn_duration_ms": turn_duration_ms,
            "prologue_ms": prologue_ms,
            "model_duration_ms": model_duration_ms,
            "tool_work_ms": tool_work_ms,
            "compression_ms": self._compression_ms,
            "compression_count": self._compression_count,
            "finalization_ms": finalization_ms,
            "unattributed_ms": max(0, turn_duration_ms - attributed_ms),
            "api_call_count": _nonnegative_int(api_call_count),
            "model_call_count": len(self._model_calls),
            "tool_call_count": sum(row["tool_count"] for row in self._tool_batches),
            "completed": bool(completed),
            "exit_reason": _safe_exit_reason(exit_reason),
            "model_calls": self._model_calls,
            "tool_batches": self._tool_batches,
            "persisted_tool_results": sum(row["persisted_count"] for row in self._tool_batches),
            "tool_original_chars": sum(row["original_chars"] for row in self._tool_batches),
            "tool_context_chars": sum(row["context_chars"] for row in self._tool_batches),
        }
        if key:
            try:
                _append_record(row)
            except Exception:
                # Telemetry must never delay or fail a user turn.
                pass
        return row
