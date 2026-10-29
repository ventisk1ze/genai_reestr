"""Базы знаний (T-012): сторона сотрудника — онтология и отправка карточки через прокси.

База одна: адрес и ветка — у владельца (REG_KB_REPO_URL/REG_KB_BRANCH), прокси берёт их из
/api/kb/status. Постоянный клон — в REG_KB_CLONE (по умолчанию ~/kb): папка с .git есть —
переиспользуем (и между отправками, и между перезапусками прокси), нет или сломан — клонируем;
сбой push/PR — стереть клон, переклонировать и повторить один раз. Отправка — через pull request,
как в T-010; учётка BB — только в памяти прокси. Серверу реестра прокси не доверяет (проверяет
ответ /api/kb/task), сервер — прокси (проверяет /api/kb/done).
"""
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from registry import config, creds
from registry.bb import BB_PR_URL_RE, bb_file_ok, bb_url_parts, kb_node_ok
from registry.bb.mcp import BbFail, BbNothing, McpClient, bb_message, mcp_value
from registry.bb.upload import (BB_LOCK, bb_local_problem, bb_no_changes, bb_pr_info, bb_server_call,
                                bb_write_cards)
from registry.errors import BadRequest

KB_MAX_NODES = 500   # предел узлов онтологии в ответе MCP — прокси не доверяет никому


def kb_url_parts(url):
    """(project, repo) из адреса клонирования /scm/<project>/<repo>(.git) — для pull request."""
    m = re.search(r"/scm/(~?[\w.\-]+)/([\w.\-]+?)(?:\.git)?/?$", url) if isinstance(url, str) else None
    return (m.group(1), m.group(2)) if m else None


def kb_file_url(url, branch, path):
    """Ссылка на файл в Bitbucket (browse): после влития PR тот же путь в целевой ветке."""
    parts = kb_url_parts(url)
    if not parts:
        raise BadRequest("Не удалось определить проект и репозиторий из адреса базы знаний")
    project, repo = parts
    base = url.split("/scm/")[0]
    seg = "/".join(quote(s, safe="") for s in path.split("/"))
    return (f"{base}/projects/{quote(project, safe='~')}/repos/{quote(repo, safe='')}"
            f"/browse/{seg}?at={quote(branch, safe='')}")


def _mcp():
    """MCP-клиент с общим пределом одной операции (как выгрузка T-010)."""
    from registry.bb.upload import bb_mcp_path
    return McpClient(str(bb_mcp_path()), time.monotonic() + config.BB_TIMEOUT)


def _ontology_checked(res):
    """Дерево онтологии из ответа MCP — прокси не доверяет: форма, имена, пути-папки, пределы."""

    def bad(why):
        return BadRequest(f"MCP-сервер вернул некорректную онтологию: {why}")

    v = res if isinstance(res, dict) else None
    nodes = v.get("nodes") if isinstance(v, dict) else None
    if not isinstance(nodes, list) or not nodes:
        raise bad("нет узлов (ожидается ontology.yml с деревом узлов)")
    seen, total = set(), [0]

    def walk(items, depth):
        out = []
        for it in items:
            total[0] += 1
            if total[0] > KB_MAX_NODES:
                raise bad(f"больше {KB_MAX_NODES} узлов")
            if not isinstance(it, dict):
                raise bad("узел — не объект")
            name, path = it.get("name"), it.get("path")
            if not isinstance(name, str) or not name.strip() or len(name) > 200:
                raise bad("у узла нет имени (name)")
            if not kb_node_ok(path):
                raise bad(f"путь узла «{str(path)[:80]}» — не папки (буквы, цифры, пробелы, дефисы)")
            if path in seen:
                raise bad(f"путь «{path}» повторяется")
            seen.add(path)
            kids = it.get("children") or []
            if not isinstance(kids, list):
                raise bad(f"у узла «{name}» children — не список")
            if kids and depth + 1 >= config.KB_NODE_DEPTH_MAX:
                raise bad(f"глубже {config.KB_NODE_DEPTH_MAX} уровней")
            out.append({"name": name.strip(), "path": path,
                        "children": walk(kids, depth + 1) if kids else []})
        return out

    return {"nodes": walk(nodes, 0)}


def _kb_target(server, headers):
    """Цель от сервера реестра, проверенная: адрес — только свой корпоративный Bitbucket, без логина."""
    code, srv = bb_server_call(server, "GET", "/api/kb/status", headers, timeout=20)
    if code != 200 or not isinstance(srv, dict) or not srv.get("configured"):
        return (code if code in (400, 401, 403, 404, 409, 429) else 502), \
               {"error": (srv or {}).get("error") or "Владелец реестра не настроил базу знаний (REG_KB_REPO_URL)"}
    t = srv.get("target") or {}
    url, branch = t.get("repo_url"), t.get("branch")
    if not isinstance(url, str) or not isinstance(branch, str) or not branch \
            or "@" in urlparse(url).netloc or bb_url_parts(url) != bb_url_parts(config.BB_BASE_URL):
        return 400, {"error": "Сервер реестра прислал недопустимый адрес базы знаний "
                              "(чужой хост или логин в адресе)"}
    return {"url": url, "branch": branch}


