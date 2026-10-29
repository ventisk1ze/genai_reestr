"""Жизненный цикл задачи и права по этапам.

Порядок: Новая → В работе → На согласовании (⇄ На доработке) → Согласована → Завершена → Принята.
Кто: TL — лид или зам команды задачи (T-007); ИСП — executor или substitute; админ — всё.
Клиент присылает код действия (action_code); новый статус и служебные поля выставляет сервер.
"""
import re
from datetime import datetime

from registry import auth, config, runtime
from registry.errors import BadRequest, Forbidden
from registry.org import norm_org, public_config, task_team
from registry.util import card_entries, clone, norm, now

STATUSES = ("Новая", "В работе", "На согласовании", "На доработке", "Согласована", "Завершена", "Принята")
F_SETUP = ("title", "description", "materials", "block", "block_note", "level", "regularity", "deadline",
           "executor", "substitute")
F_RESULT = ("result", "result_desc", "report_event", "report_date")
F_APPROVE = ("block", "block_note", "level", "bz_save", "bz_reason")
F_BZ = ("bz_card",)
F_SERVICE = ("history", "updated", "updated_by", "_v", "kind", "id", "created", "deadline_initial",
             "iterations", "cycles", "feedback", "cards", "status", "teamlead", "done_date", "bz_notified", "team", "bb")
FIELD_LABEL = {
    "title": "название", "description": "описание", "materials": "материалы", "block": "блок",
    "block_note": "описание блока", "level": "уровень", "regularity": "регулярность", "deadline": "срок",
    "executor": "исполнитель", "substitute": "замещающий", "result": "результат",
    "result_desc": "описание результата", "report_event": "событие доклада", "report_date": "дата доклада",
    "bz_save": "сохранять в БЗ", "bz_reason": "причина по БЗ", "bz_card": "факт размещения карточки",
    "teamlead": "тимлид", "team": "команда", "status": "статус", "done_date": "дата завершения", "created": "дата создания",
    "bb": "выгрузки в BB"}
# Правка без смены статуса: какие поля кто правит на каком этапе
EDIT_RIGHTS = {"Новая": {"tl": F_SETUP}, "В работе": {"ex": F_RESULT}, "На доработке": {"ex": F_RESULT},
               "На согласовании": {"tl": F_APPROVE}, "Согласована": {"ex": F_BZ}}
# Действия: из каких статусов, кто, какие поля можно поменять заодно
ACTIONS = {
    "take":      {"from": ("Новая",), "who": "ex", "fields": ()},
    "to_review": {"from": ("В работе", "На доработке"), "who": "ex", "fields": F_RESULT},
    "approve":   {"from": ("На согласовании",), "who": "tl", "fields": F_APPROVE},
    "rework":    {"from": ("На согласовании",), "who": "tl", "fields": F_APPROVE},
    "finish":    {"from": ("Согласована",), "who": "ex", "fields": F_BZ},
    "accept":    {"from": ("Завершена",), "who": "tl", "fields": ()},
    "return":    {"from": ("Завершена",), "who": "tl", "fields": ()},
    "new_cycle": {"from": ("Принята",), "who": "tl", "fields": (), "regular": True},
    "feedback":  {"from": None, "who": "tl", "fields": (), "regular": True},
}
ACTION_DENY = {
    "take": "Взять задачу в работу может только исполнитель или замещающий",
    "to_review": "Отправить на согласование может только исполнитель или замещающий",
    "approve": "Согласовать может только тимлид задачи",
    "rework": "Вернуть на доработку может только тимлид задачи",
    "finish": "Завершить задачу может только исполнитель или замещающий",
    "accept": "Принять задачу может только тимлид задачи",
    "return": "Вернуть задачу исполнителю может только тимлид задачи",
    "new_cycle": "Новый цикл начинает только тимлид задачи",
    "feedback": "Обратную связь даёт только тимлид задачи",
}
CYCLE_KEEP = ("deadline", "done_date", "status", "result", "result_desc", "report_event", "report_date",
              "bz_save", "bz_reason", "bz_notified", "bz_card", "cards", "bb")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def dmy(iso):
    m = DATE_RE.fullmatch(iso or "")
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}" if m else (iso or "")


