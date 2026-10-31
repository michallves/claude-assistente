"""Portão de saída: decide, em código e sem IA, se uma resposta pode sair.

O texto da mensagem e a proposta do atendente são dados não confiáveis, por
isso nada aqui obedece a eles: só os lê para procurar conteúdo sensível. Na
dúvida, ou diante de qualquer erro, o portão escolhe o caminho que não envia.
"""
import json
import logging
import os
import re
import tempfile
import threading
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

import config
from tipos import Acao, Decisao, Evento, Modo, Proposta, Veredito

log = logging.getLogger("portao")

CATEGORIAS_LINHA_VERMELHA = frozenset({
    "dinheiro", "contrato", "producao", "rh",
    "conflito", "saude", "familia", "seguranca",
})

_trava = threading.RLock()

# Um corte que não pôde ir para o disco continua valendo: sem isto, a próxima
# leitura do arquivo antigo desfaria a ordem de parar do dono.
_estado_nao_gravado: Optional["Estado"] = None
_ultimo_aviso = ""


# ---------------------------------------------------------------------------
# Estado
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Estado:
    modo: Modo = Modo.COPILOTO
    tudo: bool = False                # "assume tudo": piloto em qualquer chat
    escopos: dict = field(default_factory=dict)  # nome -> lista de chat_jid
    pausados: tuple = ()
    corte: bool = False
    aviso: str = ""                   # por que o estado seguro foi assumido


def _arquivo_estado() -> str:
    return getattr(config, "ARQUIVO_ESTADO_PILOTO",
                   os.path.join(config.PASTA_PROJETO, "estado_piloto.json"))


def _arquivo_diario() -> str:
    return getattr(config, "ARQUIVO_DIARIO_PILOTO",
                   os.path.join(config.PASTA_PROJETO, "diario_piloto.jsonl"))


def _lista_de_textos(valor) -> list:
    if not isinstance(valor, list) or not all(isinstance(v, str) for v in valor):
        raise ValueError("esperava lista de textos")
    return valor


def _estado_de_json(dados) -> Estado:
    if not isinstance(dados, dict):
        raise ValueError("raiz não é objeto")
    # type(...) is bool: "false" em texto seria verdadeiro num teste comum.
    if type(dados["corte"]) is not bool or type(dados["tudo"]) is not bool:
        raise ValueError("corte/tudo não são booleanos")
    escopos = dados["escopos"]
    if not isinstance(escopos, dict):
        raise ValueError("escopos não é objeto")
    return Estado(
        modo=Modo(dados["modo"]),
        tudo=dados["tudo"],
        escopos={str(k): list(_lista_de_textos(v)) for k, v in escopos.items()},
        pausados=tuple(_lista_de_textos(dados["pausados"])),
        corte=dados["corte"],
    )


def _avisar_uma_vez(aviso: str) -> None:
    # O estado é relido a cada decisão; sem isto o log repetiria a cada mensagem.
    global _ultimo_aviso
    if aviso and aviso != _ultimo_aviso:
        log.warning("estado do piloto em modo seguro: %s", aviso)
    _ultimo_aviso = aviso


def estado() -> Estado:
    """Estado vigente. Relê o arquivo sempre: é ele a fonte da verdade."""
    with _trava:
        if _estado_nao_gravado is not None:
            return _estado_nao_gravado
        try:
            with open(_arquivo_estado(), "r", encoding="utf-8") as f:
                atual = _estado_de_json(json.load(f))
            _avisar_uma_vez("")
            return atual
        except FileNotFoundError:
            aviso = "arquivo de estado ausente"
        except (OSError, ValueError, KeyError, TypeError, RecursionError) as erro:
            aviso = f"arquivo de estado ilegível ({type(erro).__name__})"
        _avisar_uma_vez(aviso)
        return Estado(aviso=aviso)


