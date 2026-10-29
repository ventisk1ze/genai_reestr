"""Базы знаний (T-012): серверная сторона — одна БЗ в настройках владельца.

База знаний — git-репозиторий с онтологией (ontology.yml), глоссарием и FAQ. Адрес клонирования и
ветка — в настройках владельца (REG_KB_REPO_URL, REG_KB_BRANCH). Сервер в BB не ходит — как в T-010:
проверяет права и узел, отдаёт прокси карточку и цель (/api/kb/task), записывает результат
(/api/kb/done). Онтологию, постоянный клон и отправку (через PR) делает прокси сотрудника.
"""
from pathlib import Path
from urllib.parse import urlparse

from registry import config, runtime
from registry.bb import BB_PR_URL_RE, BB_STATUS_OK, bb_branch_ok, bb_url_parts, kb_node_ok
from registry.cards import card_entries
from registry.errors import BadRequest, Conflict, Forbidden
from registry.org import norm_org
from registry.tasks import relation
from registry.util import clone, now


def kb_via_proxy():
    return ("Отправка в базу знаний — через свой прокси: она идёт из вашего контейнера и с вашей учёткой BB. "
            f"Откройте реестр командой bash connect.sh <табельный владельца реестра> (сам владелец — "
            f"bash connect.sh 127.0.0.1:{runtime.SERVE_PORT})")


def kb_target_problem():
    """Что не так с настройками БЗ у владельца (пусто — всё в порядке). Хост проверяет сторона сотрудника."""
    if not config.KB_REPO_URL:
        return "не задан REG_KB_REPO_URL (адрес клонирования базы знаний)"
    if "@" in urlparse(config.KB_REPO_URL).netloc:
        return "в REG_KB_REPO_URL есть логин (…@) — уберите его, учётка BB у каждого своя"
    if not (bb_url_parts(config.KB_REPO_URL) or Path(config.KB_REPO_URL).is_dir()):
        return "REG_KB_REPO_URL — нужен адрес клонирования http(s), без ?запроса и #фрагмента"
    if not bb_branch_ok(config.KB_BRANCH):
        return "REG_KB_BRANCH — недопустимое имя ветки"
    return ""


def kb_target():
    """Цель — единственная БЗ из настроек владельца: (repo_url, branch)."""
    problem = kb_target_problem()
    if problem:
        raise BadRequest(f"База знаний не настроена у владельца реестра: {problem}")
    return config.KB_REPO_URL, config.KB_BRANCH


def kb_node_checked(node):
    """Узел онтологии — путь папок (каждый узел — папка); проверка и понятная ошибка."""
    if not kb_node_ok(node):
        raise BadRequest(f"Узел онтологии — путь папок (буквы, цифры, пробелы, дефисы), "
                         f"до {config.KB_NODE_DEPTH_MAX} уровней")
    return node


def op_kb_status():
    """GET /api/kb/status прямо на сервере: база знаний — только через свой прокси (connect.sh)."""
    reason = kb_via_proxy()
    problem = kb_target_problem()
    if problem:
        reason += ". Владельцу реестра: " + problem
    out = {"available": False, "via": "proxy", "configured": not problem, "reason": reason}
    if not problem:
        out["target"] = {"repo_url": config.KB_REPO_URL, "branch": config.KB_BRANCH}
    return out


def kb_task_for(user, kind, tid, teams=None):
    """Задача для отправки карточки и права: исполнитель или замещающий (или админ), задача «Согласована»."""
    if kind not in config.KINDS or not str(tid).isdigit():
        raise BadRequest("Неизвестная задача: нужны kind (once или regular) и id")
    t = runtime.STORE.get_task(kind, int(tid))
    if t is None:
        raise BadRequest(f"Задачи {tid} нет в хранилище — обновите страницу.")
    if not user["admin"] and "ex" not in relation(user, dict(t, kind=kind), teams):
        raise Forbidden("Отправлять карточки в базу знаний может исполнитель или замещающий задачи")
    if t.get("status") != BB_STATUS_OK:
        raise Forbidden(f"Карточки отправляют в базу знаний, когда задача «{BB_STATUS_OK}», а она «{t.get('status')}»")
    return t


