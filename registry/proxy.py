"""Прокси участника команды: реестр коллеги в своём браузере через /proxy/<port>/ своего Jupyter.

В отличие от примера из инструкции DataLab: все методы (реестру нужны DELETE и заголовок
X-Token), заголовки запроса — только белый список PROXY_REQUEST_HEADERS (без кук и Authorization
браузера и без hop-by-hop: иначе ломается Content-Length/chunked), из ответа — без кук (set-cookie).
Выгрузку в BB (T-010) прокси делает сам — см. registry/bb/upload.py. Прокси остаётся на stdlib
(BaseHTTPRequestHandler) — это отдельный лёгкий процесс сотрудника, FastAPI там не нужен.
"""
import signal
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from registry import config, creds
from registry.bb.kb import kb_proxy_ontology, kb_proxy_status, kb_proxy_upload
from registry.bb.upload import bb_cleanup, bb_local_problem, bb_mcp_path, bb_proxy_status, bb_proxy_upload
from registry.util import NoRedirect, dumps

HOP_HEADERS = {"host", "content-length", "connection", "keep-alive", "accept-encoding", "transfer-encoding",
               "te", "trailers", "upgrade", "proxy-authorization", "proxy-authenticate"}
# Прокси коллеги пересылает серверу реестра только то, что нужно реестру (вход — X-Token и X-Session; X-User —
# старая несекретная подпись). Браузер через jupyter-server-proxy присылает и куку JupyterHub сотрудника, и
# Authorization, и X-XSRFToken — они дают вход в его Jupyter (и контейнер, где прокси держит учётку BB), владельцу
# сервера реестра их не отдаём. И обратно: куки от сервера реестра (заголовки set-cookie) браузеру не передаём.
PROXY_REQUEST_HEADERS = {"content-type", "accept", "accept-language", "x-token", "x-session", "x-user"}
PROXY_DROP_RESPONSE = HOP_HEADERS | {"set-cookie", "set-cookie2"}


def run_proxy(a):
    """Прокси участника команды: реестр коллеги (jlab-<табельный>:<порт>) в своём браузере."""
    t = a.target
    target = t if t.startswith("http") else (f"http://jlab-{t}:{a.target_port}" if t.isdigit() else f"http://{t}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)

    server = target.rstrip("/")

    class P(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, code, payload):
            data = dumps(payload).encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            except OSError:   # браузер ушёл, пока шла выгрузка, — результат уже записан в задачу
                pass

        def _bb(self, path):
            """Выгрузка в BB и база знаний — здесь, в контейнере сотрудника; на сервер реестра эти запросы не пересылаются."""
            try:
                if path == "/api/bb/status":
                    return self._json(*bb_proxy_status(server, self.headers))
                if path == "/api/kb/status":
                    return self._json(*kb_proxy_status(server, self.headers))
                if path == "/api/kb/ontology":
                    return self._json(*kb_proxy_ontology(server, self.headers))
                try:
                    n = max(0, int(self.headers.get("Content-Length") or 0))
                except ValueError:
                    n = -1
                if not 0 <= n <= 64 * 1024:
                    self.close_connection = True
                    return self._json(400, {"error": "Некорректный запрос выгрузки в BB"})
                raw = self.rfile.read(n) if n else b""
                if path == "/api/kb/upload":
                    return self._json(*kb_proxy_upload(server, self.headers, raw))
                return self._json(*bb_proxy_upload(server, self.headers, raw))
            except Exception as e:   # noqa — без трассировки и без учётки
                return self._json(500, {"error": "Выгрузка в BB: " + creds.bb_mask(f"{type(e).__name__}: {e}")})

        def _go(self):
            path = urlparse(self.path).path
            bb_get = ("/api/bb/status", "/api/kb/status", "/api/kb/ontology")
            bb_post = ("/api/bb/upload", "/api/kb/upload")
            if (self.command, path) in [("GET", p) for p in bb_get] + [("POST", p) for p in bb_post]:
                return self._bb(path)
            n = int(self.headers.get("Content-Length") or 0)
            req = urllib.request.Request(target + self.path, data=self.rfile.read(n) if n else None, method=self.command,
                                         headers={k: v for k, v in self.headers.items()
                                                  if k.lower() in PROXY_REQUEST_HEADERS})
            try:
                r = opener.open(req, timeout=config.LLM_TIMEOUT + 60)   # ответ ассистента бывает долгим
                code = r.status
            except urllib.error.HTTPError as e:
                r, code = e, e.code
            except Exception as e:
                body = dumps({"error": f"Реестр коллеги {target} недоступен: {e}"}).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = r.read()
            self.send_response(code)
            for k, v in r.headers.items():
                if k.lower() not in PROXY_DROP_RESPONSE:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _go

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), P)
    except OSError as e:
        sys.exit(f"Порт {a.port} занят: {e}")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"Прокси к реестру {target} слушает :{a.port}\n"
          f"В браузере: в адресе своего Jupyter замените всё после /jserver на /proxy/{a.port}/")
    bb = bb_local_problem()
    print(f"Выгрузка карточек в BB: недоступна — {bb}" if bb else
          f"Выгрузка карточек в BB: из этого контейнера — MCP-сервер {bb_mcp_path()}, Bitbucket {config.BB_BASE_URL}; "
          "учётка BB — только в памяти прокси", flush=True)
    try:
        srv.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        bb_cleanup()
