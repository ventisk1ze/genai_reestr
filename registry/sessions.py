"""Сессии и счётчики неверных попыток — в памяти сервера: после перезапуска все входят заново."""
import os
import threading
import time

from registry import runtime
from registry.org import ADMIN_NAME, lead_teams, norm_org

SESSION_TTL = float(os.environ.get("REG_SESSION_HOURS", "12")) * 3600
LOGIN_MAX_FAIL = int(os.environ.get("REG_LOGIN_MAX_FAIL", "5"))
LOGIN_LOCK_SEC = int(os.environ.get("REG_LOGIN_LOCK_SEC", "300"))
SESSIONS = {}           # session -> {"name", "admin", "exp"}
FAILS = {}              # ФИО (или ADMIN_NAME) -> {"n": неверных подряд, "until": блокировка до (time.time())}
AUTH_LOCK = threading.Lock()


def drop_sessions(name=None, keep=None):
    """Закрыть сессии человека (или всех людей, кого нет в keep)."""
    with AUTH_LOCK:
        for s, v in list(SESSIONS.items()):
            if v["admin"]:
                continue
            if (name is not None and v["name"] == name) or (keep is not None and v["name"] not in keep):
                SESSIONS.pop(s, None)


def session_user(sid):
    """Пользователь сессии: {name, role, team, lead_teams, admin} или None (нет/истекла/деактивирован)."""
    if not sid:
        return None
    with AUTH_LOCK:
        v = SESSIONS.get(sid)
        if v and v["exp"] < time.time():
            SESSIONS.pop(sid, None)
            v = None
    if not v:
        return None
    if v["admin"]:
        return {"name": ADMIN_NAME, "role": "admin", "team": "", "lead_teams": [], "admin": True}
    people, teams = norm_org(runtime.STORE.get_config())
    p = next((x for x in people if x["name"] == v["name"]), None)
    if not p or not p["active"]:   # деактивировали или удалили, пока сессия жила
        drop_sessions(v["name"])
        return None
    return {"name": p["name"], "role": p["role"], "team": p["team"], "lead_teams": lead_teams(teams, p["name"]),
            "admin": False}
