"""Схемы тел запросов API (pydantic) — декларация контракта /api/* в одном месте.

ВАЖНО: это документация и типизация контракта, а не валидация на входе. Рантайм-проверки
остаются в операциях (registry/tasks.py и др.) с прежними русскими текстами ошибок и кодами —
pydantic-коэрция типов и 422-ответы сломали бы контракт, на котором сидят фронтенд,
команды GigaCode и тесты. Поля — Optional с None по умолчанию: ровно семантика body.get().
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class LoginBody(BaseModel):
    """POST /api/login — вход по ФИО и PIN (или админский вход)."""
    name: Optional[str] = None
    pin: Optional[str] = None
    admin: Optional[bool] = None


class LogoutBody(BaseModel):
    """POST /api/logout — выход (закрывает сессию)."""
    pass


class WhoamiReply(BaseModel):
    """GET /api/whoami — пользователь текущей сессии (или null без входа)."""
    user: Optional[Dict[str, Any]] = None


class TaskBody(BaseModel):
    """POST /api/task — сохранить задачу: постановка (create), правка (edit) или действие этапа."""
    task: Dict[str, Any] = {}
    expect_v: Optional[int] = None
    action_code: Optional[str] = None
    action: Optional[str] = None
    params: Optional[Dict[str, Any]] = None


class DeleteTaskBody(BaseModel):
    """DELETE /api/task — удалить задачу (только админ)."""
    kind: Optional[str] = None
    id: Optional[Any] = None
    expect_v: Optional[int] = None


class ConfigBody(BaseModel):
    """POST /api/config — сохранить справочники (только админ)."""
    config: Optional[Dict[str, Any]] = None
    expect_v: Optional[int] = None


class AdminPinBody(BaseModel):
    """POST /api/admin/pin — задать или сбросить PIN сотрудника (только админ)."""
    name: Optional[str] = None
    pin: Optional[str] = None
    reset: Optional[bool] = None


class CardSaveBody(BaseModel):
    """POST /api/card — сохранить карточку знаний (исполнитель в «Согласована» или админ)."""
    kind: Optional[str] = None
    id: Optional[Any] = None
    title: Optional[str] = None
    body: Optional[str] = None
    path: Optional[str] = None


class LlmBody(BaseModel):
    """POST /api/llm — запрос к помощнику: диалог, скилл и движок (api | gigacode)."""
    messages: Optional[List[Dict[str, Any]]] = None
    skill: Optional[str] = None
    engine: Optional[str] = None


class AiFileBody(BaseModel):
    """POST /api/ai/file — имя и base64 файла → извлечённый текст (лимиты проверяет сервер)."""
    name: Optional[str] = None
    data: Optional[str] = None


class AiHistoryBody(BaseModel):
    """POST /api/ai/history — сбросить диалог (reset) или историю запросов (clear_queries)."""
    action: Optional[str] = None


class BbDoneBody(BaseModel):
    """POST /api/bb/done — прокси сообщает результат выгрузки: PR, ветка, файлы."""
    kind: Optional[str] = None
    id: Optional[Any] = None
    pr_url: Optional[str] = None
    pr_id: Optional[Any] = None
    branch: Optional[str] = None
    files: Optional[List[str]] = None


class KbUploadBody(BaseModel):
    """POST /api/kb/upload — отправить карточку в единственную БЗ в папку узла онтологии."""
    kind: Optional[str] = None
    id: Optional[Any] = None
    path: Optional[str] = None
    node: Optional[str] = None


class KbDoneBody(BaseModel):
    """POST /api/kb/done — прокси сообщает результат: карточка, узел, ссылки на файл и PR."""
    kind: Optional[str] = None
    id: Optional[Any] = None
    path: Optional[str] = None
    node: Optional[str] = None
    file_url: Optional[str] = None
    pr_url: Optional[str] = None
    pr_id: Optional[Any] = None
