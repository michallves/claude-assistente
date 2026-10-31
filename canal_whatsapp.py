"""Canal de entrada e saída do assistente: a bridge do whatsapp-mcp.

O assistente NÃO traz um MCP de WhatsApp próprio. Aqui só ficam duas coisas
simples:

- entrada: a bridge faz POST de cada mensagem em `WEBHOOK_URL` (um servidor
  local sobe em `iniciar_webhook`). Só mensagens do chat `ADMIN_JID` passam;
  o resto é descartado antes de chegar ao Claude.
- saída: a resposta final do Claude volta pelo `/api/send` da bridge.

Tudo o que o Claude faz além disso com WhatsApp (buscar contato, ler
histórico, mandar mensagem a terceiros, transcrever áudio, ver imagem) ele
faz como faz com qualquer outro MCP: pelas ferramentas do servidor
`whatsapp` já configurado no Claude Code do usuário.

Repositório da bridge e do MCP: https://github.com/michallves/whatsapp-mcp
"""
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

import config

_lock_envio = threading.Lock()
_ultimo_envio = 0.0

# Textos que o próprio assistente acabou de mandar. O chat do admin é um
# grupo só com o usuário, então o comando dele e a resposta do bot chegam os
# dois com isFromMe=true; o eco da resposta é reconhecido por este registro.
_enviados_recentes: list = []
_lock_enviados = threading.Lock()
_JANELA_ECO_SEG = 180.0


def _cabecalhos() -> dict:
    return {"Authorization": f"Bearer {config.BRIDGE_TOKEN}",
            "Content-Type": "application/json"}


def _lembrar_enviado(texto: str) -> None:
    agora = time.monotonic()
    with _lock_enviados:
        _enviados_recentes.append((agora, texto.strip()))
        del _enviados_recentes[:-config.DEDUPE_MAX_MENSAGENS]


def _eh_eco_proprio(texto: str) -> bool:
    agora = time.monotonic()
    texto = texto.strip()
    with _lock_enviados:
        for i, (quando, enviado) in enumerate(_enviados_recentes):
            if agora - quando > _JANELA_ECO_SEG:
                continue
            if enviado == texto:
                del _enviados_recentes[i]
                return True
    return False


def enviar_mensagem(texto: str, destinatario: str = None) -> bool:
    """Manda `texto` pro chat do admin (ou pra `destinatario`, se informado)."""
    global _ultimo_envio
    if not texto or not texto.strip():
        return False
    alvo = destinatario or config.ADMIN_JID
    with _lock_envio:
        espera = (_ultimo_envio + config.INTERVALO_MIN_ENVIO_SEG) - time.monotonic()
        if espera > 0:
            time.sleep(espera)
        _ultimo_envio = time.monotonic()
    _lembrar_enviado(texto)
    try:
        r = requests.post(f"{config.BRIDGE_URL}/api/send", headers=_cabecalhos(),
                          json={"recipient": alvo, "message": texto}, timeout=60)
        r.raise_for_status()
        return bool(r.json().get("success", True))
    except Exception as e:  # noqa: BLE001
        print(f"[canal] Falha ao enviar: {e}")
        return False


def esta_conectado() -> bool:
    """True se a bridge está com o WhatsApp conectado. Falha de rede pontual
    não conta como queda (evita alarme falso)."""
    try:
        r = requests.get(f"{config.BRIDGE_URL}/api/health",
                         headers=_cabecalhos(), timeout=10)
        return r.status_code == 200 and bool(r.json().get("connected"))
    except Exception as e:  # noqa: BLE001
        print(f"[canal] Falha ao checar conexão: {e}")
        return True


def _texto_do_evento(dados: dict) -> str:
    """Transforma o payload da bridge no texto entregue ao Claude.

    Mídia não é baixada aqui: o Claude recebe o id da mensagem e usa as
    ferramentas do MCP `whatsapp` (transcribe_audio, view_media,
    download_media) quando precisar do conteúdo.
    """
    if dados.get("eventType") == "reaction":
        return ""
    texto = (dados.get("content") or "").strip()
    midia = dados.get("mediaType") or ""
    if not midia:
        return texto
    aviso = (f"[O usuário mandou {midia} pelo WhatsApp. "
             f"chat_jid={dados.get('chatJID')} message_id={dados.get('messageId')}. "
             "Use as ferramentas do MCP whatsapp (transcribe_audio, view_media, "
             "download_media) para ler o conteúdo.]")
    return f"{aviso}\n{texto}" if texto else aviso


def _fazer_handler(ao_receber_mensagem):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silencia o log por requisição
            pass

        def do_POST(self):
            if self.path != "/webhook":
                self.send_response(404)
                self.end_headers()
                return
            enviado = self.headers.get("X-Bridge-Token", "")
            if not config.BRIDGE_TOKEN or not hmac.compare_digest(
                    enviado, config.BRIDGE_TOKEN):
                self.send_response(401)
                self.end_headers()
                return
            tamanho = int(self.headers.get("Content-Length") or 0)
            try:
                dados = json.loads(self.rfile.read(tamanho) or b"{}")
            except ValueError:
                dados = {}
            # Responde logo; o processamento não segura a bridge.
            self.send_response(200)
            self.end_headers()

            if dados.get("chatJID") != config.ADMIN_JID:
                return
            texto = _texto_do_evento(dados)
            if not texto or _eh_eco_proprio(texto):
                return
            threading.Thread(target=ao_receber_mensagem, args=(texto,),
                             daemon=True).start()

    return Handler


def iniciar_webhook(ao_receber_mensagem) -> None:
    """Sobe o servidor do webhook numa thread daemon (não bloqueia)."""
    if not config.ADMIN_JID or not config.BRIDGE_TOKEN:
        raise RuntimeError("Defina ADMIN_JID e WHATSAPP_BRIDGE_TOKEN "
                           "(variável de ambiente ou arquivo em secrets/).")
    servidor = ThreadingHTTPServer((config.WEBHOOK_HOST, config.WEBHOOK_PORTA),
                                   _fazer_handler(ao_receber_mensagem))
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    print(f"[canal] Webhook escutando em "
          f"{config.WEBHOOK_HOST}:{config.WEBHOOK_PORTA}/webhook")
