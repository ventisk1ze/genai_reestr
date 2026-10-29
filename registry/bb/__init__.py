"""Выгрузка карточек знаний в BB (Bitbucket) через MCP — общие настройки и валидаторы.

Выгружает прокси сотрудника (app.py proxy, connect.sh) — в его контейнере и с его учёткой BB:
учётка сотрудника не попадает к владельцу сервера реестра, учётки владельца — к сотруднику.
Сервер реестра в BB не ходит и MCP-сервер не запускает: он проверяет права, отдаёт прокси
карточки и настройки репозитория (/api/bb/task) и записывает в задачу результат (/api/bb/done).
Базы знаний (T-011) — те же валидаторы: узлы онтологии — папки, кириллица разрешена.
"""
import os
import re
from urllib.parse import urlparse

from registry import config

BB_NEED = (("REG_BB_REPO_URL", "repo_url"), ("REG_BB_PROJECT", "project"), ("REG_BB_REPO", "repo"))
# сегмент папки: Unicode-буквы/цифры/подчёркивание (узлы онтологии — кириллица), точка, дефис, пробел;
# не с точки (нет «..» и скрытых) и не с дефиса (имя ветки не примут за опцию git)
BB_DIR_SEG = re.compile(r"\w[\w.\- ]{0,63}")
BB_PROJECT_RE = re.compile(r"~?[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")  # ключ проекта; личный проект — ~логин
BB_REPO_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")       # slug репозитория
BB_BRANCH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}")    # не с «-»: имя ветки не примут за опцию git
BB_URL_RE = re.compile(r"https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%+/-]*)?")   # без логина, ?запроса, #
BB_PR_URL_RE = re.compile(r"https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%!$&'()*+,;=:@/?#-]*)?")
BB_FILE_BAD = re.compile(r"[/\\\x00-\x1f\x7f]")
BB_STATUS_OK = "Согласована"


def bb_server_target():
    """Настройки репозитория БЗ у владельца сервера: (target, недостающие настройки)."""
    env = lambda k: os.environ.get(k, "").strip()
    t = {"repo_url": env("REG_BB_REPO_URL"), "project": env("REG_BB_PROJECT"), "repo": env("REG_BB_REPO"),
         "branch": env("REG_BB_BRANCH") or "master", "dir": env("REG_BB_DIR").strip("/") or "KB"}
    return t, [k for k, f in BB_NEED if not t[f]]


def bb_dir_ok(d):
    return (isinstance(d, str) and 0 < len(d) <= 200 and d == d.strip()
            and all(BB_DIR_SEG.fullmatch(s) and s == s.strip() for s in d.split("/")))


def kb_node_ok(path):
    """Путь узла онтологии (каждый узел — папка): валидные сегменты и предел глубины."""
    return bb_dir_ok(path) and path.count("/") + 1 <= config.KB_NODE_DEPTH_MAX


def bb_branch_ok(b):
    return (isinstance(b, str) and bool(BB_BRANCH_RE.fullmatch(b)) and ".." not in b and "//" not in b
            and "/." not in b and "/-" not in b and not b.endswith(("/", ".", ".lock")))


def bb_file_ok(name):
    """Имя файла карточки: только basename .md — без папок, ведущей точки или дефиса и управляющих символов."""
    return (isinstance(name, str) and 3 < len(name) <= 200 and name.endswith(".md") and name == name.strip()
            and not name.startswith((".", "-")) and not BB_FILE_BAD.search(name))


def bb_repo_path_ok(p):
    """Путь файла в репозитории: <папка>/<имя карточки>.md."""
    d, _, f = p.rpartition("/") if isinstance(p, str) else ("", "", "")
    return bb_dir_ok(d) and bb_file_ok(f)


def bb_url_parts(url):
    """(схема, хост, порт) адреса http(s) без логина, запроса и фрагмента; иначе None."""
    if not isinstance(url, str) or len(url) > 500 or not BB_URL_RE.fullmatch(url):
        return None
    u = urlparse(url)
    try:
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return None
    return (u.scheme, u.hostname.lower(), port) if u.hostname else None


def bb_target_problem(t):
    """Что не так с настройками репозитория (пусто — всё в порядке). Хост проверяет сторона сотрудника."""
    if not bb_url_parts(t.get("repo_url")):
        return "REG_BB_REPO_URL — нужен адрес клонирования http(s) без логина, ?запроса и #фрагмента"
    for key, field, ok in (("REG_BB_PROJECT", "project", BB_PROJECT_RE.fullmatch),
                           ("REG_BB_REPO", "repo", BB_REPO_RE.fullmatch),
                           ("REG_BB_BRANCH", "branch", bb_branch_ok), ("REG_BB_DIR", "dir", bb_dir_ok)):
        v = t.get(field)
        if not isinstance(v, str) or not ok(v):
            return f"{key} «{str(v)[:60]}» — недопустимые символы"
    return ""
