"""Loop principal do assistente.

Fluxo:
    1. mensagem nova chega do admin no WhatsApp (webhook da bridge do whatsapp-mcp)
    2. tratar comandos especiais localmente (parar / encerrar / silencioso)
    3. enviar ao Claude Code (cerebro) — o próprio Claude decide, pelo
       contexto, se precisa confirmar algo antes de agir (ver CLAUDE.md;
       não existe mais um gate mecânico de confirmação aqui no app)
    4. responder no mesmo chat
"""
import datetime
import glob
import os
import queue
import sys
import threading
import time

import config
import canal_whatsapp as canal
import cerebro
import rotinas
from cerebro import executar_comando

# Na primeira chamada não há conversa para --continue.
_primeira_chamada = True

# Se True, suprime só os avisos de progresso ("estou usando tal
# ferramenta..."); a resposta final sempre é mandada.
_modo_silencioso = False

# Disjuntor do loop principal: depois de erros iguais seguidos, desfaz a
# última auto-mudança sozinho (ver main()). Nunca deixa o loop girar sem
# pausa — evita repetir o incidente real que já aconteceu aqui: um erro
# que se repetia a cada iteração gerou +1 milhão de linhas de log e
# consumiu CPU sem parar, em segundos.
LIMITE_ERROS_SEGUIDOS = 5
BACKOFF_ERRO_MAX_SEG = 8.0

# Intervalo de checagem de mudança de código/instruções (sem wake word pra
# "pendurar" essa checagem, precisa de um laço próprio).
INTERVALO_CHECAGEM_MUDANCAS = 2.0

# Arquivos de instrução do Claude (mudança neles = recarregar sessão, sem
# reiniciar o Python).
_ARQUIVOS_INSTRUCAO = [
    os.path.join(config.PASTA_PROJETO, "CLAUDE.md"),
    os.path.join(config.PASTA_PROJETO, ".mcp.json"),
    os.path.join(config.PASTA_PROJETO, ".claude", "settings.json"),
]


def _snapshot_arquivos():
    """Mapa {caminho: mtime} dos .py + arquivos de instrução + memória.

    Serve para detectar quando o assistente editou o próprio código/config
    ou anotou um aprendizado novo na memória.
    """
    alvos = glob.glob(os.path.join(config.PASTA_PROJETO, "**", "*.py"),
                      recursive=True)
    alvos += _ARQUIVOS_INSTRUCAO
    alvos.append(config.ARQUIVO_MEMORIA)
    snap = {}
    for caminho in alvos:
        try:
            snap[caminho] = os.path.getmtime(caminho)
        except OSError:
            snap[caminho] = 0.0
    return snap


def _mudancas(antes: dict):
    """Compara com o snapshot `antes`.

    Retorna (py_mudou, instrucao_mudou, memoria_mudou) — categorias com
    reações diferentes: código reinicia o app; instrução recarrega a sessão;
    memória só é salva no git (aprendizado é silencioso).
    """
    depois = _snapshot_arquivos()
    py_mudou = False
    instrucao_mudou = False
    memoria_mudou = False
    for caminho, mt in depois.items():
        if antes.get(caminho) != mt:
            if caminho.endswith(".py"):
                py_mudou = True
            elif caminho == config.ARQUIVO_MEMORIA:
                memoria_mudou = True
            else:
                instrucao_mudou = True
    return py_mudou, instrucao_mudou, memoria_mudou


