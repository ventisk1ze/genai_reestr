"""SQL-хранилище: SQLite локально и Greenplum в проде — одна модель поверх обоих.

Строка = документ задачи (data — JSON), версия v — для конкурентной правки.
Проверка версии атомарная (UPDATE ... WHERE v = ?), поэтому несколько серверов
тимлидов на одной GP безопасны. SqliteDb/GpDb — тонкая прослойка над соединением,
SqlStore — сама модель (общая для обоих).
"""
import json
import sqlite3
import time

from registry import config
from registry.errors import BadRequest, Conflict
from registry.util import CFG_STALE, card_entries, clone, dumps, stale


class SqliteDb:
    name = "sqlite"
    transient = ()                         # локальный файл: переподключаться незачем
    unique = (sqlite3.IntegrityError,)

    def __init__(self, path):
        self.path, self.conn = path, None

    def describe(self):
        return f"SQLite {self.path}"

    def table(self, n):
        return config.TABLE_PREFIX + n

    def sql(self, s):
        return s

    def ensure(self):
        if self.conn is None:
            self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
            self.conn.row_factory = sqlite3.Row

    def reset(self):
        if self.conn is not None:
            self.conn.close()
        self.conn = None

    def begin(self):
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.conn.execute("COMMIT")

    def rollback_quiet(self):
        try:
            self.conn.execute("ROLLBACK")
        except Exception:
            pass

    def fetch(self, s, p):
        return [dict(r) for r in self.conn.execute(s, p).fetchall()]

    def execute(self, s, p):
        return self.conn.execute(s, p).rowcount

    def ddl(self):
        t = self.table
        return [
            f"CREATE TABLE IF NOT EXISTS {t('tasks')} (kind TEXT NOT NULL, id INTEGER NOT NULL, "
            f"v INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY (kind, id))",
            f"CREATE TABLE IF NOT EXISTS {t('config')} (id INTEGER PRIMARY KEY, v INTEGER NOT NULL, data TEXT NOT NULL)",
            f"CREATE TABLE IF NOT EXISTS {t('cards')} (path TEXT PRIMARY KEY, task_kind TEXT NOT NULL, "
            f"task_id INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL)",
            f"CREATE TABLE IF NOT EXISTS {t('ai_history')} (person TEXT PRIMARY KEY, data TEXT NOT NULL)",
        ]


