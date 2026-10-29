"""Базы знаний (T-012/T-013): сторона сотрудника — онтология и отправка карточки через прокси.

Без MCP: git напрямую (клон — постоянный, при старте прокси проверяется и обновляется,
при отправке переиспользуется; сбой push/PR — переклонирование и один повтор) и pull request
через REST API Bitbucket. База одна: адрес и ветка — у владельца (REG_KB_REPO_URL/REG_KB_BRANCH),
прокси берёт их из /api/kb/status. Онтология — файл ontology.yml из клона (парсер встроенный).
Серверу реестра прокси не доверяет (проверяет ответ /api/kb/task), сервер — прокси (/api/kb/done).
"""
import json
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from registry import config, creds
from registry.bb import kb_node_ok
from registry.bb.direct import (BbFail, BbNothing, bb_message, ensure_clone, git, kb_file_url,
                                no_changes, refresh_clone, repo_url_parts, rest_create_pr, url_ok_for_creds)
from registry.bb.ontology import ontology_from_clone
from registry.bb.upload import BB_LOCK, bb_local_problem, bb_server_call, bb_write_cards
from registry.errors import BadRequest

KB_MAX_NODES = 500   # предел узлов онтологии — прокси не доверяет никому


def _ontology_checked(nodes):
    """Дерево онтологии — прокси не доверяет: форма, имена, пути-папки, пределы."""

    def bad(why):
        return BadRequest(f"Онтология некорректна: {why}")

    if not isinstance(nodes, list) or not nodes:
        raise bad("нет узлов")
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
                raise bad("у узла нет имени")
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
    """Цель от сервера реестра, проверенная: адрес — локальный путь или свой Bitbucket, без логина."""
    code, srv = bb_server_call(server, "GET", "/api/kb/status", headers, timeout=20)
    if code != 200 or not isinstance(srv, dict) or not srv.get("configured"):
        return (code if code in (400, 401, 403, 404, 409, 429) else 502), \
               {"error": (srv or {}).get("error") or "Владелец реестра не настроил базу знаний (REG_KB_REPO_URL)"}
    t = srv.get("target") or {}
    url, branch = t.get("repo_url"), t.get("branch")
    if not isinstance(url, str) or not isinstance(branch, str) or not branch \
            or not url_ok_for_creds(url) or not repo_url_parts(url):
        return 400, {"error": "Сервер реестра прислал недопустимый адрес базы знаний "
                              "(чужой хост или не разобрать проект и репозиторий)"}
    return {"url": url, "branch": branch}


def kb_startup(server):
    """При запуске прокси: клон БЗ — проверить/склонировать/обновить. Недоступность — предупреждение,
    прокси продолжает работать (отправка доклонит при необходимости)."""
    from registry.bb.upload import server_token
    try:
        got = _kb_target(server, server_token(server))
        if isinstance(got, tuple):
            print(f"База знаний: клон при запуске пропущен — {got[1].get('error')}", flush=True)
            return
        ensure_clone(got["url"], config.KB_CLONE)
        refresh_clone(config.KB_CLONE, got["branch"])
        print(f"База знаний: клон в {config.KB_CLONE} (ветка {got['branch']})", flush=True)
    except Exception as e:   # noqa — старт не должен падать из-за BB
        print(f"База знаний: клон при запуске не удался — {bb_message('clone', creds.bb_mask(str(e)))}\n"
              "  Отправка карточек доклонирует при необходимости", flush=True)


