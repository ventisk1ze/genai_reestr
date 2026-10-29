"""Контекст реестра для модели: кто пишет, его задачи, блоки и события (для обоих движков)."""
import re
from datetime import datetime, timedelta

from registry import config, runtime
from registry.org import norm_org, task_team
from registry.tasks import dmy, relation
from registry.util import norm


def who_context(user):
    """Кто пишет и его задачи — для модели (оба движка). Только поля задач и ФИО, без PIN и служебного."""
    people, teams = norm_org(runtime.STORE.get_config())
    tname = {t["id"]: t["name"] for t in teams}
    if user is None:
        return "Кто пишет: неизвестно (нет входа).\n"
    name = user["name"]
    if user["admin"]:
        who = "Администратор реестра — видит и правит все задачи"
    else:
        parts = [f"тимлид команды «{t['name']}»" for t in teams if t["lead"] == name] + \
                [f"заместитель тимлида команды «{t['name']}» (права тимлида)" for t in teams if t["deputy"] == name]
        team = tname.get(next((p["team"] for p in people if p["name"] == name), ""), "")
        who = f"{name} — " + (", ".join(parts) if parts else
                               "сотрудник" + (f" команды «{team}»" if team else " (без команды)"))
    cutoff = (datetime.now().date() - timedelta(days=config.CTX_CLOSED_DAYS)).isoformat()
    rows = []
    for t in runtime.STORE.list_tasks():
        if t.get("_error"):
            continue
        if t.get("status") == "Принята" and (t.get("done_date") or "") < cutoff:
            continue
        rel = relation(user, t, teams)
        if user["admin"]:
            roles = ["администратор"]
        else:
            roles = (["тимлид" if t.get("teamlead") == name else "тимлид команды"] if "tl" in rel else []) + \
                    (["исполнитель"] if norm(t.get("executor")) == name else []) + \
                    (["замещающий"] if norm(t.get("substitute")) == name else [])
        if roles:
            rows.append((t, roles))
    rows.sort(key=lambda r: (r[0].get("status") == "Принята", -int(r[0].get("id") or 0)))
    lines = []
    for t, roles in rows[:config.CTX_TASKS_MAX]:
        team = tname.get(task_team(t, teams), "")
        line = (f"- № {t.get('id')}, kind={t.get('kind')} ({'регулярная' if t.get('kind') == 'regular' else 'разовая'}): "
                f"«{t.get('title') or 'без названия'}» — статус «{t.get('status')}»; роль пишущего: {', '.join(roles)}; "
                f"тимлид: {t.get('teamlead') or '—'}; исполнитель: {t.get('executor') or '—'}"
                + (f"; замещающий: {t['substitute']}" if t.get("substitute") else "")
                + (f"; команда: {team}" if team else "")
                + (f"; срок: {dmy(t['deadline'])}" if t.get("deadline") else "")
                + (f"; сохранять в БЗ: {t['bz_save']}" if t.get("bz_save") else "")
                + (f"; карточек знаний: {len(t['cards'])}" if t.get("cards") else ""))
        if ("исполнитель" in roles or "замещающий" in roles) and t.get("status") in ("В работе", "На доработке",
                                                                                     "Согласована"):
            cut = lambda v: re.sub(r"\s+", " ", str(v or "")).strip()[:400]
            for k, lbl in (("description", "описание"), ("result", "результат сейчас"),
                           ("result_desc", "описание результата сейчас")):
                if cut(t.get(k)):
                    line += f"\n    {lbl}: {cut(t.get(k))}"
            if t.get("status") == "На доработке" and t.get("iterations"):
                line += f"\n    замечание тимлида: {cut(t['iterations'][-1].get('comment'))}"
        lines.append(line)
    more = f" (показаны {config.CTX_TASKS_MAX} из {len(rows)})" if len(rows) > config.CTX_TASKS_MAX else ""
    return (f"Кто пишет: {who}.\n"
            f"Задачи пишущего{more} — где он тимлид, исполнитель или замещающий; принятые — за {config.CTX_CLOSED_DAYS} дней:\n"
            + ("\n".join(lines) if lines else "- задач нет") + "\n")


def registry_context(user=None):
    """(блоки, контекст реестра для системного промпта)."""
    cfg = runtime.STORE.get_config() or {}
    blocks = "\n".join(f"- {b.get('code')}: {b.get('name')}" for b in cfg.get("blocks", [])) or "- (справочник пуст)"
    return blocks, (
        "\n\n---\n## Контекст от реестра (добавлен веб-чатом, не часть скилла)\n"
        "Канал: веб-чат реестра задач ЦК. Терминала и команд реестра здесь нет: когда скилл предлагает выполнить "
        "команду реестра, выведи результат прямо в ответе служебным блоком, который описан в скилле "
        "(```task```, ```result```, ```card```) — человек перенесёт его в реестр кнопкой.\n"
        f"Сегодня: {datetime.now().strftime('%d.%m.%Y')}.\n"
        f"События регулярного менеджмента: {', '.join(cfg.get('events', [])) or 'не заданы'}.\n"
        f"Блоки направлений ЦК:\n{blocks}\n" + who_context(user))


def skill_prompt(sk, user):
    """(имя скилла, системный промпт): текст скилла с подставленными блоками и контекстом реестра."""
    blocks, ctx = registry_context(user)
    today = datetime.now().strftime("%d.%m.%Y")
    body = sk["body"].replace("{{BLOCKS}}", blocks).replace("{{TODAY}}", today)   # старые промпты с плейсхолдерами
    return sk["name"], body + ctx


def speaker_label(user):
    """Подпись реплик пишущего в диалоге для GigaCode — по его роли."""
    if user is None:
        return "Пользователь"
    if user["admin"]:
        return "Администратор реестра"
    if not user.get("lead_teams"):
        return f"Сотрудник ({user['name']})"
    lead = any(t["lead"] == user["name"] for t in norm_org(runtime.STORE.get_config())[1])
    return ("Тимлид" if lead else "Заместитель тимлида") + f" ({user['name']})"
