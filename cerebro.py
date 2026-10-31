"""Ponte com o Claude Code via subprocess.

Mantém UMA sessão do Claude "quente" (viva) entre comandos, usando o modo
streaming do próprio CLI (stream-json). Isso mantém o Playwright/MCP aquecido
e deixa os comandos bem mais rápidos. Se a sessão quente falhar, cai
automaticamente para o modo `claude -p --continue` (mais lento, porém robusto).

Roda sempre com cwd = pasta do projeto, para carregar CLAUDE.md, .mcp.json
e .claude/settings.json (permissões pré-aprovadas).
"""
import json
import os
import queue
import shutil
import subprocess
import threading
import time

import config

def _garantir_node_no_path():
    """Garante que o Node esteja no PATH do processo (o claude precisa dele)."""
    candidatos = [
        r"C:\Program Files\nodejs",
        r"C:\Program Files (x86)\nodejs",
        os.path.join(os.environ.get("ProgramFiles", ""), "nodejs"),
    ]
    atual = os.environ.get("PATH", "")
    for d in candidatos:
        if d and os.path.isdir(d) and d.lower() not in atual.lower():
            os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
            atual = os.environ["PATH"]


def _achar_claude() -> str:
    """Localiza o executável do Claude Code de forma robusta."""
    encontrado = shutil.which("claude")
    if encontrado:
        return encontrado
    appdata = os.environ.get("APPDATA", "")
    for nome in ("claude.cmd", "claude.CMD", "claude.exe"):
        cand = os.path.join(appdata, "npm", nome)
        if os.path.isfile(cand):
            return cand
    return "claude"


_garantir_node_no_path()
CLAUDE_BIN = _achar_claude()


