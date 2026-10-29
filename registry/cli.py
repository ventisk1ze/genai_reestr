"""Команды для GigaCode (ctx, tasks, task, create-task) — через сервер реестра (REG_SERVER).

Сервер уже держит подключение к хранилищу (в т.ч. к GP), поэтому агент ходит в реестр через него:
REG_SERVER — свой сервер (по умолчанию http://127.0.0.1:39246) или сервер коллеги
http://jlab-<табельный>:39246. Ключ API берётся со страницы сервера — так же, как его получает
браузер; на диск ничего не пишется.
"""
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from registry import config
from registry.auth import PIN_RE
from registry.util import dumps


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))   # внутренняя сеть — мимо корпоративного прокси


def client(method, path, body=None, session=""):
    base = config.REG_SERVER.rstrip("/")
    try:
        page = _opener().open(base + "/", timeout=30).read().decode("utf-8", "replace")
    except Exception as e:
        sys.exit(f"Сервер реестра {base} не отвечает ({e}). Запустите bash start.sh "
                 "или проверьте REG_SERVER (сервер коллеги: http://jlab-<табельный>:39246).")
    m = re.search(r"TOKEN\s*=\s*['\"]([^'\"]+)['\"]", page)
    if not m:
        sys.exit(f"По адресу {base} отвечает не реестр задач ЦК")
    req = urllib.request.Request(base + path, method=method,
                                 data=None if body is None else json.dumps(body).encode("utf-8"),
                                 headers={"X-Token": m.group(1), "Content-Type": "application/json", "X-Session": session})
    try:
        with _opener().open(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            sys.exit("Ошибка реестра: " + json.loads(e.read()).get("error", str(e)))
        except ValueError:
            sys.exit(f"Ошибка реестра: HTTP {e.code}")


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_ctx(a):
    st = client("GET", "/api/state")
    cfg = st.get("config") or {}
    out({"today": st["meta"]["today"], "storage": st["meta"].get("storage"),
         "blocks": cfg.get("blocks", []),
         "people": [p.get("name") for p in cfg.get("people", []) if p.get("active", True)],
         "teamleads": [p.get("name") for p in cfg.get("people", []) if p.get("active", True) and p.get("role") == "teamlead"],
         "teams": [{"id": t.get("id"), "name": t.get("name"), "lead": t.get("lead"), "deputy": t.get("deputy"),
                    "members": [p.get("name") for p in cfg.get("people", [])
                                if p.get("active", True) and p.get("team") == t.get("id")]}
                   for t in cfg.get("teams", [])],
         "roles": cfg.get("roles", {}), "events": cfg.get("events", []), "regularities": cfg.get("regularities", []),
         "levels": ["run", "change", "disrupt"]})


def cmd_tasks(a):
    rows = []
    for t in client("GET", "/api/state")["tasks"]:
        if a.status and t.get("status") != a.status:
            continue
        if a.person and a.person not in (t.get("teamlead"), t.get("executor"), t.get("substitute")):
            continue
        if a.kind and t.get("kind") != a.kind:
            continue
        rows.append({k: t.get(k) for k in ("kind", "id", "status", "title", "team", "teamlead", "executor", "deadline",
                                            "block", "level", "report_event", "report_date")})
    out(rows)


def cmd_task(a):
    t = next((t for t in client("GET", "/api/state")["tasks"]
              if t.get("kind") == a.kind and str(t.get("id")) == str(a.id)), None)
    if t is None:
        sys.exit(f"Задача {a.kind} {a.id} не найдена")
    out(t)


def cmd_create_task(a):
    src = sys.stdin.read() if a.file == "-" else Path(a.file).read_text("utf-8")
    try:
        d = json.loads(src, strict=False)   # агент может оставить настоящие переводы строк внутри description
    except ValueError as e:
        sys.exit(f"create-task: файл не похож на JSON ({e})")
    if not isinstance(d, dict) or not str(d.get("title", "")).strip():
        sys.exit("В JSON нет title")
    kind = d.get("kind") or "once"
    t = {k: d[k] for k in ("title", "description", "level", "block", "deadline", "teamlead", "executor",
                           "substitute", "regularity", "materials", "report_event", "report_date") if d.get(k)}
    t.update(kind=kind, id=None, status="Новая", deadline_initial=t.get("deadline", ""))
    by = (a.by or t.get("teamlead", "")).strip()
    if not by:
        sys.exit("create-task: укажите ФИО тимлида или заместителя — --by \"ФИО\" или teamlead в JSON")
    t["teamlead"] = by   # тимлид задачи — тот, кто ставит (лид или зам)
    team = (a.team or str(d.get("team") or "")).strip()
    pin = ask_pin(f"PIN {by}: ")
    sid = client("POST", "/api/login", {"name": by, "pin": pin})["session"]
    try:
        user = client("GET", "/api/whoami", session=sid)["user"] or {}
        mine = user.get("lead_teams") or []
        if not mine:
            sys.exit(f"create-task: {by} — не тимлид и не заместитель ни одной команды в справочнике реестра; "
                     "задачи из GigaCode ставят тимлиды и заместители")
        if not team and len(mine) > 1:
            teams = {x.get("id"): x.get("name") for x in (client("GET", "/api/state").get("config") or {}).get("teams", [])}
            sys.exit(f"create-task: {by} руководит несколькими командами — укажите --team: "
                     + ", ".join(f"{i} ({teams.get(i, '')})" for i in mine))
        r = client("POST", "/api/task", {"task": t, "action": "Поставлена через GigaCode",
                                         "params": {"team": team} if team else {}}, session=sid)
    finally:
        client("POST", "/api/logout", {}, session=sid)
    out({"created": True, "kind": kind, "id": r["task"]["id"], "title": r["task"]["title"]})


def ask_pin(prompt):
    """PIN — только с терминала человека: в лава-болле (и вообще без терминала) задачу создают через веб-форму."""
    no_tty = "Терминала нет — PIN тимлида ввести негде: создайте задачу через веб-форму реестра (кнопка «Новая задача» " \
             "или «Создать задачу из карточки» в помощнике)."
    if os.environ.get("REG_NO_TTY"):
        sys.exit(no_tty)
    try:
        open("/dev/tty").close()
    except OSError:
        sys.exit(no_tty)
    try:
        pin = getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nPIN не введён — задача не создана")
    if not PIN_RE.fullmatch(pin):
        sys.exit("PIN — от 4 до 8 цифр")
    return pin
