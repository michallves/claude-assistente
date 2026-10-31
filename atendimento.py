"""Caminho de uma mensagem de terceiro: Atendente propõe, portão decide.

Nada aqui fala com o Cérebro nem interpreta comando: este texto é dado.
"""
import sys
import threading
import time
from dataclasses import replace

import atendente
import canal_whatsapp as canal
import config
import contexto
import midia
import portao
from tipos import Acao, Evento, Proposta, Veredito

# Rajada em grupo não pode abrir um processo claude por mensagem.
_vagas = threading.BoundedSemaphore(2)

_esperas = {}
_lock_esperas = threading.Lock()

# Um "já te respondo" por conversa neste intervalo: repetido, entrega o robô.
_INTERVALO_GANHAR_TEMPO_SEG = 6 * 3600
_ultimo_ganho_de_tempo = {}

# De madrugada só estes acordam o dono; o resto espera a hora em que ele costuma estar.
_CATEGORIAS_URGENTES = frozenset({"saude", "familia", "seguranca"})

# O que o próprio piloto enviou, para o eco disso não parecer o dono entrando na conversa.
_JANELA_ECO_SEG = 300
_enviados = []


def _agendar(chat_jid: str, atraso: float, funcao, *args) -> None:
    """Uma mensagem nova da pessoa cancela o que estava esperando nesse chat, tanto
    a proposta quanto o envio: a resposta velha já não responde à conversa."""
    if atraso <= 0:
        funcao(*args)
        return
    timer = threading.Timer(atraso, funcao, args)
    timer.daemon = True
    with _lock_esperas:
        anterior = _esperas.get(chat_jid)
        if anterior:
            anterior.cancel()
        _esperas[chat_jid] = timer
    timer.start()


def _lembrar_enviado(chat_jid: str, texto: str) -> None:
    agora = time.monotonic()
    with _lock_esperas:
        _enviados[:] = [e for e in _enviados if agora - e[0] < _JANELA_ECO_SEG]
        _enviados.append((agora, chat_jid, texto.strip()))


def _eco_do_piloto(evento: Evento) -> bool:
    agora = time.monotonic()
    with _lock_esperas:
        return any(agora - quando < _JANELA_ECO_SEG and chat == evento.chat_jid
                   and texto == evento.texto.strip()
                   for quando, chat, texto in _enviados)


def _propor(evento: Evento) -> Proposta:
    historico = contexto.historico(evento.chat_jid, excluir_id=evento.message_id)
    perfil = contexto.perfil(evento.chat_jid)
    if historico is None or not perfil:
        return Proposta(acao=Acao.SEGURAR, motivo="sem histórico ou perfil para responder")
    with _vagas:
        return atendente.propor(evento, historico, perfil)


def _enviar_linhas(evento: Evento, texto: str, decisao) -> None:
    for linha in texto.splitlines():
        # Um "volta" no meio da sequência corta o resto.
        if linha.strip() and portao.liberar_envio(decisao):
            _lembrar_enviado(evento.chat_jid, linha)
            canal.enviar_mensagem(linha, destinatario=evento.chat_jid)


def _ganhar_tempo(evento: Evento, proposta: Proposta, decisao) -> None:
    agora = time.monotonic()
    ultimo = _ultimo_ganho_de_tempo.get(evento.chat_jid)
    if ultimo is not None and agora - ultimo < _INTERVALO_GANHAR_TEMPO_SEG:
        return
    if portao.pode_ganhar_tempo(proposta, decisao):
        _ultimo_ganho_de_tempo[evento.chat_jid] = agora
        _lembrar_enviado(evento.chat_jid, proposta.ganhar_tempo)
        canal.enviar_mensagem(proposta.ganhar_tempo, destinatario=evento.chat_jid)


def _chamar_dono(evento: Evento, decisao) -> None:
    nome = contexto.nome_do_chat(evento.chat_jid)
    disse = (evento.texto or f"[{evento.midia}]")[:300]
    canal.enviar_mensagem(f"Preciso de você com {nome} ({decisao.motivo}).\n"
                          f"{nome} disse: {disse}")


def _atender(evento: Evento) -> None:
    # Pode ter mudado durante a espera: "volta" ou o dono entrando na conversa.
    if not portao.em_piloto(evento.chat_jid):
        return
    if evento.midia == "audio":
        transcrito = midia.transcrever(evento.chat_jid, evento.message_id)
        if transcrito:
            # O conteúdo vira texto comum e passa pelas mesmas regras do portão.
            evento = replace(evento, texto=f"[áudio transcrito] {transcrito}", midia="")
    proposta = _propor(evento)
    decisao = portao.decidir(evento, proposta)
    portao.registrar(evento, proposta, decisao)
    if portao.liberar_envio(decisao):
        atraso = contexto.atraso_de_resposta(
            evento.chat_jid, ja_passou=getattr(config, "ESPERA_RAJADA_SEG", 0))
        _agendar(evento.chat_jid, atraso, _enviar_linhas, evento, proposta.texto, decisao)
    # Com a trava fechada o diário basta: chamar o dono a cada mensagem só faria barulho.
    elif decisao.veredito is Veredito.CHAMAR_DONO and portao.trava_envio_aberta():
        _ganhar_tempo(evento, proposta, decisao)
        categoria = portao.normalizar(str(proposta.categoria or ""))
        espera = (0.0 if categoria in _CATEGORIAS_URGENTES else
                  contexto.segundos_ate_hora_ativa(contexto.horas_ativas(evento.chat_jid)))
        if espera:
            # Fora do _agendar: esse aviso é para o dono e não pode ser cancelado
            # por uma mensagem nova da pessoa.
            timer = threading.Timer(espera, _chamar_dono, [evento, decisao])
            timer.daemon = True
            timer.start()
        else:
            _chamar_dono(evento, decisao)


def ao_terceiro(evento: Evento) -> None:
    evento = replace(evento, chat_jid=contexto.jid_canonico(evento.chat_jid))
    if not portao.em_piloto(evento.chat_jid):
        return
    # As pessoas mandam várias mensagens seguidas: responde uma vez, à última,
    # e as anteriores já vêm no histórico.
    _agendar(evento.chat_jid, getattr(config, "ESPERA_RAJADA_SEG", 20.0), _atender, evento)


def ao_dono_entrou(evento: Evento) -> None:
    evento = replace(evento, chat_jid=contexto.jid_canonico(evento.chat_jid))
    # Com a trava fechada o piloto só observa, e seguir observando é o que
    # permite comparar no diário o que ele diria com o que o dono respondeu.
    if (portao.trava_envio_aberta() and portao.em_piloto(evento.chat_jid)
            and not _eco_do_piloto(evento)):
        portao.ao_dono_entrou(evento)
        with _lock_esperas:
            pendente = _esperas.pop(evento.chat_jid, None)
        if pendente:
            pendente.cancel()
        print(f"[atendimento] Você entrou em {evento.chat_jid}; saí dessa conversa.",
              file=sys.stderr)