def relation(user, t, teams=None):
    """Отношение пользователя к задаче: {'tl', 'ex'} (может быть оба, если тимлид сам себе исполнитель).
    'tl' — лид или зам команды задачи; задача без команды (тимлида нет ни в одной команде) — по полю teamlead."""
    rel, name = set(), user["name"]
    team = norm(t.get("team"))
    if not team:
        team = task_team(t, norm_org(runtime.STORE.get_config())[1] if teams is None else teams)
    if team:
        if team in (user.get("lead_teams") or ()):
            rel.add("tl")
    elif name and name == norm(t.get("teamlead")):
        rel.add("tl")
    if name and name in (norm(t.get("executor")), norm(t.get("substitute"))):
        rel.add("ex")
    return rel


def need_fields(t, fields, why):
    miss = [FIELD_LABEL.get(k, k) for k in fields if not norm(t.get(k))]
    if miss:
        raise BadRequest(f"{why}: {', '.join(miss)}")


def merge_fields(cur, t, allowed, admin):
    """Новая версия = текущая + разрешённые поля от клиента. Другие изменённые поля — 403; служебные — из cur."""
    new = clone(cur)
    if admin:   # админ правит всё, кроме служебных полей хранилища и истории
        for k, v in t.items():
            if k not in ("history", "updated", "updated_by", "_v", "kind", "id"):
                new[k] = clone(v)
        return new
    bad = []
    for k, v in t.items():
        if k in F_SERVICE or k.startswith("_"):
            continue
        if k in allowed:
            new[k] = norm(v) if isinstance(v, str) else clone(v)
        elif k in config.TASK_DEFAULTS and norm(v) != norm(cur.get(k)):
            bad.append(FIELD_LABEL.get(k, k))
    if bad:
        raise Forbidden(f"На этапе «{cur.get('status')}» вам нельзя менять: {', '.join(bad)}")
    return new


