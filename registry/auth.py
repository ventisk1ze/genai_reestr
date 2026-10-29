"""Вход по PIN: хеши PBKDF2, операции входа/выхода, админские PIN-ы.

PIN у каждого сотрудника задаёт админ; в config.people[i].pin — только соль и хеш (PBKDF2),
сам PIN нигде. Админ — владелец сервера: его PIN приходит через stdin при запуске (start.sh),
в памяти остаётся только хеш (auth.ADMIN_PIN).
"""
import hashlib
import hmac
import re
import secrets
import sys
import time
from datetime import datetime

from registry import config, runtime, sessions
from registry.errors import AuthError, BadRequest, Conflict, Forbidden, Locked
from registry.org import ADMIN_NAME, find_person, norm_org
from registry.sessions import AUTH_LOCK, FAILS, SESSIONS, drop_sessions, session_user
from registry.util import CFG_STALE, clone

PIN_RE = re.compile(r"[0-9]{4,8}")
PIN_ITER = 200_000
ADMIN_PIN = None        # {"salt", "hash", "iter"} или None — админ-вход выключен


def pin_hash(pin, salt=None, iterations=PIN_ITER):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)
    return {"salt": salt.hex(), "hash": h.hex(), "iter": iterations}


def pin_ok(pin, rec):
    try:
        h = pin_hash(pin, bytes.fromhex(rec["salt"]), int(rec["iter"]))["hash"]
        return hmac.compare_digest(h, rec["hash"])
    except (KeyError, TypeError, ValueError):
        return False


def check_pin_format(pin):
    if not isinstance(pin, str) or not PIN_RE.fullmatch(pin):
        raise BadRequest("PIN — от 4 до 8 цифр")
    return pin


def op_login(body):
    pin = body.get("pin")
    admin = body.get("admin") is True
    name = ADMIN_NAME if admin else str(body.get("name") or "").strip()
    if not name:
        raise BadRequest("Выберите себя в списке сотрудников")
    check_pin_format(pin)
    if admin:
        if ADMIN_PIN is None:
            raise Forbidden("Вход администратора выключен: сервер запущен без админского PIN (bash start.sh)")
        rec = ADMIN_PIN
    else:
        p = find_person(runtime.STORE.get_config(), name)
        if not p or not p["active"]:
            raise AuthError(f"Нет активного сотрудника «{name}» — обратитесь к администратору реестра")
        rec = p.get("pin")
        if not rec:
            raise AuthError(f"PIN для «{name}» не задан — попросите администратора реестра задать его")
    with AUTH_LOCK:
        f = FAILS.get(name)
        if f and f["until"] > time.time():
            raise Locked(f"Слишком много неверных попыток: вход для «{name}» заблокирован до "
                         f"{datetime.fromtimestamp(f['until']).strftime('%H:%M')}")
    if not pin_ok(pin, rec):
        with AUTH_LOCK:
            f = FAILS.get(name) or {"n": 0, "until": 0}
            if f["until"]:        # блокировка истекла — считаем заново
                f = {"n": 0, "until": 0}
            f["n"] += 1
            if f["n"] >= sessions.LOGIN_MAX_FAIL:
                f["until"] = time.time() + sessions.LOGIN_LOCK_SEC
            FAILS[name] = f
        if f["until"]:
            raise Locked(f"Слишком много неверных попыток: вход для «{name}» заблокирован до "
                         f"{datetime.fromtimestamp(f['until']).strftime('%H:%M')}")
        raise AuthError(f"Неверный PIN. Осталось попыток: {sessions.LOGIN_MAX_FAIL - f['n']}")
    sid = secrets.token_urlsafe(32)
    exp = time.time() + sessions.SESSION_TTL
    with AUTH_LOCK:
        FAILS.pop(name, None)
        for s, v in list(SESSIONS.items()):   # заодно убрать просроченные
            if v["exp"] < time.time():
                SESSIONS.pop(s, None)
        SESSIONS[sid] = {"name": name, "admin": admin, "exp": exp}
    return {"session": sid, "expires": datetime.fromtimestamp(exp).isoformat(timespec="seconds"),
            "user": session_user(sid)}


def op_logout(sid):
    with AUTH_LOCK:
        SESSIONS.pop(sid or "", None)
    return {"ok": True}


def require_admin(user):
    if not user["admin"]:
        raise Forbidden("Это может только администратор реестра")


def op_admin_pin(body):
    """Задать или сбросить PIN сотрудника (только админ). Переименование сбрасывает PIN — на сервере хеш по ФИО."""
    name = str(body.get("name") or "").strip()
    reset = body.get("reset") is True
    pin = None if reset else check_pin_format(body.get("pin"))
    for _ in range(3):   # версия справочника могла смениться между чтением и записью — повторяем
        cfg = runtime.STORE.get_config() or clone(config.DEFAULT_CONFIG)
        people, teams = norm_org(cfg)
        p = next((x for x in people if x["name"] == name), None)
        if p is None:
            raise BadRequest(f"Нет сотрудника «{name}» в справочнике")
        if reset:
            p.pop("pin", None)
        else:
            p["pin"] = pin_hash(pin)
        cfg["people"], cfg["teams"] = people, teams   # people.team теперь id — команды пишем вместе с ними
        try:
            runtime.STORE.save_config(cfg, cfg.get("_v"))
            break
        except Conflict:
            continue
    else:
        raise Conflict(CFG_STALE)
    drop_sessions(name)
    with AUTH_LOCK:
        FAILS.pop(name, None)
    return {"name": name, "has_pin": not reset}


def read_admin_pin_stdin():
    """start.sh пишет в канал админский PIN — следующей строкой после логина и пароля GP (если они есть).
    В памяти остаётся только хеш; без --admin-pin-stdin вход администратора выключен."""
    global ADMIN_PIN
    pin = sys.stdin.readline().strip()
    if not PIN_RE.fullmatch(pin):
        sys.exit("Админский PIN не получен или неверный: нужно от 4 до 8 цифр (bash start.sh спросит его)")
    ADMIN_PIN = pin_hash(pin)
