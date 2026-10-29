"""Полная выгрузка карточек задачи (T-010/T-013): сторона сотрудника, без MCP.

git напрямую (постоянный клон REG_BB_CLONE — при старте прокси проверяется и обновляется,
при выгрузке переиспользуется; сбой push/PR — переклонирование и один повтор) и pull request
через REST API Bitbucket. Серверу реестра прокси не доверяет (проверяет ответ /api/bb/task),
сервер — прокси (/api/bb/done). Учётка BB — только в памяти прокси (askpass-канал, REST-заголовок).
"""
import json
import shutil
import threading
from datetime import datetime
from urllib.parse import urlencode

from registry import config, creds
from registry.bb import bb_file_ok, bb_target_problem
from registry.bb.direct import (BbFail, BbNothing, bb_message, ensure_clone, git, no_changes,
                                refresh_clone, rest_create_pr, url_ok_for_creds)
from registry.errors import BadRequest
from registry.util import NoRedirect, dumps

BB_LOCK = threading.Lock()   # одна выгрузка/отправка за раз (клоны общие)


def bb_local_problem():
    """Что не так у сотрудника для работы с Bitbucket (пусто — готово). Без MCP: нужны git, учётка и адрес."""
    import shutil as _sh
    from registry.bb import bb_url_parts
    if not _sh.which("git"):
        return "в вашем контейнере не найден git — он нужен для работы с Bitbucket"
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
    import urllib.error
    import urllib.request
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
    """GET /api/bb/status на прокси: готово, если у владельца настроен репозиторий, а у сотрудника — git и учётка."""
    code, srv = bb_server_call(server, "GET", "/api/bb/status", headers, timeout=20)
    if code not in (200, 404):
        return code, srv
    problems = [p for p in (bb_local_problem(),) if p]
    if code == 404:
        problems.append("Сервер реестра не умеет выгружать в BB — владельцу нужно обновить реестр")
    elif not (isinstance(srv, dict) and srv.get("server_configured") is True):
        miss = [m for m in (srv.get("missing") or []) if isinstance(m, str)]
        problems.append("Владелец реестра не настроил репозиторий БЗ"
                        + (": нужно задать в его registry.env " + ", ".join(miss) if miss else ""))
    out = {"available": not problems, "reason": "; ".join(problems), "via": "proxy",
           "server_configured": code == 200 and isinstance(srv, dict) and srv.get("server_configured") is True}
    if isinstance(srv, dict) and isinstance(srv.get("target"), dict):
        out["target"] = {k: str(srv["target"].get(k) or "")[:300]
                         for k in ("repo_url", "project", "repo", "branch", "dir")}
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
    if isinstance(url, str) and "@" in bb_url_parts_host(url):
        raise bad("в адресе репозитория есть логин (…@) — учётка BB уходит только вашему git")
    if not url_ok_for_creds(url):
        raise bad("адрес репозитория — чужой хост или недопустимые символы (?запрос, #фрагмент, пробелы)")
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


def bb_url_parts_host(url):
    from urllib.parse import urlparse
    return urlparse(url).netloc if isinstance(url, str) else ""


def bb_write_cards(repo, d, cards):
    """Файлы карточек — только внутри клона: <клон>/<папка>/<файл>.md, без перехода по символическим ссылкам."""
    import os
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


def server_token(server):
    """Ключ API со страницы сервера реестра — для стартовых запросов (без браузера его взять больше неоткуда)."""
    import re as _re
    import urllib.request as _u
    try:
        with _u.build_opener(_u.ProxyHandler({})).open(server + "/", timeout=20) as r:
            m = _re.search(r"TOKEN\s*=\s*['\"]([^'\"]+)['\"]", r.read().decode("utf-8", "replace"))
        return {"X-Token": m.group(1)} if m else {}
    except Exception:   # noqa — сервер недоступен: стартовые клоны пропустим с предупреждением
        return {}


