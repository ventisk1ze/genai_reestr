"""Секреты процесса — только в памяти, передаются через канал на stdin (не файл, не окружение).

start.sh/connect.sh пишут их в stdin при запуске; из терминала app.py спрашивает сам.
Доступ — через атрибут модуля (creds.GP_USER): значения меняются после импорта.
"""
import base64
import getpass
import json
import re
import sys
import urllib.parse

# Логин и пароль Greenplum: приходят через --creds-stdin (start.sh) или с терминала
GP_USER = ""
GP_PASS = ""

# Логин и пароль BB: только в памяти прокси сотрудника (--bb-creds-stdin, connect.sh)
BB_USER = ""
BB_PASS = ""


def read_creds_stdin():
    """start.sh пишет в канал две строки: логин и пароль. Канал в памяти, на диск ничего не попадает."""
    global GP_USER, GP_PASS
    GP_USER = sys.stdin.readline().rstrip("\n")
    GP_PASS = sys.stdin.readline().rstrip("\n")


def prompt_gp_credentials():
    """python app.py из терминала: спросить логин/пароль GP, если их не передали."""
    global GP_USER, GP_PASS
    if not GP_USER and sys.stdin.isatty():
        GP_USER = input("Логин GP: ").strip()
        GP_PASS = getpass.getpass("Пароль GP (Enter — без пароля, по Kerberos): ")


def read_bb_creds_stdin():
    """connect.sh пишет в канал две строки: логин и пароль BB (пустой логин — выгрузка выключена).
    Только в памяти прокси — не в окружении, не в файлах; из терминала — спросим сами."""
    global BB_USER, BB_PASS
    if sys.stdin is None:
        return
    if sys.stdin.isatty():
        BB_USER = input("Логин BB (Enter — без выгрузки в BB): ").strip()
        BB_PASS = getpass.getpass("Пароль BB: ") if BB_USER else ""
        return
    line = lambda: sys.stdin.buffer.readline().decode("utf-8", "surrogateescape").rstrip("\r\n")
    user, password = line().strip(), line()
    BB_USER, BB_PASS = user, password if user else ""


def bb_mask(text):
    """Логин и пароль BB в любом тексте — звёздочками (в том числе в адресах и URL-кодированные)."""
    s = str(text or "")
    vals = set()
    for v in (BB_USER, BB_PASS):
        if v:
            vals |= {v, urllib.parse.quote(v), urllib.parse.quote(v, safe=""),
                     urllib.parse.quote_plus(v), json.dumps(v)[1:-1]}
    if BB_USER or BB_PASS:
        vals.add(base64.b64encode(f"{BB_USER}:{BB_PASS}".encode("utf-8", "surrogateescape")).decode("ascii"))
    for v in sorted(vals, key=len, reverse=True):
        s = s.replace(v, "***")
    s = re.sub(r"(://)[^/\s@]*@", r"\1***@", s)
    return re.sub(r"(?i)(authorization:\s*(basic|bearer)\s+)\S+", r"\1***", s)
