"""Точка входа: argparse и подкоманды (веб-сервер, ddl, check, copy-to, proxy, команды GigaCode).

Контракт запуска не менялся ни на флаг: start.sh/connect.sh и тесты зовут именно его.
"""
import argparse
import sys

from registry import config, creds
from registry.auth import read_admin_pin_stdin
from registry.cli import cmd_create_task, cmd_ctx, cmd_task, cmd_tasks
from registry.proxy import run_proxy
from registry.store import init_store, make_store
from registry.store.gp_store import gp_ddl


def main():
    ap = argparse.ArgumentParser(description="Реестр задач ЦК")
    ap.add_argument("--storage", default=config.STORAGE, choices=["json", "gp", "sqlite"],
                    help="где хранить реестр (по умолчанию REG_STORAGE или json)")
    ap.add_argument("--port", type=int, default=config.REG_PORT, help="порт сервера (по умолчанию 39246 — доступен команде)")
    ap.add_argument("--local", action="store_true", help="запуск на своём ПК: только localhost, открыть браузер")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--proxy", action="store_true", help=argparse.SUPPRESS)   # совместимость со старым start.sh
    ap.add_argument("--creds-stdin", action="store_true", help="прочитать логин и пароль GP из stdin (две строки)")
    ap.add_argument("--admin-pin-stdin", action="store_true",
                    help="прочитать админский PIN из stdin (строка после логина и пароля GP); без флага админ-вход выключен")
    common = argparse.ArgumentParser(add_help=False)   # --storage можно писать и после подкоманды
    common.add_argument("--storage", default=argparse.SUPPRESS, choices=["json", "gp", "sqlite"])
    common.add_argument("--creds-stdin", action="store_true", default=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("ddl", help="DDL таблиц Greenplum", parents=[common])
    sub.add_parser("check", help="проверить хранилище", parents=[common])
    c = sub.add_parser("copy-to", help="перенести все данные в другое хранилище", parents=[common])
    c.add_argument("target", choices=["json", "gp", "sqlite"])
    c.add_argument("--force", action="store_true", help="перезаписать, даже если в целевом хранилище есть задачи")
    pr = sub.add_parser("proxy", help="открыть реестр коллеги в своём браузере")
    pr.add_argument("target", help="табельный номер коллеги, jlab-<табельный>:<порт> или http://…")
    pr.add_argument("--port", type=int, default=9000, help="порт прокси в своём Jupyter (по умолчанию 9000)")
    pr.add_argument("--target-port", type=int, default=39246, help="порт реестра у коллеги (по умолчанию 39246)")
    pr.add_argument("--bb-creds-stdin", action="store_true",
                    help="прочитать логин и пароль BB из stdin (две строки) — для выгрузки карточек в BB (connect.sh)")
    sub.add_parser("ctx", help="контекст реестра для агента (блоки, люди, роли, дата)")
    t = sub.add_parser("tasks", help="список задач")
    t.add_argument("--status")
    t.add_argument("--person")
    t.add_argument("--kind", choices=config.KINDS)
    t = sub.add_parser("task", help="одна задача целиком")
    t.add_argument("kind", choices=config.KINDS)
    t.add_argument("id")
    t = sub.add_parser("create-task", help="создать задачу из JSON (файл или - для stdin)")
    t.add_argument("file")
    t.add_argument("--by", default="", help="ФИО тимлида или заместителя, от чьего имени создаётся задача (спросит его PIN)")
    t.add_argument("--team", default="", help="id или название команды — если автор руководит несколькими")
    a = ap.parse_args()
    if getattr(a, "creds_stdin", False):
        creds.read_creds_stdin()
    if a.cmd is None and a.admin_pin_stdin:
        read_admin_pin_stdin()
    if a.cmd == "proxy" and a.bb_creds_stdin:
        creds.read_bb_creds_stdin()

    if a.cmd == "ddl":
        print(";\n".join(gp_ddl()) + ";")
        return
    if a.cmd == "check":
        try:
            s = init_store(a.storage)
            print(f"OK: {s.describe()}; задач: {len(s.list_tasks())}, карточек: {len(s.list_cards())}")
        except Exception as e:
            sys.exit(f"Ошибка: {e}")
        return
    if a.cmd == "copy-to":
        if a.target == a.storage:
            sys.exit("Источник и цель совпадают")
        src = init_store(a.storage)
        data = src.export_all()
        if a.target == "gp":
            creds.prompt_gp_credentials()
        dst = make_store(a.target)
        dst.init()
        if dst.list_tasks() and not a.force:
            sys.exit(f"В {dst.describe()} уже есть задачи. Повторите с --force, чтобы перезаписать совпадающие.")
        dst.import_all(data)
        print(f"Перенесено из «{src.describe()}» в «{dst.describe()}»: задач {len(data['tasks'])}, "
              f"карточек {len(data['cards'])}, справочники — {'да' if data.get('config') else 'нет'}.")
        return
    cmds = {"ctx": cmd_ctx, "tasks": cmd_tasks, "task": cmd_task, "create-task": cmd_create_task, "proxy": run_proxy}
    if a.cmd in cmds:
        return cmds[a.cmd](a)
    from registry.server import serve
    serve(a)
