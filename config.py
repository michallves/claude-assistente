"""Configurações centrais do assistente.

Ajuste os valores aqui conforme a bridge do whatsapp-mcp e as regras
de segurança desejadas.
"""
import os

# Pasta raiz do projeto (usada como cwd ao chamar o Claude Code,
# para que ele carregue o CLAUDE.md, o .mcp.json e o .claude/settings.json).
PASTA_PROJETO = os.path.dirname(os.path.abspath(__file__))

PASTA_SEGREDOS = os.path.join(PASTA_PROJETO, "secrets")


def _ler_segredo(nome_env: str, nome_arquivo: str, padrao: str = "") -> str:
    """Lê um valor sensível da env var `nome_env`; se ausente, tenta o
    arquivo `secrets/<nome_arquivo>` (gitignored). Nunca hardcoda segredo
    no código."""
    valor = os.environ.get(nome_env)
    if valor:
        return valor.strip()
    caminho = os.path.join(PASTA_SEGREDOS, nome_arquivo)
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return padrao


# ---------------------------------------------------------------------------
# Claude Code (cérebro)
# ---------------------------------------------------------------------------
TIMEOUT_CLAUDE = 300          # segundos — ações de navegador demoram

# ---------------------------------------------------------------------------
# Canal WhatsApp (bridge do whatsapp-mcp)
# ---------------------------------------------------------------------------
# https://github.com/michallves/whatsapp-mcp — a bridge precisa estar de pé
# com WEBHOOK_ENABLED=true e WEBHOOK_URL apontando pra este assistente
# (padrão abaixo: http://127.0.0.1:8100/webhook).
BRIDGE_URL = os.environ.get("WHATSAPP_BRIDGE_URL", "http://127.0.0.1:8080")
BRIDGE_TOKEN = _ler_segredo("WHATSAPP_BRIDGE_TOKEN", "bridge_token.txt")

# Único chat que pode dar ordens ao assistente: o do dono consigo mesmo
# (<número>@s.whatsapp.net). Grupo nunca vira admin (ver roteador.py).
ADMIN_JID = _ler_segredo("ADMIN_JID", "admin_jid.txt")
# Outras formas do mesmo chat (a @lid), separadas por espaço ou linha.
JIDS_DONO = {ADMIN_JID, *_ler_segredo("JIDS_DONO_EXTRA", "jids_dono.txt").split()}

# Trava geral do envio a terceiros. Com False nada sai para outras pessoas,
# nem em piloto: o portão só registra no diário. Abrir é decisão do dono, por
# variável de ambiente, para o padrão do código continuar fechado.
ENVIO_TERCEIROS_HABILITADO = os.environ.get("ENVIO_TERCEIROS_HABILITADO") == "1"

# Chats em que o piloto pode acolher desabafo (saúde e família), por escolha do dono.
# Sinal de risco real continua chamando o dono.
CHATS_SAUDE_LIBERADA = set(_ler_segredo("CHATS_SAUDE_LIBERADA", "chats_saude_liberada.txt").split())

# Contexto do Atendente: histórico da bridge e como o dono escreve.
DB_MENSAGENS = os.environ.get("WHATSAPP_DB_PATH", os.path.expanduser(
    "~/projects/whatsapp-mcp/whatsapp-bridge/store/messages.db"))
ARQUIVO_PERFIL_ESCRITA = os.environ.get("ARQUIVO_PERFIL_ESCRITA", os.path.expanduser(
    "~/.claude/skills/humanizar-michael/SKILL.md"))
TRANSCRICAO_URL = os.environ.get(
    "WHATSAPP_TRANSCRIPTION_URL", "http://127.0.0.1:8178/v1/audio/transcriptions")
TRANSCRICAO_MODELO = os.environ.get("WHATSAPP_TRANSCRIPTION_MODEL", "large-v3-turbo")
# Haiku segurava quase tudo no teste com conversas antigas; Sonnet acerta mais o tom.
MODELO_ATENDENTE = "claude-sonnet-5-5"
# Quanto o piloto espera a pessoa parar de digitar antes de propor uma resposta.
ESPERA_RAJADA_SEG = 20.0

