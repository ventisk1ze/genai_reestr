"""Справочники реестра: сохранение конфигурации (люди, команды, блоки, матрица, события)."""
from registry import runtime
from registry.errors import BadRequest
from registry.org import norm_org, public_config, task_team
from registry.sessions import drop_sessions
from registry.util import clone


def check_teams(teams, people, old_teams):
    """Команды справочника: id и названия уникальны, лид и зам — люди из справочника и не один человек;
    удалить команду, у которой есть задачи, нельзя."""
    names = {p["name"] for p in people}
    if len({t["id"] for t in teams}) != len(teams):
        raise BadRequest("У команд повторяются id")
    for t in teams:
        if not t["name"]:
            raise BadRequest("У команды нужно название")
        for k, what in (("lead", "Тимлид"), ("deputy", "Заместитель")):
            if t[k] and t[k] not in names:
                raise BadRequest(f"{what} команды «{t['name']}» — «{t[k]}» — нет в справочнике людей")
        if t["lead"] and t["lead"] == t["deputy"]:
            raise BadRequest(f"В команде «{t['name']}» один человек не может быть и тимлидом, и заместителем")
    if len({t["name"] for t in teams}) != len(teams):
        raise BadRequest("Названия команд повторяются")
    gone = [t for t in old_teams if t["id"] not in {x["id"] for x in teams}]
    if gone:
        tasks = [t for t in runtime.STORE.list_tasks() if not t.get("_error")]
        for g in gone:
            n = sum(1 for t in tasks if task_team(t, old_teams) == g["id"])
            if n:
                raise BadRequest(f"Команду «{g['name']}» нельзя удалить: у неё есть задачи ({n}). "
                                 "Сначала передайте их другой команде или удалите")


def op_save_config(body, by):
    """Сохранение справочников (только админ): люди, команды, блоки, матрица, события, регулярности."""
    cfg = body.get("config")
    if not isinstance(cfg, dict):
        raise BadRequest("Пустой справочник")
    cfg = clone(cfg)
    stored = runtime.STORE.get_config() or {}
    old_people, old_teams = norm_org(stored)
    if not isinstance(cfg.get("teams"), list) and isinstance(stored.get("teams"), list):
        cfg["teams"] = old_teams   # клиент до T-007 шлёт справочник без teams — команды остаются прежние
    pins = {p["name"]: p["pin"] for p in old_people if p.get("pin")}
    new_people, teams = norm_org(cfg)   # команд нет нигде — миграция по тимлидам, как при чтении
    people = []
    for p in new_people:   # PIN от клиента не принимаем: хеши остаются те, что на сервере, по ФИО
        p = {k: v for k, v in p.items() if k not in ("pin", "has_pin")}
        if p["name"] in pins:
            p["pin"] = pins[p["name"]]
        people.append(p)
    if len({p["name"] for p in people}) != len(people):
        raise BadRequest("В справочнике людей повторяются ФИО")
    check_teams(teams, people, old_teams)
    cfg["people"], cfg["teams"] = people, teams
    saved = runtime.STORE.save_config(cfg, body.get("expect_v"))
    drop_sessions(keep={p["name"] for p in people if p["active"]})
    return {"config": public_config(saved)}
