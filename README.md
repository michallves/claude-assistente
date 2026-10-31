# claude-assistente

Um assistente pessoal que você comanda pelo WhatsApp e que roda o **Claude Code**
por trás. Você manda uma mensagem (texto, áudio, imagem ou arquivo) para um grupo
só seu, o Claude decide o que fazer e responde no mesmo chat.

O assistente **não tem ferramentas próprias**. Tudo o que ele sabe fazer vem dos
MCPs que você já configurou no seu Claude Code (WhatsApp, navegador, ClickUp,
Notion, GitHub, o que for). Adicionou um MCP novo ao Claude Code? O assistente
passa a usá-lo, sem mexer neste repositório.

```
WhatsApp ──► bridge (whatsapp-mcp) ──webhook──► claude-assistente ──► Claude Code ──► seus MCPs
   ▲                                                                                    │
   └──────────────────────── resposta pelo /api/send da bridge ◄────────────────────────┘
```

## Peças

| Peça | O que faz |
|---|---|
| [`whatsapp-mcp`](https://github.com/michallves/whatsapp-mcp) | Bridge do WhatsApp (envia o webhook e recebe a resposta) e MCP com as ferramentas de WhatsApp. Repositório separado. |
| `canal_whatsapp.py` | Recebe o webhook, filtra pelo chat do admin e devolve a resposta. |
| `cerebro.py` | Mantém uma sessão do Claude Code viva e conversa com ela. |
| `main.py` | Fila de tarefas, comandos locais (parar, silencioso), rotinas agendadas. |
| `rotinas.py` | Rotinas agendadas (por exemplo, um bom dia todo dia às 8h). |
| `CLAUDE.md` | Instruções permanentes do assistente e regras de segurança. |

## Requisitos

- Python 3.10+, Node.js 18+
- [Claude Code](https://docs.claude.com/en/docs/claude-code) instalado e logado
- O [`whatsapp-mcp`](https://github.com/michallves/whatsapp-mcp) rodando e pareado, e registrado como MCP no seu Claude Code (servidor chamado `whatsapp`)

## Instalação

```bash
git clone https://github.com/michallves/claude-assistente
cd claude-assistente
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
mkdir -p secrets
```

1. O canal é o seu chat consigo mesmo no WhatsApp. O JID é `<seu-número>@s.whatsapp.net`.
2. Grave o JID e o token da bridge:
   ```bash
   printf '%s' '<seu-número>@s.whatsapp.net' > secrets/admin_jid.txt
   cp <pasta-da-bridge>/store/.bridge-token secrets/bridge_token.txt
   ```
3. Na bridge, aponte o webhook para o assistente:
   ```bash
   WEBHOOK_ENABLED=true
   WEBHOOK_URL=http://127.0.0.1:8100/webhook
   WEBHOOK_NOTIFY_MEDIA=true
   ```
   Sem `WEBHOOK_NOTIFY_MEDIA=true`, a bridge só avisa texto e imagem: áudio,
   vídeo e documento não chegam ao assistente.
4. Rode `claude` uma vez dentro da pasta do projeto e aceite o aviso de confiança.
   Sem isso o Claude Code ignora o `allow` de `.claude/settings.json`, e as
   ferramentas ficam pedindo uma permissão que ninguém consegue dar.
5. Suba o assistente:
   ```bash
   python main.py
   ```

Mande uma mensagem para você mesmo. Só esse chat dá ordens. Mensagens de outras
pessoas nunca chegam ao Claude que tem ferramentas (ver Segurança).

### Como serviço (Linux)

Para subir com a máquina e voltar sozinho se cair, use o serviço de usuário do
systemd em `deploy/claude-assistente.service`. Ele assume o projeto em
`~/projects/claude-assistente`; ajuste os caminhos se for outro lugar.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/claude-assistente.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-assistente
loginctl enable-linger "$USER"
journalctl --user -u claude-assistente -f
```

## Usando outros MCPs

O Claude Code roda dentro da pasta do projeto e herda os MCPs que você tem
configurados. Para o assistente poder chamá-los sem pedir permissão a cada uso,
liste o servidor em `.claude/settings.json`:

```json
{ "permissions": { "allow": ["mcp__whatsapp", "mcp__clickup", "mcp__github"] } }
```

## Segurança

O assistente aceita ordens e executa ações na sua máquina e nas suas contas.
Trate-o como acesso remoto:

- Só o seu chat consigo mesmo dá ordens, e só com mensagem sua (`isFromMe`). Tudo
  que vier nele será lido pelo Claude, inclusive anotações: não guarde senha ali.
- Mensagem de outra pessoa nunca chega ao Cérebro. Ela só vai ao Atendente
  (`atendente.py`), um Claude sem ferramentas, MCP, hooks ou arquivos, que propõe uma
  resposta. O portão (`portao.py`), em código, decide se ela sai.
- Nada sai para terceiros com `ENVIO_TERCEIROS_HABILITADO = False` (padrão).
- O Cérebro ainda pode ler mensagens de terceiros pelo MCP quando você pede. Esse
  conteúdo é dado, não ordem, mas a barreira aí é o julgamento do Claude.
- Comandos no seu chat: `volta` (corta tudo), `copiloto`, `assume tudo`,
  `assume <escopo>`, `devolve <escopo>`, `status`, `diario` (compara o que o piloto
  diria com o que você respondeu).
- Não existe confirmação mecânica no app. Quem decide quando pedir confirmação
  é o Claude, guiado pelo `CLAUDE.md`. Leia esse arquivo antes de dar acesso a
  algo sensível.
- Segredos ficam em `secrets/` ou em variáveis de ambiente, nunca no código.
- `MEMORIA.md`, `rotinas.json` e `log.txt` são locais e ficam fora do git.

## Licença

MIT.
