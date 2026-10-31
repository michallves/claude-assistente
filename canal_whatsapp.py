"""Canal de entrada e saída do assistente: a bridge do whatsapp-mcp.

O assistente NÃO traz um MCP de WhatsApp próprio. Aqui só ficam duas coisas
simples:

- entrada: a bridge faz POST de cada mensagem em `WEBHOOK_URL` (um servidor
  local sobe em `iniciar_webhook`). Cada mensagem é classificada pelo
  `roteador` e despachada: só a rota ADMIN chega ao Claude como ordem.
- saída: a resposta final do Claude volta pelo `/api/send` da bridge.

Tudo o que o Claude faz além disso com WhatsApp (buscar contato, ler
histórico, mandar mensagem a terceiros, transcrever áudio, ver imagem) ele
faz como faz com qualquer outro MCP: pelas ferramentas do servidor
`whatsapp` já configurado no Claude Code do usuário.

Repositório da bridge e do MCP: https://github.com/michallves/whatsapp-mcp
"""
import hmac
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

import config
from roteador import classificar, evento_de_payload
from tipos import Rota

_lock_envio = threading.Lock()
_ultimo_envio = 0.0

# Textos que o próprio assistente acabou de mandar. No chat do dono consigo
# mesmo, o comando dele e a resposta do bot chegam os dois com isFromMe=true;
# o eco da resposta é reconhecido por este registro.
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


def para_whatsapp(texto: str) -> str:
    """Converte o markdown que o Claude costuma escrever para o dialeto do
    WhatsApp: `**negrito**` vira `*negrito*` e títulos `# x` viram `*x*`.
    Blocos de código (```) ficam como estão."""
    partes = re.split(r"(```.*?```)", texto, flags=re.S)
    for i in range(0, len(partes), 2):
        t = re.sub(r"\*\*(.+?)\*\*", r"*\1*", partes[i], flags=re.S)
        t = re.sub(r"^#{1,6}[ \t]+(.+?)[ \t]*$", r"*\1*", t, flags=re.M)
        partes[i] = t
    return "".join(partes)


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
    texto = para_whatsapp(texto)
    # Só o que vai ao chat do dono volta como eco nele; lembrar o resto faria
    # uma ordem igual a um texto enviado a terceiro ser descartada.
    if alvo in getattr(config, "JIDS_DONO", {config.ADMIN_JID}):
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
    texto = dados.get("content")
    texto = texto.strip() if isinstance(texto, str) else ""
    midia = dados.get("mediaType") or ""
    if not midia:
        return texto
    aviso = (f"[O usuário mandou {midia} pelo WhatsApp. "
             f"chat_jid={dados.get('chatJID')} message_id={dados.get('messageId')}. "
             "Use as ferramentas do MCP whatsapp (transcribe_audio, view_media, "
             "download_media) para ler o conteúdo.]")
    return f"{aviso}\n{texto}" if texto else aviso


def _token_valido(enviado: str) -> bool:
    # Em bytes: compare_digest lança TypeError com str não ASCII, e o cabeçalho
    # vem de quem ainda não foi autenticado.
    return bool(config.BRIDGE_TOKEN) and hmac.compare_digest(
        enviado.encode(), config.BRIDGE_TOKEN.encode())


def _ler_json(handler: BaseHTTPRequestHandler):
    try:
        tamanho = max(int(handler.headers.get("Content-Length") or 0), 0)
    except ValueError:
        tamanho = 0
    try:
        return json.loads(handler.rfile.read(tamanho) or b"{}")
    except ValueError:
        return {}


def _rodar_callback(callback, evento) -> None:
    # A mensagem da exceção pode trazer texto de terceiro, então só o tipo
    # vai para o log.
    try:
        callback(evento)
    except Exception as e:  # noqa: BLE001
        print(f"[canal] Falha ao tratar evento: {type(e).__name__}")


def _fazer_handler(ao_admin, ao_terceiro=None, ao_dono_entrou=None):
    callbacks_de_evento = {Rota.TERCEIRO: ao_terceiro,
                           Rota.DONO_ENTROU: ao_dono_entrou}

    def despachar(dados) -> None:
        evento = evento_de_payload(dados)
        jids_dono = getattr(config, "JIDS_DONO", {config.ADMIN_JID})
        rota = classificar(evento, jids_dono)
        if rota == Rota.ADMIN:
            texto = _texto_do_evento(dados)
            if not texto or _eh_eco_proprio(texto):
                return
            threading.Thread(target=ao_admin, args=(texto,),
                             daemon=True).start()
            return
        callback = callbacks_de_evento.get(rota)
        if callback is not None:
            threading.Thread(target=_rodar_callback, args=(callback, evento),
                             daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silencia o log por requisição
            pass

        def do_POST(self):
            if self.path != "/webhook":
                self.send_response(404)
                self.end_headers()
                return
            if not _token_valido(self.headers.get("X-Bridge-Token", "")):
                self.send_response(401)
                self.end_headers()
                return
            dados = _ler_json(self)
            # Responde logo; o processamento não segura a bridge.
            self.send_response(200)
            self.end_headers()
            despachar(dados)

    return Handler


def iniciar_webhook(ao_admin, ao_terceiro=None, ao_dono_entrou=None) -> None:
    """Sobe o servidor do webhook numa thread daemon (não bloqueia).

    `ao_admin(texto)` recebe as ordens do dono. `ao_terceiro(evento)` e
    `ao_dono_entrou(evento)` recebem o Evento; sem callback, a rota é
    descartada em silêncio.
    """
    if not config.ADMIN_JID or not config.BRIDGE_TOKEN:
        raise RuntimeError("Defina ADMIN_JID e WHATSAPP_BRIDGE_TOKEN "
                           "(variável de ambiente ou arquivo em secrets/).")
    servidor = ThreadingHTTPServer(
        (config.WEBHOOK_HOST, config.WEBHOOK_PORTA),
        _fazer_handler(ao_admin, ao_terceiro, ao_dono_entrou))
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    print(f"[canal] Webhook escutando em "
          f"{config.WEBHOOK_HOST}:{config.WEBHOOK_PORTA}/webhook")
