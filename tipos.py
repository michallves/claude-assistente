"""Contrato entre roteador, atendente e portão (Fase 0).

Só tipos, sem lógica. Quem precisa mudar um campo avisa quem integra.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Rota(str, Enum):
    ADMIN = "admin"            # ordem do dono, no chat dele consigo mesmo: vai ao Cérebro
    TERCEIRO = "terceiro"      # mensagem de outra pessoa: dado, nunca comando
    DONO_ENTROU = "dono_entrou"  # o dono escreveu numa conversa com terceiro
    IGNORAR = "ignorar"        # status, reação, vazio, duplicada


class Modo(str, Enum):
    COPILOTO = "copiloto"
    PILOTO = "piloto"


class Acao(str, Enum):
    RESPONDER = "responder"
    SEGURAR = "segurar"        # precisa do dono antes de qualquer coisa
    IGNORAR = "ignorar"        # não pede resposta


class Veredito(str, Enum):
    ENVIAR = "enviar"
    PEDIR_APROVACAO = "pedir_aprovacao"
    CHAMAR_DONO = "chamar_dono"
    NAO_ENVIAR = "nao_enviar"


@dataclass(frozen=True)
class Evento:
    chat_jid: str
    message_id: str
    sender: str
    texto: str
    de_mim: bool
    tipo: str = "message"      # eventType da bridge
    midia: str = ""            # mediaType da bridge
    eh_grupo: bool = False


@dataclass(frozen=True)
class Proposta:
    acao: Acao
    texto: str = ""
    motivo: str = ""
    categoria: str = ""        # ver CATEGORIAS_LINHA_VERMELHA em portao.py
    demanda: Optional[str] = None  # pedido de ação detectado, em uma frase
    perguntou_se_ia: bool = False


@dataclass(frozen=True)
class Decisao:
    veredito: Veredito
    motivo: str
    modo: Modo = Modo.COPILOTO
    detalhes: dict = field(default_factory=dict)