def op_kb_task(q, user):
    """GET /api/kb/task?kind&id&path&node — прокси берёт карточку, цель и узел (только с сессией)."""
    kind, tid = (q.get("kind") or [""])[0], (q.get("id") or [""])[0]
    path = (q.get("path") or [""])[0]
    node = kb_node_checked((q.get("node") or [""])[0])
    repo_url, branch = kb_target()
    t = kb_task_for(user, kind, tid)
    entry = next((x for x in card_entries(t) if x.get("path") == path), None)
    if entry is None:
        raise BadRequest("Такой карточки нет в задаче — сохраните её и повторите")
    try:
        title, body = runtime.STORE.read_card(path)
    except BadRequest:
        raise BadRequest(f"Карточка «{path.rsplit('/', 1)[-1]}» не найдена в хранилище — откройте её в задаче "
                         "и сохраните заново")
    return {"task": {"kind": kind, "id": int(tid), "title": t.get("title", "")},
            "card": {"path": path, "title": title, "body": body},
            "target": {"repo_url": repo_url, "branch": branch, "node": node},
            "by": user["name"]}


def op_kb_done(body, user):
    """POST /api/kb/done: прокси сообщает результат отправки — сервер пишет ссылку в карточку атомарно."""
    if not isinstance(body, dict):
        raise BadRequest("Некорректный JSON")
    kind, tid = body.get("kind"), body.get("id")
    path = body.get("path")
    if not isinstance(path, str) or not path:
        raise BadRequest("path — путь карточки в реестре")
    node = kb_node_checked(body.get("node"))
    pr_url = body.get("pr_url")
    if not isinstance(pr_url, str) or len(pr_url) > 500 or not BB_PR_URL_RE.fullmatch(pr_url):
        raise BadRequest("pr_url — ссылка на pull request: http(s), до 500 символов")
    file_url = body.get("file_url")
    if not isinstance(file_url, str) or len(file_url) > 1000 or not BB_PR_URL_RE.fullmatch(file_url):
        raise BadRequest("file_url — ссылка на файл в Bitbucket: http(s), до 1000 символов")
    pr_id = body.get("pr_id")
    if isinstance(pr_id, str) and pr_id.isdigit() and len(pr_id) <= 12:
        pr_id = int(pr_id)
    if isinstance(pr_id, bool) or not isinstance(pr_id, int) or not 0 < pr_id < 10 ** 12:
        raise BadRequest("pr_id — номер pull request, целое число")
    teams = norm_org(runtime.STORE.get_config())[1]
    rec = {"ts": now(), "by": user["name"], "node": node,
           "file_url": file_url, "pr_url": pr_url, "pr_id": pr_id}

    def finalize(t, cur):   # под блокировкой хранилища, после проверки версии — права и статус ещё раз
        cur = dict(cur, kind=kind)
        if not user["admin"] and "ex" not in relation(user, cur, teams):
            raise Forbidden("Отправлять карточки в базу знаний может исполнитель или замещающий задачи")
        if cur.get("status") != BB_STATUS_OK:
            raise Forbidden(f"Карточки отправляют в базу знаний, когда задача «{BB_STATUS_OK}», "
                            f"а она «{cur.get('status')}»")
        new = clone(cur)
        entries = card_entries(new)
        i = next((k for k, x in enumerate(entries) if x.get("path") == path), None)
        if i is None:
            raise BadRequest("Карточку удалили из задачи, пока она отправлялась, — ссылка не записана")
        entries[i]["bb"] = rec
        new["cards"] = entries
        new["history"] = list(cur.get("history") or []) + [
            {"ts": rec["ts"], "by": user["name"],
             "action": f"Карточка отправлена в базу знаний: узел «{node}», pull request №{pr_id}"}]
        new["updated"], new["updated_by"] = rec["ts"], user["name"]
        new["_v"], new["id"] = t["_v"], t["id"]
        return new

    cur = kb_task_for(user, kind, tid, teams)
    for _ in range(3):   # параллельная правка задачи между чтением и записью — перечитываем и пишем ещё раз
        try:
            return {"task": runtime.STORE.update_task(kind, cur, cur.get("_v"), finalize)}
        except Conflict:
            cur = kb_task_for(user, kind, tid, teams)
    raise Conflict(f"Задачу {tid} сейчас часто меняют — ссылка на файл не записана, повторите: {file_url}")


def op_kb_direct():
    """Онтология и отправка пришли прямо на сервер: сервер в BB не ходит."""
    raise BadRequest(kb_via_proxy())
