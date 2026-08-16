"""O app Apple do N como plataforma do Hermes.

Sem socket de entrada: as mensagens continuam chegando pelas rotas de sessão do
`api_server`. O adapter existe para dois papéis que só uma plataforma tem:

1. **Saída.** `send()` escreve na sessão; o push nasce do gatilho que o
   `watcher` observa. Um produtor só — nenhuma dedupe a manter.
2. **Plano de controle.** `verify_http_event_request` e `dispatch_http_event`
   servem `POST /api/platforms/companion/events`, a rota genérica que o
   `api_server` já tem (`gateway/platforms/api_server.py:1873`), autenticada
   pelo verificador do adapter e não pela `API_SERVER_KEY`.
"""

from __future__ import annotations

import asyncio
import contextvars
import fcntl
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

from .apns import APNsSender
from .devices import DeviceStore
from .watcher import MessageWatcher

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.0
# Tipos de evento que o telefone pode mandar antes de ter credencial: são o
# enrolamento, e exigir token neles seria exigir o que ainda não existe.
UNAUTHENTICATED_TYPES = frozenset({"pair.request", "pair.claim"})
# Todo tipo servido por este adapter. O que não está aqui é recusado como
# desconhecido antes de qualquer efeito; o que está e não é enrolamento exige
# credencial. Um tipo novo entra exigindo credencial por padrão.
KNOWN_TYPES = frozenset({"pair.request", "pair.claim", "push.register"})

# O aparelho autenticado pertence à REQUISIÇÃO, nunca ao adapter.
#
# O `api_server` chama `verify_http_event_request` e, em seguida,
# `dispatch_http_event` — dois awaits na mesma task, sem nenhum parâmetro que
# ligue um ao outro. Guardar o resultado em `self._authenticated_device` parecia
# a ponte óbvia e é um bypass: o aiohttp atende requisições concorrentes no
# mesmo event loop, então basta um anônimo entrar entre o `verify` e o
# `dispatch` de um aparelho autenticado para o anônimo herdar a credencial dele.
# Um `ContextVar` é a ponte correta: ele é por task, e uma task não vê o valor
# da outra.
#
# É por isto também que `verify_http_event_request` é `async`: o handler só
# executa o verificador no event loop quando ele é corrotina
# (`api_server.py:1909-1914`); sendo síncrono, ele vai para `asyncio.to_thread`,
# e o contexto copiado para a thread é descartado no retorno — o valor gravado
# lá nunca chegaria ao `dispatch`.
_authenticated_device: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "companion_authenticated_device", default=None
)


class CompanionAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("companion"))

        extra = getattr(config, "extra", None) or {}
        companion_dir = Path(
            extra.get("companion_dir") or Path("~/.hermes/companion").expanduser()
        )
        self._state_db = str(
            extra.get("state_db") or Path("~/.hermes/state.db").expanduser()
        )

        self._devices = DeviceStore(companion_dir / "devices.json")
        self._apns = APNsSender(companion_dir)
        self._watcher = MessageWatcher(self._state_db, companion_dir / "push-cursor.json")
        self._pairing = None  # resolvido em connect(); injetável em teste
        self._poll_task: Optional[asyncio.Task] = None
        self._lock_path = companion_dir / "push-poller.lock"
        self._lock_fd: Optional[int] = None

    # ------------------------------------------------------- ciclo de vida

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._ensure_pairing()
        if not self._apns.available():
            # Sem credencial APNs a plataforma não sobe. Um push mudo é pior
            # que uma plataforma que não subiu: os dois parecem "dia quieto".
            logger.error("[companion] APNs credentials missing; platform will not start")
            return False
        self._watcher.bootstrap()
        # Um poller só, por mais instâncias que existam.
        #
        # Medido nesta máquina: o gateway sobe a plataforma uma vez por profile
        # — "companion connected" cinco vezes no log (default, finaya, nfo,
        # product-loop-a2a-client, tibiaura). Nenhuma delas é ciente de profile,
        # então as cinco compartilham o mesmo push-cursor.json e o mesmo
        # devices.json. Sem esta trava, uma mensagem vira cinco pushes (o
        # apns-collapse-id esconde isso na tela, o que é pior: o desperdício não
        # aparece) e o cursor sofre corrida de leitura-modificação-escrita, que
        # pode PULAR um evento — e evento pulado é notificação que nunca chega.
        #
        # `flock` é por descritor aberto, então dois `open()` no mesmo processo
        # também disputam: serve tanto para os profiles quanto para um segundo
        # gateway subindo em paralelo.
        if self._acquire_poll_lock():
            self._poll_task = asyncio.create_task(self._poll_loop())
        else:
            logger.info("[companion] another instance owns the push poller; serving control plane only")
        self._running = True
        return True

    def _acquire_poll_lock(self) -> bool:
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self._lock_path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                os.close(fd)
            except (OSError, NameError, UnboundLocalError):
                pass
            return False
        self._lock_fd = fd
        return True

    def _release_poll_lock(self) -> None:
        fd, self._lock_fd = self._lock_fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    async def disconnect(self) -> None:
        self._running = False
        task, self._poll_task = self._poll_task, None
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._release_poll_lock()

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"id": chat_id, "type": "dm", "platform": "companion"}

    def _ensure_pairing(self):
        """O `PairingStore` real só é construído quando ninguém injetou um.

        Construí-lo no `__init__` faria todo teste do adapter tocar o
        `HERMES_HOME` de verdade só para instanciar a classe.
        """
        if self._pairing is None:
            from gateway.pairing import PairingStore

            self._pairing = PairingStore()
        return self._pairing

    # --------------------------------------------------------------- push

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._drain_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("[companion] push poll iteration failed", exc_info=True)
            await asyncio.sleep(POLL_SECONDS)

    def _drain_once(self) -> None:
        targets = self._devices.push_targets()
        for message in self._watcher.pending():
            if not targets:
                # Sem aparelho registrado não há o que entregar, e segurar o
                # cursor guardaria uma fila que nunca sai. Avança.
                self._watcher.commit(message.event_id)
                continue
            accepted = False
            for device_id, apns_token, environment in list(targets):
                status = self._apns.send_alert(
                    device_token=apns_token,
                    environment=environment,
                    title=message.title,
                    body=message.preview,
                    session_id=message.session_id,
                    message_id=message.message_id,
                )
                if status == 410:
                    # Token morto. Deixar no registro é um push que falha para
                    # sempre em silêncio.
                    self._devices.drop(device_id)
                    targets = self._devices.push_targets()
                elif 200 <= status < 300:
                    accepted = True
            if accepted or not targets:
                self._watcher.commit(message.event_id)
            else:
                # Nenhum aparelho aceitou: não avança. O poll seguinte tenta de
                # novo, e um push repetido substitui a linha pelo collapse-id.
                return

    # -------------------------------------------------------------- saída

    def _append_to_session(self, chat_id: str, content: str) -> Optional[str]:
        """Escreve a linha `assistant` na conversa do app e devolve o id dela.

        Divergência do plano: ele mandava chamar
        `gateway.mirror.mirror_to_session`, mas aquela função tem outra
        assinatura (`platform` primeiro, `gateway/mirror.py:25`) e, pior, acha a
        sessão pela ORIGEM da plataforma (`gateway/mirror.py:96`) — uma conversa
        do Companion nasce pelas rotas de sessão do `api_server` e nunca grava
        origem `companion`, então a busca não acharia nada. Aqui o `chat_id`
        **é** o `session_id`: é o que o push carrega e o que o app abre no tap.
        """
        from hermes_state import SessionDB

        db = SessionDB(db_path=Path(self._state_db))
        try:
            if db.get_session(str(chat_id)) is None:
                return None
            row_id = db.append_message(
                session_id=str(chat_id), role="assistant", content=content
            )
            return str(row_id)
        finally:
            db.close()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Espelha na sessão. O push nasce do gatilho, não daqui.

        Se isto empurrasse direto, existiriam dois produtores de push para o
        mesmo conteúdo e uma dedupe a manter. Escrevendo na sessão, há um
        caminho só — e o conteúdo entregue fica visível na conversa em vez de
        existir apenas como notificação órfã.
        """
        try:
            written = await asyncio.to_thread(self._append_to_session, chat_id, content)
        except Exception as exc:
            logger.warning("[companion] write to session failed: %s", exc)
            return SendResult(success=False, error=str(exc))
        if not written:
            return SendResult(success=False, error="unknown_session")
        return SendResult(success=True, message_id=written)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Obrigatório: o default da base manda "Couldn't deliver the image
        attachment" e apaga o caminho de propósito (base.py:4707-4733), porque
        para uma plataforma pública ecoar o caminho vazaria o layout do disco.

        Aqui o destino é o app do dono da máquina, e o caminho nunca chega ao
        telefone: o histórico resolve `MEDIA:` para data URL antes de servir, e
        quando não resolve o app colapsa a tag para `📎 nome.png`.

        `send_multiple_images` não é sobrescrito: o default dele roteia arquivo
        local para cá.
        """
        safe = self.validate_media_delivery_path(image_path)
        if not safe:
            return SendResult(success=False, error="unsafe_media_path")
        text = f"{caption}\nMEDIA:{safe}" if caption else f"MEDIA:{safe}"
        return await self.send(chat_id=chat_id, content=text, reply_to=reply_to,
                               metadata=metadata)

    # ----------------------------------------------------- plano de controle

    @staticmethod
    def requires_credential(event_type: str) -> bool:
        return event_type not in UNAUTHENTICATED_TYPES

    async def verify_http_event_request(self, auth_header: str) -> tuple[bool, str]:
        """Chamado pelo api_server antes de `dispatch_http_event`.

        Um verificador que levanta exceção faz o handler recusar o evento
        (api_server.py:1915-1920). Este nunca levanta — devolve (False, código).

        Pedido SEM cabeçalho entra como anônimo, e não como recusa: o
        verificador roda antes de o corpo ser lido (api_server.py:1907-1928), e
        um `False` viraria 401 antes de o `type` existir — o que tornaria
        `pair.request`/`pair.claim` inalcançáveis, já que o telefone ainda não
        tem a credencial que estaria sendo exigida. Quem barra o anônimo é o
        `dispatch_http_event`, que aí sim conhece o tipo. Um cabeçalho
        PRESENTE e inválido continua sendo recusa: credencial apresentada e
        não reconhecida falha fechado.
        """
        _authenticated_device.set(None)
        header = str(auth_header or "").strip()
        if not header:
            return (True, "")
        if not header.startswith("Bearer "):
            return (False, "missing_bearer")
        device_id = self._devices.verify(header[7:].strip())
        if not device_id:
            return (False, "unknown_device_token")
        # Conferir a aprovação a cada evento é o que torna `hermes pairing
        # revoke` imediato: não há cache nem expiração a esperar.
        try:
            approved = self._ensure_pairing().is_approved("companion", device_id)
        except Exception:
            # Sem lista de aprovados não dá para afirmar que este aparelho
            # ainda pode: recusa. Nunca logar o cabeçalho nem o erro cru.
            logger.warning("[companion] pairing store unavailable; event refused")
            return (False, "pairing_unavailable")
        if not approved:
            return (False, "device_not_approved")
        _authenticated_device.set(device_id)
        return (True, "")

    async def dispatch_http_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        event_type = str(payload.get("type") or "")
        device = _authenticated_device.get()
        if event_type not in KNOWN_TYPES:
            return {"ok": False, "error": "unknown_type"}
        if self.requires_credential(event_type) and not device:
            # O anônimo chegou até aqui porque o verificador precisa deixar o
            # enrolamento passar. É este ponto que fecha a porta para o resto.
            return {"ok": False, "error": "unauthenticated"}

        if event_type == "pair.request":
            device_id = str(payload.get("device_id") or "").strip()
            device_name = str(payload.get("device_name") or "")[:64]
            if not device_id:
                return {"ok": False, "error": "missing_device_id"}
            code = self._ensure_pairing().generate_code(
                "companion", device_id, device_name
            )
            if not code:
                # generate_code devolve None em rate limit, lockout, ou
                # pendências demais. Isso tem de virar erro visível, não um
                # pareamento pendente que nunca resolve.
                return {"ok": False, "error": "pairing_refused"}
            return {"ok": True, "status": "pending", "code": code}

        if event_type == "pair.claim":
            device_id = str(payload.get("device_id") or "").strip()
            approved = bool(
                device_id
                and self._ensure_pairing().is_approved("companion", device_id)
            )
            state, token = self._devices.claim(device_id, approved=approved)
            if state == "issued":
                return {"ok": True, "status": "issued", "token": token}
            return {"ok": True, "status": state}

        if event_type == "push.register":
            ok = self._devices.register_push(
                device,
                str(payload.get("apns_token") or ""),
                str(payload.get("environment") or "sandbox"),
            )
            return {"ok": ok} if ok else {"ok": False, "error": "invalid_push_token"}

        # Inalcançável: KNOWN_TYPES e os ramos acima andam juntos.
        return {"ok": False, "error": "unknown_type"}


def check_requirements() -> bool:
    try:
        import jwt  # noqa: F401
    except Exception:
        return False
    return Path("/usr/bin/curl").exists()


def validate_config(config: PlatformConfig) -> bool:
    return Path("~/.hermes/companion/apns.p8").expanduser().exists()


def register(ctx) -> None:
    """Ponto de entrada do plugin, chamado pelo sistema de plugins do Hermes."""
    ctx.register_platform(
        name="companion",
        label="Companion",
        adapter_factory=lambda cfg: CompanionAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        install_hint="pip install pyjwt   # já é dependência do Hermes",
    )
