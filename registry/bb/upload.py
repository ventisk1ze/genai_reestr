"""Сторона сотрудника: выгрузка карточек в BB из контейнера сотрудника через его прокси.

Прокси сам обрабатывает /api/bb/status и /api/bb/upload: по сессии сотрудника берёт у сервера
реестра права, карточки и настройки репозитория, проверяет их (серверу не доверяем), запускает
у себя MCP-сервер BB (stdio) и вызывает его инструменты; серверу отдаёт только результат.
"""
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

from registry import config, creds
from registry.bb import BB_PR_URL_RE, bb_file_ok, bb_target_problem, bb_url_parts
from registry.bb.mcp import BbFail, BbNothing, McpClient, bb_message, mcp_text, mcp_value
from registry.errors import BadRequest
from registry.util import NoRedirect, dumps

BB_LOCK = threading.Lock()   # одна выгрузка за раз
BB_LIVE = []                 # идущие выгрузки (MCP-клиент, временная папка) — дочистить, если прокси остановят


def bb_mcp_path():
    p = Path(config.BB_MCP)
    return p if p.is_absolute() else config.HERE / p


def bb_local_problem():
    """Что не так с настройками BB у сотрудника (пусто — готово)."""
    if not config.BB_MCP:
        return ("У вас не задан REG_BB_MCP — путь к server.py MCP-сервера BB: добавьте его в registry.env рядом "
                "со своим app.py и перезапустите bash connect.sh")
    if not bb_mcp_path().is_file():
        return f"Нет файла MCP-сервера BB {bb_mcp_path()} (REG_BB_MCP в вашем registry.env)"
    if not shutil.which(config.BB_MCP_PYTHON):
        return f"Не найден python для MCP-сервера: {config.BB_MCP_PYTHON} (REG_BB_MCP_PYTHON в вашем registry.env)"
    if "@" in config.BB_BASE_URL:
        return "В REG_BB_BASE_URL — логин или пароль: уберите их, учётку BB connect.sh спрашивает при каждом запуске"
    if not bb_url_parts(config.BB_BASE_URL):
        return f"REG_BB_BASE_URL={config.BB_BASE_URL[:80]}: нужен адрес корпоративного Bitbucket вида https://сервер"
    if not creds.BB_USER:
        return ("Логин и пароль BB не введены — перезапустите bash connect.sh и введите их "
                "(они остаются только в памяти вашего прокси)")
    return ""


def bb_server_call(server, method, path, headers, body=None, timeout=60):
    """Запрос прокси к серверу реестра от имени браузера сотрудника: только X-Token и X-Session из его запроса."""
    h = {"X-Token": headers.get("X-Token") or "", "X-Session": headers.get("X-Session") or "",
         "Content-Type": "application/json"}
    req = urllib.request.Request(server + path, method=method, headers=h,
                                 data=None if body is None else dumps(body).encode("utf-8"))
    cap = 64 * 1024 * 1024   # карточки — текст; больше — не читаем (сторона сотрудника серверу не доверяет)
    try:   # мимо корпоративного прокси и без перехода по редиректам: заголовки сессии — только серверу реестра
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(req, timeout=timeout) as r:
            code, raw = r.status, r.read(cap + 1)
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read(cap + 1)
    except Exception as e:
        return 502, {"error": f"Сервер реестра {server} недоступен: {e}"}
    try:
        d = json.loads(raw.decode("utf-8")) if len(raw) <= cap else None
    except ValueError:
        d = None
    return code, d if isinstance(d, dict) else {"error": f"Сервер реестра ответил не JSON (HTTP {code})"}


