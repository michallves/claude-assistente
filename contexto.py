"""O que o Atendente precisa saber para responder como o dono.

Só leitura: histórico da bridge (messages.db) e o texto de como o dono escreve.
"""
import json
import os
import sqlite3

import config

_LIMITE_TEXTO = 500


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


def historico(chat_jid: str, limite: int = 40, excluir_id: str = "",
              antes_de: str = None):
    """Últimas mensagens do chat, da mais antiga à mais nova. None se não der
    para ler: sem contexto o Atendente não deve responder."""
    sql = ("SELECT timestamp, is_from_me, sender, content, media_type FROM messages "
           "WHERE chat_jid = ? AND id != ?")
    params = [chat_jid, excluir_id]
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
