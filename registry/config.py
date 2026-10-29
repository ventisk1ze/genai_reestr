"""Все несекретные настройки реестра — в одном месте.

Порядок важен: сначала читается registry.env (REG_ENV_FILE), потом переменные окружения,
потом значения по умолчанию. Логины, пароли и токены в файлах не храним: ключи с
PASSWORD/PASS/USERNAME/LOGIN/SECRET/TOKEN игнорируются с предупреждением.
Секреты (логин/пароль GP, админский PIN, учётка BB) живут только в памяти процесса —
см. registry/creds.py; сюда они не попадают никогда.
"""
import os
import re
import secrets
import sys
import threading
from pathlib import Path

from registry.util import env_num

HERE = Path(__file__).resolve().parent.parent
STATIC_DIR = HERE / "static"
TEMPLATES_DIR = HERE / "templates"
SKILLS_DIR = Path(os.environ.get("REG_SKILLS_DIR", str(HERE / "skills")))
SECRET_KEY = re.compile(r"(PASSWORD|PASSWD|PASS|USERNAME|LOGIN|SECRET|TOKEN)", re.I)


def load_env_file(path):
    """registry.env: KEY=VALUE, только несекретное. Окружение важнее файла; логины/пароли/токены игнорируются."""
    if not path.is_file():
        return
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.removeprefix("export ").partition("=")
        k, v = k.strip(), v.strip().strip('"\'')
        if SECRET_KEY.search(k):
            print(f"registry.env: {k} пропущен — логины, пароли и токены в файлах не храним", file=sys.stderr)
            continue
        os.environ.setdefault(k, v)


_env_file = os.environ.get("REG_ENV_FILE")   # нет — registry.env рядом с app.py; "" — не читать (тесты); путь — этот файл
if _env_file != "":
    load_env_file(Path(_env_file) if _env_file else HERE / "registry.env")
os.environ.setdefault(   # шлюз моделей DataLab Pro (из инструкции платформы); registry.env или окружение важнее
    "GIGACHAT_API_URL", "http://live-query-direct-ci05293545-live-query.apps.prom-terra000257-ias.ocp.ca.sbrf.ru/v1/gc")

# ---------------------------------------------------------------- общие
KINDS = ("once", "regular")                     # разовые и регулярные задачи
TOKEN = secrets.token_urlsafe(24)               # ключ API страницы: браузер получает его с HTML
LOCK = threading.RLock()                        # одна запись в хранилище и историю лава-болла за раз
TEAM_PORTS = range(39235, 39247)                # DataLab Pro: порты, доступные команде как jlab-<табельный>:<порт>
REG_PORT = int(os.environ.get("REG_PORT", "39246"))
REG_SERVER = os.environ.get("REG_SERVER", f"http://127.0.0.1:{REG_PORT}")   # куда ходят команды GigaCode

# ---------------------------------------------------------------- хранилище
STORAGE = os.environ.get("REG_STORAGE", "json").strip().lower()
JSON_DIR = Path(os.environ.get("REG_JSON_DIR", str(HERE / "data")))
SQLITE_PATH = os.environ.get("REG_DB_PATH", str(HERE / "registry.db"))
TABLE_PREFIX = os.environ.get("REG_TABLE_PREFIX", "ck_registry_")
GP = {
    "host": os.environ.get("GP_HOST", ""),
    "port": int(os.environ.get("GP_PORT", "5432")),
    "database": os.environ.get("GP_DATABASE", ""),
    "schema": os.environ.get("GP_SCHEMA", ""),
}
GP_TIMEOUT = int(os.environ.get("GP_TIMEOUT", "60"))
GP_DISTRIBUTED = os.environ.get("REG_PG_PLAIN", "") != "1"   # REG_PG_PLAIN=1 — проверка на обычном PostgreSQL
# Логин/пароль GP сюда не входят: их передаёт start.sh через канал на stdin (registry/creds.py),
# потому что окружение процесса видно в /proc/<pid>/environ.

