"""Testes do roteador e do webhook real em porta efêmera.

Nada aqui fala com a bridge nem com o Claude: `requests` e `subprocess` ficam
trocados por mocks e cada teste confere que não foram chamados.
"""
import contextlib
import http.client
import io
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import canal_whatsapp as canal
import config
from roteador import classificar, evento_de_payload, normalizar_jid
from tipos import Evento, Rota

DONO = "5511999990000@s.whatsapp.net"
DONO_LID = "123456789@lid"
JIDS_DONO = {DONO, DONO_LID}
OUTRO = "5511888880000@s.whatsapp.net"
GRUPO = "120363000000000000@g.us"
TOKEN = "token-de-teste"


def _evento(chat_jid=OUTRO, texto="oi", de_mim=False, **extras) -> Evento:
    return Evento(chat_jid=chat_jid, message_id="M1", sender="5511",
                  texto=texto, de_mim=de_mim, **extras)


class TestEventoDePayload(unittest.TestCase):
    def test_converte_todos_os_campos(self):
        evento = evento_de_payload({
            "eventType": "reaction", "chatJID": GRUPO, "messageId": "M1",
            "sender": "5511", "content": "  oi  ", "isFromMe": True,
            "mediaType": "image",
        })
        self.assertEqual(evento, Evento(
            chat_jid=GRUPO, message_id="M1", sender="5511", texto="oi",
            de_mim=True, tipo="reaction", midia="image", eh_grupo=True))

    def test_sem_event_type_e_mensagem_comum(self):
        evento = evento_de_payload({"chatJID": OUTRO, "content": "oi"})
        self.assertEqual(evento.tipo, "message")
        self.assertFalse(evento.eh_grupo)
        self.assertFalse(evento.de_mim)

    def test_malformado_nao_lanca_e_vira_ignorar(self):
        for dados in ({}, None, [], "texto", 42, {"chatJID": None},
                      {"chatJID": 5, "content": {"a": 1}, "isFromMe": [],
                       "mediaType": 7, "eventType": 8, "messageId": 9,
                       "sender": None}):
            with self.subTest(dados=dados):
                evento = evento_de_payload(dados)
                self.assertEqual(evento.chat_jid, "")
                self.assertEqual(classificar(evento, JIDS_DONO), Rota.IGNORAR)

    def test_is_from_me_so_vale_como_booleano(self):
        for valor in ("true", "false", 1, [1], None):
            with self.subTest(valor=valor):
                evento = evento_de_payload(
                    {"chatJID": DONO, "content": "oi", "isFromMe": valor})
                self.assertFalse(evento.de_mim)
                self.assertEqual(classificar(evento, JIDS_DONO), Rota.IGNORAR)


class TestNormalizarJid(unittest.TestCase):
    def test_tira_sufixo_de_dispositivo(self):
        self.assertEqual(normalizar_jid("5511:12@s.whatsapp.net"),
                         "5511@s.whatsapp.net")
        self.assertEqual(normalizar_jid("123:7@lid"), "123@lid")

    def test_mantem_o_que_nao_tem_sufixo(self):
        for jid in (DONO, DONO_LID, GRUPO, "status@broadcast", ""):
            self.assertEqual(normalizar_jid(jid), jid)

    def test_valor_que_nao_e_texto_vira_vazio(self):
        self.assertEqual(normalizar_jid(None), "")
        self.assertEqual(normalizar_jid(12), "")

    def test_sufixo_no_evento_ou_na_config_ainda_casa_com_o_dono(self):
        com_sufixo = "5511999990000:12@s.whatsapp.net"
        self.assertEqual(
            classificar(_evento(com_sufixo, de_mim=True), JIDS_DONO), Rota.ADMIN)
        self.assertEqual(
            classificar(_evento(DONO, de_mim=True), {com_sufixo}), Rota.ADMIN)
        payload = evento_de_payload(
            {"chatJID": com_sufixo, "content": "oi", "isFromMe": True})
        self.assertEqual(payload.chat_jid, DONO)