def kb_proxy_status(server, headers):
    """GET /api/kb/status на прокси: готово, если у владельца настроена БЗ, а у сотрудника — MCP и учётка."""
    code, srv = bb_server_call(server, "GET", "/api/kb/status", headers, timeout=20)
    if code not in (200, 404):
        return code, srv
    problems = [p for p in (bb_local_problem(),) if p]
    if code == 404:
        problems.append("Сервер реестра ещё не умеет базы знаний — владельцу нужно обновить реестр")
    elif isinstance(srv, dict) and not srv.get("configured"):
        problems.append("Владелец реестра не настроил базу знаний (REG_KB_REPO_URL)")
    out = {"available": not problems, "reason": "; ".join(problems), "via": "proxy",
           "configured": code == 200 and bool(isinstance(srv, dict) and srv.get("configured"))}
    if isinstance(srv, dict) and isinstance(srv.get("target"), dict):
        out["target"] = {k: str(srv["target"].get(k) or "")[:300] for k in ("repo_url", "branch")}
    return 200, out


def kb_proxy_ontology(server, headers):
    """GET /api/kb/ontology на прокси: дерево узлов из ontology.yml БЗ (парсит MCP-сервер)."""
    problem = bb_local_problem()
    if problem:
        return 400, {"error": problem}
    got = _kb_target(server, headers)
    if isinstance(got, tuple):
        return got
    mcp = _mcp()
    try:
        mcp.start("запуск MCP-сервера")
        mcp.initialize("запуск MCP-сервера")
        res = mcp.call("kb_ontology", {"url": got["url"], "branch": got["branch"]}, "онтология")
        return 200, _ontology_checked(mcp_value(res))
    except BadRequest as e:
        return 400, {"error": str(e)}
    except BbFail as e:
        return 400, {"error": bb_message(e.step, e.text), "step": e.step}
    except Exception as e:   # noqa
        return 500, {"error": "Онтология не получена: " + creds.bb_mask(f"{type(e).__name__}: {e}")}
    finally:
        mcp.close()


def kb_check_reply(d, kind, tid, path, node):
    """Ответ сервера реестра (/api/kb/task) — серверу не доверяем: задача, карточка, цель, узел."""

    def bad(why):
        return BadRequest("Сервер реестра прислал недопустимые данные: " + why)

    task, card, tg = d.get("task"), d.get("card"), d.get("target")
    if not isinstance(task, dict) or task.get("kind") != kind or str(task.get("id")) != str(tid):
        raise bad("в ответе другая задача")
    if not isinstance(card, dict) or card.get("path") != path:
        raise bad("в ответе другая карточка")
    if not isinstance(tg, dict) or tg.get("node") != node:
        raise bad("в ответе другой узел онтологии")
    if not isinstance(card.get("title"), str) or not card["title"].strip() \
            or not isinstance(card.get("body"), str):
        raise bad("у карточки нет вопроса или текста")
    if not isinstance(tg.get("branch"), str) or not tg["branch"]:
        raise bad("нет ветки базы знаний")
    if not isinstance(tg.get("repo_url"), str) or not tg["repo_url"]:
        raise bad("нет адреса базы знаний")
    return {"title": card["title"], "body": card["body"], "branch": tg["branch"], "url": tg["repo_url"]}


def _kb_ensure_clone(mcp, url):
    """Постоянный клон (REG_KB_CLONE): папка с .git — переиспользуем; нет или сломан — клонируем заново.
    Клон переживает перезапуск прокси: это кэш, bb_cleanup его не трогает."""
    p = Path(config.KB_CLONE)
    if (p / ".git").is_dir():
        return p
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)   # остаток без .git — переклонировать
    p.parent.mkdir(parents=True, exist_ok=True)
    mcp.call("bitbucket_clone", {"url": url, "path": str(p)}, "clone")
    return p


def _kb_push_card(mcp, url, branch, node, full, file, card, pr_branch):
    """Одна попытка отправки: клон → ветки → файл в папку узла → commit → push → pull request."""
    repo_dir = _kb_ensure_clone(mcp, url)
    rp = str(repo_dir)
    mcp.call("bitbucket_checkout", {"repo_path": rp, "branch": branch}, "checkout")
    mcp.call("bitbucket_checkout", {"repo_path": rp, "branch": pr_branch, "create": True}, "новая ветка")
    bb_write_cards(rp, node, [{"file": file, "title": card["title"], "body": card["body"]}])
    mcp.call("bitbucket_add", {"repo_path": rp, "paths": [full]}, "add")
    if bb_no_changes(mcp.call("bitbucket_status", {"repo_path": rp}, "status")):
        raise BbNothing()   # уже лежит — переклонирование не поможет, это точный ответ
    mcp.call("bitbucket_commit", {"repo_path": rp,
                                  "message": f"Карточка знаний «{card['title']}» (узел «{node}»)"},
             "commit")
    mcp.call("bitbucket_push", {"repo_path": rp, "remote": "origin", "branch": pr_branch}, "push")
    parts = kb_url_parts(url)
    if not parts:
        raise BadRequest("Не удалось определить проект и репозиторий из адреса базы знаний")
    res = mcp.call("bitbucket_create_pull_request",
                   {"project": parts[0], "repo": parts[1], "source_branch": pr_branch,
                    "target_branch": branch, "title": f"Карточка знаний: {card['title']}",
                    "description": f"Карточка из реестра задач ЦК, узел «{node}»."},
                   "pull request")
    pr_url, pid = bb_pr_info(res)
    if not (isinstance(pr_url, str) and len(pr_url) <= 500 and BB_PR_URL_RE.fullmatch(pr_url)
            and str(pid).isdigit() and 0 < int(pid) < 10 ** 12):
        raise BbFail("pull request", f"MCP-сервер не вернул ссылку и номер pull request "
                                     f"(проверьте ветку {pr_branch} в Bitbucket)")
    return {"pr_url": pr_url, "pr_id": int(pid)}