DEFAULT_CONFIG = {
    "_v": 1,
    "ck_name": "ЦК GenAI",
    "roles": {"head": "", "deputy": "", "bz_owner": "", "bz_metrics": ""},
    "people": [],
    "blocks": [
        {"code": "БЗ-01", "name": "БЗ ЦК GenAI: наполнение"},
        {"code": "БЗ-02", "name": "БЗ ЦК GenAI: контроль метрик"},
        {"code": "KM-01", "name": "KM СВА: ИЗ - МУЗ"},
        {"code": "KM-02", "name": "KM СВА: ИЗ - контроль метрик БЗ СВА"},
        {"code": "KM-03", "name": "KM СВА: ИЗ - организация процесса по сбору и анализу ОС"},
        {"code": "RET-01", "name": "АС Ретривер: КО - Планирование и коммуникации"},
        {"code": "RET-02", "name": "АС Ретривер: КО - БТ"},
        {"code": "RET-03", "name": "АС Ретривер: КО - Тестирование релизов"},
        {"code": "DS-01", "name": "DS Community: Управление + БЗ + дайджест"},
        {"code": "DS-02", "name": "DS Community: Дайджест"},
        {"code": "RND-01", "name": "RnD: Поиск в АС Ретривер - чат с ИИ + обычный"},
        {"code": "RND-02", "name": "RnD: Поиск в АС Ретривер - обычный поиск"},
        {"code": "RND-03", "name": "RnD: другое (LLM-Wiki, дообучение LoRa и др.)"},
    ],
    "matrix": {},
    "events": ["АрхСовет", "DS Community", "BL"],
    "regularities": ["ежедневно", "еженедельно", "1 раз / 2 недели", "1 раз / месяц", "1 раз / квартал"],
}

TASK_DEFAULTS = {
    "status": "Новая", "created": "", "deadline": "", "deadline_initial": "", "title": "", "description": "",
    "block": "", "block_note": "", "level": "", "teamlead": "", "executor": "", "substitute": "", "materials": "",
    "regularity": "", "result": "", "result_desc": "", "report_event": "", "report_date": "", "done_date": "",
    "bz_save": "", "bz_reason": "", "bz_notified": "", "bz_card": "", "team": "",
    "iterations": [], "feedback": [], "cycles": [], "cards": [], "history": [],
    "bb": [],   # выгрузки карточек в BB (T-010): [{ts, by, pr_url, pr_id, branch, files}] — пишет только сервер
}

# ---------------------------------------------------------------- ИИ-ассистент: скиллы и контекст
# Скиллы — один источник для веба и GigaCode: <REG_SKILLS_DIR>/<имя>/SKILL.md (frontmatter + текст).
DEFAULT_SKILL = os.environ.get("REG_DEFAULT_SKILL", "postanovka")                     # у лида, зама и админа
DEFAULT_SKILL_EMPLOYEE = os.environ.get("REG_DEFAULT_SKILL_EMPLOYEE", "rezultat")   # у сотрудника
SKILL_ROLES = {"teamlead": "тимлидов, их заместителей и администратора", "admin": "администратора",
               "employee": "сотрудников"}
CTX_TASKS_MAX = 50       # задач пишущего в контексте модели
CTX_CLOSED_DAYS = 30     # принятые задачи — только недавние

# ---------------------------------------------------------------- ИИ-ассистент: шлюз модели (API)
LLM_MODEL = os.environ.get("GIGACHAT_MODEL", "Qwen3.5-397b")
LLM_TOKEN = os.environ.pop("GIGACHAT_API_TOKEN", "")   # на DataLab Pro не нужен; если шлюз потребует — Bearer
LLM_TIMEOUT = int(os.environ.get("GIGACHAT_TIMEOUT", "600"))
LLM_MAX_TOKENS = int(os.environ.get("GIGACHAT_MAX_TOKENS", "4000"))
LLM_TEMPERATURE = float(os.environ.get("GIGACHAT_TEMPERATURE", "0.3"))
LLM_VERIFY_SSL = os.environ.get("GIGACHAT_VERIFY_SSL", "true").lower() not in {"0", "false", "no"}
LLM_RETRY = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}