class TestClassificarAdmin(unittest.TestCase):
    def test_admin_nas_duas_formas_do_chat_do_dono(self):
        for jid in (DONO, DONO_LID):
            with self.subTest(jid=jid):
                self.assertEqual(
                    classificar(_evento(jid, de_mim=True), JIDS_DONO), Rota.ADMIN)

    def test_outra_pessoa_no_chat_do_dono_e_ignorar(self):
        for jid in (DONO, DONO_LID):
            with self.subTest(jid=jid):
                self.assertEqual(
                    classificar(_evento(jid, de_mim=False), JIDS_DONO),
                    Rota.IGNORAR)

    def test_sender_nao_substitui_de_mim(self):
        evento = Evento(chat_jid=DONO, message_id="M1", sender=DONO,
                        texto="apaga tudo", de_mim=False)
        self.assertEqual(classificar(evento, JIDS_DONO), Rota.IGNORAR)

    def test_grupo_nunca_e_admin_mesmo_listado(self):
        listado = JIDS_DONO | {GRUPO}
        self.assertEqual(
            classificar(evento_de_payload(
                {"chatJID": GRUPO, "content": "oi", "isFromMe": True}), listado),
            Rota.DONO_ENTROU)
        self.assertEqual(
            classificar(_evento(GRUPO, de_mim=False, eh_grupo=True), listado),
            Rota.TERCEIRO)

    def test_grupo_com_eh_grupo_errado_tambem_nao_e_admin(self):
        evento = _evento(GRUPO, de_mim=True, eh_grupo=False)
        self.assertEqual(classificar(evento, {GRUPO}), Rota.DONO_ENTROU)

    def test_eh_grupo_marcado_barra_admin(self):
        evento = _evento(DONO, de_mim=True, eh_grupo=True)
        self.assertEqual(classificar(evento, JIDS_DONO), Rota.DONO_ENTROU)

    def test_sem_jids_de_dono_nao_ha_admin(self):
        self.assertEqual(
            classificar(_evento(DONO, de_mim=True), set()), Rota.DONO_ENTROU)

    def test_admin_com_midia_sem_texto(self):
        evento = _evento(DONO, texto="", de_mim=True, midia="audio")
        self.assertEqual(classificar(evento, JIDS_DONO), Rota.ADMIN)


class TestClassificarOutrasRotas(unittest.TestCase):
    def test_dono_em_outro_chat(self):
        self.assertEqual(
            classificar(_evento(OUTRO, de_mim=True), JIDS_DONO), Rota.DONO_ENTROU)
        self.assertEqual(
            classificar(_evento(GRUPO, de_mim=True, eh_grupo=True), JIDS_DONO),
            Rota.DONO_ENTROU)

    def test_terceiro_em_outro_chat(self):
        self.assertEqual(
            classificar(_evento(OUTRO, de_mim=False), JIDS_DONO), Rota.TERCEIRO)
        self.assertEqual(
            classificar(_evento(GRUPO, de_mim=False, eh_grupo=True), JIDS_DONO),
            Rota.TERCEIRO)

    def test_terceiro_com_midia_sem_texto(self):
        evento = _evento(OUTRO, texto="", midia="image")
        self.assertEqual(classificar(evento, JIDS_DONO), Rota.TERCEIRO)

    def test_status_e_newsletter_sao_ignorados(self):
        for jid in ("status@broadcast", "123@newsletter", "456@broadcast"):
            for de_mim in (True, False):
                with self.subTest(jid=jid, de_mim=de_mim):
                    self.assertEqual(
                        classificar(_evento(jid, de_mim=de_mim), JIDS_DONO),
                        Rota.IGNORAR)

    def test_reacao_e_ignorada_ate_do_dono_no_chat_dele(self):
        casos = (
            _evento(DONO, texto="👍", de_mim=True, tipo="reaction"),
            _evento(DONO, texto="👍", de_mim=True, tipo="reaction",
                    midia="reaction"),
            _evento(DONO, texto="👍", de_mim=True, midia="reaction"),
            _evento(OUTRO, texto="👍", de_mim=True, tipo="reaction"),
            _evento(OUTRO, texto="👍", de_mim=False, tipo="reaction"),
        )
        for evento in casos:
            with self.subTest(evento=evento):
                self.assertEqual(classificar(evento, JIDS_DONO), Rota.IGNORAR)

    def test_sem_texto_e_sem_midia_e_ignorado(self):
        for texto in ("", "   \n"):
            for jid, de_mim in ((DONO, True), (OUTRO, False), (OUTRO, True)):
                with self.subTest(texto=texto, jid=jid, de_mim=de_mim):
                    evento = _evento(jid, texto=texto, de_mim=de_mim)
                    self.assertEqual(classificar(evento, JIDS_DONO), Rota.IGNORAR)

    def test_jid_vazio_ou_invalido_e_ignorado(self):
        for jid in ("", "  ", "sem-servidor", "@s.whatsapp.net", "5511@"):
            with self.subTest(jid=jid):
                self.assertEqual(
                    classificar(_evento(jid, de_mim=True), JIDS_DONO | {jid}),
                    Rota.IGNORAR)
        self.assertEqual(
            classificar(evento_de_payload({"content": "oi", "isFromMe": True}),
                        {""}),
            Rota.IGNORAR)


