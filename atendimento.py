"""Caminho de uma mensagem de terceiro: Atendente propõe, portão decide.

Nada aqui fala com o Cérebro nem interpreta comando: este texto é dado.
"""
import sys
import threading

import atendente
import canal_whatsapp as canal
import config
import contexto
import portao
from tipos import Acao, Evento, Proposta, Veredito

# Rajada em grupo não pode abrir um processo claude por mensagem.
_vagas = threading.BoundedSemaphore(2)

_esperas = {}
_lock_esperas = threading.Lock()


def _propor(evento: Evento) -> Proposta:
    historico = contexto.historico(evento.chat_jid, excluir_id=evento.message_id)
    perfil = contexto.perfil(evento.chat_jid)
    if historico is None or not perfil:
        return Proposta(acao=Acao.SEGURAR, motivo="sem histórico ou perfil para responder")
    with _vagas:
        return atendente.propor(evento, historico, perfil)


def _atender(evento: Evento) -> None:
    # Pode ter mudado durante a espera: "volta" ou o dono entrando na conversa.
    if not portao.em_piloto(evento.chat_jid):
        return
    proposta = _propor(evento)
    decisao = portao.decidir(evento, proposta)
    portao.registrar(evento, proposta, decisao)
    if portao.liberar_envio(decisao):
        for linha in proposta.texto.splitlines():
            # Um "volta" no meio da sequência corta o resto.
            if linha.strip() and portao.liberar_envio(decisao):
                canal.enviar_mensagem(linha, destinatario=evento.chat_jid)
    # Com a trava fechada o diário basta: chamar o dono a cada mensagem só faria barulho.
    elif decisao.veredito is Veredito.CHAMAR_DONO and portao.trava_envio_aberta():
        canal.enviar_mensagem(f"Preciso de você em {evento.chat_jid}: {decisao.motivo}.")


def ao_terceiro(evento: Evento) -> None:
    if not portao.em_piloto(evento.chat_jid):
        return
    # As pessoas mandam várias mensagens seguidas: responde uma vez, à última,
    # e as anteriores já vêm no histórico.
    espera = getattr(config, "ESPERA_RAJADA_SEG", 20.0)
    if espera <= 0:
        _atender(evento)
        return
    timer = threading.Timer(espera, _atender, [evento])
    timer.daemon = True
    with _lock_esperas:
        anterior = _esperas.get(evento.chat_jid)
        if anterior:
            anterior.cancel()
        _esperas[evento.chat_jid] = timer
    timer.start()


def ao_dono_entrou(evento: Evento) -> None:
    # Com a trava fechada o piloto só observa, e seguir observando é o que
    # permite comparar no diário o que ele diria com o que o dono respondeu.
    if portao.trava_envio_aberta() and portao.em_piloto(evento.chat_jid):
        portao.ao_dono_entrou(evento)
        print(f"[atendimento] Você entrou em {evento.chat_jid}; saí dessa conversa.",
              file=sys.stderr)
