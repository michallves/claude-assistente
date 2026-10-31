"""O que o Atendente precisa saber para responder como o dono.

Só leitura: histórico da bridge (messages.db) e o texto de como o dono escreve.
"""
import json
import os
import random
import sqlite3
import unicodedata
from collections import Counter
from datetime import datetime, timedelta

import config

_LIMITE_TEXTO = 500
# Demora maior que isso já é outra conversa, não demora para responder.
_MAIOR_DEMORA_SEG = 3 * 3600
# Hora com menos que isto das mensagens do dono conta como hora em que ele não está.
_FRACAO_HORA_ATIVA = 0.01


def _sem_acento(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in sem_acento if not unicodedata.combining(c)).casefold().strip()


def _conectar() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{config.DB_MENSAGENS}?mode=ro", uri=True, timeout=5)


def _item(linha) -> dict:
    quando, de_mim, remetente, texto, midia = linha
    item = {"quando": quando, "de_mim": bool(de_mim),
            "texto": (texto or "")[:_LIMITE_TEXTO]}
    if midia:
        item["midia"] = midia
    if not de_mim:
        item["remetente"] = remetente
    return item


def _formas_do_chat(chat_jid: str) -> list:
    # Parte da conversa pode ter ficado guardada num chat @lid separado do número.
    if not chat_jid.endswith("@s.whatsapp.net"):
        return [chat_jid]
    try:
        with _whatsmeow() as db:
            linha = db.execute("SELECT lid FROM whatsmeow_lid_map WHERE pn = ?",
                               (chat_jid.split("@")[0],)).fetchone()
    except sqlite3.Error:
        return [chat_jid]
    return [chat_jid, f"{linha[0]}@lid"] if linha else [chat_jid]


def historico(chat_jid: str, limite: int = 40, excluir_id: str = "",
              antes_de: str = None):
    """Últimas mensagens do chat, da mais antiga à mais nova. None se não der
    para ler: sem contexto o Atendente não deve responder."""
    formas = _formas_do_chat(chat_jid)
    sql = ("SELECT timestamp, is_from_me, sender, content, media_type FROM messages "
           f"WHERE chat_jid IN ({','.join('?' * len(formas))}) AND id != ?")
    params = [*formas, excluir_id]
    if antes_de:
        sql += " AND timestamp < ?"
        params.append(antes_de)
    params.append(limite)
    try:
        with _conectar() as db:
            linhas = db.execute(sql + " ORDER BY timestamp DESC LIMIT ?", params).fetchall()
    except sqlite3.Error as erro:
        print(f"[contexto] Não li o histórico: {type(erro).__name__}")
        return None
    return [_item(l) for l in reversed(linhas)]


def perfil(chat_jid: str, amostras: int = 60, antes_de: str = None) -> str:
    """Como o dono escreve, em geral e com esta pessoa. Vazio se não der para ler."""
    try:
        with open(config.ARQUIVO_PERFIL_ESCRITA, encoding="utf-8") as f:
            regras = f.read()
        sql = ("SELECT content FROM messages WHERE chat_jid = ? AND is_from_me = 1 "
               "AND content != ''")
        params = [chat_jid]
        if antes_de:
            sql += " AND timestamp < ?"
            params.append(antes_de)
        params.append(amostras)
        with _conectar() as db:
            linhas = db.execute(sql + " ORDER BY timestamp DESC LIMIT ?", params).fetchall()
    except (OSError, sqlite3.Error) as erro:
        print(f"[contexto] Não li o perfil: {type(erro).__name__}")
        return ""
    exemplos = "\n".join(l[0][:200] for l in reversed(linhas))
    return f"{regras}\n\nMensagens reais recentes dele para esta pessoa:\n{exemplos}"


def _nome_chat(db, chat_jid: str) -> str:
    linha = db.execute("SELECT name FROM chats WHERE jid = ?", (chat_jid,)).fetchone()
    return (linha and linha[0]) or chat_jid


