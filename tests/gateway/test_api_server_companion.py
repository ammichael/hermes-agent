"""Cobre as duas edições de core que o plugin companion depende.

Estas duas linhas vivem no api_server.py, fora do plugin, e são exatamente
o que um `hermes update` desfaz em silêncio. Sem este arquivo, a regressão
aparece como "o push parou" e "a imagem sumiu ao reabrir a conversa".
"""

import inspect
import struct
import zlib
from pathlib import Path

from gateway.platforms.api_server import APIServerAdapter, _resolve_media_to_data_urls


class TestSessionSourceAllowlist:
    def test_companion_ios_survives_normalization(self):
        assert APIServerAdapter._normalize_session_source("companion_ios") == "companion_ios"

    def test_companion_mac_survives_normalization(self):
        assert APIServerAdapter._normalize_session_source("companion_mac") == "companion_mac"

    def test_unknown_source_still_falls_back_to_api_server(self):
        # A allowlist continua sendo allowlist: um source inventado não passa.
        assert APIServerAdapter._normalize_session_source("qualquer_coisa") == "api_server"

    def test_browser_alias_is_preserved(self):
        # Regressão: o ramo especial de "browser" não pode ser perdido na edição.
        assert APIServerAdapter._normalize_session_source("browser") == "hermes_browser"


def _write_png(path: Path) -> None:
    """Um PNG 1x1 válido, sem depender de Pillow."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\xff\xff")
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


class TestHistoryMediaResolution:
    def test_media_tag_becomes_data_url(self, tmp_path):
        image = tmp_path / "grafico.png"
        _write_png(image)
        resolved = _resolve_media_to_data_urls(f"Segue o gráfico MEDIA:{image}")
        assert "data:image/png;base64," in resolved
        assert str(image) not in resolved

    def test_oversized_image_is_left_untouched(self, tmp_path, monkeypatch):
        import gateway.platforms.api_server as api_server

        image = tmp_path / "enorme.png"
        _write_png(image)
        monkeypatch.setattr(api_server, "_MEDIA_DATA_URL_MAX_BYTES", 1)
        resolved = _resolve_media_to_data_urls(f"MEDIA:{image}")
        # A tag fica crua — e é por isso que o app precisa colapsá-la (Task 17).
        assert "data:image" not in resolved


class TestSessionMessagesAppliesMediaResolution:
    def test_handler_source_applies_resolution_to_assistant_rows(self):
        """O handler tem de CHAMAR a resolução; sem isso os testes acima provam
        a função e não provam o histórico.

        A cadeia é handler -> `_resolve_history_media` -> `_resolve_media_to_data_urls`,
        e as duas pontas são afirmadas: apagar qualquer uma das duas linhas (que é
        o que um `hermes update` faz em silêncio) derruba este teste.
        """
        handler = inspect.getsource(APIServerAdapter._handle_session_messages)
        assert "_resolve_history_media" in handler

        helper = inspect.getsource(APIServerAdapter._resolve_history_media)
        assert "_resolve_media_to_data_urls" in helper

    def test_resolution_helper_touches_only_assistant_rows(self, tmp_path):
        """Uma linha de `user` com MEDIA: é o eco do que o próprio usuário
        mandou e já foi resolvida no envio — reinlinear seria dobrar os bytes."""
        image = tmp_path / "grafico.png"
        _write_png(image)

        rows = [
            {"role": "assistant", "content": f"MEDIA:{image}"},
            {"role": "user", "content": f"MEDIA:{image}"},
            {"role": "assistant", "content": "sem mídia nenhuma"},
        ]
        resolved = APIServerAdapter._resolve_history_media(rows)

        assert "data:image/png;base64," in resolved[0]["content"]
        assert resolved[1]["content"] == f"MEDIA:{image}"
        assert resolved[2]["content"] == "sem mídia nenhuma"

    def test_resolution_helper_survives_a_row_without_content(self):
        # get_messages devolve linhas de tool call sem `content` string.
        rows = [{"role": "assistant"}, {"role": "assistant", "content": None}]
        assert APIServerAdapter._resolve_history_media(rows) == rows