def _codigo_valido() -> bool:
    """Confere se os .py do projeto compilam antes de trocar de processo.

    Defesa extra contra reiniciar para um estado quebrado — ex.: uma
    auto-edição de várias etapas capturada pelo restart no meio do
    caminho (arquivo consistente sintaticamente, mas incompleto). Só pega
    erro de SINTAXE; erros como "nome não definido" só aparecem em tempo
    de execução — por isso o disjuntor em main() é a rede de segurança
    principal, esta é só uma camada a mais.
    """
    import subprocess
    alvos = glob.glob(os.path.join(config.PASTA_PROJETO, "**", "*.py"),
                      recursive=True)
    try:
        r = subprocess.run([sys.executable, "-m", "py_compile", *alvos],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            print(f"[main] Código com erro de sintaxe, não vou reiniciar "
                  f"para ele:\n{r.stderr}")
        return r.returncode == 0
    except Exception as e:  # noqa: BLE001
        print(f"[main] Não consegui validar o código ({e}); reiniciando mesmo assim.")
        return True   # falha na própria checagem não deve travar reinícios legítimos


def _reiniciar_app():
    """Reinicia o próprio processo Python para aplicar mudanças no código."""
    cerebro.encerrar_sessao()
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:  # noqa: BLE001 — fallback: novo processo + sair
        print(f"[main] os.execv falhou ({e}); relançando processo.")
        import subprocess
        subprocess.Popen([sys.executable] + sys.argv, cwd=config.PASTA_PROJETO)
        sys.exit(0)


# --- Rede de segurança git (feita pelo app, não pelo cérebro) ---------------
def _git(*args):
    import subprocess
    try:
        return subprocess.run(["git", *args], cwd=config.PASTA_PROJETO,
                              capture_output=True, text=True, timeout=30,
                              encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


def _auto_commit(mensagem: str):
    """Salva no git qualquer alteração que o assistente fez em si mesmo."""
    _git("add", "-A")
    _git("commit", "-m", mensagem)


def _desfazer_ultima() -> bool:
    """Reverte a última alteração via git. Retorna True se deu certo."""
    r = _git("revert", "--no-edit", "HEAD")
    return r is not None and r.returncode == 0


def checar_e_aplicar_mudancas(snap_ref: list):
    """Detecta e aplica mudanças no código/instruções desde `snap_ref[0]`.

    Roda periodicamente no loop principal — assim qualquer alteração feita
    no projeto (pelo próprio assistente por mensagem, ou editada manualmente
    por fora) é aplicada sozinha, sem precisar reiniciar o processo à mão.
    """
    py_mudou, instrucao_mudou, memoria_mudou = _mudancas(snap_ref[0])
    if not (py_mudou or instrucao_mudou or memoria_mudou):
        return

    if py_mudou:
        if not _codigo_valido():
            # Provavelmente uma edição de várias etapas ainda em andamento
            # (arquivo mudou, mas não terminou de mudar). Não reinicia para
            # um estado quebrado — tenta de novo no próximo ciclo, quando
            # a edição estiver completa e o código voltar a compilar.
            return
        _auto_commit("auto: alteração detectada no projeto")
        print("[main] Código alterado — reiniciando para aplicar.")
        canal.enviar_mensagem("Detectei uma atualização no meu código. Reiniciando para aplicar.")
        _aguardar_fila_tarefas()  # não perde uma tarefa em andamento na fila
        _reiniciar_app()          # não retorna (troca o processo)
    elif instrucao_mudou:
        _auto_commit("auto: alteração detectada no projeto")
        print("[main] Instruções alteradas — recarregando sessão.")
        cerebro.reiniciar_sessao()
        canal.enviar_mensagem("Atualizei minhas instruções.")
        snap_ref[0] = _snapshot_arquivos()
    else:
        # Só a memória mudou (aprendizado novo). MEMORIA.md tem conteúdo
        # pessoal (relacionamentos, contatos, etc.) e o repositório tem
        # colaboradores — por isso está no .gitignore desde 2026-07-09 e
        # NÃO é versionado (o _auto_commit abaixo não pega esse arquivo
        # especificamente, só serve pra outra coisa que porventura tenha
        # mudado junto). Nada de reiniciar nem recarregar — a sessão
        # quente acabou de aprender isso; sessões novas carregam via
        # import do CLAUDE.md.
        _auto_commit("auto: memória atualizada (aprendizado)")
        print("[main] Memória atualizada — aprendizado salvo (só localmente).")
        snap_ref[0] = _snapshot_arquivos()


def registrar_log(comando: str, acao: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(config.ARQUIVO_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] COMANDO: {comando!r} | ACAO: {acao}\n")
    except OSError:
        pass


# Evita mandar dois avisos de progresso ao mesmo tempo (ficaria fora de ordem).
_avisando_progresso = threading.Lock()

_FRASES_FERRAMENTA = {
    "bash": "Um momento, executando um comando...",
    "edit": "Estou editando um arquivo...",
    "write": "Estou escrevendo um arquivo...",
    "multiedit": "Estou editando um arquivo...",
    "read": "Deixa eu ler isso...",
}


def _avisar_ferramenta(nome_ferramenta: str):
    """Manda um aviso curto de que o assistente começou a agir.

    Chamado (via cerebro) assim que a primeira ferramenta de um comando
    aparece no stream — dá feedback em vez de silêncio total enquanto ele
    executa a ação (navegador, comando, arquivo...).
    """
    if _modo_silencioso:
        return

    nome = (nome_ferramenta or "").lower()
    if "playwright" in nome or "browser" in nome:
        frase = "Estou usando o navegador..."
    else:
        frase = _FRASES_FERRAMENTA.get(nome, "Estou trabalhando nisso...")

    if not _avisando_progresso.acquire(blocking=False):
        return  # já tem outro aviso em voo; não sobrepõe

    def _mandar_e_liberar():
        try:
            canal.enviar_mensagem(frase)
        finally:
            _avisando_progresso.release()

    threading.Thread(target=_mandar_e_liberar, daemon=True).start()


# ---------------------------------------------------------------------------
# Fila de tarefas: executa comandos em background, um de cada vez, sem
# travar o recebimento de mensagens novas. Assim dá pra mandar outra
# mensagem (perguntar outra coisa, pedir outra ação) enquanto uma tarefa
# mais longa (ex.: navegador) ainda está rodando.
# ---------------------------------------------------------------------------
_fila_tarefas = queue.Queue()
_worker_iniciado = False


def _worker_tarefas():
    """Consome a fila de comandos, um de cada vez, respondendo no WhatsApp."""
    global _primeira_chamada
    while True:
        texto = _fila_tarefas.get()
        registrar_log(texto, "[fila] comecando a executar")
        try:
            # O lembrete de aprendizado vai junto de cada tarefa: reforça a
            # anotação na MEMORIA.md no momento certo (a regra global do
            # CLAUDE.md sozinha se mostrou inconsistente nos testes).
            resposta = executar_comando(texto + config.LEMBRETE_APRENDIZADO,
                                        continuar=not _primeira_chamada,
                                        ao_ferramenta=_avisar_ferramenta)
            _primeira_chamada = False
            registrar_log(texto, f"[fila] executado, resposta: {resposta!r}")
            print(f"[main] Claude: {resposta!r}")
            canal.enviar_mensagem(resposta)
        except Exception as e:  # noqa: BLE001 — worker não pode morrer
            print(f"[main] Erro executando tarefa da fila: {e}", file=sys.stderr)
            registrar_log(texto, f"[fila] ERRO: {e}")
        finally:
            _fila_tarefas.task_done()


def _garantir_worker():
    global _worker_iniciado
    if not _worker_iniciado:
        threading.Thread(target=_worker_tarefas, daemon=True).start()
        _worker_iniciado = True


def enfileirar_comando(texto: str, avisar: bool = True):
    """Coloca um comando na fila e volta na hora — não bloqueia o webhook.

    `avisar=False` suprime o "Deixa comigo." quando já há tarefa rodando —
    usado por disparos automáticos de rotina, que não devem tagarelar.
    """
    _garantir_worker()
    havia_tarefa_em_andamento = _fila_tarefas.unfinished_tasks > 0
    registrar_log(texto, f"[fila] enfileirado (havia tarefa em andamento: "
                         f"{havia_tarefa_em_andamento})")
    _fila_tarefas.put(texto)
    if havia_tarefa_em_andamento and avisar:
        threading.Thread(
            target=canal.enviar_mensagem,
            args=("Deixa comigo.",),
            daemon=True,
        ).start()


def _aguardar_fila_tarefas(timeout: float = 60.0):
    """Espera a fila esvaziar antes de reiniciar — não perde tarefa em andamento."""
    inicio = time.monotonic()
    while _fila_tarefas.unfinished_tasks > 0 and (time.monotonic() - inicio) < timeout:
        time.sleep(0.2)


# ---------------------------------------------------------------------------
# Agrupamento de mensagens em sequência: quando o usuário manda várias
# mensagens seguidas (o jeito natural de digitar no WhatsApp, uma linha de
# cada vez), não faz sentido responder cada uma isolada e picar o contexto.
# Em vez de enfileirar na hora, cada mensagem entra num buffer e um timer
# curto é (re)armado; enquanto chegar mensagem nova dentro da janela, o
# timer reinicia. Só quando o usuário para de digitar por DEBOUNCE_SEG é que
# o buffer é "fechado", juntado num texto só e enfileirado como UMA tarefa —
# aí o Claude vê tudo junto e responde uma vez.
# ---------------------------------------------------------------------------
DEBOUNCE_MENSAGENS_SEG = 3.0

_buffer_lock = threading.Lock()
_buffer_msgs = []
_buffer_timer = None


def _flush_buffer():
    """Fecha o buffer: junta as mensagens acumuladas e enfileira como uma só."""
    global _buffer_timer
    with _buffer_lock:
        msgs = _buffer_msgs[:]
        _buffer_msgs.clear()
        _buffer_timer = None
    if not msgs:
        return
    texto = msgs[0] if len(msgs) == 1 else "\n\n".join(msgs)
    if len(msgs) > 1:
        registrar_log(texto, f"[buffer] {len(msgs)} mensagens juntadas em uma")
    enfileirar_comando(texto)


def _bufferizar_comando(texto: str):
    """Acumula a mensagem e (re)arma o timer de fechamento do buffer.

    Cada mensagem nova dentro da janela reinicia a contagem — assim uma
    rajada de mensagens seguidas vira uma tarefa só, disparada quando o
    usuário para de digitar.
    """
    global _buffer_timer
    with _buffer_lock:
        _buffer_msgs.append(texto)
        if _buffer_timer is not None:
            _buffer_timer.cancel()
        _buffer_timer = threading.Timer(DEBOUNCE_MENSAGENS_SEG, _flush_buffer)
        _buffer_timer.daemon = True
        _buffer_timer.start()


# ---------------------------------------------------------------------------
# Agendador de rotinas (ver rotinas.py). O loop principal chama
# `checar_rotinas()` a cada ciclo; quando uma rotina está no horário, ela é
# disparada. Rotina de texto fixo pro admin vai direto pelo canal (barato);
# qualquer coisa dinâmica ou pra terceiro passa pelo cérebro, que gera o
# texto e/ou valida a restrição de envio antes de mandar.
# ---------------------------------------------------------------------------
def _instrucao_rotina(r: dict) -> str:
    """Monta a instrução que o cérebro recebe pra executar uma rotina.

    Deixa explícito que é uma rotina PRÉ-AUTORIZADA (o usuário autorizou ao
    criá-la) — senão o Claude poderia recusar por ser "mensagem a terceiro
    sem pedido na conversa".
    """
    dest = r.get("destinatario") or {}
    numero = dest.get("numero")
    nome = dest.get("nome")
    tipo = r.get("tipo", "fixo")
    conteudo = r.get("conteudo", "")

    cab = ("[ROTINA AUTOMÁTICA — o usuário pré-autorizou este envio quando "
           "criou esta rotina; NÃO é um pedido novo pra terceiro, pode "
           "executar sem perguntar de novo] ")

    if numero:
        alvo = f"{nome} (número {numero})" if nome else f"o número {numero}"
        validacao = (
            " IMPORTANTE antes de enviar: valide a restrição ativa (seção "
            "'Restrições ativas' da MEMORIA.md). Se este contato cair na regra "
            "de não poder iniciar conversa (nunca trocou mensagem com o bot "
            "enquanto a restrição vale), NÃO envie — em vez disso me avise que "
            f"não deu pra mandar pra {nome or numero} porque está restrito. "
        )
        if tipo == "dinamico":
            corpo = (f"Gere agora uma mensagem NOVA e natural (diferente das "
                     f"anteriores) para {alvo}, seguindo esta ideia: "
                     f"«{conteudo}». ")
        else:
            corpo = f"Envie para {alvo} exatamente esta mensagem: «{conteudo}». "
        return cab + corpo + validacao + "Depois me confirme o que aconteceu."

    # Sem destinatário = manda pro próprio admin.
    if tipo == "dinamico":
        return (cab + "Escreva agora, na sua própria resposta (que já vai pra "
                f"mim), uma mensagem seguindo esta ideia: «{conteudo}».")
    return cab + f"Apenas me mande esta mensagem: «{conteudo}»."


def _disparar_rotina(r: dict):
    tipo = r.get("tipo", "fixo")
    dest = r.get("destinatario")
    if tipo == "fixo" and not dest:
        # Texto fixo pro admin: manda direto, sem gastar uma rodada do cérebro.
        canal.enviar_mensagem(r.get("conteudo", ""))
    else:
        # Dinâmica ou pra terceiro: o cérebro gera/valida/envia.
        enfileirar_comando(_instrucao_rotina(r), avisar=False)


def checar_rotinas():
    """Dispara as rotinas que estão no horário (chamada pelo loop principal)."""
    try:
        pendentes = rotinas.pendentes()
    except Exception as e:  # noqa: BLE001 — nunca deixar o loop morrer por isso
        print(f"[main] Erro ao checar rotinas: {e}", file=sys.stderr)
        return
    for r in pendentes:
        rid = r.get("id")
        registrar_log(f"<rotina {rid}>", f"disparando: {r.get('nome')!r}")
        try:
            _disparar_rotina(r)
            rotinas.marcar_executada(rid)
        except Exception as e:  # noqa: BLE001
            print(f"[main] Erro disparando rotina {rid}: {e}", file=sys.stderr)
            registrar_log(f"<rotina {rid}>", f"ERRO ao disparar: {e}")


# Comandos curtos (CMD_PARAR) só disparam se a mensagem inteira for curta —
# evita confundir "para" (preposição comum: "mensagem PARA Gabriel") com o
# comando de parar/cancelar.
_PARAR_MAX_PALAVRAS = 5


def _termo_correspondente(texto: str, lista):
    """Retorna o termo da lista que bateu no texto (para log/diagnóstico),
    ou None se nenhum bateu."""
    t = texto.lower()
    for termo in lista:
        if termo in t:
            return termo
    return None


def _contem(texto: str, lista) -> bool:
    return _termo_correspondente(texto, lista) is not None


def tratar_comando_especial(texto: str) -> str:
    """Retorna 'encerrar', 'tratado' ou '' (não é comando especial).

    Cada ramo registra no log (log.txt) qual termo exato bateu — serve de
    trilha de diagnóstico: se um comando normal for engolido por engano por
    um comando especial, dá pra ver EXATAMENTE qual termo causou isso sem
    precisar reproduzir o problema ao vivo.
    """
    global _modo_silencioso

    termo = _termo_correspondente(texto, config.CMD_REINICIAR)
    if termo:
        canal.enviar_mensagem("Reiniciando. Já volto.")
        registrar_log(texto, f"reiniciar assistente (termo casado: {termo!r})")
        _reiniciar_app()          # não retorna (troca o processo)
        return "tratado"

    termo = _termo_correspondente(texto, config.CMD_DESFAZER)
    if termo:
        canal.enviar_mensagem("Desfazendo a última mudança.")
        registrar_log(texto, f"desfazer/git revert (termo casado: {termo!r})")
        if _desfazer_ultima():
            _reiniciar_app()      # reinicia para aplicar o revert (não retorna)
        else:
            canal.enviar_mensagem("Não consegui desfazer a última mudança.")
        return "tratado"

    termo = _termo_correspondente(texto, config.CMD_ENCERRAR)
    if termo:
        canal.enviar_mensagem("Encerrando. Até logo!")
        registrar_log(texto, f"encerrar assistente (termo casado: {termo!r})")
        return "encerrar"

    termo = _termo_correspondente(texto, config.CMD_SILENCIO)
    if termo:
        _modo_silencioso = True
        registrar_log(texto, f"modo silencioso ON (termo casado: {termo!r})")
        print("[main] Modo silencioso ligado.")
        return "tratado"

    termo = _termo_correspondente(texto, config.CMD_FALAR)
    if termo:
        _modo_silencioso = False
        canal.enviar_mensagem("Voltei a avisar o progresso.")
        registrar_log(texto, f"modo silencioso OFF (termo casado: {termo!r})")
        return "tratado"

    termo = _termo_correspondente(texto, config.CMD_PARAR)
    if termo:
        palavras = texto.split()
        if len(palavras) <= _PARAR_MAX_PALAVRAS:
            canal.enviar_mensagem("Ok, cancelado.")
            registrar_log(texto, f"parar/cancelar (termo casado: {termo!r}, "
                                 f"{len(palavras)} palavras)")
            return "tratado"
        else:
            # Termo bateu, mas a frase é longa demais pra ser um comando de
            # "parar" de verdade (ex.: "mensagem PARA Gabriel") — não trata
            # como especial, deixa seguir pro fluxo normal (fila de tarefas).
            registrar_log(texto, f"IGNOROU parar/cancelar: termo {termo!r} bateu, "
                                 f"mas frase tem {len(palavras)} palavras "
                                 f"(limite {_PARAR_MAX_PALAVRAS}) — tratando como "
                                 f"comando normal")

    return ""


def processar_mensagem(texto: str) -> bool:
    """Processa uma mensagem recebida no WhatsApp. Retorna False para encerrar.

    NÃO existe mais um gate mecânico de confirmação (palavra perigosa ->
    pausa -> exige "sim, confirmo") — decisão explícita do usuário
    (2026-07-09): confiar no julgamento do próprio Claude, via CLAUDE.md,
    pra decidir quando pedir confirmação antes de agir (uma pergunta
    normal na conversa, não um estado separado aqui no app). Isso vale
    pra TODAS as categorias que antes estavam em PALAVRAS_PERIGOSAS
    (deletar, pagar, mandar mensagem pra terceiro, etc.) — a única
    barreira contra ação destrutiva/irreversível agora é o próprio
    CLAUDE.md. Não reintroduza esse gate sem o usuário pedir de novo.
    """
    if not texto.strip():
        return True

    print(f"[main] Mensagem: {texto!r}")

    # 1) Comandos especiais locais.
    especial = tratar_comando_especial(texto)
    if especial == "encerrar":
        return False
    if especial == "tratado":
        return True

    # 2) Comando normal -> buffer (agrupa mensagens em sequência) -> fila de
    # tarefas. Não trava o recebimento de mensagens novas. O próprio Claude
    # decide, pelo contexto, se precisa confirmar algo antes de executar.
    registrar_log(texto, "[ramo 2: normal] bufferizado")
    _bufferizar_comando(texto)
    return True


def garantir_login() -> bool:
    """Garante que o Claude Code está logado antes de iniciar o assistente.

    Se não estiver logado, pede pra rodar `claude auth login` manualmente
    no terminal da VM (não dá pra fazer OAuth interativo por WhatsApp).
    """
    print("[main] Verificando login do Claude Code...")
    if cerebro.esta_logado():
        print("[main] Login OK.")
        return True

    print("\n" + "=" * 60)
    print(" Você ainda NÃO fez login no Claude Code nesta VM.")
    print(" Rode `claude auth login` no terminal e faça o login.")
    print("=" * 60 + "\n")
    return False


def _checar_conexao_periodica(conectado_ref: list, ultima_checagem: list):
    """Confere de vez em quando (não a cada 2s) se o WhatsApp ainda está
    conectado. Se cair, não dá pra avisar por WhatsApp — o canal em si
    está fora do ar — então só loga bem visível, pra quem olhar o
    terminal/log perceber rápido que precisa reconectar manualmente
    (parear o QR de novo na bridge)."""
    agora = time.monotonic()
    if agora - ultima_checagem[0] < config.INTERVALO_CHECAGEM_CONEXAO_SEG:
        return
    ultima_checagem[0] = agora

    conectado = canal.esta_conectado()
    if conectado != conectado_ref[0]:
        if not conectado:
            print("[main] !!! WHATSAPP DESCONECTADO — precisa reconectar "
                  "manualmente (parear o QR de novo na bridge). Nenhuma mensagem "
                  "será processada até lá.", file=sys.stderr)
            registrar_log("<conexao>", "WhatsApp caiu (state != open)")
        else:
            print("[main] WhatsApp reconectado.")
            registrar_log("<conexao>", "WhatsApp reconectou")
        conectado_ref[0] = conectado


def main():
    print("=" * 60)
    print(" Assistente — canal WhatsApp (bridge do whatsapp-mcp)")
    print(" (Ctrl+C para encerrar)")
    print("=" * 60)

    if not garantir_login():
        return

    def _ao_receber(texto: str):
        try:
            if not processar_mensagem(texto):
                print("[main] Encerramento pedido via mensagem.")
        except Exception as e:  # noqa: BLE001 — callback do webhook não pode morrer
            print(f"[main] Erro processando mensagem recebida: {e}", file=sys.stderr)
            registrar_log(texto, f"ERRO ao processar mensagem recebida: {e}")

    canal.iniciar_webhook(ao_receber_mensagem=_ao_receber)
    canal.enviar_mensagem("Assistente pronto.")

    snap_ref = [_snapshot_arquivos()]

    # Disjuntor: se o MESMO erro se repetir muitas vezes seguidas, algo está
    # sistematicamente quebrado (ex.: uma auto-edição capturada pelo restart
    # no meio do caminho) — desfaz a última mudança sozinho em vez de girar
    # para sempre. E nunca deixa o loop rodar sem pausa nenhuma entre erros.
    erros_seguidos = 0
    ultimo_erro_msg = None

    # Estado da última checagem de conexão (ver _checar_conexao_periodica).
    conectado_ref = [True]
    ultima_checagem_conexao = [0.0]

    while True:
        try:
            checar_e_aplicar_mudancas(snap_ref)
            _checar_conexao_periodica(conectado_ref, ultima_checagem_conexao)
            checar_rotinas()
            time.sleep(INTERVALO_CHECAGEM_MUDANCAS)
            erros_seguidos = 0
        except KeyboardInterrupt:
            print("\n[main] Encerrado pelo usuário.")
            break
        except Exception as e:  # noqa: BLE001 — loop não pode morrer por 1 erro
            msg = str(e)
            print(f"[main] Erro no loop: {msg}", file=sys.stderr)
            registrar_log("<erro interno>", msg)

            erros_seguidos = erros_seguidos + 1 if msg == ultimo_erro_msg else 1
            ultimo_erro_msg = msg

            if erros_seguidos >= LIMITE_ERROS_SEGUIDOS:
                print(f"[main] Mesmo erro {erros_seguidos}x seguidas — "
                      "desfazendo a última mudança automaticamente.")
                registrar_log("<auto-recuperacao>",
                              f"revertido apos {erros_seguidos}x: {msg}")
                if _desfazer_ultima():
                    _reiniciar_app()      # não retorna
                erros_seguidos = 0        # não conseguiu reverter: só desacelera

            time.sleep(min(BACKOFF_ERRO_MAX_SEG, 0.5 * (2 ** min(erros_seguidos, 4))))
            continue

    cerebro.encerrar_sessao()


if __name__ == "__main__":
    main()
