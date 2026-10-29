"""Greenplum через asyncpg: одно соединение, как в корпоративном шаблоне (single_connection_gp_utils).

asyncpg асинхронный, а операции хранилища синхронные: соединение живёт в своём
event loop в фоновом потоке, вызовы гоняются через run_coroutine_threadsafe.
GP6 не умеет вставку с заменой — вставки с заменой делаются delete+insert в одной транзакции.
"""
import asyncio
import re
import subprocess
import threading

from registry import config, creds
from registry.store.sqlite_store import SqlStore, SqliteDb


def kerberos_refresh():
    """Как в корпоративном шаблоне: нет живого тикета — пробуем продлить (kinit -R). Пароль не нужен."""
    try:
        if subprocess.run(["klist", "-s"], capture_output=True, timeout=10).returncode != 0:
            subprocess.run(["kinit", "-R"], capture_output=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def gp_ddl():
    s, p = config.GP["schema"] or "<GP_SCHEMA>", config.TABLE_PREFIX
    dist = (lambda k: f" DISTRIBUTED BY ({k})") if config.GP_DISTRIBUTED else (lambda k: "")
    return [
        f"CREATE TABLE IF NOT EXISTS {s}.{p}tasks (kind text NOT NULL, id integer NOT NULL, v integer NOT NULL, "
        f"data text NOT NULL, PRIMARY KEY (kind, id)){dist('kind, id')}",
        f"CREATE TABLE IF NOT EXISTS {s}.{p}config (id integer NOT NULL, v integer NOT NULL, data text NOT NULL, "
        f"PRIMARY KEY (id)){dist('id')}",
        f"CREATE TABLE IF NOT EXISTS {s}.{p}cards (path text NOT NULL, task_kind text NOT NULL, task_id integer NOT NULL, "
        f"title text NOT NULL, body text NOT NULL, PRIMARY KEY (path)){dist('path')}",
        f"CREATE TABLE IF NOT EXISTS {s}.{p}ai_history (person text NOT NULL, data text NOT NULL, "
        f"PRIMARY KEY (person)){dist('person')}",
    ]


class GpDb:
    """Greenplum через asyncpg, одно соединение (как single_connection_gp_utils.py).
    asyncpg асинхронный, а сервер — потоковый: соединение живёт в своём event loop в фоновом потоке."""
    name = "gp"

    def __init__(self):
        try:
            import asyncpg
        except ImportError:
            raise RuntimeError("Для REG_STORAGE=gp нужен asyncpg: pip install asyncpg (из зеркала PyPI)")
        miss = [f"GP_{k.upper()}" for k in ("host", "database", "schema") if not config.GP[k]]
        if miss:
            raise RuntimeError("REG_STORAGE=gp: не заданы " + ", ".join(miss) + " (registry.env или export)")
        if not creds.GP_USER:
            raise RuntimeError("REG_STORAGE=gp: нет логина GP — start.sh спрашивает его при каждом запуске")
        self.apg = asyncpg
        self.transient = (asyncpg.exceptions.ConnectionDoesNotExistError, asyncpg.exceptions.InterfaceError,
                          ConnectionError, TimeoutError, OSError)
        self.unique = (asyncpg.exceptions.UniqueViolationError,)
        self.conn = None
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="gp-loop").start()

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(config.GP_TIMEOUT)

    def describe(self):
        return f"Greenplum {config.GP['host']}:{config.GP['port']}/{config.GP['database']}, схема {config.GP['schema']}"

    def table(self, n):
        return f"{config.GP['schema']}.{config.TABLE_PREFIX}{n}"

    def sql(self, s):
        n = iter(range(1, 1000))
        return re.sub(r"\?", lambda m: f"${next(n)}", s)

    def ensure(self):
        if self.conn is not None and not self.conn.is_closed():
            return
        kerberos_refresh()
        # пароль передаётся как есть (без quote_plus — он нужен только внутри DSN-строки)
        self.conn = self._run(self.apg.connect(
            host=config.GP["host"], port=config.GP["port"], database=config.GP["database"], user=creds.GP_USER,
            password=creds.GP_PASS or None, timeout=30, command_timeout=config.GP_TIMEOUT))

    def reset(self):
        c, self.conn = self.conn, None
        if c is not None:
            try:
                self._run(c.close(timeout=5))
            except Exception:
                pass

    def begin(self):
        self._run(self.conn.execute("BEGIN"))

    def commit(self):
        self._run(self.conn.execute("COMMIT"))

    def rollback_quiet(self):
        try:
            if self.conn is not None and not self.conn.is_closed():
                self._run(self.conn.execute("ROLLBACK"))
        except Exception:
            self.reset()

    def fetch(self, s, p):
        return [dict(r) for r in self._run(self.conn.fetch(s, *p))]

    def execute(self, s, p):
        last = (self._run(self.conn.execute(s, *p)) or "").split()[-1:]
        return int(last[0]) if last and last[0].isdigit() else 0

    def ddl(self):
        return gp_ddl()


def make_store(kind):
    """Хранилище по имени режима: json | sqlite | gp (SqlStore одинаков для обоих SQL)."""
    from registry.store.json_store import JsonStore

    if kind == "json":
        return JsonStore(config.JSON_DIR)
    if kind == "sqlite":
        return SqlStore(SqliteDb(config.SQLITE_PATH))
    if kind == "gp":
        return SqlStore(GpDb())
    raise RuntimeError(f"REG_STORAGE={kind}: допустимо json, gp или sqlite")
