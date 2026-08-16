"""Registro de dispositivos do Companion: quem está pareado e com que token.

Divisão de responsabilidade com o `PairingStore` do Hermes: ele guarda **quem**
está aprovado (`is_approved`), este módulo guarda **com que credencial**. O
`PairingStore.approve_request` consome a pendência e não deixa artefato para o
telefone buscar depois, então o token precisa de casa própria.

O token nunca é persistido em claro — só o SHA-256. Um arquivo legível não
revela credencial nenhuma.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Optional

TOKEN_BYTES = 32
_HEX = set("0123456789abcdefABCDEF")


def _looks_like_apns_token(value: str) -> bool:
    # Token de device da APNs é hex. 64 chars no aparelho físico; o simulador
    # é mais longo. Aceitar a faixa, recusar qualquer coisa não-hex.
    return bool(value) and 32 <= len(value) <= 200 and all(c in _HEX for c in value)


class DeviceStore:
    """Persistência de `devices.json`, com escrita atômica e modo 0600."""

    def __init__(self, path: Path):
        self._path = Path(path)

    # ---------------------------------------------------------------- I/O

    def _load(self) -> dict:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        # Escrever em temporário no mesmo diretório, ajustar o modo ANTES de
        # publicar, e só então `replace`. Um `chmod` depois do `replace` deixa
        # uma janela em que o arquivo final está legível por outros.
        fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".devices-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        # Um arquivo pré-existente com modo frouxo sobrevive ao `replace` do
        # ponto de vista do inode novo, mas garantir aqui torna o invariante
        # verificável por teste sem depender do estado anterior.
        os.chmod(self._path, 0o600)

    # ------------------------------------------------------------- pairing

    def claim(self, device_id: str, *, approved: bool) -> tuple[str, Optional[str]]:
        """Emite o token deste aparelho, uma vez só.

        Devolve ("issued", token) | ("already_claimed", None) | ("pending", None).
        """
        device_id = str(device_id or "").strip()
        if not device_id:
            return ("pending", None)
        if not approved:
            return ("pending", None)

        data = self._load()
        entry = data.get(device_id)
        if isinstance(entry, dict) and entry.get("token_hash"):
            return ("already_claimed", None)

        token = secrets.token_hex(TOKEN_BYTES)
        entry = entry if isinstance(entry, dict) else {}
        entry["token_hash"] = hashlib.sha256(token.encode()).hexdigest()
        data[device_id] = entry
        self._save(data)
        return ("issued", token)

    def verify(self, token: str) -> Optional[str]:
        """Devolve o `device_id` do token, ou None."""
        token = str(token or "").strip()
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        for device_id, entry in self._load().items():
            if not isinstance(entry, dict):
                continue
            stored = str(entry.get("token_hash") or "")
            if stored and secrets.compare_digest(stored, digest):
                return device_id
        return None

    # ---------------------------------------------------------------- APNs

    def register_push(self, device_id: str, apns_token: str, environment: str) -> bool:
        device_id = str(device_id or "").strip()
        apns_token = str(apns_token or "").strip()
        environment = "production" if str(environment).lower() == "production" else "sandbox"
        if not _looks_like_apns_token(apns_token):
            return False

        data = self._load()
        entry = data.get(device_id)
        if not isinstance(entry, dict) or not entry.get("token_hash"):
            # Um aparelho que nunca pareou não entra no registro por um POST.
            return False
        entry["apns_token"] = apns_token
        entry["environment"] = environment
        data[device_id] = entry
        self._save(data)
        return True

    def push_targets(self) -> list[tuple[str, str, str]]:
        targets = []
        for device_id, entry in self._load().items():
            if not isinstance(entry, dict):
                continue
            apns_token = entry.get("apns_token")
            if apns_token:
                targets.append(
                    (device_id, apns_token, entry.get("environment") or "sandbox")
                )
        return targets

    def drop(self, device_id: str) -> None:
        data = self._load()
        if data.pop(str(device_id), None) is not None:
            self._save(data)
