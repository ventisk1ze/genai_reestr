"""Хранилище: JSON-файлы (режим по умолчанию, запись в БД выключена).

config.json, once/0001.json, regular/0001.json, cards/*.md, ai_history.json — в JSON_DIR.
Запись атомарная: tmp-файл + replace, не останется полузаписанного файла.
"""
import json
import re
from pathlib import Path

from registry import config
from registry.errors import BadRequest, Conflict
from registry.util import CFG_STALE, card_entries, card_owner, clone, stale


class JsonStore:
    """Запись в БД выключена: всё в локальных файлах (config.json, once/0001.json, cards/*.md)."""
    name = "json"

    def __init__(self, root):
        self.root = Path(root)

    def describe(self):
        return f"JSON-файлы в {self.root} (запись в БД выключена)"

    def _w(self, p, obj):
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", "utf-8", newline="\n")
        tmp.replace(p)   # атомарная замена — не останется полузаписанного файла

    def _r(self, p):
        return json.loads(p.read_text("utf-8"))

    def _tp(self, kind, tid):
        return self.root / kind / f"{int(tid):04d}.json"

    def _cp(self, path):
        p = (self.root / path).resolve()
        if not str(p).startswith(str((self.root / "cards").resolve())):
            raise BadRequest("Некорректный путь карточки")
        return p

    def init(self):
        for d in (*config.KINDS, "cards"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        if not (self.root / "config.json").exists():
            self._w(self.root / "config.json", config.DEFAULT_CONFIG)

    def get_config(self):
        p = self.root / "config.json"
        return self._r(p) if p.exists() else None

    def save_config(self, cfg, expect_v):
        with config.LOCK:
            cur = self.get_config() or {}
            if cur.get("_v") != expect_v:
                raise Conflict(CFG_STALE)
            cfg["_v"] = int(cur.get("_v") or 0) + 1
            self._w(self.root / "config.json", cfg)
            return cfg

    def list_tasks(self):
        out = []
        for kind in config.KINDS:
            for f in sorted((self.root / kind).glob("*.json")):
                try:
                    t = self._r(f)
                    t["kind"] = kind
                except Exception as e:  # битый файл не роняет интерфейс
                    t = {"kind": kind, "id": f.stem, "_error": str(e), "title": f"Ошибка чтения {f.name}"}
                out.append(t)
        return out

    def get_task(self, kind, tid):
        p = self._tp(kind, tid)
        return self._r(p) if p.exists() else None

    def create_task(self, kind, t, finalize):
        with config.LOCK:
            ids = [int(f.stem) for f in (self.root / kind).glob("*.json") if f.stem.isdigit()]
            t = clone(t)
            t["id"], t["_v"] = max(ids, default=0) + 1, 1
            t = finalize(t, None)
            self._w(self._tp(kind, t["id"]), t)
            return t

    def update_task(self, kind, t, expect_v, finalize):
        with config.LOCK:
            cur = self.get_task(kind, t["id"])
            if cur is None:
                raise Conflict(f"Задачи {t['id']} нет в хранилище — обновите страницу.")
            if cur.get("_v") != expect_v:
                raise stale(cur, kind, t["id"])
            t = clone(t)
            t["_v"] = int(cur.get("_v") or 0) + 1
            t = finalize(t, cur)
            self._w(self._tp(kind, t["id"]), t)
            return t

    def delete_task(self, kind, tid, expect_v):
        with config.LOCK:
            cur = self.get_task(kind, tid)
            if cur is None:
                raise Conflict(f"Задачи {tid} нет в хранилище — обновите страницу.")
            if cur.get("_v") != expect_v:
                raise stale(cur, kind, tid)
            self._tp(kind, tid).unlink()

    def list_cards(self):
        return sorted(f"cards/{p.name}" for p in (self.root / "cards").glob("*.md"))

    def read_card(self, path):
        p = self._cp(path)
        if not p.exists():
            raise BadRequest("Карточка не найдена")
        m = re.match(r"#\s*(.*)\n+([\s\S]*)$", p.read_text("utf-8"))
        return (m.group(1), m.group(2).strip()) if m else ("", p.read_text("utf-8"))

    def save_card(self, kind, tid, path, title, body, old):
        with config.LOCK:
            if old and old != path and self._cp(old).exists():
                self._cp(old).unlink()
            p = self._cp(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# {title}\n\n{body}\n", "utf-8", newline="\n")
            t = self.get_task(kind, tid)
            if t is not None:
                # cards[] — объекты {path, bb?} (T-011); старые строки читаются совместимо
                cards = [c for c in card_entries(t) if c.get("path") != old]
                if not any(c.get("path") == path for c in cards):
                    cards.append({"path": path})
                t["cards"], t["_v"] = cards, int(t.get("_v") or 0) + 1
                self._w(self._tp(kind, tid), t)

    # --- история лава-болла (T-006): один файл на всех, ключ — ФИО; командой копирования не переносится
    def get_ai(self, person):
        p = self.root / "ai_history.json"
        return (self._r(p) if p.exists() else {}).get(person) or {}

    def save_ai(self, person, data):
        with config.LOCK:
            p = self.root / "ai_history.json"
            all_ = self._r(p) if p.exists() else {}
            all_[person] = data
            self._w(p, all_)

    def export_all(self):
        cards = []
        for path in self.list_cards():
            kind, tid = card_owner(path)
            title, body = self.read_card(path)
            cards.append({"path": path, "kind": kind or "once", "id": tid or 0, "title": title, "body": body})
        return {"config": self.get_config(), "tasks": self.list_tasks(), "cards": cards}

    def import_all(self, data):
        with config.LOCK:
            if data.get("config"):
                self._w(self.root / "config.json", data["config"])
            for t in data["tasks"]:
                self._w(self._tp(t["kind"], t["id"]), t)
            for c in data["cards"]:
                p = self._cp(c["path"])
                p.write_text(f"# {c['title']}\n\n{c['body']}\n", "utf-8", newline="\n")
