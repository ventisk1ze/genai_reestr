"""Общие помощники без состояния: время, JSON, имена файлов карточек, окружение, HTTP-утилиты."""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime

from registry.errors import Conflict

# Сообщение о конфликте версий справочника (у задач — свой текст с автором правки, см. stale)
CFG_STALE = "Справочники уже изменил другой тимлид — обновите страницу и повторите."


def now():
    """Метка времени с точностью до секунды — для истории и «изменено»."""
    return datetime.now().isoformat(timespec="seconds")


def dumps(o):
    """JSON без экранирования кириллицы — для ответов и записи в хранилище."""
    return json.dumps(o, ensure_ascii=False)


def clone(o):
    """Глубокая копия через JSON — данные реестра это всегда JSON-структуры."""
    return json.loads(json.dumps(o))


def norm(v):
    """Строка без краёв; None и не-строка → '' (пустое поле). Было _norm в app.py."""
    return (v.strip() if isinstance(v, str) else v) if v is not None else ""


def safe_name(s, limit=80):
    """Имя файла карточки: без запрещённых символов Windows, одним куском, не длиннее limit."""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s).strip().strip(".")
    return re.sub(r"\s+", " ", s)[:limit] or "карточка"


def card_path_for(kind, tid, title):
    """Путь карточки знаний в хранилище: cards/<id или Регid>_<название>.md."""
    return f"cards/{tid if kind == 'once' else f'Рег{tid}'}_{safe_name(title)}.md"


def card_owner(path):
    """(kind, id) задачи, которой принадлежит карточка, по её пути; чужой путь — (None, None)."""
    m = re.match(r"cards/(Рег)?(\d+)_", path)
    return (("regular" if m.group(1) else "once"), int(m.group(2))) if m else (None, None)


def card_entries(t):
    """cards[] задачи, нормализованные: строка (старый формат, до T-011) → {"path": строка}.
    Элемент может нести bb — результат отправки в базу знаний. На диске старые строки не трогаем."""
    out = []
    for c in (t.get("cards") if isinstance(t, dict) else None) or []:
        out.append(dict(c) if isinstance(c, dict) else {"path": str(c)})
    return out


def stale(cur, kind, tid):
    """Конфликт версий задачи: кто и когда изменил её раньше."""
    who = (cur or {}).get("updated_by") or "другой тимлид"
    return Conflict(f"Задачу {tid} уже изменил(а) {who} ({(cur or {}).get('updated', '')}). "
                    "Обновите страницу и повторите.")


def env_num(key, default, cast=int):
    """Положительное число из окружения или registry.env; пусто или ошибка — значение по умолчанию."""
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        v = cast(raw.replace(",", "."))
        if 0 < v < 10 ** 12:
            return v
    except ValueError:
        pass
    print(f"{key}={raw!r}: нужно положительное число — беру {default}", file=sys.stderr)
    return default


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редиректы отдаём клиенту как есть (прокси и запросы к серверу реестра не ходят по редиректам)."""

    def redirect_request(self, *args, **kwargs):
        return None