# Onde o servidor do webhook escuta.
WEBHOOK_HOST = os.environ.get("WEBHOOK_HOST", "127.0.0.1")
WEBHOOK_PORTA = int(os.environ.get("WEBHOOK_PORTA", "8100"))

# Quantas respostas recentes o assistente guarda pra reconhecer o eco delas
# voltando pelo webhook (no grupo solo, comando e resposta têm isFromMe=true).
DEDUPE_MAX_MENSAGENS = 50

# Espaçamento mínimo (segundos) entre duas mensagens enviadas.
INTERVALO_MIN_ENVIO_SEG = 2.0

# De quanto em quanto tempo o app confere se a bridge ainda está conectada.
INTERVALO_CHECAGEM_CONEXAO_SEG = 60.0

# Diário do piloto (também lido pelo comando "diario").
ARQUIVO_DIARIO_PILOTO = os.path.join(PASTA_PROJETO, "diario_piloto.jsonl")

# ---------------------------------------------------------------------------
# Segurança
# ---------------------------------------------------------------------------
# NÃO existe mais uma lista de palavras que força confirmação mecânica
# aqui no app (PALAVRAS_PERIGOSAS foi removida em 2026-07-09, a pedido
# explícito do usuário) — a decisão de quando pedir confirmação antes de
# agir é 100% do julgamento do Claude, reforçada em CLAUDE.md. Não
# reintroduza esse gate sem o usuário pedir de novo.

# Comandos especiais tratados localmente (sem passar pelo Claude).
# "para" (sozinha) foi removida de propósito: é preposição comuníssima em
# português ("mensagem PARA Gabriel") e causava falso positivo, cancelando
# comandos legítimos que continham a palavra sem ter nada a ver com parar.
# Ver main.py: além dessa lista, exige-se frase curta pra disparar.
CMD_PARAR = ["parar", "pare", "cancelar", "cancela"]
CMD_ENCERRAR = ["encerrar", "encerra", "fechar assistente", "desligar assistente"]
# Liga/desliga só os avisos de progresso ("estou usando tal ferramenta...");
# a resposta final sempre é mandada.
CMD_SILENCIO = ["modo silencioso", "silencioso", "fica quieto"]
CMD_FALAR = ["modo normal", "voltar a falar", "pode falar"]
# Reinício do PRÓPRIO assistente (não confundir com reiniciar a VM).
# Verificado ANTES da checagem de palavra perigosa, no main.py.
CMD_REINICIAR = [
    "reiniciar você", "reinicie você", "reiniciar voce", "reinicie voce",
    "reiniciar o assistente", "reiniciar o programa", "reiniciar o app",
    "se reinicie", "se reinicia", "reiniciar você mesmo",
]
# Desfazer a última mudança que o assistente fez em si mesmo (git revert).
CMD_DESFAZER = [
    "desfazer a última", "desfaça a última", "desfaca a ultima",
    "desfazer última mudança", "desfazer mudança", "reverter mudança",
    "volta atrás", "volta atras", "desfaz a última",
]

ARQUIVO_LOG = os.path.join(PASTA_PROJETO, "log.txt")

# Memória de longo prazo do assistente (aprendizado automático): carregada
# em toda sessão do Claude via import no CLAUDE.md. Quando só ela muda, o
# app apenas salva no git — sem reiniciar nada (ver main.py).
ARQUIVO_MEMORIA = os.path.join(PASTA_PROJETO, "MEMORIA.md")

# Lembrete anexado pelo app a cada tarefa enviada ao cérebro. Reforça o
# aprendizado automático NO momento da tarefa — nos testes, a regra global
# do CLAUDE.md sozinha se mostrou inconsistente (o modelo pulava a anotação
# em tarefas rápidas).
LEMBRETE_APRENDIZADO = (
    "\n\n[nota automática do app — não mencione isso na resposta: "
    "depois de concluir, se a receita desta tarefa ainda NÃO estiver na "
    "MEMORIA.md, faça Read na MEMORIA.md e Edit acrescentando UMA linha "
    "objetiva na seção certa.]"
)
