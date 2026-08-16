"""Descobre mensagens novas em conversas do Companion, sem gancho no request.

O `state.db` já tem um gatilho que registra toda inserção em `messages` na
tabela `gateway_events`. Fazer poll dessa tabela cobre as duas origens de uma
vez — a resposta ao que o usuário digitou e a entrega proativa de cron ou job —
porque as duas terminam num INSERT. Um gancho no fim de
`_handle_session_chat_stream` cobriria só a primeira, exigiria edição de core, e
rodaria dentro do request: uma APNs lenta atrasaria a resposta na tela.

O gatilho veio de um branch que foi abandonado. Ele está instalado e disparando,
mas uma recriação do banco o levaria embora — e o sintoma seria push que
simplesmente para, sem erro. Por isso `ensure_trigger` roda antes de todo poll.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

PREVIEW_MAX_CHARS = 180

TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS gateway_event_message_created
AFTER INSERT ON messages BEGIN
    INSERT INTO gateway_events(type, session_id, message_id, timestamp)
    VALUES ('message.created', new.session_id, new.id, new.timestamp);
END;
"""

# `title` primeiro, `display_name` como reserva: no banco real desta máquina são
# 3315 sessões com `title` e 635 com `display_name`, e o `api_server` — que é
# quem cria as conversas do Companion — nunca escreve `display_name`. Ler só a
# segunda coluna dava "N" como título em quase toda notificação.
PENDING_SQL = """
SELECT e.id, e.session_id, e.message_id,
       COALESCE(NULLIF(s.title, ''), NULLIF(s.display_name, '')), m.content
  FROM gateway_events e
  JOIN messages  m ON m.id = e.message_id
  JOIN sessions  s ON s.id = e.session_id
 WHERE e.id > ?
   AND e.type = 'message.created'
   AND m.role = 'assistant'
   AND s.source IN ('companion_ios', 'companion_mac')
 ORDER BY e.id
 LIMIT 50
"""


@dataclass(frozen=True)
class PendingMessage:
    event_id: int
    session_id: str
    message_id: str
    title: str
    preview: str


def ensure_trigger(conn: sqlite3.Connection) -> None:
    conn.executescript(TRIGGER_SQL)
    conn.commit()


# `MEDIA:/Users/mike/.hermes/cache/images/x.png` e
# `[Image attached at: /Users/…]` são as duas convenções de mídia do Hermes, e
# as duas carregam um caminho absoluto do disco do Mac. A resolução para data
# URL acontece nas rotas de chat e no histórico — **não** aqui: o watcher lê a
# linha crua de `messages`. Sem esta limpeza, toda resposta com imagem vira uma
# notificação com o layout do `HERMES_HOME` estampado na tela de bloqueio, que
# um push nem exige desbloquear para ler.
_MEDIA_TAG_RE = re.compile(r"MEDIA:\s*(\S+)", re.IGNORECASE)
_IMAGE_ATTACHED_RE = re.compile(r"\[Image attached at:\s*([^\]\r\n]+)\]", re.IGNORECASE)


def _collapse_paths(text: str) -> str:
    def _name(match: "re.Match[str]") -> str:
        leaf = PurePosixPath(match.group(1).strip().strip("\"'`")).name
        return f"📎 {leaf}" if leaf else "📎 Imagem"

    text = _IMAGE_ATTACHED_RE.sub(_name, text)
    return _MEDIA_TAG_RE.sub(_name, text)


def _preview(content: Optional[str]) -> str:
    text = " ".join(_collapse_paths(str(content or "")).split())
    if len(text) <= PREVIEW_MAX_CHARS:
        return text
    return text[: PREVIEW_MAX_CHARS - 1] + "…"


class MessageWatcher:
    def __init__(self, db_path: str, cursor_path: Path):
        self._db_path = str(db_path)
        self._cursor_path = Path(cursor_path)

    # -------------------------------------------------------------- cursor

    def _read_cursor(self) -> int:
        try:
            data = json.loads(self._cursor_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return -1
        if not isinstance(data, dict):
            return -1
        value = data.get("last_event_id")
        return int(value) if isinstance(value, int) else -1

    def _write_cursor(self, value: int) -> None:
        directory = self._cursor_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".cursor-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"last_event_id": int(value)}, handle)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._cursor_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------------------------------------------------------------- poll

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5)
        ensure_trigger(conn)
        return conn

    def bootstrap(self) -> int:
        """Na primeira subida, começa do agora — nenhum evento histórico notifica."""
        existing = self._read_cursor()
        if existing >= 0:
            return existing
        conn = self._connect()
        try:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM gateway_events").fetchone()
        finally:
            conn.close()
        cursor = int(row[0])
        self._write_cursor(cursor)
        return cursor

    def pending(self) -> list[PendingMessage]:
        after = self._read_cursor()
        if after < 0:
            after = self.bootstrap()
        conn = self._connect()
        try:
            rows = conn.execute(PENDING_SQL, (after,)).fetchall()
        finally:
            conn.close()
        return [
            PendingMessage(
                event_id=int(row[0]),
                session_id=str(row[1]),
                message_id=str(row[2]),
                title=str(row[3] or "N"),
                preview=_preview(row[4]),
            )
            for row in rows
        ]

    def commit(self, event_id: int) -> None:
        """Avança o cursor. Chamado SÓ depois de a APNs aceitar."""
        if int(event_id) > self._read_cursor():
            self._write_cursor(int(event_id))
