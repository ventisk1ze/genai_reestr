"""T-002 + уроки: лава-болл через фейковый gigacode. Секреты не утекают в окружение дочернего процесса
(а значит, и в os.environ сервера), режим подтверждений — только default|plan, create-task из веба — без терминала."""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import Server, teams_for  # noqa: E402

ADMIN_PIN = "86420135"
GP_USER, GP_PASS = "test_gp_user_q7", "Gp-Пароль-Тест-903"
LEAD, LEAD_PIN = "Тестов Тест Тестович", "97531"

FAKE = """#!{py}
import json, os, sys
json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "stdin": sys.stdin.read()}},
          open({out!r}, "w", encoding="utf-8"), ensure_ascii=False)
print("Фейковый ответ GigaCode")
"""


def make_fake(dirpath):
    out = Path(dirpath) / "gigacode-call.json"
    exe = Path(dirpath) / "gigacode"
    exe.write_text(FAKE.format(py=sys.executable, out=str(out)), "utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe, out


def start(approval="default"):
    fake_dir = tempfile.TemporaryDirectory()
    exe, out = make_fake(fake_dir.name)
    s = Server("sqlite", args=["--creds-stdin", "--admin-pin-stdin"], stdin_lines=[GP_USER, GP_PASS, ADMIN_PIN],
               env={"REG_GIGACODE_BIN": str(exe), "REG_GIGACODE_APPROVAL": approval,
                    "PATH": fake_dir.name + os.pathsep + os.environ.get("PATH", "")})
    code, r = s.call("POST", "/api/login", {"admin": True, "pin": ADMIN_PIN})
    assert code == 200, r
    adm = r["session"]
    cfg = s.call("GET", "/api/state")[1]["config"]
    cfg["people"] = [{"name": LEAD, "email": "testov@example.test", "role": "teamlead", "active": True}]
    cfg["teams"] = teams_for(cfg["people"])
    assert s.call("POST", "/api/config", {"config": cfg, "expect_v": cfg["_v"]}, session=adm)[0] == 200
    assert s.call("POST", "/api/admin/pin", {"name": LEAD, "pin": LEAD_PIN}, session=adm)[0] == 200
    sid = s.call("POST", "/api/login", {"name": LEAD, "pin": LEAD_PIN})[1]["session"]
    return s, fake_dir, out, sid


ASK = {"engine": "gigacode", "messages": [{"role": "user", "content": "Поставь задачу про выдуманный отчёт"}]}


class GigaCodeFromWebTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s, cls.fake_dir, cls.out, cls.sid = start()
        code, cls.r = cls.s.call("POST", "/api/llm", ASK, session=cls.sid)
        cls.call = json.loads(cls.out.read_text("utf-8")) if cls.out.exists() else None

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()
        cls.fake_dir.cleanup()

    def test_called(self):
        self.assertIsNotNone(self.call, self.r)
        self.assertIn("Фейковый ответ", json.dumps(self.r, ensure_ascii=False))

    def test_no_secrets_in_child_env(self):
        """os.environ сервера не содержит админский PIN и пароль GP — иначе они видны дочерним процессам."""
        blob = json.dumps(self.call["env"], ensure_ascii=False) + " ".join(self.call["argv"])
        for secret in (ADMIN_PIN, GP_PASS, LEAD_PIN):
            self.assertNotIn(secret, blob)
        self.assertNotIn(ADMIN_PIN, self.call["stdin"])
        self.assertNotIn(GP_PASS, self.call["stdin"])

    def test_approval_mode_default(self):
        argv = self.call["argv"]
        self.assertIn("--approval-mode", argv)
        self.assertEqual(argv[argv.index("--approval-mode") + 1], "default")
        self.assertNotIn("auto-edit", argv)

    def test_create_task_without_tty_in_child(self):
        self.assertEqual(self.call["env"].get("REG_NO_TTY"), "1")


class GigaCodeApprovalTest(unittest.TestCase):
    def test_auto_edit_refused(self):
        s, fake_dir, out, sid = start("auto-edit")
        try:
            code, r = s.call("POST", "/api/llm", ASK, session=sid)
            self.assertEqual(code, 400, r)
            self.assertFalse(out.exists(), "gigacode не должен запускаться с auto-edit")
        finally:
            s.stop()
            fake_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