def bb_startup(server):
    """При запуске прокси: клон репозитория выгрузки — проверить/склонировать/обновить (best effort)."""
    try:
        code, srv = bb_server_call(server, "GET", "/api/bb/status", server_token(server), timeout=20)
        tg = srv.get("target") if isinstance(srv, dict) else None
        if code != 200 or not isinstance(tg, dict) or not tg.get("repo_url"):
            print("Выгрузка в BB: клон при запуске пропущен — репозиторий не настроен у владельца", flush=True)
            return
        ensure_clone(tg["repo_url"], config.BB_CLONE)
        refresh_clone(config.BB_CLONE, tg["branch"])
        print(f"Выгрузка в BB: клон в {config.BB_CLONE} (ветка {tg['branch']})", flush=True)
    except Exception as e:   # noqa — старт не должен падать из-за BB
        print(f"Выгрузка в BB: клон при запуске не удался — {bb_message('clone', creds.bb_mask(str(e)))}\n"
              "  Выгрузка доклонирует при необходимости", flush=True)


def bb_upload(task, cards, tg, by, attempt=1):
    """Выгрузка всех карточек задачи: свежая база (fetch) → ветка kb/… → файлы в папку → commit → push →
    PR через REST. Возвращает {pr_url, pr_id, branch, files}. attempt>1 — повтор после переклонирования:
    ветка с суффиксом (та же секунда — иначе push уже запушенной ветки не пройдёт)."""
    kind, tid, title = task["kind"], task["id"], task["title"]
    label = f"Рег. {tid}" if kind == "regular" else str(tid)
    head = title if len(title) <= 120 else title[:119].rstrip() + "…"
    branch = f"kb/{kind}-{tid}-{datetime.now():%Y%m%d-%H%M%S}" + (f"-r{attempt}" if attempt > 1 else "")
    clone = ensure_clone(tg["repo_url"], config.BB_CLONE)
    rp = str(clone)
    refresh_clone(clone, tg["branch"])
    git(["checkout", "--quiet", "-b", branch], cwd=rp, step="новая ветка")
    files = bb_write_cards(rp, tg["dir"], cards)
    git(["add", "--", *files], cwd=rp, step="add")
    if no_changes(clone):
        raise BbNothing()
    git(["commit", "--quiet", "-m", f"Карточки знаний по задаче {label}: {head}"], cwd=rp, step="commit")
    git(["push", "--quiet", "origin", branch], cwd=rp, step="push")
    desc = (f"Карточки знаний из реестра задач ЦК по задаче {label} «{title}».\n"
            f"Выгрузил(а) из реестра: {by or 'сотрудник'}.\n\nФайлы ({len(files)}):\n"
            + "\n".join(f"- {f}" for f in files))
    pr_url, pr_id = rest_create_pr(tg["project"], tg["repo"],
                                   title=f"Карточки знаний: задача {label} — {head}",
                                   description=desc, src=branch, dst=tg["branch"])
    return {"pr_url": pr_url, "pr_id": pr_id, "branch": branch, "files": files}


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
        except BbFail:
            shutil.rmtree(config.BB_CLONE, ignore_errors=True)   # клон мог протухнуть — переклонировать
            try:
                res = bb_upload(task, cards, tg, by, attempt=2)
            except BbNothing:
                return 400, {"error": "В BB эти карточки уже лежат без изменений — выгружать нечего", "step": "status"}
        code, done = bb_server_call(server, "POST", "/api/bb/done", headers, {"kind": kind, "id": tid, **res})
        if code != 200:
            return code, {**res, "error": f"Pull request создан: {res['pr_url']} — но записать его в задачу не удалось "
                                          f"({(done or {}).get('error') or f'HTTP {code}'}). Впишите ссылку в «Факт размещения "
                                          "карточки» вручную."}
        print(f"BB: задача {kind} {tid} — pull request №{res['pr_id']} {res['pr_url']} (ветка {res['branch']})",
              flush=True)
        return 200, {**res, "task": done.get("task")}
    except BbFail as e:
        msg = bb_message(e.step, e.text)
        print(f"BB: {msg}", flush=True)
        return 400, {"error": msg, "step": e.step}
    except Exception as e:   # noqa — что бы ни случилось, учётку в ответ не отдаём
        return 500, {"error": "Выгрузка в BB не удалась: " + creds.bb_mask(f"{type(e).__name__}: {e}")}
    finally:
        BB_LOCK.release()