def apply_action(code, cur, t, user, params, teams=None):
    """Проверка прав и перехода; возвращает (новая версия задачи, текст для истории)."""
    admin, status, kind = user["admin"], cur.get("status"), cur.get("kind")
    rel = relation(user, cur, teams)
    if code == "edit":
        if admin:
            new = merge_fields(cur, t, (), True)
            if new.get("status") not in STATUSES:
                raise BadRequest("Статус — один из: " + ", ".join(STATUSES))
            allowed = tuple(config.TASK_DEFAULTS)
        else:
            rights = EDIT_RIGHTS.get(status, {})
            allowed = tuple(f for r in rel for f in rights.get(r, ()))
            if not allowed:
                raise Forbidden(f"На этапе «{status}» вам нельзя менять эту задачу")
            new = merge_fields(cur, t, allowed, False)
        changed = [k for k in allowed if norm(new.get(k)) != norm(cur.get(k))]
        return new, ("Правка: " + ", ".join(FIELD_LABEL.get(k, k) for k in changed)) if changed else "Правка"
    spec = ACTIONS.get(code)
    if spec is None:
        raise BadRequest(f"Неизвестное действие «{code}»")
    if spec.get("regular") and kind != "regular":
        raise BadRequest("Это действие — только для регулярных задач")
    if spec["from"] is not None and status not in spec["from"]:
        raise Forbidden(f"Задача в статусе «{status}» — это действие сейчас недоступно (обновите страницу)")
    if not admin and spec["who"] not in rel:
        raise Forbidden(ACTION_DENY[code])
    new = merge_fields(cur, t, spec["fields"], admin)
    new["status"] = cur.get("status")   # статус ставит только сервер — по коду действия
    p = params if isinstance(params, dict) else {}
    if code == "take":
        new["status"] = "В работе"
        return new, "Взята в работу"
    if code == "to_review":
        need_fields(new, ("result", "result_desc"), "Для согласования заполните")
        new["status"] = "На согласовании"
        return new, "Отправлена на согласование"
    if code == "approve":
        need_fields(new, ("block", "level", "bz_save", "bz_reason")
                    + (("block_note",) if new.get("block") == "other" else ()), "Чтобы согласовать, заполните")
        if new["bz_save"] not in ("да", "нет"):
            raise BadRequest("Сохранять в БЗ: «да» или «нет»")
        if new["bz_save"] == "нет":
            new["status"] = "Завершена"
            return new, "Согласована, в БЗ не сохраняется — завершена"
        new["status"] = "Согласована"
        return new, "Согласована"
    if code == "rework" or (code == "return" and p.get("to") == "rework"):
        comment, dl = norm(p.get("comment")), norm(p.get("deadline")) or (
            norm(cur.get("deadline")) if code == "return" else "")
        if not comment:
            raise BadRequest("Напишите, что доработать")
        if not DATE_RE.fullmatch(dl):
            raise BadRequest("Укажите новый срок (ГГГГ-ММ-ДД)")
        new["iterations"] = list(cur.get("iterations") or []) + [
            {"date": datetime.now().date().isoformat(), "comment": comment, "deadline": dl,
             "deadline_prev": cur.get("deadline", "")}]
        new["deadline_initial"] = cur.get("deadline_initial") or cur.get("deadline", "")
        new["deadline"], new["status"] = dl, "На доработке"
        return new, f"Возвращена на доработку до {dmy(dl)}"
    if code == "finish":
        if not new.get("cards") and not norm(new.get("bz_card")):
            raise BadRequest("Чтобы завершить, добавьте карточку знаний или заполните факт размещения карточки")
        new["status"] = "Завершена"
        return new, "База знаний готова — завершена"
    if code == "accept":
        new["status"] = "Принята"
        new["done_date"] = cur.get("done_date") or datetime.now().date().isoformat()
        return new, "Принята"
    if code == "return":
        if p.get("to") != "approved":
            raise BadRequest("Куда вернуть: to = approved (база знаний) или rework (доработка результата)")
        if cur.get("bz_save") != "да":
            raise BadRequest("В БЗ задача не сохраняется — вернуть можно только на доработку")
        new["status"] = "Согласована"
        comment = norm(p.get("comment"))
        return new, "Возвращена исполнителю: база знаний" + (f" — {comment}" if comment else "")
    if code == "new_cycle":
        dl = norm(p.get("deadline"))
        if not DATE_RE.fullmatch(dl):
            raise BadRequest("Укажите срок нового цикла (ГГГГ-ММ-ДД)")
        new["cycles"] = list(cur.get("cycles") or []) + [
            {**{k: clone(cur.get(k, config.TASK_DEFAULTS.get(k, ""))) for k in CYCLE_KEEP},
             "closed": datetime.now().date().isoformat()}]
        for k in CYCLE_KEEP:
            new[k] = clone(config.TASK_DEFAULTS[k])
        new.update(status="Новая", deadline=dl, deadline_initial=dl, iterations=[])
        return new, f"Новый цикл до {dmy(dl)}"
    if code == "feedback":
        text = norm(p.get("text"))
        if not text:
            raise BadRequest("Напишите обратную связь")
        new["feedback"] = list(cur.get("feedback") or []) + [
            {"date": datetime.now().date().isoformat(), "by": user["name"], "text": text}]
        return new, "Обратная связь исполнителю"
    raise BadRequest(f"Неизвестное действие «{code}»")


def new_task_from(original, user, teams, params=None):
    """Постановка: лид или зам команды (он же тимлид задачи) или админ. Статус и служебные поля — от сервера.
    Команда — та, где автор лид или зам; если таких несколько — params.team (или task.team) обязателен."""
    mine = user.get("lead_teams") or []
    if not user["admin"] and not mine:
        raise Forbidden("Ставить задачи могут только тимлиды, их заместители и администратор реестра")
    kind = original.get("kind")
    want = norm((params if isinstance(params, dict) else {}).get("team")) or norm(original.get("team"))
    if want and want not in {x["id"] for x in teams}:   # команду можно назвать и по названию
        want = next((x["id"] for x in teams if x["name"] == want), want)
    if user["admin"]:
        t = {k: clone(v) for k, v in original.items() if k not in ("history", "updated", "updated_by", "_v", "id")}
        t["team"] = want or task_team(dict(t, team=""), teams)
        if t["team"] and t["team"] not in {x["id"] for x in teams}:
            raise BadRequest(f"Нет команды «{t['team']}» в справочнике")
    else:
        t = {k: norm(original.get(k)) for k in F_SETUP + ("report_event", "report_date") if k in original}
        t["teamlead"] = user["name"]
        if want:
            if want not in mine:
                raise Forbidden("Ставить задачи можно только в команду, где вы тимлид или заместитель")
            t["team"] = want
        elif len(mine) == 1:
            t["team"] = mine[0]
        else:
            raise BadRequest("Вы руководите несколькими командами — выберите команду задачи")
    for k, v in config.TASK_DEFAULTS.items():
        t.setdefault(k, clone(v))
    if not user["admin"] or t.get("status") not in STATUSES:
        t["status"] = "Новая"
    need_fields(t, ("title", "teamlead", "executor", "deadline") + (("regularity",) if kind == "regular" else ()),
                "Для постановки заполните")
    t["kind"] = kind
    t["created"] = (t.get("created") if user["admin"] else "") or datetime.now().date().isoformat()
    t["deadline_initial"] = t.get("deadline_initial") or t["deadline"]
    return t


