"""MCP-клиент на stdlib: JSON-RPC 2.0 через stdin/stdout MCP-сервера BB, одно сообщение — одна строка JSON.

Учётка BB приходит в процесс MCP-сервера по отдельному каналу (fd) и кладётся в os.environ
уже запущенного процесса: /proc/<pid>/environ показывает исходное окружение execve, учётки там нет.
"""
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from registry import config, creds
from registry.util import dumps

MCP_PROTOCOL = "2025-06-18"
MCP_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MCP_CLIENT_INFO = {"name": "ck-registry", "version": "2.0"}
MCP_ERR_PREFIX = re.compile(r"^\s*Error executing tool [\w.-]+:\s*")
# Лаунчер MCP-сервера: учётка приходит по отдельному каналу (в аргументах — только номер fd) и кладётся в os.environ
# уже запущенного процесса — это его память. stdin сервера — только под JSON-RPC.
BB_LAUNCHER = """import os, runpy, sys
fd, path = int(sys.argv[1]), os.path.abspath(sys.argv[2])
with os.fdopen(fd, "r", encoding="utf-8", errors="surrogateescape", newline="\\n") as f:
    user, password = f.readline().rstrip("\\n"), f.readline().rstrip("\\n")
os.environ["BITBUCKET_USERNAME"], os.environ["BITBUCKET_PASSWORD"] = user, password
del user, password
folder = os.path.dirname(path)
sys.argv = [path]
sys.path.insert(0, folder)
os.chdir(folder)
runpy.run_path(path, run_name="__main__")
"""
BB_STEP = {"запуск MCP-сервера": "MCP-сервер BB не запустился", "clone": "не удалось клонировать репозиторий БЗ",
           "checkout": "не удалось перейти на целевую ветку", "новая ветка": "не удалось создать новую ветку",
           "запись файлов": "не удалось записать файлы карточек в клон", "add": "не удалось добавить файлы в git",
           "status": "не удалось проверить изменения", "commit": "не удалось сделать коммит",
           "push": "не удалось отправить ветку в Bitbucket", "pull request": "не удалось создать pull request"}
BB_HINTS = (   # первая подходящая подсказка — по тексту ошибки (уже с маской учётки)
    (r"please tell me who you are|user\.email|user\.name|empty ident|auto-detect email",
     "задайте git-идентичность в своём контейнере — git config --global user.name \"Фамилия Имя\" и "
     "git config --global user.email \"ваша почта\" — и повторите выгрузку"),
    (r"no module named|modulenotfounderror|importerror",
     "у python для MCP-сервера нет его пакетов — укажите в REG_BB_MCP_PYTHON python окружения MCP-сервера "
     "(например, его .venv/bin/python) и перезапустите bash connect.sh"),
    (r"no such file or directory: '?git'?|git: not found|git: command not found",
     "в вашем контейнере не найден git — он нужен MCP-серверу BB"),
    (r"authentication failed|\b401\b|unauthori[sz]ed|invalid (username|password|credentials)|"
     r"incorrect (username|password)|could not read (username|password)",
     "Bitbucket не принял логин или пароль BB — перезапустите bash connect.sh и введите их заново"),
    (r"\b403\b|forbidden|permission denied|not permitted|access denied|insufficient permission",
     "у вашей учётки BB нет прав на этот репозиторий — попросите доступ у владельца репозитория БЗ"),
    (r"certificate|\bssl\b",
     "Bitbucket не прошёл проверку SSL-сертификата в вашем контейнере — это настройка git или MCP-сервера"),
    (r"could not resolve host|name or service not known|connection refused|timed out|network is unreachable|"
     r"failed to connect|connection reset",
     "Bitbucket недоступен из вашего контейнера — проверьте сеть и REG_BB_BASE_URL"),
    (r"did not match any|pathspec|unknown revision|invalid reference|couldn't find remote ref",
     "в репозитории нет целевой ветки — проверьте REG_BB_BRANCH у владельца реестра"),
    (r"repository not found|not found|\b404\b|does not exist",
     "проверьте у владельца реестра REG_BB_REPO_URL, REG_BB_PROJECT и REG_BB_REPO"),
)


class BbFail(Exception):
    """Ошибка шага выгрузки в BB: step — где упало, text — суть (маска учётки — при выводе, bb_message)."""

    def __init__(self, step, text=""):
        super().__init__(text)
        self.step, self.text = step, str(text or "")


class BbNothing(Exception):
    """В BB карточки уже такие же — коммитить нечего."""


def bb_message(step, text):
    """Ошибка выгрузки для человека: по-русски, с шагом и подсказкой, без учётки."""
    text = creds.bb_mask(text).strip()
    if len(text) > 700:
        text = text[:700].rstrip() + "…"
    hint = next((h for rx, h in BB_HINTS if re.search(rx, text, re.I)), "")
    return (f"BB, шаг «{step}»: {BB_STEP.get(step, 'ошибка')}" + (f": {text}" if text else "")
            + (f"\nЧто сделать: {hint}." if hint else ""))


def mcp_text(res):
    """Текст результата инструмента MCP (без английского префикса SDK «Error executing tool …:»)."""
    parts = [c["text"] for c in (res.get("content") or []) if isinstance(c, dict) and c.get("type") == "text"
            and isinstance(c.get("text"), str)]
    return MCP_ERR_PREFIX.sub("", "\n".join(parts)).strip()


def mcp_value(res):
    """Значение результата: structuredContent (может быть обёрнут в {"result": …}) или JSON из текста, иначе текст."""
    sc = res.get("structuredContent")
    if isinstance(sc, dict) and set(sc) == {"result"}:
        sc = sc["result"]
    if sc is not None:
        return sc
    text = mcp_text(res)
    try:
        return json.loads(text)
    except ValueError:
        return text