def kb_proxy_upload(server, headers, raw):
    """POST /api/kb/upload на прокси: карточка — в папку узла онтологии БЗ, через pull request."""
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return 400, {"error": "Некорректный JSON"}
    kind, tid = (body.get("kind"), body.get("id")) if isinstance(body, dict) else (None, None)
    if kind not in config.KINDS or not str(tid).isdigit():
        return 400, {"error": "Неизвестная задача: нужны kind (once или regular) и id"}
    tid = int(tid)
    path, node = body.get("path"), body.get("node")
    if not isinstance(path, str) or not path:
        return 400, {"error": "path — путь карточки в реестре"}
    if not isinstance(node, str) or not kb_node_ok(node):
        return 400, {"error": "Узел онтологии — путь папок, выберите его из списка"}
    problem = bb_local_problem()
    if problem:
        return 400, {"error": problem}
    if not BB_LOCK.acquire(blocking=False):
        return 409, {"error": "Уже идёт отправка в базу знаний — дождитесь, пока она закончится"}
    mcp = _mcp()
    try:
        from urllib.parse import urlencode
        q = urlencode({"kind": kind, "id": tid, "path": path, "node": node})
        code, data = bb_server_call(server, "GET", "/api/kb/task?" + q, headers)
        if code != 200:
            return code, data   # права, статус, карточка, цель — ответ сервера реестра как есть
        try:
            card = kb_check_reply(data, kind, tid, path, node)
        except BadRequest as e:
            return 400, {"error": str(e)}
        file = os.path.basename(path)   # имя файла — как в реестре (id_название.md)
        if not bb_file_ok(file):
            return 400, {"error": "Имя файла карточки недопустимо для репозитория"}
        full = f"{node}/{file}"
        mcp.start("запуск MCP-сервера")
        mcp.initialize("запуск MCP-сервера")
        sent = None
        for attempt in (1, 2):
            pr_branch = f"kb-card/{kind}-{tid}-{datetime.now():%Y%m%d-%H%M%S}"
            try:
                sent = _kb_push_card(mcp, card["url"], card["branch"], node, full, file, card, pr_branch)
                break
            except BbNothing:
                return 400, {"error": "В базе знаний эта карточка уже лежит без изменений — отправлять нечего",
                             "step": "status"}
            except BbFail as e:
                if attempt == 2:
                    raise
                # клон мог протухнуть (ветка ушла вперёд, репозиторий сломан) — переклонировать и повторить раз
                shutil.rmtree(config.KB_CLONE, ignore_errors=True)
        file_url = kb_file_url(card["url"], card["branch"], full)
        done = {"kind": kind, "id": tid, "path": path, "node": node,
                "file_url": file_url, "pr_url": sent["pr_url"], "pr_id": sent["pr_id"]}
        code, saved = bb_server_call(server, "POST", "/api/kb/done", headers, done)
        if code != 200:
            return code, {**done, "error": f"Pull request создан: {sent['pr_url']} — но записать ссылку в карточку "
                                           f"не удалось ({(saved or {}).get('error') or f'HTTP {code}'}). "
                                           f"Ссылка: {file_url}"}
        print(f"KB: задача {kind} {tid} — карточка в «{node}», pull request №{sent['pr_id']} {sent['pr_url']}",
              flush=True)
        return 200, {**done, "task": saved.get("task")}
    except BbFail as e:
        e.diag = mcp.stderr_tail(lines=60, n=2000) if mcp.proc else ""   # для журнала прокси, с маской учётки
        msg = bb_message(e.step, e.text)
        if e.diag:
            print(f"KB: {msg}\n  stderr MCP-сервера: {e.diag}", flush=True)
        return 400, {"error": msg, "step": e.step}
    except Exception as e:   # noqa — что бы ни случилось, учётку в ответ не отдаём
        return 500, {"error": "Отправка в базу знаний не удалась: " + creds.bb_mask(f"{type(e).__name__}: {e}")}
    finally:
        mcp.close()
        BB_LOCK.release()
