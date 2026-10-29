"""Сторона сервера реестра: статус выгрузки, карточки и настройки для прокси, запись результата.

Сервер в BB не ходит и MCP-сервер не запускает — только проверяет права и пишет в задачу.
"""
from registry import config, runtime
from registry.bb import (BB_PR_URL_RE, BB_STATUS_OK, bb_branch_ok, bb_repo_path_ok,
                         bb_server_target)
from registry.cards import card_entries
from registry.errors import BadRequest, Conflict, Forbidden
from registry.org import norm_org
from registry.tasks import relation
from registry.util import clone, norm, now


def bb_via_proxy():
    return ("Выгрузка в BB — через свой прокси: она идёт из вашего контейнера и с вашей учёткой BB. Откройте реестр "
            f"командой bash connect.sh <табельный владельца реестра> (сам владелец — bash connect.sh 127.0.0.1:{runtime.SERVE_PORT})")


def op_bb_status():
    """GET /api/bb/status прямо на сервере: выгрузка — только через свой прокси (connect.sh)."""
    t, missing = bb_server_target()
    reason = bb_via_proxy()
    if missing:
        reason += ". Владельцу реестра нужно задать в registry.env: " + ", ".join(missing)
    out = {"available": False, "server_configured": not missing, "reason": reason, "missing": missing}
    if not missing:
        out["target"] = t
    return out


def bb_task_for(user, kind, tid, teams=None):
    """Задача для выгрузки и права: исполнитель или замещающий (или админ), задача «Согласована»."""
    if kind not in config.KINDS or not str(tid).isdigit():
        raise BadRequest("Неизвестная задача: нужны kind (once или regular) и id")
    t = runtime.STORE.get_task(kind, int(tid))
    if t is None:
        raise BadRequest(f"Задачи {tid} нет в хранилище — обновите страницу.")
    bb_rights(user, dict(t, kind=kind), teams)
    return t


def bb_rights(user, t, teams=None):
    if not user["admin"] and "ex" not in relation(user, t, teams):
        raise Forbidden("Выгружать карточки знаний в BB может исполнитель или замещающий задачи")
    if t.get("status") != BB_STATUS_OK:
        raise Forbidden(f"Карточки выгружают в BB, когда задача «{BB_STATUS_OK}», а она «{t.get('status')}»")


def op_bb_task(q, user):
    """GET /api/bb/task?kind&id — прокси сотрудника берёт карточки и настройки репозитория (только с сессией)."""
    kind, tid = (q.get("kind") or [""])[0], (q.get("id") or [""])[0]
    t = bb_task_for(user, kind, tid)
    paths = [e.get("path") for e in card_entries(t) if isinstance(e.get("path"), str) and e.get("path")]
    if not paths:
        raise BadRequest("У задачи нет карточек знаний — сначала добавьте карточку, потом выгружайте в BB")
    target, missing = bb_server_target()
    if missing:
        raise BadRequest("Выгрузка в BB не настроена у владельца реестра: нужно задать в его registry.env "
                         + ", ".join(missing) + " и перезапустить bash start.sh")
    cards = []
    for p in paths:
        try:
            title, body = runtime.STORE.read_card(p)
        except BadRequest:
            raise BadRequest(f"Карточка «{p.rsplit('/', 1)[-1]}» не найдена в хранилище — откройте её в задаче "
                             "и сохраните заново")
        cards.append({"file": p.rsplit("/", 1)[-1], "title": title, "body": body})
    return {"task": {"kind": kind, "id": int(tid), "title": t.get("title", "")}, "cards": cards, "target": target,
            "by": user["name"]}


def op_bb_done(body, user):
    """POST /api/bb/done: прокси сотрудника сообщает результат выгрузки — сервер пишет его в задачу атомарно."""
    if not isinstance(body, dict):
        raise BadRequest("Некорректный JSON")
    kind, tid = body.get("kind"), body.get("id")
    cur = bb_task_for(user, kind, tid)
    pr_url = body.get("pr_url")
    if not isinstance(pr_url, str) or len(pr_url) > 500 or not BB_PR_URL_RE.fullmatch(pr_url):
        raise BadRequest("pr_url — ссылка на pull request: http(s), до 500 символов")
    pr_id = body.get("pr_id")
    if isinstance(pr_id, str) and pr_id.isdigit() and len(pr_id) <= 12:
        pr_id = int(pr_id)
    if isinstance(pr_id, bool) or not isinstance(pr_id, int) or not 0 < pr_id < 10 ** 12:
        raise BadRequest("pr_id — номер pull request, целое число")
    branch, files = body.get("branch"), body.get("files")
    if not bb_branch_ok(branch):
        raise BadRequest("branch — имя ветки из латинских букв, цифр и . _ / -")
    if not isinstance(files, list) or not 0 < len(files) <= 500 or not all(bb_repo_path_ok(f) for f in files) \
            or len(set(files)) != len(files):
        raise BadRequest("files — список файлов карточек в репозитории: <папка>/<имя>.md")
    teams = norm_org(runtime.STORE.get_config())[1]
    rec = {"ts": now(), "by": user["name"], "pr_url": pr_url, "pr_id": pr_id, "branch": branch, "files": files}
    link = f"Pull request в BB: {pr_url}"

    def finalize(t, cur):   # под блокировкой хранилища, после проверки версии — права и статус ещё раз
        cur = dict(cur, kind=kind)
        bb_rights(user, cur, teams)
        new = clone(cur)
        new["bb"] = list(cur.get("bb") or []) + [rec]
        card = norm(cur.get("bz_card"))
        new["bz_card"] = link if not card else card if pr_url in card else f"{card}; {link}"
        new["history"] = list(cur.get("history") or []) + [
            {"ts": rec["ts"], "by": user["name"], "action": f"Карточки выгружены в BB: pull request №{pr_id}"}]
        new["updated"], new["updated_by"] = rec["ts"], user["name"]
        new["_v"], new["id"] = t["_v"], t["id"]
        return new

    for _ in range(3):   # параллельная правка задачи между чтением и записью — перечитываем и пишем ещё раз
        try:
            return {"task": runtime.STORE.update_task(kind, cur, cur.get("_v"), finalize)}
        except Conflict:
            cur = bb_task_for(user, kind, tid, teams)
    raise Conflict(f"Задачу {tid} сейчас часто меняют — ссылка на pull request не записана, повторите: {pr_url}")


def op_bb_upload_direct():
    """POST /api/bb/upload пришёл прямо на сервер: сервер реестра в BB не ходит и MCP-сервер не запускает."""
    raise BadRequest(bb_via_proxy())
