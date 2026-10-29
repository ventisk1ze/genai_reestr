"""Карточки знаний: чтение и сохранение (привязаны к задаче, путь — cards/<id>_<название>.md).

Модель карточки (T-011): элемент task.cards[] — объект {path, bb?}, где bb — результат отправки
в базу знаний {ts, by, project, repo, node, file_url, pr_url, pr_id}. Старые записи — строки-пути:
на диске не трогаем, при чтении нормализуем в объекты (card_entries).
"""
from registry import config, runtime
from registry.errors import BadRequest, Forbidden
from registry.tasks import relation
from registry.util import card_entries, card_owner, card_path_for   # card_entries — модель карточки (T-011)

__all__ = ["card_entries", "op_read_card", "op_save_card"]


def op_save_card(body, user):
    """Сохранить карточку знаний: исполнитель или замещающий, когда задача «Согласована» (или админ)."""
    kind, tid = body.get("kind"), body.get("id")
    if kind not in config.KINDS or not str(tid).isdigit():
        raise BadRequest("Карточка должна быть привязана к задаче")
    cur = runtime.STORE.get_task(kind, int(tid))
    if cur is None:
        raise BadRequest(f"Задачи {tid} нет в хранилище — обновите страницу.")
    if not user["admin"] and ("ex" not in relation(user, cur) or cur.get("status") != "Согласована"):
        raise Forbidden("Карточки знаний добавляет исполнитель или замещающий, когда задача «Согласована»")
    title, text = (body.get("title") or "").strip(), (body.get("body") or "").strip()
    if not title or not text:
        raise BadRequest("У карточки нужны название-вопрос и текст")
    old = body.get("path") or ""
    if old and card_owner(old) != (kind, int(tid)):
        raise Forbidden("Эта карточка знаний относится к другой задаче")
    path = card_path_for(kind, int(tid), title)
    runtime.STORE.save_card(kind, int(tid), path, title, text, old)
    return {"path": path}


def op_read_card(path):
    title, body = runtime.STORE.read_card(path)
    return {"path": path, "text": f"# {title}\n\n{body}\n"}