def bb_proxy_status(server, headers):
    """GET /api/bb/status на прокси: готово, если у владельца настроен репозиторий, а у сотрудника — MCP и учётка."""
    code, srv = bb_server_call(server, "GET", "/api/bb/status", headers, timeout=20)
    if code not in (200, 404):
        return code, srv
    configured = code == 200 and srv.get("server_configured") is True
    problems = [p for p in (bb_local_problem(),) if p]
    if code == 404:
        problems.append("Сервер реестра не умеет выгружать в BB — владельцу нужно обновить реестр")
    elif not configured:
        miss = [m for m in srv.get("missing") or [] if isinstance(m, str) and re.fullmatch(r"REG_BB_[A-Z_]{1,30}", m)]
        problems.append("Владелец реестра не настроил репозиторий БЗ"
                        + (": нужно задать в его registry.env " + ", ".join(miss) if miss else ""))
    out = {"available": not problems, "reason": "; ".join(problems), "via": "proxy", "server_configured": configured}
    if isinstance(srv.get("target"), dict):
        out["target"] = {k: str(srv["target"].get(k) or "")[:300] for k in ("repo_url", "project", "repo", "branch", "dir")}
    return 200, out


def bb_check_reply(d, kind, tid):
    """Ответ сервера реестра (/api/bb/task) на стороне сотрудника — серверу не доверяем: хост репозитория,
    папка, имена файлов, проект, репозиторий и ветка. Возвращает (задача, карточки, репозиторий, кто выгружает)."""
    def bad(why):
        return BadRequest("Сервер реестра прислал недопустимые настройки BB: " + why)
    task, cards, tg = d.get("task"), d.get("cards"), d.get("target")
    if not isinstance(task, dict) or task.get("kind") != kind or str(task.get("id")) != str(tid):
        raise bad("в ответе другая задача")
    if not isinstance(tg, dict):
        raise bad("нет настроек репозитория")
    url = tg.get("repo_url")
    if isinstance(url, str) and "@" in urlparse(url).netloc:
        raise bad("в адресе репозитория есть логин (…@) — учётка BB уходит только вашему MCP-серверу")
    if not bb_url_parts(url):
        raise bad("адрес репозитория — не http(s) или с недопустимыми символами (?запрос, #фрагмент, пробелы)")
    if bb_url_parts(url) != bb_url_parts(config.BB_BASE_URL):
        raise bad(f"репозиторий на {urlparse(url).netloc} — это не ваш корпоративный Bitbucket "
                  f"{urlparse(config.BB_BASE_URL).netloc} (REG_BB_BASE_URL), учётку BB туда не отправляем")
    problem = bb_target_problem(tg)
    if problem:
        raise bad(problem)
    if not isinstance(cards, list) or not cards or len(cards) > 500:
        raise bad("нет карточек или их слишком много")
    out, seen = [], set()
    for c in cards:
        f = c.get("file") if isinstance(c, dict) else None
        if not bb_file_ok(f) or f in seen:
            raise bad(f"имя файла карточки «{str(f)[:80]}» — нужно имя .md без папок, повторов и ведущей точки")
        if not isinstance(c.get("title"), str) or not c["title"].strip() or not isinstance(c.get("body"), str):
            raise bad(f"у карточки «{f}» нет вопроса или текста")
        seen.add(f)
        out.append({"file": f, "title": c["title"], "body": c["body"]})
    title = task.get("title") if isinstance(task.get("title"), str) else ""
    by = d.get("by") if isinstance(d.get("by"), str) else ""
    return ({"kind": kind, "id": int(tid), "title": title}, out,
            {k: tg[k] for k in ("repo_url", "project", "repo", "branch", "dir")}, by[:200])


def bb_clone_dir(path):
    """Клон, который сделал MCP-сервер: сам path или единственная папка с .git внутри него."""
    if os.path.isdir(os.path.join(path, ".git")):
        return path
    subs = [d for d in (os.listdir(path) if os.path.isdir(path) else [])
            if os.path.isdir(os.path.join(path, d, ".git"))]
    if len(subs) == 1:
        return os.path.join(path, subs[0])
    raise BbFail("clone", "после клонирования в папке выгрузки нет git-репозитория — MCP-сервер положил клон "
                          "в другое место")


