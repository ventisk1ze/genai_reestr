#!/usr/bin/env python3
"""Реестр задач ЦК — веб-интерфейс + ИИ-ассистент со скиллами (DataLab Pro).

Точка входа (её зовут start.sh, connect.sh, команды GigaCode и тесты). Весь код —
в пакете registry/; состав модулей — в docstring registry/__init__.py.

Хранилище — переключатель REG_STORAGE (или --storage):
    json    локальные JSON-файлы в ./data — по умолчанию, запись в БД выключена
    gp      Greenplum (asyncpg, одиночное соединение — как в корпоративном шаблоне)
    sqlite  локальная SQLite — для отладки схемы

Команды:
    python app.py [--storage S] [--port 39246] [--local]   веб-сервер (по умолчанию доступен команде DataLab Pro)
    python app.py proxy <табельный|host:port> [--port 9000] [--bb-creds-stdin]
                                           открыть реестр коллеги в своём браузере; с --bb-creds-stdin прокси
                                           выгружает карточки в BB из своего контейнера (логин и пароль BB — stdin)
    python app.py ddl                      DDL таблиц Greenplum (выполнить самому или отдать DBA)
    python app.py check [--storage gp]     проверить подключение и схему
    python app.py copy-to gp [--storage json] [--force]   перенести все данные в другое хранилище
    python app.py ctx | tasks | task KIND ID | create-task FILE [--by ФИО] [--team КОМАНДА]
                                           команды для GigaCode — работают через сервер реестра (REG_SERVER);
                                           create-task спрашивает PIN тимлида с терминала

Несекретные настройки читаются из registry.env рядом с app.py (переменные окружения важнее файла).

Логины и пароли нигде не хранятся: start.sh спрашивает логин и пароль GP и админский PIN при каждом запуске
и передаёт серверу через канал на stdin (не файл, не окружение); из терминала app.py спросит логин GP сам.
Вход в веб — ФИО и PIN сотрудника (PIN задаёт админ, хранится только хеш с солью); сессии — в памяти сервера.
Зависимости: fastapi, uvicorn, jinja2 (requirements.txt); для REG_STORAGE=gp — asyncpg.
"""
from registry.main import main

if __name__ == "__main__":
    main()