def _gravar(novo: Estado) -> None:
    caminho = _arquivo_estado()
    pasta = os.path.dirname(os.path.abspath(caminho))
    conteudo = json.dumps({
        "modo": novo.modo.value,
        "tudo": novo.tudo,
        "escopos": novo.escopos,
        "pausados": sorted(novo.pausados),
        "corte": novo.corte,
    }, ensure_ascii=False, indent=2)
    fd, temporario = tempfile.mkstemp(prefix=".estado_piloto.", dir=pasta)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(conteudo)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporario, caminho)
    except BaseException:
        try:
            os.unlink(temporario)
        except OSError:
            pass
        raise
    # Sem sincronizar a pasta, uma queda de energia pode desfazer o replace.
    fd_pasta = os.open(pasta, os.O_RDONLY)
    try:
        os.fsync(fd_pasta)
    finally:
        os.close(fd_pasta)


def _mudar(transformar: Callable[[Estado], Estado], restringe: bool) -> bool:
    """Aplica e grava a mudança. Retorna se foi para o disco.

    Se a gravação falhar, mudança que restringe vale em memória até o próximo
    reinício; mudança que libera é descartada, porque liberar sem persistir
    seria abrir o portão num estado que ninguém consegue auditar.
    """
    global _estado_nao_gravado
    with _trava:
        novo = replace(transformar(estado()), aviso="")
        try:
            _gravar(novo)
        except OSError as erro:
            log.error("não consegui gravar o estado do piloto: %s", erro)
            if restringe:
                _estado_nao_gravado = novo
            return False
        _estado_nao_gravado = None
        return True


def _limpar_memoria() -> None:
    """Só para testes: esquece o que ficou em memória entre um caso e outro."""
    global _estado_nao_gravado, _ultimo_aviso
    with _trava:
        _estado_nao_gravado = None
        _ultimo_aviso = ""


def ligar_corte() -> bool:
    # Limpa os escopos também: o piloto só volta com uma ordem nova e explícita,
    # nunca ressuscitando o que estava autorizado antes do corte.
    return _mudar(lambda e: replace(e, corte=True, modo=Modo.COPILOTO,
                                    tudo=False, escopos={}), restringe=True)


def voltar_ao_copiloto() -> bool:
    # Copiloto nunca envia sem aprovação, então sair do corte para cá é seguro.
    return _mudar(lambda e: replace(e, corte=False, modo=Modo.COPILOTO, tudo=False,
                                    escopos={}), restringe=True)


def assumir_tudo() -> bool:
    return _mudar(lambda e: replace(e, modo=Modo.PILOTO, tudo=True, corte=False),
                  restringe=False)


def assumir_escopo(nome: str, jids: Iterable[str]) -> bool:
    def aplicar(e: Estado) -> Estado:
        return replace(e, modo=Modo.PILOTO,
                       escopos={**e.escopos, nome: list(jids)})
    return _mudar(aplicar, restringe=False)


def devolver(jids: Iterable[str]) -> bool:
    alvos = set(jids)
    return _mudar(lambda e: replace(
        e, pausados=tuple(j for j in e.pausados if j not in alvos)),
        restringe=False)


def ao_dono_entrou(evento: Evento) -> None:
    """Pausa o chat até devolução explícita."""
    jid = evento.chat_jid
    if not jid:
        return
    _mudar(lambda e: e if jid in e.pausados
           else replace(e, pausados=(*e.pausados, jid)), restringe=True)


def trava_envio_aberta() -> bool:
    return getattr(config, "ENVIO_TERCEIROS_HABILITADO", False) is True


# ---------------------------------------------------------------------------
# Detector de conteúdo sensível
# ---------------------------------------------------------------------------
# Só os disfarces baratos de digitar; homoglifo fora desta tabela escapa.
_HOMOGLIFOS = str.maketrans("аеорсхіѕ", "aeopcxis")
_LEET = str.maketrans("013457@$", "oieastas")


def _limpar(texto: str) -> str:
    """Tira o que é invisível (espaço de largura zero, hífen suave) sem mudar caixa."""
    texto = unicodedata.normalize("NFKC", texto)
    return "".join(c for c in texto if unicodedata.category(c) != "Cf")