def resumo_diario(quantos: int = 5) -> str:
    """Compara o que o piloto diria com o que o dono de fato respondeu."""
    try:
        with open(config.ARQUIVO_DIARIO_PILOTO, encoding="utf-8") as f:
            registros = [json.loads(l) for l in f if l.strip()][-quantos:]
    except (OSError, ValueError):
        return "O diário ainda está vazio."
    if not registros:
        return "O diário ainda está vazio."
    blocos = []
    try:
        with _conectar() as db:
            for r in registros:
                chat = r["chat_jid"]
                recebida = db.execute(
                    "SELECT content, timestamp, media_type FROM messages "
                    "WHERE id = ? AND chat_jid = ?", (r["message_id"], chat)).fetchone()
                disse, voce = "(não achei)", "(nada ainda)"
                if recebida:
                    disse = recebida[0] or f"[{recebida[2]}]"
                    seguintes = db.execute(
                        "SELECT is_from_me, content FROM messages WHERE chat_jid = ? "
                        "AND timestamp > ? ORDER BY timestamp LIMIT 10",
                        (chat, recebida[1])).fetchall()
                    minhas = []
                    for de_mim, texto in seguintes:
                        if not de_mim and minhas:
                            break
                        if de_mim and texto:
                            minhas.append(texto)
                    voce = " / ".join(minhas) or voce
                proposta = r.get("texto_proposto") or "(não responderia)"
                blocos.append(f"{_nome_chat(db, chat)}: {disse[:200]}\n"
                              f"Eu diria: {proposta[:300]}\n"
                              f"Você disse: {voce[:300]}\n"
                              f"Decisão: {r['veredito']} ({r['motivo']})")
    except sqlite3.Error as erro:
        return f"Não consegui ler o histórico: {type(erro).__name__}."
    return "\n\n".join(blocos)


def _whatsmeow() -> sqlite3.Connection:
    pasta = os.path.dirname(config.DB_MENSAGENS)
    return sqlite3.connect(f"file:{pasta}/whatsapp.db?mode=ro", uri=True, timeout=5)


def jid_canonico(jid: str) -> str:
    """A mesma pessoa pode chegar como <lid>@lid; portão e escopos usam o número."""
    if not jid.endswith("@lid"):
        return jid
    try:
        with _whatsmeow() as db:
            linha = db.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ?",
                               (jid.split("@")[0],)).fetchone()
    except sqlite3.Error:
        return jid
    return f"{linha[0]}@s.whatsapp.net" if linha else jid


def jids_por_nome(nome: str) -> list:
    """Chats individuais ou grupos cujo nome é exatamente `nome`, sem caixa nem acento."""
    alvo = _sem_acento(nome)
    try:
        with _conectar() as db:
            linhas = db.execute("SELECT jid, name FROM chats WHERE name != ''").fetchall()
    except sqlite3.Error:
        return []
    return [j for j, n in linhas if _sem_acento(n) == alvo]


def nome_do_chat(chat_jid: str) -> str:
    try:
        with _conectar() as db:
            return _nome_chat(db, chat_jid)
    except sqlite3.Error:
        return chat_jid


def _padrao_do_dono(chat_jid: str):
    """(demoras reais em segundos, horas em que o dono de fato escreve nesse chat)."""
    with _conectar() as db:
        linhas = db.execute(
            "SELECT timestamp, is_from_me FROM messages WHERE chat_jid = ? "
            "AND timestamp >= date('now', '-90 day') ORDER BY timestamp",
            (chat_jid,)).fetchall()
    demoras, horas = [], Counter()
    anterior = None
    for carimbo, de_mim in linhas:
        momento = datetime.fromisoformat(carimbo).astimezone()
        if de_mim:
            horas[momento.hour] += 1
            if anterior is not None and not anterior[1]:
                segundos = (momento - anterior[0]).total_seconds()
                if 0 <= segundos < _MAIOR_DEMORA_SEG:
                    demoras.append(segundos)
        anterior = (momento, de_mim)
    total = sum(horas.values())
    ativas = {h for h, n in horas.items() if n / total >= _FRACAO_HORA_ATIVA} if total else set()
    return demoras, ativas


def horas_ativas(chat_jid: str) -> set:
    try:
        return _padrao_do_dono(chat_jid)[1]
    except (sqlite3.Error, ValueError):
        return set()


def segundos_ate_hora_ativa(ativas: set, agora: datetime = None) -> float:
    """Zero se o dono costuma escrever nesta hora; senão, até a próxima que costuma."""
    agora = agora or datetime.now().astimezone()
    if not ativas or agora.hour in ativas:
        return 0.0
    for avanco in range(1, 25):
        momento = (agora + timedelta(hours=avanco)).replace(minute=0, second=0, microsecond=0)
        if momento.hour in ativas:
            return (momento - agora).total_seconds()
    return 0.0


def atraso_de_resposta(chat_jid: str, ja_passou: float = 0.0) -> float:
    """Quanto esperar para responder no ritmo real do dono com esta pessoa. Sem
    dados, usa um atraso curto em vez de responder na hora."""
    try:
        demoras, ativas = _padrao_do_dono(chat_jid)
    except (sqlite3.Error, ValueError):
        return 60.0
    demora = random.choice(demoras) if demoras else 60.0
    fora_de_hora = segundos_ate_hora_ativa(ativas)
    if fora_de_hora:
        return fora_de_hora + random.uniform(0, 1200)
    return max(0.0, demora - ja_passou)