def kb_proxy_status(server, headers):
    """GET /api/kb/status на прокси: готово, если у владельца настроена БЗ, а у сотрудника — git и учётка."""
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
    """GET /api/kb/ontology на прокси: дерево узлов из ontology.yml клона (парсер встроенный)."""
    problem = bb_local_problem()
    if problem:
        return 400, {"error": problem}
    got = _kb_target(server, headers)
    if isinstance(got, tuple):
        return got
    try:
        clone = ensure_clone(got["url"], config.KB_CLONE)
        refresh_clone(clone, got["branch"])
        return 200, _ontology_checked(ontology_from_clone(clone, got["branch"]))
    except BadRequest as e:
        return 400, {"error": str(e)}
    except BbFail as e:
        return 400, {"error": bb_message(e.step, e.text), "step": e.step}
    except Exception as e:   # noqa
        return 500, {"error": "Онтология не получена: " + creds.bb_mask(f"{type(e).__name__}: {e}")}


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
    if not isinstance(tg.get("repo_url"), str) or not tg["repo_url"] or not url_ok_for_creds(tg["repo_url"]):
        raise bad("недопустимый адрес базы знаний")
    return {"title": card["title"], "body": card["body"], "branch": tg["branch"], "url": tg["repo_url"]}


def _kb_push_card(card, branch, node, full, file, pr_branch):
    """Одна попытка отправки: свежая база (fetch) → ветка → файл в папку узла → commit → push → PR (REST)."""
    clone = ensure_clone(card["url"], config.KB_CLONE)
    rp = str(clone)
    refresh_clone(clone, branch)
    git(["checkout", "--quiet", "-b", pr_branch], cwd=rp, step="новая ветка")
    bb_write_cards(rp, node, [{"file": file, "title": card["title"], "body": card["body"]}])
    git(["add", "--", full], cwd=rp, step="add")
    if no_changes(clone):
        raise BbNothing()   # уже лежит — переклонирование не поможет, это точный ответ
    git(["commit", "--quiet", "-m", f"Карточка знаний «{card['title']}» (узел «{node}»)"],
        cwd=rp, step="commit")
    git(["push", "--quiet", "origin", pr_branch], cwd=rp, step="push")
    parts = repo_url_parts(card["url"])
    pr_url, pr_id = rest_create_pr(parts[0], parts[1],
                                   title=f"Карточка знаний: {card['title']}",
                                   description=f"Карточка из реестра задач ЦК, узел «{node}».",
                                   src=pr_branch, dst=branch)
    return {"pr_url": pr_url, "pr_id": pr_id}


def kb_proxy_upload(server, headers, raw):
    """POST /api/kb/upload на прокси: карточка — в папку узла онтологии БЗ, PR через REST."""
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
    try:
        q = urlencode({"kind": kind, "id": tid, "path": path, "node": node})
        code, data = bb_server_call(server, "GET", "/api/kb/task?" + q, headers)
        if code != 200:
            return code, data   # права, статус, карточка, цель — ответ сервера реестра как есть
        try:
            card = kb_check_reply(data, kind, tid, path, node)
        except BadRequest as e:
            return 400, {"error": str(e)}
        file = Path(path).name   # имя файла — как в реестре (id_название.md)
        from registry.bb import bb_file_ok
        if not bb_file_ok(file):
            return 400, {"error": "Имя файла карточки недопустимо для репозитория"}
        full = f"{node}/{file}"
        sent = None
        for attempt in (1, 2):
            pr_branch = (f"kb-card/{kind}-{tid}-{datetime.now():%Y%m%d-%H%M%S}"
                         + (f"-r{attempt}" if attempt > 1 else ""))
            try:
                sent = _kb_push_card(card, card["branch"], node, full, file, pr_branch)
                break
            except BbNothing:
                return 400, {"error": "В базе знаний эта карточка уже лежит без изменений — отправлять нечего",
                             "step": "status"}
            except BbFail as e:
                if attempt == 2:
                    raise
                # клон мог протухнуть или сломаться — переклонировать и повторить раз
                shutil.rmtree(config.KB_CLONE, ignore_errors=True)
        parts = repo_url_parts(card["url"])
        file_url = kb_file_url(parts[0], parts[1], card["branch"], full)
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
        msg = bb_message(e.step, e.text)
        print(f"KB: {msg}", flush=True)
        return 400, {"error": msg, "step": e.step}
    except Exception as e:   # noqa — что бы ни случилось, учётку в ответ не отдаём
        return 500, {"error": "Отправка в базу знаний не удалась: " + creds.bb_mask(f"{type(e).__name__}: {e}")}
    finally:
        BB_LOCK.release()
