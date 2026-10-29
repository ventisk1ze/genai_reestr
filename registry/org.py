"""Справочник людей и команд: чистые функции над config (без обращений к хранилищу).

people[].team — id команды; роль следует из команд: лид или зам хотя бы одной — 'teamlead'.
Справочник без teams (до T-007) переводится без потерь: команда на каждого тимлида.
"""
from registry.util import norm

ROLES = ("employee", "teamlead")
ADMIN_NAME = "Администратор"


def norm_org(cfg):
    """Люди и команды справочника: (people, teams). people[].team — id команды, role следует из команд
    (лид или зам хотя бы одной команды — 'teamlead'). Справочник без teams (до T-007) переводится без потерь:
    команда на каждого тимлида (название — его ФИО), состав — по прежнему people.team (= ФИО лида)."""
    cfg = cfg or {}
    people = []
    for p in cfg.get("people") or []:
        if not isinstance(p, dict) or not str(p.get("name") or "").strip():
            continue
        p = dict(p, name=str(p["name"]).strip(), team=str(p.get("team") or "").strip())
        p["active"] = p.get("active") is not False
        people.append(p)
    if isinstance(cfg.get("teams"), list):
        teams = []
        for t in cfg["teams"]:
            if isinstance(t, dict) and str(t.get("id") or "").strip():
                teams.append({"id": str(t["id"]).strip(), "name": str(t.get("name") or "").strip(),
                              "lead": str(t.get("lead") or "").strip(), "deputy": str(t.get("deputy") or "").strip()})
    else:   # миграция: тимлид — роль из справочника или 0 в старой матрице
        leads = {n for row in (cfg.get("matrix") or {}).values() for n, v in (row or {}).items()
                 if str(v).strip() == "0"}
        teams = []
        for p in people:
            role = p.get("role") if p.get("role") in ROLES else ("teamlead" if p["name"] in leads else "employee")
            if role == "teamlead":
                teams.append({"id": f"t{len(teams) + 1}", "name": p["name"], "lead": p["name"], "deputy": ""})
                p["team"] = p["name"]
        for p in people:   # команда без тимлида в справочнике — тоже команда, состав не теряется
            if p["team"] and not any(t["name"] == p["team"] for t in teams):
                teams.append({"id": f"t{len(teams) + 1}", "name": p["team"], "lead": "", "deputy": ""})
    ids = {t["id"] for t in teams}
    by_name = {t["name"]: t["id"] for t in teams}
    by_lead = {t["lead"]: t["id"] for t in teams if t["lead"]}
    leading = {n for t in teams for n in (t["lead"], t["deputy"]) if n}
    for p in people:   # старый клиент мог прислать в team ФИО лида или название — переводим в id
        if p["team"] not in ids:
            p["team"] = by_name.get(p["team"]) or by_lead.get(p["team"]) or ""
        p["role"] = "teamlead" if p["name"] in leading else "employee"
    return people, teams


def norm_people(cfg):
    return norm_org(cfg)[0]


def lead_teams(teams, name):
    """id команд, где человек — лид или зам."""
    return [t["id"] for t in teams if name and name in (t["lead"], t["deputy"])]


def task_team(t, teams):
    """Команда задачи; у старых задач без поля team — та, где тимлид задачи — лид (или зам)."""
    if norm(t.get("team")):
        return norm(t.get("team"))
    tl = norm(t.get("teamlead"))
    if not tl:
        return ""
    return next((x["id"] for x in teams if x["lead"] == tl), "") or \
        next((x["id"] for x in teams if x["deputy"] == tl), "")


def public_config(cfg):
    """Справочник для клиента: вместо соли и хеша — только флаг «PIN задан»."""
    if not cfg:
        return cfg
    cfg = dict(cfg)
    people, cfg["teams"] = norm_org(cfg)
    cfg["people"] = [{**{k: v for k, v in p.items() if k != "pin"}, "has_pin": bool(p.get("pin"))}
                     for p in people]
    return cfg


def find_person(cfg, name):
    return next((p for p in norm_people(cfg or {}) if p["name"] == name), None)