def normalizar(texto: str) -> str:
    """Minúsculas, sem acento, sem caractere invisível, espaços colapsados."""
    decomposto = unicodedata.normalize("NFKD", _limpar(texto))
    sem_acento = "".join(c for c in decomposto if unicodedata.category(c) != "Mn")
    return " ".join(sem_acento.casefold().translate(_HOMOGLIFOS).split())


_NUMEROS_POR_EXTENSO = (
    "um|dois|tres|quatro|cinco|seis|sete|oito|nove|dez|onze|doze|treze|"
    "quatorze|catorze|quinze|dezesseis|dezessete|dezoito|dezenove|vinte|trinta|"
    "quarenta|cinquenta|sessenta|setenta|oitenta|noventa|cem|cento|duzentos|"
    "trezentos|quatrocentos|quinhentos|seiscentos|setecentos|oitocentos|"
    "novecentos|mil"
)
# As fronteiras são por letra, não \b: "_" conta como palavra para \b e deixaria
# "api_token" e "db_password" passarem.
_CHAVE_INOFENSIVA = (
    r"do carro|da moto|de casa|da casa|do ap|do apartamento|da porta|do portao|"
    r"da sala|do escritorio|reserva|de fenda|inglesa|de ouro|de boca"
)
_PADROES_PALAVRA = (
    ("senha", re.compile(r"(?<![a-z])senhas?(?![a-z])")),
    ("token", re.compile(r"tokens?(?![a-z])")),
    ("senha", re.compile(r"(?:passwords?|passwd|pwd)(?![a-z])")),
    ("segredo", re.compile(r"secrets?(?![a-z])|(?<![a-z])credencia(?:l|is)(?![a-z])")),
    ("chave", re.compile(r"(?:api|private|access|secret)[\s_-]?keys?(?![a-z])")),
    ("chave", re.compile(
        r"(?<!palavra[- ])(?<!palavras[- ])(?<!ponto[- ])(?<!pontos[- ])"
        r"(?<![a-z])chaves?(?![a-z])"
        rf"(?!\s+(?:{_CHAVE_INOFENSIVA})(?![a-z]))")),
    ("codigo", re.compile(
        r"(?<![a-z])codigo (?:de )?(?:verificacao|seguranca|autenticacao|confirmacao)")),
    ("pix", re.compile(
        r"(?<![a-z])pix(?:e|ou|ei|zinho|ando|ado)?(?![a-z])"
        r"|(?<![a-z])(?:vou|vai|pode|posso|consegue|pra|para|me)\s+pixar(?![a-z])")),
    ("deploy", re.compile(r"(?<![a-z])deplo[yi]")),
    ("producao", re.compile(r"(?<![a-z])(?:producao|production|prod)(?![a-z])")),
)
_PADROES_VALOR = (
    re.compile(r"(?<![a-z])r\s*\$"),
    re.compile(r"\$\s*\d"),
    re.compile(r"\d\s*(?:mil\s+)?(?:reais|real|contos?|pilas?|brl)(?![a-z])"),
    re.compile(rf"(?<![a-z])(?:{_NUMEROS_POR_EXTENSO})\s+reais(?![a-z])"),
)
# Rodam com a caixa original: AKIA em minúsculas é só uma palavra qualquer.
_PADROES_SEGREDO = (
    re.compile(r"(?<![A-Za-z0-9])[sS][kK]-[A-Za-z0-9_-]{4,}"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{4,}"),
    re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{4,}"),
    re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{4,}"),
)
_INICIO_CHAVE_PRIVADA = (
    r"-*\s*begin\s+(?:[a-z0-9]+\s+){0,3}private\s+key\s*-*"
)
_CHAVE_PRIVADA = re.compile(_INICIO_CHAVE_PRIVADA, re.IGNORECASE)
_LETRAS_SEPARADAS = re.compile(
    r"(?<![a-z0-9])[a-z0-9](?:[\s.\-_*]+[a-z0-9])+(?![a-z0-9])")
_PALAVRAS_SEPARAVEIS = ("senha", "token", "password", "pix", "chave", "deploy",
                        "producao")


def detectar_sensivel(texto: str) -> Optional[str]:
    """Nome do padrão achado, ou None. Nunca devolve o trecho: ele pode ser o segredo."""
    if not texto:
        return None
    limpo = _limpar(texto)
    if any(p.search(limpo) for p in _PADROES_SEGREDO) or _CHAVE_PRIVADA.search(limpo):
        return "segredo"
    normal = normalizar(texto)
    if any(p.search(normal) for p in _PADROES_VALOR):
        return "valor"
    leet = normal.translate(_LEET)
    for nome, padrao in _PADROES_PALAVRA:
        if padrao.search(normal) or padrao.search(leet):
            return nome
    # "s e n h a" e "p.i.x" tiram a palavra do alcance dos padrões acima.
    for trecho in _LETRAS_SEPARADAS.finditer(normal):
        junto = re.sub(r"[\s.\-_*]+", "", trecho.group()).translate(_LEET)
        for palavra in _PALAVRAS_SEPARAVEIS:
            if palavra in junto:
                return palavra
    return None


# ---------------------------------------------------------------------------
# Decisão
# ---------------------------------------------------------------------------
def _acao(proposta: Proposta) -> Optional[Acao]:
    try:
        return Acao(proposta.acao)
    except ValueError:
        return None


def _no_escopo(atual: Estado, chat_jid: str) -> bool:
    if atual.tudo:
        return True
    return any(chat_jid in jids for jids in atual.escopos.values())


def pode_ganhar_tempo(proposta: Proposta, decisao: Decisao) -> bool:
    """Só em resposta segurada por falta do dono, nunca em assunto delicado, pergunta
    sobre ser IA, conteúdo sensível, corte ou chat pausado."""
    frase = (proposta.ganhar_tempo or "").strip()
    return (trava_envio_aberta()
            and decisao.veredito is Veredito.CHAMAR_DONO
            and decisao.detalhes.get("regra") in ("segurar", "demanda")
            and not proposta.perguntou_se_ia
            and normalizar(str(proposta.categoria or "")) not in CATEGORIAS_LINHA_VERMELHA
            and bool(frase) and "\n" not in frase and len(frase) <= 60
            and not detectar_sensivel(frase))


def em_piloto(chat_jid: str) -> bool:
    """True se este chat está sob o piloto agora. Fora disso nem vale gastar o
    Atendente nem pausar o chat quando o dono escreve nele."""
    atual = estado()
    return (atual.modo is Modo.PILOTO and not atual.corte
            and chat_jid not in atual.pausados and _no_escopo(atual, chat_jid))


def _regras(atual: Estado, evento: Evento, proposta: Proposta) -> Decisao:
    def veredito(tipo: Veredito, motivo: str, **detalhes) -> Decisao:
        return Decisao(tipo, motivo, atual.modo, detalhes)

    acao = _acao(proposta)
    if atual.corte:
        if acao is Acao.IGNORAR:
            return veredito(Veredito.NAO_ENVIAR, "corte ligado", regra="corte")
        return veredito(Veredito.CHAMAR_DONO, "corte ligado", regra="corte")
    if evento.chat_jid in atual.pausados:
        return veredito(Veredito.NAO_ENVIAR, "dono na conversa", regra="pausado")
    if acao is Acao.IGNORAR:
        return veredito(Veredito.NAO_ENVIAR, "não pede resposta", regra="ignorar")

    # Ação desconhecida é tratada como segurar: o contrato pode crescer sem que
    # o portão saiba, e o que ele não reconhece não sai.
    if acao is not Acao.RESPONDER:
        return veredito(Veredito.CHAMAR_DONO, "resposta segurada", regra="segurar")
    if proposta.perguntou_se_ia:
        return veredito(Veredito.CHAMAR_DONO, "perguntaram se é IA", regra="ia")
    if normalizar(str(proposta.categoria or "")) in CATEGORIAS_LINHA_VERMELHA:
        return veredito(Veredito.CHAMAR_DONO, "categoria de linha vermelha",
                        regra="linha_vermelha")

    if str(proposta.demanda or "").strip():
        return veredito(Veredito.CHAMAR_DONO, "há demanda para o dono", regra="demanda")

    for onde, texto in (("proposta", proposta.texto), ("mensagem", evento.texto)):
        achado = detectar_sensivel(texto or "")
        if achado:
            return veredito(Veredito.CHAMAR_DONO,
                            f"conteúdo sensível na {onde}: {achado}",
                            regra="sensivel", padrao=achado, onde=onde)

    # O detector acima não enxerga o conteúdo de áudio, imagem ou documento.
    if evento.midia:
        return veredito(Veredito.CHAMAR_DONO, "mensagem com mídia", regra="midia")

    # Sem texto ou sem destino não há o que enviar, mesmo com "assume tudo".
    if not (proposta.texto or "").strip() or not evento.chat_jid:
        return veredito(Veredito.CHAMAR_DONO, "proposta sem texto ou sem chat",
                        regra="incompleta")

    if atual.modo is not Modo.PILOTO:
        return veredito(Veredito.PEDIR_APROVACAO, "modo copiloto", regra="copiloto")
    if not _no_escopo(atual, evento.chat_jid):
        return veredito(Veredito.PEDIR_APROVACAO, "chat fora do escopo do piloto",
                        regra="fora_do_escopo")
    return veredito(Veredito.ENVIAR, "piloto no escopo", regra="piloto",
                    chat_jid=evento.chat_jid)


def decidir(evento: Evento, proposta: Proposta) -> Decisao:
    try:
        return _regras(estado(), evento, proposta)
    except Exception:
        log.exception("erro ao decidir; chamando o dono")
        return Decisao(Veredito.CHAMAR_DONO, "erro interno no portão",
                       detalhes={"regra": "erro"})


def liberar_envio(decisao: Decisao) -> bool:
    """Trava geral: última conferência antes de algo sair para terceiros."""
    try:
        if not trava_envio_aberta() or decisao.veredito is not Veredito.ENVIAR:
            return False
        # Entre decidir e enviar o dono pode ter dado "volta" ou entrado no chat;
        # a ordem dele tem efeito imediato, inclusive sobre decisão já tomada.
        atual = estado()
        return not (atual.corte
                    or decisao.detalhes.get("chat_jid") in atual.pausados)
    except Exception:
        log.exception("erro ao liberar envio; segurando")
        return False


# ---------------------------------------------------------------------------
# Diário
# ---------------------------------------------------------------------------
_OMITIDO = "[segredo omitido]"
_MASCARAS = (
    *_PADROES_SEGREDO,
    re.compile(_INICIO_CHAVE_PRIVADA + r"[\s\S]*?(?:end\s+(?:[a-z0-9]+\s+){0,3}"
               r"private\s+key\s*-*|\Z)", re.IGNORECASE),
    re.compile(r"(?:senha|password|passwd|pwd|token|secret)\s*[:=]\s*\S+",
               re.IGNORECASE),
)


def _mascarar(texto: str) -> str:
    # O diário fica em texto puro no disco: não pode virar cópia do segredo que
    # o portão acabou de barrar.
    limpo = _limpar(texto)
    for mascara in _MASCARAS:
        limpo = mascara.sub(_OMITIDO, limpo)
    return limpo


def _valor(campo) -> str:
    return str(getattr(campo, "value", campo))


def registrar(evento: Evento, proposta: Proposta, decisao: Decisao) -> bool:
    """Acrescenta uma linha ao diário. Falha de disco não derruba o atendimento."""
    linha = json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "chat_jid": evento.chat_jid,
        "message_id": evento.message_id,
        "modo": _valor(decisao.modo),
        "veredito": _valor(decisao.veredito),
        "motivo": decisao.motivo,
        # Vem do modelo, que leu a mensagem: o limite impede que ela vaze por aqui.
        "categoria": str(proposta.categoria or "")[:40],
        "texto_proposto": _mascarar(proposta.texto or ""),
    }, ensure_ascii=False) + "\n"
    try:
        with _trava:
            fd = os.open(_arquivo_diario(),
                         os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, linha.encode("utf-8"))
            finally:
                os.close(fd)
        return True
    except OSError as erro:
        log.error("não consegui escrever no diário do piloto: %s", erro)
        return False
