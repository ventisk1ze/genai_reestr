"""T-003: жизненный цикл со статусом «Согласована» и права по этапам.
Критерии приёмки — docs/backlog.md (T-003), матрица прав — от тимлида, контракт — от dev-t003.
Все данные выдуманные."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import ROOT, Server, frontend_text, teams_for# noqa: E402

# ---- контракт (правится только здесь)
ADMIN_ARGS = ["--admin-pin-stdin"]
ACTION_KEY = "action_code"        # код действия в теле POST /api/task; параметры действия — в "params"
A = {                              # логическое действие теста -> код контракта
    "create": "create", "edit": "edit", "result": "edit",   # правка постановки / результата — одна «edit»
    "take": "take", "review": "to_review", "approve": "approve", "rework": "rework", "complete": "finish",
    "accept": "accept", "reopen": "return", "cycle": "new_cycle", "feedback": "feedback",
}
REOPEN_BZ = {"to": "approved", "comment": "Карточка неполная"}            # Завершена -> Согласована
REOPEN_REWORK = {"to": "rework", "comment": "Переделать вывод", "deadline": "2031-02-01"}
HISTORY_ADMIN = "Правка администратором"
ERR = "error"

NEW, WORK, REVIEW, REWORK, APPROVED, DONE, ACCEPTED = (
    "Новая", "В работе", "На согласовании", "На доработке", "Согласована", "Завершена", "Принята")
ORDER = [NEW, WORK, REVIEW, REWORK, APPROVED, DONE, ACCEPTED]

ADMIN_PIN = "60718293"
LEAD = "Постановкин Олег Тестович"
LEAD2 = "Чужаков Глеб Тестович"
EXE = "Исполнова Мария Тестовна"
SUB = "Заменова Ольга Тестовна"
OUT = "Сторонний Пётр Тестович"
PINS = {LEAD: "31415926", LEAD2: "27182818", EXE: "1618", SUB: "14142", OUT: "17320"}
PEOPLE = [
    {"name": LEAD, "email": "postanovkin@example.test", "role": "teamlead", "team": LEAD, "active": True},
    {"name": LEAD2, "email": "chuzhakov@example.test", "role": "teamlead", "team": LEAD2, "active": True},
    {"name": EXE, "email": "ispolnova@example.test", "role": "employee", "team": LEAD, "active": True},
    {"name": SUB, "email": "zamenova@example.test", "role": "employee", "team": LEAD, "active": True},
    {"name": OUT, "email": "storonniy@example.test", "role": "employee", "team": LEAD2, "active": True},
]

POSTANOVKA = {"title": "Выдуманная задача T-003", "description": "Подготовить тестовый отчёт",
              "teamlead": LEAD, "executor": EXE, "substitute": SUB, "deadline": "2030-12-31"}
RESULT = {"result": "Отчёт", "result_desc": "Выдуманный отчёт готов", "report_event": "", "report_date": ""}
DECISION = {"block": "KM-01", "level": "ЦК", "bz_save": "да", "bz_reason": "Пригодится команде"}
SERVICE = ("history", "updated_by", "_v", "id", "created", "deadline_initial", "iterations", "cycles",
           "feedback", "status")


class Base(unittest.TestCase):
    """Один сервер с админом и пятью людьми на класс."""
    storage = "json"

    @classmethod
    def setUpClass(cls):
        cls.s = Server(cls.storage, args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN])
        try:
            code, r = cls.s.call("POST", "/api/login", {"admin": True, "pin": ADMIN_PIN})
            assert code == 200, r
            cls.adm = r["session"]
            cfg = cls.s.call("GET", "/api/state")[1]["config"]
            cfg["people"] = [dict(p) for p in PEOPLE]
            cfg["teams"] = teams_for(PEOPLE)
            code, r = cls.s.call("POST", "/api/config", {"config": cfg, "expect_v": cfg.get("_v")}, session=cls.adm)
            assert code == 200, r
            cls.sid = {"admin": cls.adm}
            for name, pin in PINS.items():
                code, r = cls.s.call("POST", "/api/admin/pin", {"name": name, "pin": pin}, session=cls.adm)
                assert code == 200, (name, r)
                code, r = cls.s.call("POST", "/api/login", {"name": name, "pin": pin})
                assert code == 200, (name, r)
                cls.sid[name] = r["session"]
        except Exception:
            cls.s.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    # ---- хелперы
    def get(self, t):
        tasks = self.s.call("GET", "/api/state")[1]["tasks"]
        return next(x for x in tasks if x["kind"] == t["kind"] and x["id"] == t["id"])

    def post(self, who, task, op=None, expect_v="cur", **extra):
        body = {"task": task, "action": extra.pop("action", "Тест"), **extra}
        if op:
            body[ACTION_KEY] = A[op]
        if expect_v == "cur":
            expect_v = task.get("_v")
        if expect_v is not None:
            body["expect_v"] = expect_v
        return self.s.call("POST", "/api/task", body, session=self.sid[who])

    def create(self, who=LEAD, kind="once", **fields):
        task = {"kind": kind, "id": None, **POSTANOVKA, **fields}
        if kind == "regular":
            task.setdefault("regularity", "еженедельно")
        code, r = self.post(who, task, "create", expect_v=None)
        self.assertEqual(code, 200, r)
        return r["task"]

    def act(self, who, t, op, ok=True, **changes):
        """Действие над свежей версией задачи; changes — правки полей (+ параметры действия через params=)."""
        params = changes.pop("params", {})
        cur = self.get(t)
        code, r = self.post(who, {**cur, **changes}, op, **({"params": params} if params else {}))
        if ok:
            self.assertEqual(code, 200, (op, who, r))
            return r["task"]
        return code, r

    def to(self, status, kind="once", **fields):
        """Создать задачу и довести её до статуса правильными участниками."""
        t = self.create(kind=kind, **fields)
        path = {NEW: [], WORK: ["take"], REVIEW: ["take", "review"], REWORK: ["take", "review", "rework"],
                APPROVED: ["take", "review", "approve"], DONE: ["take", "review", "approve", "complete"],
                ACCEPTED: ["take", "review", "approve", "complete", "accept"]}[status]
        for op in path:
            t = self.step(t, op)
        self.assertEqual(t["status"], status)
        return t

    def step(self, t, op):
        if op == "take":
            return self.act(EXE, t, "take")
        if op == "review":
            return self.act(EXE, t, "review", **RESULT)
        if op == "approve":
            return self.act(LEAD, t, "approve", **DECISION)
        if op == "rework":
            return self.act(LEAD, t, "rework", params={"comment": "Добавить выводы", "deadline": "2031-01-15"})
        if op == "complete":
            self.card(EXE, t)
            return self.act(EXE, t, "complete")
        if op == "accept":
            return self.act(LEAD, t, "accept")
        raise KeyError(op)

    def card(self, who, t, title="Как сделать тестовый отчёт?"):
        code, r = self.s.call("POST", "/api/card", {"kind": t["kind"], "id": t["id"], "title": title,
                                                    "body": "Выдуманный текст карточки"}, session=self.sid[who])
        return code, r

    def refused(self, who, t, op, code_expected=403, **changes):
        """Действие отклонено с нужным кодом, русский текст, задача не изменилась."""
        before = self.get(t)
        code, r = self.act(who, t, op, ok=False, **changes)
        self.assertEqual(code, code_expected, (op, who, r))
        self.assertRegex(r.get(ERR, ""), "[А-Яа-яЁё]", r)
        after = self.get(t)
        self.assertEqual(after["_v"], before["_v"], (op, who))
        self.assertEqual(after["status"], before["status"])
        return r


# ================================================================ критерий 1: полный путь
class FlowTest(Base):
    def test_full_path_once(self):
        t = self.create()
        self.assertEqual((t["status"], t["teamlead"]), (NEW, LEAD))
        t = self.act(LEAD, t, "edit", description="Уточнённая постановка")
        self.assertEqual(t["description"], "Уточнённая постановка")
        t = self.act(EXE, t, "take")
        self.assertEqual(t["status"], WORK)
        t = self.act(EXE, t, "result", **RESULT)
        self.assertEqual((t["status"], t["result"]), (WORK, RESULT["result"]))
        t = self.act(EXE, t, "review", **RESULT)
        self.assertEqual(t["status"], REVIEW)
        t = self.act(LEAD, t, "approve", **DECISION)
        self.assertEqual(t["status"], APPROVED)
        self.assertEqual(self.card(EXE, t)[0], 200)
        t = self.act(EXE, t, "complete")
        self.assertEqual(t["status"], DONE)
        t = self.act(LEAD, t, "accept")
        self.assertEqual(t["status"], ACCEPTED)
        self.assertEqual([h["by"] for h in t["history"]][-7:], [LEAD, EXE, EXE, EXE, LEAD, EXE, LEAD])

    def test_substitute_is_executor(self):
        t = self.create()
        t = self.act(SUB, t, "take")
        t = self.act(SUB, t, "review", **RESULT)
        t = self.act(LEAD, t, "approve", **DECISION)
        self.assertEqual(self.card(SUB, t)[0], 200)
        t = self.act(SUB, t, "complete")
        self.assertEqual(t["status"], DONE)

    def test_bz_no_skips_approved(self):
        t = self.to(REVIEW)
        t = self.act(LEAD, t, "approve", **{**DECISION, "bz_save": "нет", "bz_reason": "Разовая выдумка"})
        self.assertEqual(t["status"], DONE)
        self.assertEqual(self.act(LEAD, t, "accept")["status"], ACCEPTED)

    def test_rework_and_second_review(self):
        t = self.to(REWORK)
        self.assertEqual(len(t["iterations"]), 1)
        self.assertEqual(t["iterations"][0]["comment"], "Добавить выводы")
        self.assertEqual(t["deadline"], "2031-01-15")
        self.assertEqual(t["deadline_initial"], POSTANOVKA["deadline"])
        t = self.act(EXE, t, "result", result_desc="Добавлены выводы")
        self.assertEqual(t["status"], REWORK)
        t = self.act(EXE, t, "review", result_desc="Добавлены выводы")
        self.assertEqual(t["status"], REVIEW)
        t = self.act(LEAD, t, "approve", **DECISION)
        self.assertEqual(t["status"], APPROVED)

    def test_reopen_from_done_to_bz(self):
        t = self.to(DONE)
        t = self.act(LEAD, t, "reopen", params=REOPEN_BZ)
        self.assertEqual(t["status"], APPROVED)
        t = self.act(EXE, t, "complete")   # карточка уже есть
        self.assertEqual(t["status"], DONE)

    def test_reopen_from_done_to_rework(self):
        t = self.to(DONE)
        t = self.act(LEAD, t, "reopen", params=REOPEN_REWORK)
        self.assertEqual((t["status"], t["deadline"]), (REWORK, "2031-02-01"))
        self.assertEqual(t["iterations"][-1]["comment"], REOPEN_REWORK["comment"])

    def test_reopen_bz_when_bz_not_saved(self):
        t = self.to(REVIEW)
        t = self.act(LEAD, t, "approve", **{**DECISION, "bz_save": "нет"})
        self.refused(LEAD, t, "reopen", code_expected=400, params=REOPEN_BZ)
        self.assertEqual(self.act(LEAD, t, "reopen", params=REOPEN_REWORK)["status"], REWORK)


class SqliteFlowTest(FlowTest):
    storage = "sqlite"


# ================================================================ критерий 2: матрица отказов
# какие действия кому разрешены в каком статусе (кроме админа); всё остальное — 403
EXECS = (EXE, SUB)
ALLOWED = {
    (NEW, "edit"): (LEAD,), (NEW, "take"): EXECS,
    (WORK, "result"): EXECS, (WORK, "review"): EXECS,
    (REWORK, "result"): EXECS, (REWORK, "review"): EXECS,
    (REVIEW, "approve"): (LEAD,), (REVIEW, "rework"): (LEAD,),
    (APPROVED, "complete"): EXECS,
    (DONE, "accept"): (LEAD,), (DONE, "reopen"): (LEAD,),
}
STEP_OPS = ("edit", "take", "result", "review", "approve", "rework", "complete", "accept", "reopen")
# правки и параметры, с которыми действие было бы полным — чтобы отказ был именно по правам, а не 400
OP_ARGS = {
    "edit": {"description": "Чужая правка постановки"},
    "take": {}, "result": {"result_desc": "Чужая правка результата"}, "review": dict(RESULT),
    "approve": dict(DECISION), "rework": {"params": {"comment": "Чужой возврат", "deadline": "2031-03-01"}},
    "complete": {"bz_card": "Размещена в АС Ретривер"}, "accept": {},
    "reopen": {"params": dict(REOPEN_REWORK)},
}
ACTORS = (LEAD, LEAD2, EXE, SUB, OUT)


class MatrixTest(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tasks = {}

    def task_in(self, status):
        if status not in self.tasks:
            self.tasks[status] = self.to(status)
        return self.tasks[status]

    def test_refusals(self):
        for status in ORDER:
            t = self.task_in(status)
            for op in STEP_OPS:
                allowed = ALLOWED.get((status, op), ())
                for who in ACTORS:
                    if who in allowed:
                        continue
                    with self.subTest(status=status, op=op, who=who):
                        self.refused(who, t, op, **{k: (dict(v) if isinstance(v, dict) else v)
                                                    for k, v in OP_ARGS[op].items()})

    def test_accepted_closed_for_everyone(self):
        t = self.task_in(ACCEPTED)
        for who in ACTORS:
            with self.subTest(who=who):
                self.refused(who, t, "edit", title="Правка закрытой")
                self.refused(who, t, "result", result="Правка закрытой")


# ================================================================ критерий 3: поля по этапам
OP_RESULT = {"review": REVIEW, "approve": APPROVED, "complete": DONE, "accept": ACCEPTED, "take": WORK}


class FieldsTest(Base):
    def test_executor_cannot_touch_postanovka(self):
        t = self.to(WORK)
        for k, v in (("description", "Подмена"), ("deadline", "2040-01-01"), ("title", "Подмена"),
                     ("executor", OUT), ("substitute", OUT)):
            with self.subTest(field=k):
                self.refused(EXE, t, "result", **{k: v})
        self.not_applied(EXE, t, "result", "teamlead", LEAD2)   # служебное: 403 или молча не применено

    def test_lead_cannot_edit_postanovka_in_work(self):
        t = self.to(WORK)
        self.refused(LEAD, t, "edit", description="Поздняя правка")

    def test_lead_cannot_touch_result(self):
        t = self.to(WORK)
        self.refused(LEAD, t, "edit", result="Подменённый результат")
        t = self.create()
        self.refused(LEAD, t, "edit", result="Результат заранее")

    def test_executor_cannot_touch_decision_or_bz(self):
        t = self.to(WORK)
        for k, v in (("block", "KM-02"), ("level", "ЦК"), ("bz_save", "нет")):
            with self.subTest(field=k):
                self.refused(EXE, t, "result", **{k: v})

    def not_applied(self, who, t, op, k, v, **extra):
        """Подделка поля не проходит: 403 без смены версии или 200, но значение на сервере не то, что прислали."""
        before = self.get(t)
        code, r = self.act(who, t, op, ok=False, **{k: v, **extra})
        after = self.get(t)
        if code == 200:
            self.assertNotEqual(after[k], v, f"{k} подделан через {op}")
            if k == "history":   # история только дописывается сервером
                self.assertEqual(after["history"][:len(before["history"])], before["history"])
                self.assertEqual(len(after["history"]), len(before["history"]) + 1)
            elif k == "updated_by":
                self.assertEqual(after["updated_by"], who)
            elif k == "status":
                self.assertEqual(after["status"], OP_RESULT.get(op, before["status"]), op)
            elif not (k == "done_date" and op == "accept"):   # accept ставит дату сам
                self.assertEqual(after[k], before[k], f"{k} изменился через {op}")
        else:
            self.assertEqual(code, 403, r)
            self.assertEqual(after["_v"], before["_v"])

    def test_service_fields_not_forged(self):
        forged = {"status": REWORK, "history": [], "updated_by": OUT, "iterations": [{"comment": "x"}],
                  "cycles": [{"status": ACCEPTED}], "feedback": [{"text": "x"}], "created": "2000-01-01",
                  "deadline_initial": "2000-01-01", "teamlead": LEAD2, "done_date": "2000-01-01",
                  "cards": ["cards/999_подделка.md"]}
        cases = ((LEAD, NEW, "edit", {}), (EXE, WORK, "result", {}), (EXE, WORK, "review", RESULT),
                 (LEAD, REVIEW, "approve", DECISION), (EXE, APPROVED, "complete", {"bz_card": "Ссылка"}),
                 (LEAD, DONE, "accept", {}))
        for who, status, op, extra in cases:
            for k, v in forged.items():
                with self.subTest(who=who, op=op, field=k):
                    self.not_applied(who, self.to(status), op, k, v, **extra)

    def test_create_cannot_forge_status_or_history(self):
        t = self.create(status=ACCEPTED, history=[{"by": OUT, "action": "Подделка"}], result="Готово заранее")
        self.assertEqual(t["status"], NEW)
        self.assertNotIn(OUT, str(t["history"]))


# ================================================================ критерий 4: обязательные поля
class RequiredTest(Base):
    def missing(self, who, t, op, **changes):
        self.refused(who, t, op, code_expected=400, **changes)

    def test_review_needs_result(self):
        t = self.to(WORK)
        for k in ("result", "result_desc"):
            with self.subTest(field=k):
                self.missing(EXE, t, "review", **{**RESULT, k: ""})

    def test_approve_needs_decision(self):
        t = self.to(REVIEW)
        for k in ("block", "level", "bz_save", "bz_reason"):
            with self.subTest(field=k):
                self.missing(LEAD, t, "approve", **{**DECISION, k: ""})

    def test_rework_needs_comment_and_deadline(self):
        t = self.to(REVIEW)
        for params in ({"comment": "", "deadline": "2031-01-01"}, {"comment": "Доработать", "deadline": ""}, {}):
            with self.subTest(params=params):
                self.missing(LEAD, t, "rework", params=params)

    def test_complete_needs_card(self):
        t = self.to(APPROVED)
        self.missing(EXE, t, "complete", bz_card="")
        t = self.act(EXE, t, "complete", bz_card="Размещена в АС Ретривер: выдуманная ссылка")
        self.assertEqual(t["status"], DONE)


# ================================================================ критерий 5: карточки БЗ
class CardsTest(Base):
    def test_executors_in_approved(self):
        t = self.to(APPROVED)
        self.assertEqual(self.card(EXE, t, "Вопрос исполнителя?")[0], 200)
        self.assertEqual(self.card(SUB, t, "Вопрос замещающего?")[0], 200)
        self.assertEqual(self.card("admin", t, "Вопрос админа?")[0], 200)

    def test_others_in_approved(self):
        t = self.to(APPROVED)
        for who in (LEAD, LEAD2, OUT):
            with self.subTest(who=who):
                code, r = self.card(who, t, f"Чужая карточка {who}?")
                self.assertEqual(code, 403, r)
                self.assertRegex(r.get(ERR, ""), "[А-Яа-я]")

    def test_executor_other_statuses(self):
        for status in (NEW, WORK, REVIEW, REWORK, DONE, ACCEPTED):
            t = self.to(status)
            with self.subTest(status=status):
                self.assertEqual(self.card(EXE, t)[0], 403)
                self.assertEqual(self.card("admin", t, "Админская карточка?")[0], 200)


# ================================================================ критерий 6: удаление
class DeleteTest(Base):
    def delete(self, who, t):
        cur = self.get(t)
        return self.s.call("DELETE", "/api/task", {"kind": t["kind"], "id": t["id"], "expect_v": cur["_v"]},
                           session=self.sid[who])

    def test_only_admin_deletes(self):
        t = self.create()
        for who in ACTORS:
            with self.subTest(who=who):
                code, r = self.delete(who, t)
                self.assertEqual(code, 403, r)
                self.assertRegex(r.get(ERR, ""), "[А-Яа-я]")
        self.assertEqual(self.delete("admin", t)[0], 200)
        tasks = self.s.call("GET", "/api/state")[1]["tasks"]
        self.assertFalse([x for x in tasks if x["kind"] == t["kind"] and x["id"] == t["id"]])


# ================================================================ критерий 7: админ и создание
class AdminCreateTest(Base):
    def test_employee_cannot_create(self):
        for who in (EXE, SUB, OUT):
            with self.subTest(who=who):
                code, r = self.post(who, {"kind": "once", "id": None, **POSTANOVKA}, "create", expect_v=None)
                self.assertEqual(code, 403, r)
                self.assertRegex(r.get(ERR, ""), "[А-Яа-я]")

    def test_lead_is_forced_teamlead(self):
        t = self.create(LEAD2, teamlead=LEAD)
        self.assertEqual(t["teamlead"], LEAD2)

    def test_admin_creates_for_any_lead(self):
        t = self.create("admin", teamlead=LEAD2)
        self.assertEqual(t["teamlead"], LEAD2)

    def test_admin_edits_anything_in_any_status(self):
        for status in (NEW, WORK, APPROVED, ACCEPTED):
            t = self.to(status)
            with self.subTest(status=status):
                cur = self.get(t)
                code, r = self.post("admin", {**cur, "title": "Админ поправил", "result": "Админский",
                                              "deadline": "2032-01-01", "status": REWORK},
                                    action="Правка: всё")
                self.assertEqual(code, 200, r)
                t2 = r["task"]
                self.assertEqual((t2["title"], t2["result"], t2["status"]), ("Админ поправил", "Админский", REWORK))
                self.assertTrue(t2["history"][-1]["action"].startswith(HISTORY_ADMIN), t2["history"][-1])


# ================================================================ критерий 8: регулярные
class RegularTest(Base):
    def test_new_cycle_only_lead_from_accepted(self):
        t = self.to(DONE, kind="regular")
        p = {"params": {"deadline": "2031-06-30"}}
        self.refused(LEAD, t, "cycle", **p)
        t = self.act(LEAD, t, "accept")
        for who in (LEAD2, EXE, SUB, OUT):
            with self.subTest(who=who):
                self.refused(who, t, "cycle", params={"deadline": "2031-06-30"})
        t = self.act(LEAD, t, "cycle", params={"deadline": "2031-06-30"})
        self.assertEqual((t["status"], t["deadline"], t["result"]), (NEW, "2031-06-30", ""))
        self.assertEqual(len(t["cycles"]), 1)

    def test_cycle_refused_in_other_statuses(self):
        for status in (NEW, WORK, REVIEW, APPROVED):
            t = self.to(status, kind="regular")
            with self.subTest(status=status):
                self.refused(LEAD, t, "cycle", params={"deadline": "2031-06-30"})

    def test_feedback_only_lead(self):
        t = self.to(WORK, kind="regular")
        for who in (LEAD2, EXE, SUB, OUT):
            with self.subTest(who=who):
                self.refused(who, t, "feedback", params={"text": "Чужая оценка"})
        t = self.act(LEAD, t, "feedback", params={"text": "Хорошо, продолжай"})
        self.assertEqual(t["feedback"][-1]["text"], "Хорошо, продолжай")
        self.assertEqual(t["feedback"][-1]["by"], LEAD)
        self.assertEqual(t["status"], WORK)


# ================================================================ критерий 9: версии + права
class ConflictTest(Base):
    def test_stale_version_409(self):
        t = self.create()
        t1 = self.act(LEAD, t, "edit", description="Первая правка")
        code, r = self.post(LEAD, {**t, "description": "Вторая правка"}, "edit", expect_v=t["_v"])
        self.assertEqual(code, 409, r)
        self.assertEqual(self.get(t)["description"], t1["description"])

    def test_stale_take_409(self):
        t = self.create()
        self.act(EXE, t, "take")
        code, r = self.post(SUB, t, "take", expect_v=t["_v"])
        self.assertIn(code, (403, 409), r)
        self.assertEqual(self.get(t)["status"], WORK)

    def test_parallel_take_one_wins(self):
        import threading
        for _ in range(5):
            t = self.create()
            cur, res = self.get(t), {}
            ths = [threading.Thread(target=lambda w=w: res.__setitem__(w, self.post(w, cur, "take")[0]))
                   for w in EXECS]
            [x.start() for x in ths]
            [x.join() for x in ths]
            self.assertEqual(sorted(res.values()), [200, 409], res)
            after = self.get(t)
            self.assertEqual((after["status"], after["_v"]), (WORK, cur["_v"] + 1))

    def test_stale_and_foreign_never_passes(self):
        t = self.create()
        self.act(LEAD, t, "edit", description="Свежая")
        code, r = self.post(OUT, {**t, "description": "Чужая"}, "edit", expect_v=t["_v"])
        self.assertIn(code, (403, 409), r)


class SqliteConflictTest(ConflictTest):
    storage = "sqlite"


# ================================================================ граничные случаи контракта
class EdgeTest(Base):
    def test_lead_fills_decision_on_review(self):
        t = self.to(REVIEW)
        t = self.act(LEAD, t, "edit", block="KM-02", level="ЦК")
        self.assertEqual((t["status"], t["block"]), (REVIEW, "KM-02"))
        self.refused(EXE, t, "edit", block="KM-01")

    def test_executor_saves_bz_card_in_approved(self):
        t = self.to(APPROVED)
        t = self.act(EXE, t, "edit", bz_card="Размещена в АС Ретривер")
        self.assertEqual((t["status"], t["bz_card"]), (APPROVED, "Размещена в АС Ретривер"))
        self.refused(LEAD, t, "edit", bz_card="Подмена")

    def test_history_text_from_server(self):
        t = self.create()
        cur = self.get(t)
        code, r = self.post(EXE, cur, "take", action="Принята руководителем ЦК")
        self.assertEqual(code, 200, r)
        self.assertEqual(r["task"]["history"][-1]["action"], "Взята в работу")

    def test_bad_codes(self):
        t = self.create()
        cur = self.get(t)
        for code_, task in (("nonsense", cur), ("create", cur), ("take", {**cur, "id": None})):
            with self.subTest(code=code_):
                body = {"task": task, ACTION_KEY: code_, "expect_v": cur["_v"]}
                code, r = self.s.call("POST", "/api/task", body, session=self.sid[EXE])
                self.assertEqual(code, 400, r)
        self.assertEqual(self.get(t)["_v"], cur["_v"])
        self.refused(LEAD, t, "feedback", code_expected=400, params={"text": "Разовой — нельзя"})

    def test_admin_runs_actions_for_anyone(self):
        t = self.create()
        for op, kw in (("take", {}), ("review", RESULT), ("approve", DECISION)):
            t = self.act("admin", t, op, **kw)
            self.assertTrue(t["history"][-1]["action"].startswith(HISTORY_ADMIN), t["history"][-1])
            self.assertEqual(t["history"][-1]["by"] in (LEAD, EXE), False)
        self.assertEqual(t["status"], APPROVED)

    def test_admin_bad_status_400(self):
        t = self.create()
        self.refused("admin", t, "edit", code_expected=400, status="Несуществующий")

    def test_lead_as_own_executor(self):
        t = self.create(executor=LEAD, substitute="")
        t = self.act(LEAD, t, "take")
        t = self.act(LEAD, t, "review", **RESULT)
        t = self.act(LEAD, t, "approve", **{**DECISION, "bz_save": "нет"})
        self.assertEqual(t["status"], DONE)

    def test_card_of_other_task(self):
        a, b = self.to(APPROVED), self.to(APPROVED)
        code, r = self.card(EXE, a, "Карточка задачи A?")
        self.assertEqual(code, 200, r)
        code, r = self.s.call("POST", "/api/card", {"kind": b["kind"], "id": b["id"], "title": "Перенос?",
                                                    "body": "Текст", "path": r["path"]}, session=self.sid[EXE])
        self.assertEqual(code, 403, r)


# ================================================================ критерий 10: интерфейс и письма
class StaticTest(unittest.TestCase):
    html = frontend_text()

    def js_const(self, name):
        import re
        m = re.search(r"const " + name + r"\s*=\s*(.+?);\n", self.html)
        self.assertTrue(m, name)
        return m.group(1)

    def test_status_in_lists(self):
        self.assertIn("'Согласована'", self.js_const("ST"))
        self.assertIn("'Согласована'", self.js_const("RAIL"))
        st = self.js_const("ST")
        self.assertLess(st.index("'На согласовании'"), st.index("'Согласована'"))
        self.assertLess(st.index("'Согласована'"), st.index("'Завершена'"))

    def test_status_color(self):
        import re
        m = re.search(r"'Согласована'\s*:\s*'(--st-[a-z-]+)'", self.js_const("ST_VAR"))
        self.assertTrue(m)
        self.assertRegex(self.html, re.escape(m.group(1)) + r"\s*:")   # переменная цвета определена в CSS

    def test_mail_subject(self):
        self.assertIn("_Согласована", self.html)

    def mail(self, stage):
        """Шаблон письма этапа: (кому, копия, тема) из таблицы шаблонов в index.html."""
        import re
        m = re.search(r"\n\s*" + stage + r"\s*:\s*\{to:\[([^\]]*)\],\s*cc:\[([^\]]*)\],\s*subj:`([^`]*)`",
                      self.html)
        self.assertTrue(m, f"нет шаблона письма {stage}")
        return m.group(1).replace(" ", ""), m.group(2).replace(" ", ""), m.group(3)

    def test_mail_approved_to_executor(self):
        to, cc, subj = self.mail("approved")
        self.assertEqual(to, "t.executor")
        self.assertIn("t.substitute", cc)
        self.assertTrue(subj.endswith("_Согласована"), subj)

    def test_mail_accepted_to_head(self):
        to, cc, subj = self.mail("accepted")
        self.assertEqual(to, "R.head")
        self.assertTrue(subj.endswith("_Принята"), subj)

    def test_mail_done_to_teamlead(self):
        to, cc, subj = self.mail("done")
        self.assertEqual(to, "t.teamlead")
        self.assertTrue(subj.endswith("_Завершена"), subj)

    def test_mail_stage_by_status(self):
        rec = self.js_const("MAIL_REC")
        for status, stage in (("Согласована", "approved"), ("Завершена", "done"), ("Принята", "accepted")):
            self.assertIn(f"'{status}':'{stage}'", rec.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
