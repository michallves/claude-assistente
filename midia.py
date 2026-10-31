"""Transcreve áudio recebido, no servidor local de transcrição, sem passar por IA com ferramentas."""
import os

import requests

import config


def _caminho_no_host(caminho: str) -> str:
    # A bridge roda em Docker e devolve o caminho que ela enxerga.
    if caminho.startswith("/app/store/"):
        return os.path.join(os.path.dirname(config.DB_MENSAGENS), caminho[len("/app/store/"):])
    return caminho


def transcrever(chat_jid: str, message_id: str):
    """Texto do áudio, ou None se não der: quem chama trata como mídia não lida."""
    try:
        r = requests.post(f"{config.BRIDGE_URL}/api/download",
                          headers={"Authorization": f"Bearer {config.BRIDGE_TOKEN}"},
                          json={"message_id": message_id, "chat_jid": chat_jid}, timeout=60)
        r.raise_for_status()
        corpo = r.json()
        if not corpo.get("success") or not corpo.get("path"):
            return None
        caminho = _caminho_no_host(corpo["path"])
        with open(caminho, "rb") as arquivo:
            r = requests.post(config.TRANSCRICAO_URL,
                              files={"file": (os.path.basename(caminho), arquivo)},
                              data={"model": config.TRANSCRICAO_MODELO, "language": "pt",
                                    "response_format": "json"}, timeout=180)
        r.raise_for_status()
        return (r.json().get("text") or "").strip() or None
    except (requests.RequestException, OSError, ValueError) as erro:
        print(f"[midia] Não transcrevi: {type(erro).__name__}")
        return None
