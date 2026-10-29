"""T-002: PBKDF2 — боевые 200000 итераций (настройки числа итераций нет — решение тимлида);
REG_ENV_FILE задаёт путь к registry.env ("" — не читать), секреты из файла пропускаются."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import Server  # noqa: E402

ADMIN_PIN = "3141"
EMP, EMP_PIN = "Примерова Анна Сергеевна", "2718"


def stored_iter(env=None):
    s = Server("json", args=["--admin-pin-stdin"], stdin_lines=[ADMIN_PIN], env=env)
    try:
        adm = s.call("POST", "/api/login", {"admin": True, "pin": ADMIN_PIN})[1]["session"]
        cfg = s.call("GET", "/api/state")[1]["config"]
        cfg["people"] = [{"name": EMP, "email": "primerova@example.test", "role": "employee", "active": True}]
        assert s.call("POST", "/api/config", {"config": cfg, "expect_v": cfg["_v"]}, session=adm)[0] == 200
        assert s.call("POST", "/api/admin/pin", {"name": EMP, "pin": EMP_PIN}, session=adm)[0] == 200
        assert s.call("POST", "/api/login", {"name": EMP, "pin": EMP_PIN})[0] == 200
        cfg = json.loads((s.dir / "json" / "config.json").read_text("utf-8"))
        return int(next(p for p in cfg["people"] if p["name"] == EMP)["pin"]["iter"])
    finally:
        s.stop()


class PinIterationsTest(unittest.TestCase):
    def test_default_is_production(self):
        self.assertEqual(stored_iter(), 200_000)


class EnvFileTest(unittest.TestCase):
    def model(self, env_file, log=None):
        s = Server("json", env={"REG_ENV_FILE": env_file})
        try:
            code, r = s.call("GET", "/api/llm")
            self.assertEqual(code, 200, r)
            if log is not None:
                log.append(s.output())
            return r["model"]
        finally:
            s.stop()

    def test_env_file_path_and_disable(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "test.env"
            f.write_text("GIGACHAT_MODEL=fake-model-x7\n", "utf-8")
            self.assertEqual(self.model(str(f)), "fake-model-x7")
            self.assertNotEqual(self.model(""), "fake-model-x7")

    def test_secrets_in_env_file_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "test.env"
            f.write_text("GP_PASSWORD=Файл-Пароль-552\r\nGIGACHAT_MODEL=fake-model-y3\r\n", "utf-8")
            log = []
            self.assertEqual(self.model(str(f), log), "fake-model-y3")
            self.assertIn("GP_PASSWORD", log[0])          # сообщение «пропущен»
            self.assertNotIn("Файл-Пароль-552", log[0])


if __name__ == "__main__":
    unittest.main()
