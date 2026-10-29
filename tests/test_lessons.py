"""Регрессия на «уроки» из CLAUDE.md — грабли, на которые уже наступали."""
import re
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import ROOT, backend_text, frontend_text  # noqa: E402
HTML = frontend_text()   # весь фронтенд одним текстом (шаблон + css + js)
APP = backend_text()       # весь бэкенд одним текстом (app.py + registry/)


class LessonsTest(unittest.TestCase):
    def test_animations_not_disabled_by_reduced_motion(self):
        """В VDI prefers-reduced-motion включён — анимации глушить по нему нельзя."""
        self.assertNotIn("prefers-reduced-motion", HTML)

    def test_assistant_ball_not_using_busy_class(self):
        """Класс состояния шара не должен совпадать с .busy заставки (display:none)."""
        self.assertIsNone(re.search(r"#lava'?\)?\.classList\.(toggle|add)\('busy'", HTML))
        self.assertIsNone(re.search(r"\.lava\.busy\b", HTML))

    def test_gp_sql_without_on_conflict(self):
        """GP6 не знает ON CONFLICT; таблицы — с DISTRIBUTED BY."""
        self.assertNotIn("ON CONFLICT", APP.upper())
        self.assertIn("DISTRIBUTED BY", APP)

    def test_x_user_header_encoded(self):
        """Кириллица в заголовке не проходит: X-User — encodeURIComponent на клиенте, unquote на сервере."""
        for m in re.finditer(r"'X-User'\s*:\s*([^,}\n]+)", HTML):
            self.assertIn("encodeURIComponent", m.group(1))
        for m in re.finditer(r"headers\.get\(\"X-User\"\)", APP):
            self.assertIn("unquote(", APP[max(0, m.start() - 20):m.start()])

    def test_tests_do_not_use_pkill(self):
        """Тесты не вызывают p-kill по шаблону — он убивает собственную оболочку; останавливаем по PID."""
        call = re.compile(r"[\"'\[]\s*p" + r"kill\b")
        for f in (ROOT / "tests").glob("*.py"):
            self.assertIsNone(call.search(f.read_text("utf-8")), f.name)

    def test_gigacode_web_approval_whitelist(self):
        """GigaCode из веба — только default|plan."""
        self.assertIn('("default", "plan")', APP)


if __name__ == "__main__":
    unittest.main()
