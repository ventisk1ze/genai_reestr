"""Выбор и инициализация хранилища: json | sqlite | gp (REG_STORAGE или --storage).

runtime.STORE меняется здесь при запуске — остальные модули читают его через атрибут.
"""
from registry import creds, runtime
from registry.store.gp_store import make_store


def init_store(kind):
    """Создать хранилище, инициализировать схемы/файлы и записать в runtime.STORE."""
    if kind == "gp":
        creds.prompt_gp_credentials()
    runtime.STORE = make_store(kind)
    runtime.STORE.init()
    return runtime.STORE
