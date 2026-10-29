"""Дымовые тесты реестра: сервер поднимается, задачи создаются, конфликт версий ловится.
Сервер — подпроцессом на свободном порту, реестр — во временной папке (каркас — tests/helpers.py).
С T-002 запись — только с сессией: админ (PIN через stdin) заводит тимлида и его PIN, тимлид входит."""
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import ROOT, Server, frontend_text, teams_for# noqa: E402

ADMIN_PIN = "2580"
LEAD, LEAD_PIN = "Тестов Тест Тестович", "1357"


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Server("json", args=["--admin-pin-stdin"], stdin_lines=[ADMIN_PIN])
        code, r = cls.s.call("POST", "/api/login", {"admin": True, "pin": ADMIN_PIN})
        assert code == 200, r
        adm = r["session"]
        cfg = cls.s.call("GET", "/api/state")[1]["config"]
        cfg["people"] = [{"name": LEAD, "email": "testov@example.test", "role": "teamlead", "active": True}]
        cfg["teams"] = teams_for(cfg["people"])
        code, r = cls.s.call("POST", "/api/config", {"config": cfg, "expect_v": cfg["_v"]}, session=adm)
        assert code == 200, r
        code, r = cls.s.call("POST", "/api/admin/pin", {"name": LEAD, "pin": LEAD_PIN}, session=adm)
        assert code == 200, r
        code, r = cls.s.call("POST", "/api/login", {"name": LEAD, "pin": LEAD_PIN})
        assert code == 200, r
        cls.sid = r["session"]

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    def new_task(self, title="Тестовая задача"):
        code, r = self.s.call("POST", "/api/task", {"task": {"kind": "once", "id": None, "title": title,
                                                            "teamlead": LEAD, "executor": LEAD,
                                                            "deadline": "2030-12-31"},
                                                   "action": "Поставлена"}, user=LEAD, session=self.sid)
        self.assertEqual(code, 200, r)
        return r["task"]

    def test_state_and_config(self):
        code, st = self.s.call("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(st["meta"]["storage"], "json")
        self.assertTrue(st["config"]["blocks"])

    def test_create_task_defaults_and_author(self):
        t = self.new_task()
        self.assertEqual(t["status"], "Новая")
        self.assertEqual(t["updated_by"], LEAD)   # кириллица: в X-User (quote) и в сессии
        self.assertEqual(t["_v"], 1)

    def test_version_conflict(self):
        t = self.new_task("Конфликт версий")
        code, _ = self.s.call("POST", "/api/task", {"task": {**t, "title": "Правка 1"}, "expect_v": t["_v"]},
                              session=self.sid)
        self.assertEqual(code, 200)
        code, r = self.s.call("POST", "/api/task", {"task": {**t, "title": "Правка 2"}, "expect_v": t["_v"]},
                              session=self.sid)
        self.assertEqual(code, 409, r)

    def test_api_requires_token(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.s.base + "/api/state", timeout=5)
        self.assertEqual(cm.exception.code, 403)

    def test_local_host_check_default_on(self):
        """--local по умолчанию принимает только localhost: чужой Host — 403 с подсказкой
        (защита от DNS-rebinding: злая страница резолвит свой домен на 127.0.0.1)."""
        s = Server("json")
        try:
            req = urllib.request.Request(s.base + "/", headers={"Host": "jlab-12345.sbrf.ru"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 403)
            self.assertIn("--local", cm.exception.read().decode("utf-8"))
        finally:
            s.stop()

    def test_local_host_check_off(self):
        """REG_LOCAL_HOST_CHECK=0: --local без проверки Host — например, проброс через локальный прокси."""
        s = Server("json", env={"REG_LOCAL_HOST_CHECK": "0"})
        try:
            req = urllib.request.Request(s.base + "/", headers={"Host": "jlab-12345.sbrf.ru"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual(r.status, 200)
        finally:
            s.stop()

    def test_assets_paths_relative(self):
        """Урок DataLab: страница живёт за /proxy/<порт>/ — абсолютные /static/… уходят мимо прокси.
        Стили и модуль main.js — относительные пути; адрес без слеша чинит скрипт в каркасе."""
        html = frontend_text()
        self.assertNotIn('href="/static/', html, "абсолютный путь стилей не работает за префиксом")
        self.assertNotIn('src="/static/', html, "абсолютный путь скрипта не работает за префиксом")
        for f in ("base.css", "card.css", "ai.css", "theme.css"):
            self.assertIn(f'href="static/css/{f}"', html)
        self.assertIn('src="static/js/main.js"', html)
        self.assertIn('link[rel="stylesheet"]', html, "нет починки стилей для адреса без слеша")

    def test_page_base_path_without_trailing_slash(self):
        html = frontend_text()
        self.assertIn("p.endsWith('/') ? p :", html, "адрес …/proxy/39246 без слеша должен работать")


if __name__ == "__main__":
    unittest.main()