def op_state():
    """Реестр целиком: справочник (без хешей PIN), задачи, карточки, метаданные."""
    cfg = runtime.STORE.get_config()
    teams = norm_org(cfg)[1]
    tasks = runtime.STORE.list_tasks()
    for t in tasks:   # старые задачи без команды — команда по тимлиду (запишется при первой правке)
        if not t.get("_error"):
            t["team"] = task_team(t, teams)
            t["cards"] = card_entries(t)   # строки (до T-011) → объекты {path, bb?}
    return {
        "config": public_config(cfg),
        "tasks": tasks,
        "cards": runtime.STORE.list_cards(),
        "meta": {"today": datetime.now().date().isoformat(), "storage": runtime.STORE.name,
                 "storage_info": runtime.STORE.describe(),
                 "admin_login": auth.ADMIN_PIN is not None},
    }


def op_save_task(body, user):
    """Сохранение задачи: постановка (create), правка этапа (edit) или действие этапа (action_code)."""
    original = body.get("task") or {}
    kind = original.get("kind")
    if kind not in config.KINDS:
        raise BadRequest("Неизвестный тип задачи")
    by, admin = user["name"], user["admin"]
    is_new = original.get("id") in (None, "")
    code = str(body.get("action_code") or ("create" if is_new else "edit")).strip()
    text = str(body.get("action") or "").strip()   # подпись в историю от клиента — только для постановки и правки

    def stamp(t, action):
        if admin:
            action = "Правка администратором: " + action.removeprefix("Правка: ")
        t["history"] = t.get("history") or []
        t["history"] = t["history"] + [{"ts": now(), "by": by, "action": action}]
        t["updated"], t["updated_by"], t["kind"] = now(), by, kind
        return t

    if is_new:
        if code != "create":
            raise BadRequest("Новую задачу можно только поставить (action_code = create)")
        t = new_task_from(original, user, norm_org(runtime.STORE.get_config())[1], body.get("params"))
        t["history"] = []
        return {"task": runtime.STORE.create_task(kind, t, lambda t, cur: stamp(t, text or "Поставлена"))}
    if code == "create":
        raise BadRequest("Задача уже поставлена — используйте действие этапа")

    teams = norm_org(runtime.STORE.get_config())[1]   # до блокировки хранилища: в SQL справочник читается своей транзакцией

    def finalize(t, cur):   # вызывается хранилищем под блокировкой, после проверки версии
        cur = dict(cur, kind=kind)
        new, auto = apply_action(code, cur, t, user, body.get("params"), teams)
        if new.get("team") and new["team"] not in {x["id"] for x in teams}:
            raise BadRequest(f"Нет команды «{new['team']}» в справочнике")
        new["team"] = norm(new.get("team")) or task_team(cur, teams)   # старая задача получает команду
        new["_v"], new["id"] = t["_v"], t["id"]
        new["history"] = list(cur.get("history") or [])
        return stamp(new, (text if code == "edit" and text else None) or auto or "Изменение")

    return {"task": runtime.STORE.update_task(kind, original, body.get("expect_v"), finalize)}


def op_delete_task(body, user):
    """Удаление задачи — только админ."""
    auth.require_admin(user)
    kind, tid = body.get("kind"), body.get("id")
    if kind not in config.KINDS or not str(tid).isdigit():
        raise BadRequest("Неизвестная задача")
    runtime.STORE.delete_task(kind, int(tid), body.get("expect_v"))
    return {"kind": kind, "id": int(tid)}