# ---------------------------------------------------------------- ИИ-ассистент: GigaCode в лава-болле
# `gigacode -p` в неинтерактивном режиме, история диалога — через stdin.
# Режим подтверждений default: в неинтерактивном режиме подтверждать некому, поэтому shell-команды и правки
# файлов агенту недоступны — он читает и отвечает. auto-edit (агент сам правит файлы реестра) запрещён.
GIGACODE_BIN = os.environ.get("REG_GIGACODE_BIN", "gigacode")
GIGACODE_MODEL = os.environ.get("REG_GIGACODE_MODEL", "")
GIGACODE_APPROVAL = os.environ.get("REG_GIGACODE_APPROVAL", "default")
GIGACODE_TIMEOUT = int(os.environ.get("REG_GIGACODE_TIMEOUT", "600"))
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# ---------------------------------------------------------------- файлы в лава-болле (T-009)
# .docx и .txt, текст извлекает сервер в памяти — на диск файлы не пишутся.
# Лимиты применяет сервер, клиенту не доверяет: размер файла, текст одного файла, текст всех файлов диалога.
AI_FILE_MAX_MB = env_num("REG_AI_FILE_MAX_MB", 10.0, float)
AI_FILE_MAX_BYTES = int(AI_FILE_MAX_MB * 1024 * 1024)
AI_FILE_MAX_CHARS = env_num("REG_AI_FILE_MAX_CHARS", 50000)
AI_FILES_MAX_CHARS = env_num("REG_AI_FILES_MAX_CHARS", 100000)
AI_FILES_PER_MSG = 5
AI_FILE_TYPES = (".docx", ".txt")
AI_FILE_BODY_MAX = (AI_FILE_MAX_BYTES + 2) // 3 * 4 + 64 * 1024   # тело POST /api/ai/file: base64 файла + JSON

# ---------------------------------------------------------------- выгрузка карточек в BB (T-010)
# Настройки несекретные, в registry.env: у владельца — репозиторий (REG_BB_REPO_URL, REG_BB_PROJECT,
# REG_BB_REPO, REG_BB_BRANCH, REG_BB_DIR), у сотрудника — свой MCP-сервер и Bitbucket (REG_BB_MCP,
# REG_BB_MCP_PYTHON, REG_BB_BASE_URL, REG_BB_TIMEOUT). Логин и пароль BB — только в памяти прокси.
BB_MCP = os.path.expanduser(os.environ.get("REG_BB_MCP", "").strip())          # путь к server.py MCP-сервера BB
BB_MCP_PYTHON = os.path.expanduser(os.environ.get("REG_BB_MCP_PYTHON", "").strip()) or "python3"
BB_BASE_URL = os.environ.get("REG_BB_BASE_URL", "").strip().rstrip("/") or "https://df-bitbucket.ca.sbrf.ru"
BB_TIMEOUT = env_num("REG_BB_TIMEOUT", 600)   # общий предел одной выгрузки, с

# ---------------------------------------------------------------- базы знаний (T-012)
# Одна база знаний — git-репозиторий с онтологией (ontology.yml), глоссарием и FAQ. Адрес и ветка —
# у владельца; узлы онтологии — папки для карточек. Онтологию парсит MCP-сервер (pyyaml у него есть),
# реестр получает готовое дерево JSON. Постоянный клон — у сотрудника (REG_KB_CLONE).
KB_REPO_URL = os.environ.get("REG_KB_REPO_URL", "").strip()   # адрес клонирования единственной БЗ
KB_BRANCH = os.environ.get("REG_KB_BRANCH", "master").strip() or "master"
KB_CLONE = os.path.expanduser(os.environ.get("REG_KB_CLONE", "~/kb").strip() or "~/kb")
KB_NODE_DEPTH_MAX = int(os.environ.get("REG_KB_NODE_DEPTH_MAX", "8"))    # глубина узла онтологии (папок)
