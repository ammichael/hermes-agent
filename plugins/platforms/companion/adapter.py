"""Stub do adapter da plataforma Companion — a classe real chega na Task 9.

Ele existe agora porque `__init__.py` reexporta `register`, e importar
`plugins.platforms.companion.devices` executa o `__init__` do pacote. Sem este
arquivo, o módulo de dispositivos seria inimportável.

Enquanto não existir `plugin.yaml` neste diretório, nem o enum `Platform`
(`gateway/config.py:395`) nem o carregador de plugins do CLI enxergam o pacote:
os dois exigem manifesto **e** `__init__.py`. Ou seja, ninguém chama este
`register` antes da Task 9.
"""


def register(ctx) -> None:  # completado na Task 9
    raise NotImplementedError("Task 9")