# ---------------------------------------------------------------------------
# Modo simples (one-shot) — usado no login e como fallback
# ---------------------------------------------------------------------------
def _rodar(args):
    return subprocess.run(
        [CLAUDE_BIN, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=config.TIMEOUT_CLAUDE,
        cwd=config.PASTA_PROJETO,
    )


def esta_logado() -> bool:
    """Retorna True se o Claude Code já está autenticado.

    Usa `claude auth status --json`: checagem local, ~0.5s, sem gastar uma
    chamada de modelo (bem mais rápido que testar com um `-p "ok"`).
    """
    try:
        resultado = subprocess.run(
            [CLAUDE_BIN, "auth", "status", "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, cwd=config.PASTA_PROJETO,
        )
    except subprocess.TimeoutExpired:
        return False
    except FileNotFoundError:
        return False
    try:
        info = json.loads(resultado.stdout or "{}")
        return bool(info.get("loggedIn"))
    except json.JSONDecodeError:
        return False


def iniciar_login():
    """Abre o Claude Code de forma interativa para o usuário fazer login."""
    try:
        subprocess.run([CLAUDE_BIN], cwd=config.PASTA_PROJETO)
    except FileNotFoundError:
        print("Claude Code não encontrado. Instale com "
              "npm install -g @anthropic-ai/claude-code")


def _executar_oneshot(texto: str, continuar: bool = True) -> str:
    """Modo antigo, uma chamada por comando. Fallback confiável."""
    base = ["-p", texto, "--output-format", "text"]
    try:
        if continuar:
            resultado = _rodar(["--continue", *base])
            stderr = (resultado.stderr or "").lower()
            if resultado.returncode != 0 and "conversation" in stderr:
                resultado = _rodar(base)
        else:
            resultado = _rodar(base)
    except subprocess.TimeoutExpired:
        return "Desculpe, o comando demorou demais e foi cancelado."
    except FileNotFoundError:
        return ("Não encontrei o Claude Code. Instale com "
                "npm install -g @anthropic-ai/claude-code e faça login.")
    saida = (resultado.stdout or "").strip()
    if not saida:
        erro = (resultado.stderr or "").strip()
        return erro or "Não obtive resposta do Claude Code."
    return saida


# ---------------------------------------------------------------------------
# Sessão "quente" — um processo claude vivo entre comandos
# ---------------------------------------------------------------------------
class _SessaoQuente:
    def __init__(self):
        self.proc = None
        self.q = queue.Queue()
        self.lock = threading.Lock()

    def _vivo(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _iniciar(self):
        _garantir_node_no_path()
        self.proc = subprocess.Popen(
            [CLAUDE_BIN, "-p",
             "--input-format", "stream-json",
             "--output-format", "stream-json",
             "--verbose"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            cwd=config.PASTA_PROJETO, bufsize=1,
        )
        self.q = queue.Queue()
        t = threading.Thread(target=self._ler, daemon=True)
        t.start()

    def _ler(self):
        try:
            for linha in self.proc.stdout:
                self.q.put(linha)
        except Exception:
            pass
        self.q.put(None)  # EOF

    def encerrar(self):
        with self.lock:
            if self.proc is not None:
                try:
                    if self.proc.stdin:
                        self.proc.stdin.close()
                except Exception:
                    pass
                try:
                    self.proc.terminate()
                except Exception:
                    pass
                self.proc = None

    def enviar(self, texto: str, timeout: float, ao_ferramenta=None) -> str:
        """Envia um comando à sessão quente e devolve a resposta.

        `ao_ferramenta`, se informado, é chamado com o NOME da primeira
        ferramenta usada nessa resposta (ex.: "Bash", "mcp__playwright__...")
        assim que ela aparecer no stream — serve para avisar em voz que o
        assistente começou a agir, em vez de ficar mudo até terminar.

        Lança exceção se algo der errado (o chamador faz o fallback).
        """
        with self.lock:
            if not self._vivo():
                self._iniciar()
            # Descarta eventos antigos que sobraram na fila.
            while not self.q.empty():
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    break

            msg = {"type": "user", "message": {"role": "user",
                   "content": [{"type": "text", "text": texto}]}}
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

            fim = time.monotonic() + timeout
            parcial = ""
            avisado = False
            while True:
                rest = fim - time.monotonic()
                if rest <= 0:
                    raise TimeoutError("tempo esgotado na sessão quente")
                try:
                    linha = self.q.get(timeout=rest)
                except queue.Empty:
                    raise TimeoutError("sem resposta da sessão quente")
                if linha is None:
                    self.proc = None
                    raise RuntimeError("processo da sessão quente encerrou")
                linha = linha.strip()
                if not linha:
                    continue
                try:
                    ev = json.loads(linha)
                except json.JSONDecodeError:
                    continue
                tipo = ev.get("type")
                if tipo == "assistant":
                    for bloco in ev.get("message", {}).get("content", []):
                        if bloco.get("type") == "text":
                            parcial += bloco.get("text", "")
                        elif (bloco.get("type") == "tool_use" and not avisado
                              and ao_ferramenta is not None):
                            avisado = True
                            try:
                                ao_ferramenta(bloco.get("name", ""))
                            except Exception:  # noqa: BLE001
                                pass
                elif tipo == "result":
                    return (ev.get("result") or parcial or "").strip()


_sessao = _SessaoQuente()


def executar_comando(texto: str, continuar: bool = True, ao_ferramenta=None) -> str:
    """Envia `texto` ao Claude Code (sessão quente) e retorna a resposta.

    `ao_ferramenta`: ver `_SessaoQuente.enviar` — avisa quando uma ferramenta
    começa a ser usada (navegador, comando, arquivo...), para dar feedback em
    voz de que o assistente está agindo. Só funciona na sessão quente; o
    fallback one-shot não tem esse aviso.

    Em caso de falha na sessão quente, cai para o modo one-shot.
    O parâmetro `continuar` só afeta o fallback (a sessão quente já
    preserva o contexto naturalmente).
    """
    if not texto:
        return ""
    try:
        resposta = _sessao.enviar(texto, config.TIMEOUT_CLAUDE,
                                  ao_ferramenta=ao_ferramenta)
        if resposta:
            return resposta
        # Resposta vazia: tenta o fallback.
    except Exception as e:  # noqa: BLE001
        print(f"[cerebro] Sessão quente falhou ({e}); usando modo simples.")
        try:
            _sessao.encerrar()
        except Exception:
            pass
    return _executar_oneshot(texto, continuar)


def reiniciar_sessao():
    """Reinicia a sessão quente (ex.: após mudar CLAUDE.md/.mcp.json/settings)."""
    _sessao.encerrar()


def encerrar_sessao():
    """Fecha a sessão quente (chamado no shutdown / antes de reiniciar o app)."""
    _sessao.encerrar()


if __name__ == "__main__":
    # Teste isolado.
    print(executar_comando("Responda em uma frase curta: que dia é hoje?"))
    print(executar_comando("E em uma palavra: qual a capital do Brasil?"))
    encerrar_sessao()