class McpClient:
    """MCP-клиент на stdlib: JSON-RPC 2.0 через stdin/stdout MCP-сервера, одно сообщение — одна строка JSON."""

    def __init__(self, server_py, deadline):
        self.server_py, self.deadline = server_py, deadline
        self.proc, self.seq, self.q = None, 0, queue.Queue()
        self.err, self.err_lock = bytearray(), threading.Lock()   # хвост stderr сервера — для диагностики

    def start(self, step):
        data = f"{creds.BB_USER}\n{creds.BB_PASS}\n".encode("utf-8", "surrogateescape")
        if len(data) > 4000:
            raise BbFail(step, "логин или пароль BB слишком длинные")
        env = {k: v for k, v in os.environ.items() if k not in ("BITBUCKET_USERNAME", "BITBUCKET_PASSWORD")}
        env.update(BITBUCKET_BASE_URL=config.BB_BASE_URL, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        r, w = os.pipe()
        try:   # учётка — в канал до запуска: она меньше буфера канала, запись не ждёт читателя
            while data:
                data = data[os.write(w, data):]
        finally:
            os.close(w)
        try:
            self.proc = subprocess.Popen([config.BB_MCP_PYTHON, "-c", BB_LAUNCHER, str(r), self.server_py],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         pass_fds=(r,), env=env, cwd=str(Path(self.server_py).parent),
                                         start_new_session=True)   # своя группа: сервер и его git — останавливаем вместе
        except OSError as e:
            raise BbFail(step, f"не удалось запустить {config.BB_MCP_PYTHON} (REG_BB_MCP_PYTHON): {e.strerror or e}")
        finally:
            os.close(r)
        threading.Thread(target=self._read_out, daemon=True, name="mcp-out").start()
        threading.Thread(target=self._read_err, daemon=True, name="mcp-err").start()

    def _read_out(self):
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:   # не JSON-RPC — в диагностику
                    self._note(raw)
                    continue
                for m in msg if isinstance(msg, list) else [msg]:
                    if isinstance(m, dict):
                        self.q.put(m)
        except (OSError, ValueError):
            pass
        finally:
            self.q.put(None)

    def _read_err(self):
        try:
            for chunk in iter(lambda: self.proc.stderr.read1(4096), b""):
                self._note(chunk)
        except (OSError, ValueError):
            pass

    def _note(self, chunk):
        with self.err_lock:
            self.err += chunk
            del self.err[:-8000]

    def stderr_tail(self, lines=3, n=500):
        """Последние строки stderr сервера (у трассировки — суть ошибки), с маской учётки."""
        with self.err_lock:
            text = bytes(self.err).decode("utf-8", "replace")
        last = [x.strip() for x in creds.bb_mask(text).splitlines() if x.strip()][-lines:]
        return " | ".join(last)[-n:]

    def _died(self, step):
        try:
            self.proc.wait(1)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.1)   # дочитать stderr упавшего сервера
        code, tail = self.proc.returncode, self.stderr_tail()
        return BbFail(step, "MCP-сервер BB завершился" + (f" с кодом {code}" if code is not None else "")
                      + (f": {tail}" if tail else ""))

    def _send(self, obj, step):
        try:
            self.proc.stdin.write((dumps(obj) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError):
            raise self._died(step)

    def request(self, method, params, step):
        self.seq += 1
        rid = self.seq
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, step)
        while True:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise BbFail(step, f"MCP-сервер не ответил за {config.BB_TIMEOUT} с (предел одной выгрузки — REG_BB_TIMEOUT)")
            try:
                msg = self.q.get(timeout=left)
            except queue.Empty:
                continue
            if msg is None:
                self.q.put(None)
                raise self._died(step)
            if "method" in msg:   # запрос или уведомление сервера
                if msg.get("id") is not None:
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}} if msg["method"] == "ping" else
                               {"jsonrpc": "2.0", "id": msg["id"],
                                "error": {"code": -32601, "message": "Method not found"}}, step)
                continue
            if msg.get("id") != rid:
                continue
            if msg.get("error") is not None:
                e = msg["error"]
                raise BbFail(step, (e.get("message") if isinstance(e, dict) else str(e)) or "ошибка JSON-RPC")
            res = msg.get("result")
            return res if isinstance(res, dict) else {}

    def initialize(self, step):
        res = self.request("initialize", {"protocolVersion": MCP_PROTOCOL, "capabilities": {},
                                          "clientInfo": MCP_CLIENT_INFO}, step)
        ver = res.get("protocolVersion")
        if ver not in MCP_PROTOCOLS:
            raise BbFail(step, f"MCP-сервер отвечает по версии протокола «{str(ver)[:40]}», реестр знает "
                               + ", ".join(MCP_PROTOCOLS))
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"}, step)

    def call(self, tool, args, step):
        res = self.request("tools/call", {"name": tool, "arguments": args}, step)
        if res.get("isError"):
            raise BbFail(step, mcp_text(res) or "инструмент вернул ошибку без текста")
        return res

    def close(self):
        """Закрыть stdin (stdio-сервер MCP завершается сам), подождать; не вышел — TERM и KILL его группе."""
        p = self.proc
        if p is None:
            return
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(3)
            return
        except subprocess.TimeoutExpired:
            pass
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(p.pid, sig)
            except OSError:
                pass
            try:
                p.wait(3)
                return
            except subprocess.TimeoutExpired:
                continue
