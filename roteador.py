"""Decide para onde vai cada mensagem que a bridge entrega.

Funções puras: sem rede, sem config e sem estado.
"""
import re

from tipos import Evento, Rota

_SUFIXO_DISPOSITIVO = re.compile(r":\d+(?=@)")

# Listas de transmissão entram junto com status@broadcast: o envio do dono
# para uma lista não é uma conversa em que ele entrou.
_SERVIDORES_SEM_CONVERSA = ("broadcast", "newsletter")


def normalizar_jid(jid) -> str:
    """Tira o sufixo de dispositivo: `5511999:12@lid` vira `5511999@lid`.

    O id da sessão do WhatsApp vem nessa forma; sem normalizar, o chat do
    dono deixaria de casar com `jids_dono` conforme o aparelho.
    """
    if not isinstance(jid, str):
        return ""
    return _SUFIXO_DISPOSITIVO.sub("", jid.strip(), count=1)


def _texto_ou_vazio(valor) -> str:
    return valor if isinstance(valor, str) else ""


def evento_de_payload(dados: dict) -> Evento:
    """Converte o JSON da bridge em Evento. Nunca lança: o que vier fora do
    formato vira campo vazio e cai em IGNORAR na classificação."""
    if not isinstance(dados, dict):
        dados = {}
    chat_jid = normalizar_jid(dados.get("chatJID"))
    return Evento(
        chat_jid=chat_jid,
        message_id=_texto_ou_vazio(dados.get("messageId")),
        sender=_texto_ou_vazio(dados.get("sender")),
        texto=_texto_ou_vazio(dados.get("content")).strip(),
        # Só o booleano conta: a string "false" é verdadeira em Python e
        # abriria a rota ADMIN.
        de_mim=dados.get("isFromMe") is True,
        # A bridge omite eventType em mensagem comum.
        tipo=_texto_ou_vazio(dados.get("eventType")) or "message",
        midia=_texto_ou_vazio(dados.get("mediaType")),
        eh_grupo=chat_jid.endswith("@g.us"),
    )


def classificar(evento: Evento, jids_dono: set) -> Rota:
    """Escolhe a rota do evento. `jids_dono` são as formas do chat do dono
    consigo mesmo (número@s.whatsapp.net e eventual @lid)."""
    jid = normalizar_jid(evento.chat_jid)
    usuario, _, servidor = jid.partition("@")
    if not usuario or not servidor or servidor in _SERVIDORES_SEM_CONVERSA:
        return Rota.IGNORAR
    # A bridge marca a reação nos dois campos e põe o emoji em `content`.
    if evento.tipo == "reaction" or evento.midia == "reaction":
        return Rota.IGNORAR
    if not evento.texto.strip() and not evento.midia:
        return Rota.IGNORAR

    # Fronteira de segurança: ADMIN exige o chat do dono E a mensagem ser
    # dele. Grupo fica de fora mesmo listado em `jids_dono`, porque outra
    # pessoa pode participar dele.
    grupo = evento.eh_grupo or servidor == "g.us"
    if not grupo and jid in {normalizar_jid(j) for j in jids_dono}:
        return Rota.ADMIN if evento.de_mim is True else Rota.IGNORAR
    return Rota.DONO_ENTROU if evento.de_mim is True else Rota.TERCEIRO
