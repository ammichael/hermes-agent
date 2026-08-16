"""Envio de alerta APNs para o Companion.

Por que `curl` e não `httpx`: a APNs exige HTTP/2, e o venv do Hermes não tem
`h2` instalado — `httpx` negocia HTTP/1.1 e a Apple recusa. O molde é o de
`companion-live-activity-banner.py` no repositório do app, que já resolve isto
em produção há meses.

O corpo vai num arquivo `0600` dentro de um diretório temporário `0700`, e a
config do curl vai por stdin, para que nem o JWT nem o device token apareçam na
linha de comando (visível em `ps` para qualquer processo do usuário).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess
import tempfile
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

TOPIC = "dev.meevi.n"
PRODUCTION_HOST = "api.push.apple.com"
SANDBOX_HOST = "api.sandbox.push.apple.com"
# A Apple aceita JWT de até 1 hora; renovar a cada 45 min dá folga sem
# reassinar a cada push.
JWT_TTL_SECONDS = 45 * 60


class APNsSender:
    def __init__(
        self,
        credentials_dir: pathlib.Path,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        self._dir = pathlib.Path(credentials_dir)
        self._runner = runner
        self._jwt: Optional[str] = None
        self._jwt_at: float = 0.0

    # --------------------------------------------------------- credenciais

    def _credentials(self) -> Optional[tuple[str, str, str]]:
        try:
            pem = (self._dir / "apns.p8").read_text(encoding="utf-8")
            meta = json.loads((self._dir / "apns.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict):
            return None
        key_id = str(meta.get("keyID") or "").strip()
        team_id = str(meta.get("teamID") or "").strip()
        if not pem.strip() or not key_id or not team_id:
            return None
        return (pem, key_id, team_id)

    def available(self) -> bool:
        return self._credentials() is not None

    def _bearer(self, monotonic: float) -> Optional[str]:
        if self._jwt and (monotonic - self._jwt_at) < JWT_TTL_SECONDS:
            return self._jwt
        creds = self._credentials()
        if creds is None:
            return None
        pem, key_id, team_id = creds
        try:
            import jwt as pyjwt

            token = pyjwt.encode(
                {"iss": team_id, "iat": int(time.time())},
                pem,
                algorithm="ES256",
                headers={"kid": key_id},
            )
        except Exception:
            # Nunca logar a chave nem o erro cru: o traceback do PyJWT pode
            # carregar material do PEM.
            logger.warning("[companion] APNs JWT signing failed")
            return None
        self._jwt = token if isinstance(token, str) else token.decode()
        self._jwt_at = monotonic
        return self._jwt

    # ---------------------------------------------------------------- envio

    def send_alert(
        self,
        *,
        device_token: str,
        environment: str,
        title: str,
        body: str,
        session_id: str,
        message_id: str,
    ) -> int:
        """Devolve o código HTTP da APNs, ou 0 quando o curl não respondeu."""
        bearer = self._bearer(time.monotonic())
        if not bearer:
            return 0

        host = PRODUCTION_HOST if environment == "production" else SANDBOX_HOST
        payload = {
            "aps": {
                "alert": {"title": title, "body": body},
                "sound": "default",
                "thread-id": session_id,
            },
            "kind": "message",
            "session_id": session_id,
            "message_id": str(message_id),
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        with tempfile.TemporaryDirectory(prefix="n-companion-push-") as temp_dir:
            os.chmod(temp_dir, 0o700)
            directory = pathlib.Path(temp_dir)
            body_path = directory / "body"
            headers_path = directory / "headers"
            body_path.write_text(raw, encoding="utf-8")
            os.chmod(body_path, 0o600)
            headers_path.touch()
            os.chmod(headers_path, 0o600)

            config = "\n".join((
                f'url = "https://{host}/3/device/{device_token}"',
                f'header = "authorization: bearer {bearer}"',
                f'header = "apns-topic: {TOPIC}"',
                'header = "apns-push-type: alert"',
                'header = "apns-priority: 10"',
                f'header = "apns-collapse-id: n-msg-{session_id}"',
                'header = "content-type: application/json"',
                f'data-binary = "@{body_path}"',
            ))

            try:
                self._runner(
                    [
                        "/usr/bin/curl", "-sS", "--config", "-",
                        "-D", str(headers_path),
                        "-o", str(directory / "provider-body"),
                        "--http2", "-X", "POST",
                    ],
                    input=config,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=30,
                    check=False,
                )
            except Exception:
                # Fronteira de subprocesso: nunca deixar o comando (que contém
                # o bearer) vazar num traceback.
                logger.warning("[companion] APNs request failed to run")
                return 0

            return _status_from_headers(headers_path)


def _status_from_headers(path: pathlib.Path) -> int:
    try:
        first = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return 0
    parts = first.split()
    for part in parts[1:]:
        if part.isdigit():
            return int(part)
    return 0