class SqlStore:
    """Та же модель, что у JSON: строка = документ задачи (data — JSON), версия v — для конкурентной правки.
    Проверка версии атомарная (UPDATE ... WHERE v = ?), поэтому несколько серверов тимлидов на одной GP безопасны."""

    def __init__(self, db):
        self.db, self.name = db, db.name

    def describe(self):
        return self.db.describe()

    def t(self, n):
        return self.db.table(n)

    def q(self, sql, *p):
        return self.db.fetch(self.db.sql(sql), p)

    def x(self, sql, *p):
        return self.db.execute(self.db.sql(sql), p)

    def tx(self, fn):
        with config.LOCK:
            for attempt in (1, 2):
                try:
                    self.db.ensure()
                    self.db.begin()
                except self.db.transient:
                    self.db.reset()
                    if attempt == 2:
                        raise
                    continue
                try:
                    res = fn()
                    self.db.commit()
                    return res
                except self.db.transient:   # соединение порвалось — сервер сам откатит, повторяем один раз
                    self.db.reset()
                    if attempt == 2:
                        raise
                except BaseException:
                    self.db.rollback_quiet()
                    raise

    def init(self):
        def fn():
            for s in self.db.ddl():
                self.x(s)
            if not self.q(f"SELECT id FROM {self.t('config')} WHERE id = 1"):
                cfg = clone(config.DEFAULT_CONFIG)
                self.x(f"INSERT INTO {self.t('config')} (id, v, data) VALUES (1, ?, ?)", cfg["_v"], dumps(cfg))
        try:
            self.tx(fn)
        except Exception as e:
            if self.name == "gp" and any(w in type(e).__name__ for w in ("Password", "Authorization", "Authentication")):
                raise RuntimeError(f"Greenplum не принял логин/пароль ({e}). Проверьте их или Kerberos-тикет (klist).") from e
            if self.name == "gp" and isinstance(e, (OSError, TimeoutError)):
                raise RuntimeError(f"Greenplum {config.GP['host']}:{config.GP['port']} недоступен ({e}). "
                                   "Проверьте GP_HOST/GP_PORT в registry.env и сетевой доступ из Jupyter.") from e
            if self.name == "gp":
                raise RuntimeError(f"Не удалось создать или открыть таблицы в схеме {config.GP['schema']}: {e}\n"
                                   "Если нет прав на CREATE — выполните DDL из `python app.py ddl` "
                                   "под учёткой с правами или отдайте его DBA.") from e
            raise

    # --- справочники
    def get_config(self):
        rows = self.tx(lambda: self.q(f"SELECT v, data FROM {self.t('config')} WHERE id = 1"))
        if not rows:
            return None
        cfg = json.loads(rows[0]["data"])
        cfg["_v"] = rows[0]["v"]
        return cfg

    def save_config(self, cfg, expect_v):
        def fn():
            rows = self.q(f"SELECT v FROM {self.t('config')} WHERE id = 1")
            cur_v = rows[0]["v"] if rows else None
            if cur_v != expect_v:
                raise Conflict(CFG_STALE)
            c = clone(cfg)
            c["_v"] = (cur_v or 0) + 1
            if rows:
                if self.x(f"UPDATE {self.t('config')} SET v = ?, data = ? WHERE id = 1 AND v = ?",
                          c["_v"], dumps(c), cur_v) != 1:
                    raise Conflict(CFG_STALE)
            else:
                self.x(f"INSERT INTO {self.t('config')} (id, v, data) VALUES (1, ?, ?)", c["_v"], dumps(c))
            return c
        return self.tx(fn)

    # --- задачи
    def _row(self, r):
        try:
            t = json.loads(r["data"])
        except Exception as e:
            t = {"_error": str(e), "title": "Ошибка чтения записи"}
        t["kind"], t["id"], t["_v"] = r["kind"], r["id"], r["v"]
        return t

    def list_tasks(self):
        return [self._row(r) for r in self.tx(lambda: self.q(
            f"SELECT kind, id, v, data FROM {self.t('tasks')} ORDER BY kind, id"))]

    def get_task(self, kind, tid):
        rows = self.tx(lambda: self.q(f"SELECT kind, id, v, data FROM {self.t('tasks')} WHERE kind = ? AND id = ?",
                                      kind, int(tid)))
        return self._row(rows[0]) if rows else None

    def create_task(self, kind, t, finalize):
        for attempt in range(5):
            def fn():
                m = self.q(f"SELECT COALESCE(MAX(id), 0) AS m FROM {self.t('tasks')} WHERE kind = ?", kind)[0]["m"]
                tt = clone(t)
                tt["id"], tt["_v"] = int(m) + 1, 1
                tt = finalize(tt, None)
                self.x(f"INSERT INTO {self.t('tasks')} (kind, id, v, data) VALUES (?, ?, ?, ?)",
                       kind, tt["id"], 1, dumps(tt))
                return tt
            try:
                return self.tx(fn)
            except self.db.unique:          # другой тимлид занял номер одновременно — берём следующий
                time.sleep(0.2 * (attempt + 1))
        raise Conflict("Не удалось выдать номер задачи — повторите.")

    def update_task(self, kind, t, expect_v, finalize):
        tid = int(t["id"])

        def fn():
            rows = self.q(f"SELECT kind, id, v, data FROM {self.t('tasks')} WHERE kind = ? AND id = ?", kind, tid)
            if not rows:
                raise Conflict(f"Задачи {tid} нет в хранилище — обновите страницу.")
            cur = self._row(rows[0])
            if cur["_v"] != expect_v:
                raise stale(cur, kind, tid)
            tt = clone(t)
            tt["id"], tt["_v"] = tid, cur["_v"] + 1
            tt = finalize(tt, cur)
            if self.x(f"UPDATE {self.t('tasks')} SET v = ?, data = ? WHERE kind = ? AND id = ? AND v = ?",
                      tt["_v"], dumps(tt), kind, tid, cur["_v"]) != 1:
                raise stale(cur, kind, tid)
            return tt
        return self.tx(fn)

    def delete_task(self, kind, tid, expect_v):
        def fn():
            rows = self.q(f"SELECT kind, id, v, data FROM {self.t('tasks')} WHERE kind = ? AND id = ?", kind, int(tid))
            if not rows:
                raise Conflict(f"Задачи {tid} нет в хранилище — обновите страницу.")
            cur = self._row(rows[0])
            if cur["_v"] != expect_v or self.x(
                    f"DELETE FROM {self.t('tasks')} WHERE kind = ? AND id = ? AND v = ?", kind, int(tid), cur["_v"]) != 1:
                raise stale(cur, kind, tid)
        self.tx(fn)

    # --- карточки знаний
    def list_cards(self):
        return [r["path"] for r in self.tx(lambda: self.q(f"SELECT path FROM {self.t('cards')} ORDER BY path"))]

    def read_card(self, path):
        rows = self.tx(lambda: self.q(f"SELECT title, body FROM {self.t('cards')} WHERE path = ?", path))
        if not rows:
            raise BadRequest("Карточка не найдена")
        return rows[0]["title"], rows[0]["body"]

    def save_card(self, kind, tid, path, title, body, old):
        def fn():
            for p in {old, path} - {""}:
                self.x(f"DELETE FROM {self.t('cards')} WHERE path = ?", p)
            self.x(f"INSERT INTO {self.t('cards')} (path, task_kind, task_id, title, body) VALUES (?, ?, ?, ?, ?)",
                   path, kind, int(tid), title, body)
            rows = self.q(f"SELECT kind, id, v, data FROM {self.t('tasks')} WHERE kind = ? AND id = ?", kind, int(tid))
            if rows:
                t = self._row(rows[0])
                # cards[] — объекты {path, bb?} (T-011); старые строки читаются совместимо
                cards = [c for c in card_entries(t) if c.get("path") != old]
                if not any(c.get("path") == path for c in cards):
                    cards.append({"path": path})
                t["cards"], t["_v"] = cards, t["_v"] + 1
                self.x(f"UPDATE {self.t('tasks')} SET v = ?, data = ? WHERE kind = ? AND id = ?",
                       t["_v"], dumps(t), kind, int(tid))
        self.tx(fn)

    # --- история лава-болла (T-006); в GP6 нет вставки с заменой — delete+insert в одной транзакции
    def get_ai(self, person):
        rows = self.tx(lambda: self.q(f"SELECT data FROM {self.t('ai_history')} WHERE person = ?", person))
        return json.loads(rows[0]["data"]) if rows else {}

    def save_ai(self, person, data):
        def fn():
            self.x(f"DELETE FROM {self.t('ai_history')} WHERE person = ?", person)
            self.x(f"INSERT INTO {self.t('ai_history')} (person, data) VALUES (?, ?)", person, dumps(data))
        self.tx(fn)

    # --- перенос (история лава-болла не переносится)
    def export_all(self):
        cards = [{"path": r["path"], "kind": r["task_kind"], "id": r["task_id"], "title": r["title"], "body": r["body"]}
                 for r in self.tx(lambda: self.q(f"SELECT * FROM {self.t('cards')} ORDER BY path"))]
        return {"config": self.get_config(), "tasks": self.list_tasks(), "cards": cards}

    def import_all(self, data):
        def fn():
            if data.get("config"):
                c = data["config"]
                self.x(f"DELETE FROM {self.t('config')} WHERE id = 1")
                self.x(f"INSERT INTO {self.t('config')} (id, v, data) VALUES (1, ?, ?)", int(c.get("_v") or 1), dumps(c))
            for t in data["tasks"]:
                if "_error" in t:
                    continue
                self.x(f"DELETE FROM {self.t('tasks')} WHERE kind = ? AND id = ?", t["kind"], int(t["id"]))
                self.x(f"INSERT INTO {self.t('tasks')} (kind, id, v, data) VALUES (?, ?, ?, ?)",
                       t["kind"], int(t["id"]), int(t.get("_v") or 1), dumps(t))
            for c in data["cards"]:
                self.x(f"DELETE FROM {self.t('cards')} WHERE path = ?", c["path"])
                self.x(f"INSERT INTO {self.t('cards')} (path, task_kind, task_id, title, body) VALUES (?, ?, ?, ?, ?)",
                       c["path"], c["kind"], int(c["id"]), c["title"], c["body"])
        self.tx(fn)
