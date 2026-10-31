"""Caminho de uma mensagem de terceiro: Atendente propõe, portão decide.

Nada aqui fala com o Cérebro nem interpreta comando: este texto é dado.
"""
import sys
import threading

import atendente
import canal_whatsapp as canal
import portao
from tipos import Evento, Veredito

# Rajada em grupo não pode abrir um processo claude por mensagem.
_vagas = threading.BoundedSemaphore(2)


def ao_terceiro(evento: Evento) -> None:
    if not portao.em_piloto(evento.chat_jid):
        return
    with _vagas:
        proposta = atendente.propor(evento, [])
    decisao = portao.decidir(evento, proposta)
    portao.registrar(evento, proposta, decisao)
    if portao.liberar_envio(decisao):
        canal.enviar_mensagem(proposta.texto, destinatario=evento.chat_jid)
    elif decisao.veredito is Veredito.CHAMAR_DONO:
        canal.enviar_mensagem(f"Preciso de você em {evento.chat_jid}: {decisao.motivo}.")


def ao_dono_entrou(evento: Evento) -> None:
    if portao.em_piloto(evento.chat_jid):
        portao.ao_dono_entrou(evento)
        print(f"[atendimento] Você entrou em {evento.chat_jid}; saí dessa conversa.",
              file=sys.stderr)
