"""Sistema de rotinas agendadas do assistente.

As rotinas ficam em `rotinas.json` (na raiz do projeto, gitignored porque
podem conter número de contato). O AGENDADOR fica pendurado no loop
principal (ver main.py): de tempos em tempos ele chama `pendentes()` e
dispara o que estiver na hora.

O CÉREBRO (Claude) é quem CRIA/LISTA/EDITA/APAGA rotinas — editando o
`rotinas.json` direto com as ferramentas de arquivo, quando o usuário pede
em linguagem natural ("cria uma rotina...", "lista minhas rotinas",
"apaga a rotina 2"). Por isso o schema abaixo precisa ser estável.

Schema de cada rotina (uma entrada da lista em rotinas.json):
    {
      "id": 1,                      # inteiro único (o maior id + 1 ao criar)
      "nome": "bom dia Fulano",    # descrição curta pra listar/referenciar
      "tipo": "fixo" | "dinamico",  # fixo = repete o mesmo texto;
                                    # dinamico = Claude gera na hora (varia)
      "conteudo": "...",            # fixo: o texto exato a enviar
                                    # dinamico: a INSTRUÇÃO/ideia pro Claude
      "destinatario": null          # null = manda pro admin (uso normal)
                     | {"nome": "Fulano de Tal", "numero": "55DDNNNNNNNNN@s.whatsapp.net"},
      "horario": "08:00",           # HH:MM local. Pode ser LISTA
                                    # ["08:00","09:00",...] p/ vários no dia.
      "recorrencia": {              # quando repetir:
          "tipo": "diario"                                  # todo dia
          # | {"tipo": "semanal", "dias": [5]}              # dias (0=seg .. 6=dom)
          # | {"tipo": "unica", "data": "2026-07-12"}       # uma vez só
      },
      "ativa": true,                # false = pausada (não dispara)
      "ultima_execucao": null       # "AAAA-MM-DD" do último disparo (anti-repetição)
    }

Regras de disparo:
- Só dispara se `ativa` e se aquele HORÁRIO ainda não disparou (anti-repetição
  por slot; `ultima_execucao` guarda a chave "AAAA-MM-DDTHH:MM" do último slot).
- Só dispara na janela [horário, horário + GRACE_MIN min]. Se a VM estava
  desligada e voltou muito depois do horário, PULA o slot (não dispara atrasado).
"""
import datetime
import json
import os

import config

ARQUIVO = os.path.join(config.PASTA_PROJETO, "rotinas.json")

# Só dispara se estiver no máximo GRACE_MIN minutos atrasada — senão pula o
# dia (evita o "bom dia" chegar 22h porque a VM ficou desligada de manhã).
GRACE_MIN = 20


def carregar() -> list:
    """Lê a lista de rotinas do disco (lista vazia se não existir/corromper)."""
    try:
        with open(ARQUIVO, "r", encoding="utf-8") as f:
            dados = json.load(f)
            return dados if isinstance(dados, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def salvar(rotinas: list) -> None:
    """Grava a lista de rotinas de forma atômica (tmp + replace)."""
    tmp = ARQUIVO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rotinas, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ARQUIVO)


def _horarios(r: dict) -> list:
    """Horários de uma rotina. Aceita `horario` como string "HH:MM" ou lista
    de strings ["08:00","09:00",...] pra disparar em vários horários no dia."""
    h = r.get("horario")
    if isinstance(h, list):
        return [str(x) for x in h]
    return [str(h)] if h else []


def _slot_no_ponto(r: dict, agora: datetime.datetime):
    """Se AGORA está na janela [horário, horário+GRACE] de ALGUM horário da
    rotina e esse horário específico ainda não disparou, devolve a chave do slot
    ("AAAA-MM-DDTHH:MM"); senão None. Anti-repetição é POR SLOT — permite vários
    disparos no mesmo dia em rotinas com múltiplos horários."""
    hoje = agora.date()
    for hstr in _horarios(r):
        try:
            hh, mm = (int(x) for x in hstr.split(":"))
        except (ValueError, AttributeError):
            continue
        alvo = agora.replace(hour=hh, minute=mm, second=0, microsecond=0)
        atraso = (agora - alvo).total_seconds()
        if 0 <= atraso <= GRACE_MIN * 60:
            chave = f"{hoje.isoformat()}T{hh:02d}:{mm:02d}"
            if r.get("ultima_execucao") != chave:
                return chave
    return None


def _deve_disparar(r: dict, agora: datetime.datetime) -> bool:
    if not r.get("ativa", True):
        return False

    hoje = agora.date()

    rec = r.get("recorrencia", "diario")
    if isinstance(rec, str):
        rec = {"tipo": rec}
    tipo_rec = rec.get("tipo", "diario")

    if tipo_rec == "semanal":
        if agora.weekday() not in rec.get("dias", []):
            return False
    elif tipo_rec == "unica":
        if rec.get("data") != hoje.isoformat():
            return False
    # "diario" -> serve qualquer dia

    return _slot_no_ponto(r, agora) is not None


def pendentes(agora: datetime.datetime = None) -> list:
    """Rotinas que devem disparar AGORA (respeitando janela e anti-repetição)."""
    agora = agora or datetime.datetime.now()
    return [r for r in carregar() if _deve_disparar(r, agora)]


def marcar_executada(rid, agora: datetime.datetime = None) -> None:
    """Marca o SLOT atual como disparado (anti-repetição por horário, guardando
    a chave "AAAA-MM-DDTHH:MM"). Rotina 'unica' é desativada."""
    agora = agora or datetime.datetime.now()
    rotinas = carregar()
    for r in rotinas:
        if r.get("id") == rid:
            chave = _slot_no_ponto(r, agora)
            r["ultima_execucao"] = chave or f"{agora.date().isoformat()}T{agora.strftime('%H:%M')}"
            rec = r.get("recorrencia", "diario")
            if isinstance(rec, dict) and rec.get("tipo") == "unica":
                r["ativa"] = False
    salvar(rotinas)
