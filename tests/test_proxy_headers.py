"""Прокси коллеги (`app.py proxy`, connect.sh) и заголовки браузера — найдено при проверке T-010.

Браузер сотрудника ходит на свой прокси через jupyter-server-proxy своего Jupyter (/user/<табельный>/proxy/9000/),
а jupyter-server-proxy передаёт прокси заголовки браузера как есть — в том числе Cookie сессии JupyterHub
(jupyterhub-user-<табельный>, _xsrf) и Authorization. Если прокси перешлёт их серверу реестра владельца, владелец
получит сессию сотрудника в его Jupyter — а это доступ к его контейнеру, где работает прокси с учёткой BB в памяти
(T-010: «учётка сотрудника никогда не попадает к владельцу сервера реестра»). Реестру куки и Authorization не нужны:
вход — X-Token и X-Session. И наоборот: Set-Cookie от сервера владельца не должен доходить до браузера сотрудника
(подбросить куку на домен Jupyter). Сервер владельца — поддельный (http.server в тесте). Данные выдуманные."""
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import APP, clean_env, free_port  # noqa: E402

NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BROWSER_SECRETS = {"Cookie": "jupyterhub-user-1234567=FAKE-HUB-COOKIE; _xsrf=FAKE-XSRF-T010",
                   "Authorization": "token FAKE-JUPYTER-TOKEN-T010", "X-XSRFToken": "FAKE-XSRF-T010"}
NEEDED = {"X-Token": "fake-token-t010", "X-Session": "fake-session-t010", "Content-Type": "application/json"}
OPTIONAL = {"X-User": "%D0%A2%D0%B5%D1%81%D1%82", "Accept": "application/json"}   # можно пересылать, можно нет


class Owner:
    """Поддельный сервер владельца: запоминает заголовки запросов, в ответ ставит куку на весь домен."""

    def __init__(self):
        self.headers = []
        owner = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _any(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                owner.headers.append({k.lower(): v for k, v in self.headers.items()})
                data = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", "_xsrf=OWNER-SET; Path=/")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = _any

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ProxyHeadersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owner = Owner()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.port = free_port()
        cls.log = open(Path(cls._tmp.name) / "proxy.log", "w+b")
        cls.proc = subprocess.Popen([sys.executable, str(APP), "proxy", f"127.0.0.1:{cls.owner.port}",
                                     "--port", str(cls.port)], env=clean_env(), cwd=cls._tmp.name,
                                    stdin=subprocess.DEVNULL, stdout=cls.log, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.2).close()
                return
            except OSError:
                if cls.proc.poll() is not None:
                    break
                time.sleep(0.1)
        cls.tearDownClass()
        raise RuntimeError("прокси не поднялся")

    @classmethod
    def tearDownClass(cls):
        if cls.proc.poll() is None:   # строго по PID
            cls.proc.terminate()
            try:
                cls.proc.wait(5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(5)
        cls.log.close()
        cls.owner.stop()
        cls._tmp.cleanup()

    def request(self, method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode("utf-8"),
                                     headers={**BROWSER_SECRETS, **NEEDED, **OPTIONAL})
        try:
            with NO_PROXY.open(req, timeout=15) as r:
                return r.status, dict(r.headers.items())
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers.items())

    def test_browser_credentials_not_forwarded_to_owner(self):
        """Cookie, Authorization и X-XSRFToken браузера сотрудника не уходят на сервер владельца; нужное реестру — уходит."""
        for method, path, body in (("GET", "/api/state", None), ("POST", "/api/task", {"task": {}}),
                                   ("DELETE", "/api/task", {"kind": "once", "id": 1})):
            with self.subTest(method=method, path=path):
                n = len(self.owner.headers)
                code, _ = self.request(method, path, body)
                self.assertEqual(code, 200)
                got = self.owner.headers[n]
                leaked = [k for k in ("cookie", "authorization", "x-xsrftoken") if k in got]
                self.assertEqual(leaked, [], f"сервер владельца получил {leaked}: {[got[k] for k in leaked]}")
                for k, v in NEEDED.items():
                    self.assertEqual(got.get(k.lower()), v, k)

    def test_owner_cookies_not_passed_to_browser(self):
        """Set-Cookie от сервера владельца не доходит до браузера сотрудника (кука на домен его Jupyter)."""
        code, headers = self.request("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertNotIn("set-cookie", {k.lower() for k in headers})


if __name__ == "__main__":
    unittest.main()
