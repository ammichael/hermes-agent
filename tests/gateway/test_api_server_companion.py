"""Cobre as duas edições de core que o plugin companion depende.

Estas duas linhas vivem no api_server.py, fora do plugin, e são exatamente
o que um `hermes update` desfaz em silêncio. Sem este arquivo, a regressão
aparece como "o push parou" e "a imagem sumiu ao reabrir a conversa".
"""

from gateway.platforms.api_server import APIServerAdapter


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