class TestWebhookHttp(unittest.TestCase):
    """Sobe o servidor por `iniciar_webhook`, como o main faz, em porta efêmera."""

    def setUp(self):
        for alvo, atributo in ((canal.requests, "post"), (canal.requests, "get"),
                               (canal.requests.Session, "request")):
            self._mock_que_nao_pode_rodar(mock.patch.object(alvo, atributo))
        self._mock_que_nao_pode_rodar(mock.patch("subprocess.Popen"))

        for atributo, valor in (("BRIDGE_TOKEN", TOKEN), ("ADMIN_JID", DONO),
                                ("JIDS_DONO", JIDS_DONO),
                                ("WEBHOOK_HOST", "127.0.0.1"),
                                ("WEBHOOK_PORTA", 0)):
            trava = mock.patch.object(config, atributo, valor, create=True)
            trava.start()
            self.addCleanup(trava.stop)

        with canal._lock_enviados:
            canal._enviados_recentes.clear()
        self.addCleanup(canal._enviados_recentes.clear)

        self.saida = io.StringIO()
        self.chamadas = []
        self.threads_dos_callbacks = []

    def _mock_que_nao_pode_rodar(self, trava):
        falso = trava.start()
        self.addCleanup(trava.stop)
        self.addCleanup(falso.assert_not_called)

    def _anotar(self, nome):
        def callback(valor):
            self.threads_dos_callbacks.append(threading.current_thread())
            self.chamadas.append((nome, valor))
        return callback

    def _subir(self, ao_admin=None, ao_terceiro=None, ao_dono_entrou=None):
        servidores = []

        def fabrica(*args, **kwargs):
            servidores.append(ThreadingHTTPServer(*args, **kwargs))
            return servidores[-1]

        with mock.patch.object(canal, "ThreadingHTTPServer", side_effect=fabrica), \
                contextlib.redirect_stdout(io.StringIO()):
            canal.iniciar_webhook(ao_admin or self._anotar("admin"),
                                  ao_terceiro, ao_dono_entrou)
        servidor = servidores[0]
        self.addCleanup(servidor.server_close)
        self.addCleanup(servidor.shutdown)
        self.porta = servidor.server_address[1]
        self.threads_base = set(threading.enumerate())

    def _subir_com_tudo(self):
        self._subir(self._anotar("admin"), self._anotar("terceiro"),
                    self._anotar("dono_entrou"))

    def _post(self, corpo, token=TOKEN, caminho="/webhook", cabecalhos=None) -> int:
        if not isinstance(corpo, bytes):
            corpo = json.dumps(corpo).encode()
        conexao = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=5)
        self.addCleanup(conexao.close)
        conexao.putrequest("POST", caminho)
        if token is not None:
            conexao.putheader("X-Bridge-Token", token)
        for nome, valor in (cabecalhos or {"Content-Length": str(len(corpo))}).items():
            conexao.putheader(nome, valor)
        conexao.endheaders(corpo)
        resposta = conexao.getresponse()
        # O servidor só fecha a conexão depois que o do_POST termina, então
        # ler até o fim garante que o despacho já aconteceu.
        resposta.read()
        return resposta.status

    def _esperar_callbacks(self):
        for thread in set(threading.enumerate()) - self.threads_base:
            if thread is not threading.current_thread():
                thread.join(timeout=5)

    def _postar_e_esperar(self, corpo, **kwargs) -> int:
        with contextlib.redirect_stdout(self.saida):
            status = self._post(corpo, **kwargs)
            self._esperar_callbacks()
        return status

    @staticmethod
    def _mensagem(chat=OUTRO, texto="segredo do terceiro", de_mim=False, **extras):
        return {"chatJID": chat, "messageId": "M1", "sender": "5511",
                "content": texto, "isFromMe": de_mim, **extras}

    def test_token_errado_ausente_ou_nao_ascii_da_401(self):
        self._subir_com_tudo()
        admin = self._mensagem(DONO, de_mim=True)
        for token in ("errado", "", None, "tok\xe9n"):
            with self.subTest(token=token):
                self.assertEqual(self._postar_e_esperar(admin, token=token), 401)
        self.assertEqual(self.chamadas, [])

    def test_sem_token_configurado_tudo_da_401(self):
        self._subir_com_tudo()
        with mock.patch.object(config, "BRIDGE_TOKEN", ""):
            status = self._postar_e_esperar(self._mensagem(), token="")
        self.assertEqual(status, 401)
        self.assertEqual(self.chamadas, [])

    def test_caminho_errado_da_404(self):
        self._subir_com_tudo()
        self.assertEqual(self._postar_e_esperar(self._mensagem(), caminho="/x"), 404)
        self.assertEqual(self.chamadas, [])

    def test_admin_recebe_o_texto(self):
        self._subir_com_tudo()
        status = self._postar_e_esperar(self._mensagem(DONO, "faça", de_mim=True))
        self.assertEqual(status, 200)
        self.assertEqual(self.chamadas, [("admin", "faça")])

    def test_admin_de_midia_recebe_o_aviso(self):
        self._subir_com_tudo()
        self._postar_e_esperar(
            self._mensagem(DONO, "", de_mim=True, mediaType="audio"))
        self.assertEqual(len(self.chamadas), 1)
        nome, texto = self.chamadas[0]
        self.assertEqual(nome, "admin")
        self.assertIn("mandou audio", texto)
        self.assertIn("message_id=M1", texto)

    def test_admin_com_content_que_nao_e_texto_nao_derruba(self):
        self._subir_com_tudo()
        self.assertEqual(self._postar_e_esperar(
            self._mensagem(DONO, 123, de_mim=True, mediaType="image")), 200)
        self.assertEqual([nome for nome, _ in self.chamadas], ["admin"])

    def test_terceiro_recebe_evento_em_thread_daemon(self):
        self._subir_com_tudo()
        self.assertEqual(self._postar_e_esperar(self._mensagem(texto="olá")), 200)
        self.assertEqual(len(self.chamadas), 1)
        nome, evento = self.chamadas[0]
        self.assertEqual(nome, "terceiro")
        self.assertIsInstance(evento, Evento)
        self.assertEqual((evento.chat_jid, evento.texto), (OUTRO, "olá"))
        self.assertTrue(self.threads_dos_callbacks[0].daemon)

    def test_dono_entrou_recebe_evento_em_thread_daemon(self):
        self._subir_com_tudo()
        self._postar_e_esperar(self._mensagem(texto="eu assumo", de_mim=True))
        self.assertEqual(len(self.chamadas), 1)
        nome, evento = self.chamadas[0]
        self.assertEqual(nome, "dono_entrou")
        self.assertTrue(evento.de_mim)
        self.assertTrue(self.threads_dos_callbacks[0].daemon)

    def test_terceiro_no_chat_do_dono_nao_despacha(self):
        self._subir_com_tudo()
        self.assertEqual(self._postar_e_esperar(self._mensagem(DONO)), 200)
        self.assertEqual(self.chamadas, [])

    def test_rotas_ignoradas_dao_200_e_nao_despacham(self):
        self._subir_com_tudo()
        for corpo in (self._mensagem("status@broadcast"),
                      self._mensagem("1@newsletter"),
                      self._mensagem(DONO, "👍", True, eventType="reaction",
                                     mediaType="reaction"),
                      self._mensagem(OUTRO, ""),
                      self._mensagem("")):
            with self.subTest(corpo=corpo):
                self.assertEqual(self._postar_e_esperar(corpo), 200)
        self.assertEqual(self.chamadas, [])

    def test_corpo_malformado_da_200_sem_despacho(self):
        self._subir_com_tudo()
        for corpo in (b"", b"{quebrado", b"[1, 2]", b"42", b"null"):
            with self.subTest(corpo=corpo):
                self.assertEqual(self._postar_e_esperar(corpo), 200)
        self.assertEqual(self.chamadas, [])

    def test_content_length_invalido_nao_derruba(self):
        self._subir_com_tudo()
        for valor in ("abc", "-5"):
            with self.subTest(valor=valor):
                self.assertEqual(self._postar_e_esperar(
                    b"", cabecalhos={"Content-Length": valor}), 200)
        self.assertEqual(self.chamadas, [])

    def test_callback_ausente_descarta_sem_log_do_conteudo(self):
        self._subir()
        self.assertEqual(self._postar_e_esperar(self._mensagem()), 200)
        self.assertEqual(self._postar_e_esperar(
            self._mensagem(texto="segredo do dono", de_mim=True)), 200)
        self.assertEqual(self.chamadas, [])
        self.assertEqual(self.saida.getvalue(), "")

    def test_callback_com_erro_nao_loga_o_texto(self):
        def falha(evento):
            raise ValueError(f"falhou com {evento.texto}")

        self._subir(ao_terceiro=falha)
        erros = io.StringIO()
        with contextlib.redirect_stderr(erros):
            self.assertEqual(self._postar_e_esperar(self._mensagem()), 200)
        self.assertIn("ValueError", self.saida.getvalue())
        self.assertNotIn("segredo", self.saida.getvalue())
        self.assertNotIn("segredo", erros.getvalue())

    def test_200_nao_espera_o_callback(self):
        libera = threading.Event()
        terminou = threading.Event()

        def lento(_evento):
            libera.wait(timeout=5)
            terminou.set()

        self._subir(ao_terceiro=lento)
        self.addCleanup(libera.set)
        self.assertEqual(self._post(self._mensagem()), 200)
        self.assertFalse(terminou.is_set())
        libera.set()
        self.assertTrue(terminou.wait(timeout=5))

    def test_eco_some_na_rota_admin(self):
        self._subir_com_tudo()
        canal._lembrar_enviado("resposta do bot")
        self._postar_e_esperar(self._mensagem(DONO, "resposta do bot", de_mim=True))
        self.assertEqual(self.chamadas, [])
        # O eco é consumido: o dono repetir a frase depois é ordem de verdade.
        self._postar_e_esperar(self._mensagem(DONO, "resposta do bot", de_mim=True))
        self.assertEqual(self.chamadas, [("admin", "resposta do bot")])

    def test_eco_nao_vale_fora_da_rota_admin(self):
        self._subir_com_tudo()
        canal._lembrar_enviado("mesmo texto")
        self._postar_e_esperar(self._mensagem(OUTRO, "mesmo texto", de_mim=True))
        self._postar_e_esperar(self._mensagem(OUTRO, "mesmo texto", de_mim=False))
        self.assertEqual([nome for nome, _ in self.chamadas],
                         ["dono_entrou", "terceiro"])
        self.assertEqual(len(canal._enviados_recentes), 1)

    def _sem_jids_dono_na_config(self):
        ausente = object()
        original = config.__dict__.pop("JIDS_DONO", ausente)
        if original is not ausente:
            self.addCleanup(setattr, config, "JIDS_DONO", original)

    def test_sem_jids_dono_usa_admin_jid(self):
        self._sem_jids_dono_na_config()
        self._subir_com_tudo()
        self._postar_e_esperar(self._mensagem(DONO, "faça", de_mim=True))
        self.assertEqual(self.chamadas, [("admin", "faça")])

    def test_config_no_grupo_antigo_nao_vira_admin(self):
        self._sem_jids_dono_na_config()
        self._subir_com_tudo()
        with mock.patch.object(config, "ADMIN_JID", GRUPO):
            self._postar_e_esperar(self._mensagem(GRUPO, "faça", de_mim=True))
        self.assertEqual([nome for nome, _ in self.chamadas], ["dono_entrou"])


if __name__ == "__main__":
    unittest.main()
