"""Comandos curtos do dono para controlar o piloto.

`tratar` não sabe quem escreveu: chame só para mensagens em Rota.ADMIN. Vinda de
terceiro, "assume tudo" abriria o portão.
"""
import re
from typing import Optional

import config
import contexto
import portao

_TAMANHO_MAXIMO = 80
_PONTUACAO_FINAL = " .!?…"
_JID = re.compile(r"^[0-9a-z.:_-]+@(?:s\.whatsapp\.net|g\.us|lid)$")


def _escopos_conhecidos() -> dict:
    mapa = getattr(config, "ESCOPOS_PILOTO", {})
    if not isinstance(mapa, dict):
        return {}
    conhecidos = {}
    for nome, jids in mapa.items():
        if isinstance(nome, str) and isinstance(jids, (list, tuple, set, frozenset)):
            validos = [j for j in jids if isinstance(j, str) and j]
            if validos:
                conhecidos[portao.normalizar(nome)] = validos
    return conhecidos


def _aviso_trava() -> str:
    if portao.trava_envio_aberta():
        return "A trava de envio está aberta: vou responder sozinho nesse alcance."
    return "A trava de envio está fechada: nada será enviado ainda, só registro no diário."


def _volta() -> str:
    if portao.ligar_corte():
        return "Corte ligado e piloto desligado. Nada sai sem você."
    return ("Corte ligado e piloto desligado, mas NÃO consegui gravar em disco: "
            "vale só até o próximo reinício.")


def _copiloto() -> str:
    gravou = portao.voltar_ao_copiloto()
    resposta = "Modo copiloto: toda resposta passa por você."
    if not gravou:
        resposta += " NÃO consegui gravar em disco: vale só até o próximo reinício."
    return resposta


def _assume_tudo() -> str:
    if not portao.assumir_tudo():
        return "Não consegui gravar em disco, então nada mudou."
    return f"Piloto em todos os chats e corte desligado. {_aviso_trava()}"


def _assume(alvo: str) -> Optional[str]:
    conhecidos = _escopos_conhecidos()
    if alvo in conhecidos:
        jids = conhecidos[alvo]
    elif _JID.match(alvo):
        jids = [alvo]
    elif " " in alvo:
        return None
    else:
        nomes = ", ".join(sorted(conhecidos)) or "nenhum"
        return (f"Não conheço \"{alvo}\", então nada mudou. "
                f"Use o JID completo ou um escopo: {nomes}.")

    if not portao.assumir_escopo(alvo, jids):
        return "Não consegui gravar em disco, então nada mudou."
    resposta = f"Piloto em {alvo}. {_aviso_trava()}"
    if portao.estado().corte:
        resposta += " O corte continua ligado; só \"assume tudo\" desliga."
    return resposta


def _devolve(alvo: str) -> Optional[str]:
    conhecidos = _escopos_conhecidos()
    pausados = portao.estado().pausados
    if alvo in conhecidos:
        candidatos = conhecidos[alvo]
    elif _JID.match(alvo):
        candidatos = [alvo]
    elif " " in alvo:
        return None
    else:
        # O status lista JIDs, mas é natural digitar só o número deles.
        candidatos = [j for j in pausados if j.split("@")[0] == alvo]

    alvos = [j for j in candidatos if j in pausados]
    if not alvos:
        return f"Nenhum chat pausado corresponde a \"{alvo}\", então nada mudou."
    if not portao.devolver(alvos):
        return "Não consegui gravar em disco, então a pausa continua."
    return f"Devolvido: {', '.join(alvos)}."


def _status() -> str:
    atual = portao.estado()
    if atual.tudo:
        escopos = "tudo"
    else:
        escopos = ", ".join(sorted(atual.escopos)) or "nenhum"
    trava = ("aberta" if portao.trava_envio_aberta()
             else "fechada, nada sai para terceiros")
    linhas = [
        f"Modo: {atual.modo.value}",
        f"Escopos: {escopos}",
        f"Pausados: {', '.join(atual.pausados) or 'nenhum'}",
        f"Corte: {'ligado' if atual.corte else 'desligado'}",
        f"Trava de envio: {trava}",
    ]
    if atual.aviso:
        linhas.append(f"Atenção: {atual.aviso}; usando o estado seguro.")
    return "\n".join(linhas)


def tratar(texto: str) -> Optional[str]:
    """Resposta do comando, ou None se o texto não for comando (segue ao Cérebro)."""
    if not isinstance(texto, str):
        return None
    comando = portao.normalizar(texto).rstrip(_PONTUACAO_FINAL)
    if not comando or len(comando) > _TAMANHO_MAXIMO:
        return None

    if comando in ("volta", "voltei"):
        return _volta()
    if comando == "copiloto":
        return _copiloto()
    if comando == "assume tudo":
        return _assume_tudo()
    if comando == "status":
        return _status()
    if comando == "diario":
        return contexto.resumo_diario()
    if comando.startswith("assume "):
        return _assume(comando[len("assume "):])
    if comando.startswith("devolve "):
        return _devolve(comando[len("devolve "):])
    return None
