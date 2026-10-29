"""Фейковый REST Bitbucket для тестов (только stdlib) — вместо настоящего /rest/api/1.0 (T-013).

POST /rest/api/1.0/projects/<project>/repos/<repo>/pull-requests: Basic-авторизация (ожидаемые
тестовые логин и пароль), ветки проверяются в локальном bare-репозитории, номер pull request —
из файла-счётчика. Журнал: метод, путь, хеш учётки из заголовка, тело запроса — по нему тест
проверяет, что прокси прислал правильные учётку, проект, репозиторий и ветки.
Режимы отказа (set_fail): "" | "401" | "403" | "404" | "500" | "slow". Все данные выдуманные."""
import base64
import hashlib
import json
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PR_PATH = re.compile(r"^/rest/api/1\.0/projects/(~?[\w.\-]+)/repos/([\w.\-]+)/pull-requests$")


def _sha(v):
    return hashlib.sha256(v.encode("utf-8")).hexdigest() if v else None


class FakeBitbucket:
    """repos: {"<project>/<repo>": "<путь к bare-репозиторию>"}; login/password — ожидаемая учётка."""

    def __init__(self, work, repos, login, password):
        self.dir = Path(work)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.repos, self.login, self.password = dict(repos), login, password
        self.counter = self.dir / "pr-counter"
        self.journal = self.dir / "rest-journal.jsonl"
        self.fail = ""
        reg = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _j(self, code, obj):
                data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                reg._note(self.path, self.headers.get("Authorization") or "", raw)
                if reg.fail == "slow":
                    time.sleep(3600)
                if reg.fail in ("401", "403", "404", "500"):
                    return self._j(int(reg.fail), {"errors": [{"message": f"HTTP {reg.fail} (режим отказа теста)"}]})
                m = PR_PATH.match(self.path)
                if not m:
                    return self._j(404, {"errors": [{"message": "Not found"}]})
                user, pw = reg._auth(self.headers.get("Authorization") or "")
                if (user, pw) != (reg.login, reg.password):
                    return self._j(401, {"errors": [{"message": "Authentication failed. Wrong username or password"}]})
                bare = reg.repos.get(f"{m.group(1)}/{m.group(2)}")
                if not bare:
                    return self._j(404, {"errors": [{"message": "Repository not found"}]})
                try:
                    payload = json.loads(raw)
                    src = payload["fromRef"]["id"].removeprefix("refs/heads/")
                    dst = payload["toRef"]["id"].removeprefix("refs/heads/")
                    title = payload["title"]
                except Exception:
                    return self._j(400, {"errors": [{"message": "Bad request"}]})
                for br in (src, dst):
                    p = subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "--verify", "--quiet",
                                        f"refs/heads/{br}"], capture_output=True)
                    if p.returncode or not p.stdout.strip():
                        return self._j(404, {"errors": [{"message": f"Branch {br} does not exist"}]})
                num = int(reg.counter.read_text() or 0) + 1 if reg.counter.exists() else 1
                reg.counter.write_text(str(num))
                href = f"{reg.base}/projects/{m.group(1)}/repos/{m.group(2)}/pull-requests/{num}"
                self._j(201, {"id": num, "title": title, "state": "OPEN",
                              "links": {"self": [{"href": href}]}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.socket.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _auth(self, header):
        try:
            b64 = header.removeprefix("Basic ").encode("ascii")
            user, _, pw = base64.b64decode(b64).decode("utf-8").partition(":")
            return user, pw
        except Exception:
            return None, None

    def _note(self, path, auth, raw):
        user, _ = self._auth(auth)
        try:
            payload = json.loads(raw)
        except Exception:
            payload = None
        with open(self.journal, "a", encoding="utf-8") as f:
            f.write(json.dumps({"path": path, "auth_user_sha": _sha(user), "auth_ok": user == self.login,
                                "payload": payload}, ensure_ascii=False) + "\n")

    def records(self):
        if not self.journal.exists():
            return []
        return [json.loads(x) for x in self.journal.read_text("utf-8").splitlines()]

    def mark(self):
        return len(self.records())

    def since(self, m):
        return self.records()[m:]

    def set_fail(self, mode):
        self.fail = mode

    def stop(self):
        self.httpd.shutdown()