def bb_write_cards(repo, d, cards):
    """Файлы карточек — только внутри клона: <клон>/<папка>/<файл>.md, без перехода по символическим ссылкам."""
    root = os.path.realpath(repo)
    cur = root
    for seg in d.split("/"):
        cur = os.path.join(cur, seg)
        if os.path.islink(cur):
            raise BbFail("запись файлов", f"«{d}» в репозитории — символическая ссылка; карточки пишем только внутри клона")
        if not os.path.isdir(cur):
            if os.path.lexists(cur):
                raise BbFail("запись файлов", f"«{d}» в репозитории — не папка")
            os.mkdir(cur)
    if not os.path.realpath(cur).startswith(root + os.sep):
        raise BbFail("запись файлов", f"папка «{d}» оказалась вне клона")
    files = []
    for c in cards:
        try:
            fd = os.open(os.path.join(cur, c["file"]), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        except OSError as e:
            raise BbFail("запись файлов", f"{d}/{c['file']}: {e.strerror or e}")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"# {c['title']}\n\n{c['body']}\n")   # как в JsonStore
        files.append(f"{d}/{c['file']}")
    return files


def bb_no_changes(res):
    v = mcp_value(res)
    if isinstance(v, (list, dict)):
        return not v
    s = str(v if v is not None else "").strip()
    return not s or bool(re.search(r"nothing to commit|working tree clean", s, re.I))


def bb_pr_info(res):
    """Ссылка и номер pull request из ответа bitbucket_create_pull_request ({id, title, state, url})."""
    v = mcp_value(res)
    if isinstance(v, str):
        m = re.search(r"\{.*\}", v, re.S)
        try:
            v = json.loads(m.group(0)) if m else v
        except ValueError:
            pass
    url, pid = "", None
    if isinstance(v, dict):
        url, pid = v.get("url") or "", v.get("id")
        if not url:
            try:
                url = v["links"]["self"][0]["href"]
            except (KeyError, IndexError, TypeError):
                url = ""
    elif isinstance(v, str):
        m = re.search(r"https?://\S+/pull-requests/\d+", v)
        url = m.group(0) if m else ""
    url = url if isinstance(url, str) else ""
    if pid in (None, "") and url:
        m = re.search(r"/pull-requests/(\d+)", url)
        pid = m.group(1) if m else None
    return url, pid


def bb_upload(task, cards, tg, by):
    """Выгрузка во временный клон через MCP-сервер BB; возвращает {pr_url, pr_id, branch, files}.
    Клон и MCP-сервер убираются всегда — и при ошибке."""
    kind, tid, title = task["kind"], task["id"], task["title"]
    label = f"Рег. {tid}" if kind == "regular" else str(tid)
    head = title if len(title) <= 120 else title[:119].rstrip() + "…"
    branch = f"kb/{kind}-{tid}-{datetime.now():%Y%m%d-%H%M%S}"
    tmp = tempfile.mkdtemp(prefix="ck-bb-")
    mcp = McpClient(str(bb_mcp_path()), time.monotonic() + config.BB_TIMEOUT)
    live = (mcp, tmp)
    BB_LIVE.append(live)
    try:
        mcp.start("запуск MCP-сервера")
        mcp.initialize("запуск MCP-сервера")
        repo = os.path.join(tmp, "repo")
        mcp.call("bitbucket_clone", {"url": tg["repo_url"], "path": repo}, "clone")
        repo = bb_clone_dir(repo)
        mcp.call("bitbucket_checkout", {"repo_path": repo, "branch": tg["branch"]}, "checkout")
        mcp.call("bitbucket_checkout", {"repo_path": repo, "branch": branch, "create": True}, "новая ветка")
        files = bb_write_cards(repo, tg["dir"], cards)
        mcp.call("bitbucket_add", {"repo_path": repo, "paths": files}, "add")
        if bb_no_changes(mcp.call("bitbucket_status", {"repo_path": repo}, "status")):
            raise BbNothing()
        try:
            mcp.call("bitbucket_commit", {"repo_path": repo, "message": f"Карточки знаний по задаче {label}: {head}"},
                     "commit")
        except BbFail as e:
            if re.search(r"nothing to commit|nothing added to commit|no changes added", e.text, re.I):
                raise BbNothing()
            raise
        mcp.call("bitbucket_push", {"repo_path": repo, "remote": "origin", "branch": branch}, "push")
        desc = (f"Карточки знаний из реестра задач ЦК по задаче {label} «{title}».\n"
                f"Выгрузил(а) из реестра: {by or 'сотрудник'}.\n\nФайлы ({len(files)}):\n"
                + "\n".join(f"- {f}" for f in files))
        res = mcp.call("bitbucket_create_pull_request",
                       {"project": tg["project"], "repo": tg["repo"], "source_branch": branch,
                        "target_branch": tg["branch"], "title": f"Карточки знаний: задача {label} — {head}",
                        "description": desc}, "pull request")
        url, pid = bb_pr_info(res)
        if not (isinstance(url, str) and len(url) <= 500 and BB_PR_URL_RE.fullmatch(url)
                and str(pid).isdigit() and 0 < int(pid) < 10 ** 12):
            raise BbFail("pull request", f"MCP-сервер не вернул ссылку и номер pull request (проверьте ветку {branch} "
                                         f"в Bitbucket): {mcp_text(res)[:300]}")
        return {"pr_url": url, "pr_id": int(pid), "branch": branch, "files": files}
    except BbFail as e:   # для журнала прокси: хвост stderr MCP-сервера (~2000 символов, с маской учётки)
        e.diag = mcp.stderr_tail(lines=60, n=2000) if mcp.proc else ""
        raise
    finally:
        mcp.close()
        shutil.rmtree(tmp, ignore_errors=True)
        if live in BB_LIVE:
            BB_LIVE.remove(live)


