import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import config
import contexto


class Contexto(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory()
        self.addCleanup(self.pasta.cleanup)
        db = os.path.join(self.pasta.name, "messages.db")
        p = mock.patch.object(config, "DB_MENSAGENS", db)
        p.start()
        self.addCleanup(p.stop)
        c = sqlite3.connect(db)
        c.execute("create table messages (id,chat_jid,timestamp,is_from_me,sender,content,media_type)")
        c.execute("create table chats (jid,name)")
        c.executemany("insert into chats values (?,?)",
                      [("1@s.whatsapp.net", "Wésley"), ("2@s.whatsapp.net", "Ana"),
                       ("3@s.whatsapp.net", "Ana")])
        base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
        for i in range(10):
            quando = (base + timedelta(minutes=i)).isoformat(sep=" ")
            c.execute("insert into messages values (?,?,?,?,?,?,?)",
                      (f"m{i}", "1@s.whatsapp.net", quando, i % 2, "1", f"t{i}", ""))
        c.commit(); c.close()
        w = sqlite3.connect(os.path.join(self.pasta.name, "whatsapp.db"))
        w.execute("create table whatsmeow_lid_map (lid,pn)")
        w.execute("insert into whatsmeow_lid_map values ('99','1')")
        w.commit(); w.close()

    def test_historico_em_ordem_sem_a_atual(self):
        h = contexto.historico("1@s.whatsapp.net", excluir_id="m9")
        self.assertEqual([i["texto"] for i in h], [f"t{i}" for i in range(9)])
        self.assertTrue(h[1]["de_mim"])
        self.assertNotIn("remetente", h[1])

    def test_banco_ausente_devolve_none(self):
        with mock.patch.object(config, "DB_MENSAGENS", "/nao/existe.db"):
            self.assertIsNone(contexto.historico("1@s.whatsapp.net"))

    def test_lid_vira_numero(self):
        self.assertEqual(contexto.jid_canonico("99@lid"), "1@s.whatsapp.net")
        self.assertEqual(contexto.jid_canonico("7@lid"), "7@lid")
        self.assertEqual(contexto.jid_canonico("1@s.whatsapp.net"), "1@s.whatsapp.net")

    def test_nome_ignora_acento_e_caixa(self):
        self.assertEqual(contexto.jids_por_nome("wesley"), ["1@s.whatsapp.net"])
        self.assertEqual(len(contexto.jids_por_nome("ana")), 2)
        self.assertEqual(contexto.jids_por_nome("ninguem"), [])

    def test_fora_de_hora_espera_a_proxima_ativa(self):
        tz = timezone.utc
        tres = datetime(2026, 1, 1, 3, 30, tzinfo=tz)
        self.assertEqual(contexto.segundos_ate_hora_ativa({9, 10}, tres), 5.5 * 3600)
        self.assertEqual(contexto.segundos_ate_hora_ativa({3}, tres), 0.0)
        self.assertEqual(contexto.segundos_ate_hora_ativa(set(), tres), 0.0)

    def test_atraso_vem_das_demoras_reais(self):
        with mock.patch.object(contexto, "segundos_ate_hora_ativa", return_value=0.0):
            self.assertEqual(contexto.atraso_de_resposta("1@s.whatsapp.net"), 60.0)
            self.assertEqual(contexto.atraso_de_resposta("1@s.whatsapp.net", ja_passou=20), 40.0)