def bb_proxy_upload(server, headers, raw):
    """POST /api/bb/upload на прокси сотрудника → (код, ответ браузеру)."""
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return 400, {"error": "Некорректный JSON"}
    kind, tid = (body.get("kind"), body.get("id")) if isinstance(body, dict) else (None, None)
    if kind not in config.KINDS or not str(tid).isdigit():
        return 400, {"error": "Неизвестная задача: нужны kind (once или regular) и id"}
    tid = int(tid)
    problem = bb_local_problem()
    if problem:
        return 400, {"error": problem}
    if not BB_LOCK.acquire(blocking=False):
        return 409, {"error": "Уже идёт выгрузка в BB — дождитесь, пока она закончится"}
    try:
        code, data = bb_server_call(server, "GET", "/api/bb/task?" + urlencode({"kind": kind, "id": tid}), headers)
        if code != 200:
            return code, data   # права, статус, карточки, настройки владельца — ответ сервера реестра как есть
        try:
            task, cards, tg, by = bb_check_reply(data, kind, tid)
        except BadRequest as e:
            return 400, {"error": str(e)}
        try:
            res = bb_upload(task, cards, tg, by)
        except BbNothing:
            return 400, {"error": "В BB эти карточки уже лежат без изменений — выгружать нечего", "step": "status"}
        except BbFail as e:
            msg = bb_message(e.step, e.text)
            diag = getattr(e, "diag", "")
            print(f"BB: задача {kind} {tid} — {msg}" + (f"\n  stderr MCP-сервера: {diag}" if diag else ""), flush=True)
            return 400, {"error": msg, "step": e.step}
        code, done = bb_server_call(server, "POST", "/api/bb/done", headers, {"kind": kind, "id": tid, **res})
        if code != 200:
            return code, {**res, "error": f"Pull request создан: {res['pr_url']} — но записать его в задачу не удалось "
                                          f"({done.get('error') or f'HTTP {code}'}). Впишите ссылку в «Факт размещения "
                                          "карточки» вручную."}
        print(f"BB: задача {kind} {tid} — pull request №{res['pr_id']} {res['pr_url']} (ветка {res['branch']})",
              flush=True)
        return 200, {**res, "task": done.get("task")}
    except Exception as e:   # noqa — что бы ни случилось, учётку в ответ не отдаём
        return 500, {"error": "Выгрузка в BB не удалась: " + creds.bb_mask(f"{type(e).__name__}: {e}")}
    finally:
        BB_LOCK.release()


def bb_cleanup():
    """Прокси останавливают посреди выгрузки: остановить MCP-сервер и удалить временный клон."""
    for mcp, tmp in list(BB_LIVE):
        mcp.close()
        shutil.rmtree(tmp, ignore_errors=True)
