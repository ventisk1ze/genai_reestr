"""T-009: файлы .docx и .txt в помощнике (лава-болл) — извлечение текста, лимиты, передача модели и GigaCode, история.
Критерии приёмки — docs/backlog.md (T-009). Контракт (от тимлида; уточняемое — в константах ниже):
  POST /api/ai/file {"name", "data": "<base64>"} — только с сессией (иначе 401)
       -> 200 {"name", "text", "chars", "truncated"}; chars — длина всего извлечённого текста, text обрезан до лимита;
       ошибки — 400 {"error": "<по-русски>"}: .doc, .pdf, повреждённый/не docx, пустой текст, больше лимита, плохой base64
  POST /api/llm: у user-сообщений необязательное files: [{"name", "text", "truncated"}]; сервер сам режет каждый файл
       и общий объём; больше 5 файлов в сообщении — 400; пустой content допустим, если есть files;
       модели перед текстом сообщения — блоки файлов с явной разметкой (имя и текст)
  GET /api/ai/history: у user-сообщений с файлами — files (name, text, truncated; не влезшие в общий бюджет —
       без текста и с признаком DROPPED_KEY)
Лимиты: REG_AI_FILE_MAX_MB (10), REG_AI_FILE_MAX_CHARS (50 000), REG_AI_FILES_MAX_CHARS (100 000).
.docx собираются прямо в тесте через zipfile — без бинарных фикстур; данные выдуманные (правило 3).
Модель — мок HTTP-сервер (GIGACHAT_API_URL), GigaCode — фейковый скрипт первым в PATH; процессы — по PID (helpers).
Интерфейс: playwright не установлен — только статические проверки index.html."""
import ast
import base64
import codecs
import io
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import APP, ROOT, Server, backend_text, clean_env, free_port, frontend_css, frontend_text, teams_for# noqa: E402

# ---------------------------------------------------------------- контракт (правится только здесь)
API_FILE = "/api/ai/file"
API_HIST = "/api/ai/history"
ENV_MB, ENV_CHARS, ENV_TOTAL = "REG_AI_FILE_MAX_MB", "REG_AI_FILE_MAX_CHARS", "REG_AI_FILES_MAX_CHARS"
DEF_CHARS, DEF_TOTAL = 50_000, 100_000
MAX_FILES = 5
FILE_LABEL = "Файл «{}»"            # разметка блока файла для модели (критерий 3)
DROPPED_NOTE = "текст не передаётся"  # пометка у файла, не влезшего в общий бюджет (критерий 5)
DROPPED_KEY = "dropped"             # признак такого файла в истории (уточняется у dev)
TRUNC_MARK = re.compile(r"обрез", re.I)   # пометка для модели, что текст файла обрезан
GC_DIALOG = "# Диалог"              # раздел диалога в stdin GigaCode
SLACK = 200                         # допуск, если сервер дописывает пометку об обрезке прямо в text
RU = "[А-Яа-яЁё]"

HTML = frontend_text()
APP_SRC = APP.read_text("utf-8")
README = (ROOT / "README.md").read_text("utf-8")
ENV_EXAMPLE = (ROOT / "registry.env.example").read_text("utf-8")

ADMIN_ARGS = ["--admin-pin-stdin"]
ADMIN_PIN = "58203147"
LEAD = "Файлов Лев Тестович"
EMP = "Вложенцева Ада Тестовна"
EMP2 = "Сторонний Пётр Тестович"
PINS = {LEAD: "47382910", EMP: "5512", EMP2: "66231"}
PEOPLE = [
    {"name": LEAD, "email": "faylov@example.test", "role": "teamlead", "active": True},
    {"name": EMP, "email": "vlozhentseva@example.test", "role": "employee", "team": LEAD, "active": True},
    {"name": EMP2, "email": "storonniy@example.test", "role": "employee", "team": LEAD, "active": True},
]

# ---------------------------------------------------------------- .docx в памяти (минимальный пакет OOXML)
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
CONTENT_TYPES = (XML_DECL + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                 '<Default Extension="xml" ContentType="application/xml"/>'
                 '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
                 'officedocument.wordprocessingml.document.main+xml"/></Types>')
RELS = (XML_DECL + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="word/document.xml"/></Relationships>')
TAB, BR = "<w:tab/>", "<w:br/>"
LI = '<w:pPr><w:pStyle w:val="ListParagraph"/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504          # заголовок старого .doc (OLE2)
PDF = (b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
       b"trailer\n<< /Root 1 0 R >>\n%%EOF\n")


def wdoc(body, prolog="", decl=XML_DECL):
    return f'{decl}{prolog}<w:document xmlns:w="{W_NS}" xmlns:r="{R_NS}"><w:body>{body}</w:body></w:document>'


def wt(text):
    return f'<w:t xml:space="preserve">{escape(text)}</w:t>'


def run(*parts):
    return "<w:r>" + "".join(parts) + "</w:r>"


def par(*runs, ppr=""):
    return f"<w:p>{ppr}{''.join(runs)}</w:p>"


def P(text):
    return par(run(wt(text)))


def table(rows):
    return "<w:tbl><w:tblPr/>" + "".join(
        "<w:tr>" + "".join(f"<w:tc>{P(c)}</w:tc>" for c in row) + "</w:tr>" for row in rows) + "</w:tbl>"


def docx(body=None, document=None, members=None, stored=False):
    """Пакет .docx: [Content_Types].xml, _rels/.rels и word/document.xml (document=None и body=None — без него)."""
    if document is None and body is not None:
        document = wdoc(body)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        if document is not None:
            z.writestr("word/document.xml", document.encode("utf-8") if isinstance(document, str) else document)
        for k, v in (members or {}).items():
            z.writestr(k, v)
    return buf.getvalue()


def bomb_docx(mb, unit=b" "):
    """Zip-бомба: настоящий абзац + mb мегабайт повторов unit (пробелы или пустые абзацы <w:p/>) в document.xml;
    сжатый файл — десятки-сотни КБ (сжатие ~1000:1). Без защиты сервер вернёт 200 с текстом абзаца —
    так бомба и отличается от «пустого текста». Пустые абзацы ещё и раздувают дерево XML в памяти сервера."""
    head, tail = wdoc(P("Выдуманный абзац внутри бомбы") + "@@").encode("utf-8").split(b"@@")
    chunk = unit * ((1 << 20) // len(unit))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        with z.open("word/document.xml", "w") as f:
            f.write(head)
            for _ in range(mb):
                f.write(chunk)
            f.write(tail)
    return buf.getvalue()


def nested_docx(depth):
    """document.xml с вложенностью depth уровней (сжимается в единицы КБ)."""
    return docx(document=wdoc("<w:p>" + "<w:r>" * depth + wt("Глубоко") + "</w:r>" * depth + "</w:p>"))


def b64(data):
    return base64.b64encode(data).decode("ascii")


def filler(n, start, end, word="выдумка "):
    """Текст ровно из n символов: start, повтор word, end (без пробелов по краям)."""
    mid = n - len(start) - len(end)
    assert mid >= 0
    return start + (word * (mid // len(word) + 1))[:mid] + end


def as_text(c):
    return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)


def user_texts(req):
    return [as_text(m.get("content")) for m in req["messages"] if m.get("role") == "user"]


def dialog_text(req):
    """Всё, что ушло модели, кроме системного промпта."""
    return "\n".join(as_text(m.get("content")) for m in req["messages"] if m.get("role") != "system")


def ufile(name, text, truncated=False):
    return {"name": name, "text": text, "truncated": truncated}


def umsg(content, *files):
    m = {"role": "user", "content": content}
    if files:
        m["files"] = list(files)
    return m


def amsg(content="Выдуманный ответ помощника"):
    return {"role": "assistant", "content": content}


# ---------------------------------------------------------------- мок шлюза модели и фейковый GigaCode
class MockModel:
    """Отвечает как /chat/completions и запоминает запросы; fail=True — HTTP 400 (сервер не повторяет)."""

    def __init__(self):
        self.fail = False
        self.requests = []
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                mock.requests.append(json.loads(self.rfile.read(n) or b"{}"))
                if mock.fail:
                    data, code = b'{"error":"bad request (mock)"}', 400
                else:
                    data = json.dumps({"choices": [{"message": {"content": f"Ответ мока Т9 №{len(mock.requests)}"}}]},
                                      ensure_ascii=False).encode("utf-8")
                    code = 200
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


FAKE_GC = """#!{py}
import json, sys
data = sys.stdin.read()
with open({out!r}, "a", encoding="utf-8") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": data}}, ensure_ascii=False) + "\\n")
print("Фейковый ответ GigaCode Т9")
"""


def setup_people(s):
    code, r = s.call("POST", "/api/login", {"admin": True, "pin": ADMIN_PIN})
    assert code == 200, r
    adm = r["session"]
    cfg = s.call("GET", "/api/state")[1]["config"]
    cfg["people"] = [dict(p) for p in PEOPLE]
    cfg["teams"] = teams_for(PEOPLE)
    code, r = s.call("POST", "/api/config", {"config": cfg, "expect_v": cfg.get("_v")}, session=adm)
    assert code == 200, r
    for name, pin in PINS.items():
        code, r = s.call("POST", "/api/admin/pin", {"name": name, "pin": pin}, session=adm)
        assert code == 200, (name, r)
    return login_all(s)


def login_all(s):
    code, r = s.call("POST", "/api/login", {"admin": True, "pin": ADMIN_PIN})
    assert code == 200, r
    sid = {"admin": r["session"]}
    for name, pin in PINS.items():
        code, r = s.call("POST", "/api/login", {"name": name, "pin": pin})
        assert code == 200, (name, r)
        sid[name] = r["session"]
    return sid


def files_under(*dirs):
    """Файлы в папках (кроме журнала сервера) — путь -> содержимое."""
    out = {}
    for d in dirs:
        for p in Path(d).rglob("*"):
            if p.is_file() and not p.name.startswith("server-"):
                out[str(p)] = p.read_bytes()
    return out


class Base(unittest.TestCase):
    """Сервер подпроцессом: мок модели, фейковый gigacode, TMPDIR сервера — своя пустая папка."""
    storage = "json"
    env = {}

    @classmethod
    def setUpClass(cls):
        cls.mock = MockModel()
        cls.work = tempfile.TemporaryDirectory()
        cls.srv_tmp = tempfile.TemporaryDirectory()
        work = Path(cls.work.name)
        cls.gc_calls = work / "gc-calls.jsonl"
        exe = work / "bin" / "gigacode"
        exe.parent.mkdir()
        exe.write_text(FAKE_GC.format(py=sys.executable, out=str(cls.gc_calls)), "utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        cls.tmpdir = work / "tmp"
        cls.tmpdir.mkdir()
        env = {"GIGACHAT_API_URL": cls.mock.url, "REG_GIGACODE_BIN": str(exe), "TMPDIR": str(cls.tmpdir),
               "PATH": str(exe.parent) + os.pathsep + os.environ.get("PATH", ""), **cls.env}
        try:
            cls.s = Server(cls.storage, args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN], env=env, tmp=cls.srv_tmp)
        except Exception:
            cls.mock.stop()
            cls.work.cleanup()
            cls.srv_tmp.cleanup()
            raise
        try:
            cls.sid = setup_people(cls.s)
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()
        cls.mock.stop()
        cls.work.cleanup()
        cls.srv_tmp.cleanup()

    def setUp(self):
        self.mock.fail = False
        for who in (EMP, EMP2, LEAD, "admin"):   # каждый тест — с чистой историей
            for action in ("reset", "clear_queries"):
                code, r = self.s.call("POST", API_HIST, {"action": action}, session=self.sid[who])
                self.assertEqual(code, 200, r)

    # ---- /api/ai/file
    def upload(self, name, data=b"", who=EMP, raw=None):
        body = {"name": name, "data": raw if raw is not None else b64(data)}
        try:
            return self.s.call("POST", API_FILE, body, session=self.sid[who])
        except (ConnectionError, urllib.error.URLError) as e:
            self.fail(f"сервер оборвал соединение вместо ответа с понятной ошибкой: {e!r}")

    def ok_file(self, name, data, who=EMP):
        code, r = self.upload(name, data, who)
        self.assertEqual(code, 200, (name, r))
        self.assertTrue({"name", "text", "chars", "truncated"} <= set(r), r)
        return r

    def bad_file(self, name, data=b"", raw=None, who=EMP):
        code, r = self.upload(name, data, who, raw)
        self.assertEqual(code, 400, (name, str(r)[:300]))
        self.assertRegex(r.get("error", ""), RU, r)
        return r["error"]

    # ---- /api/llm и история
    def llm(self, messages, who=EMP, **extra):
        return self.s.call("POST", "/api/llm", {"messages": messages, **extra}, session=self.sid[who])

    def ok_llm(self, messages, who=EMP, **extra):
        code, r = self.llm(messages, who, **extra)
        self.assertEqual(code, 200, r)
        return r

    def model_req(self, n):
        """Ровно один новый запрос к модели после отметки n."""
        self.assertEqual(len(self.mock.requests), n + 1, "запрос не дошёл до модели (или ушёл не один раз)")
        return self.mock.requests[-1]

    def last_user(self, req):
        users = user_texts(req)
        self.assertTrue(users, req)
        return users[-1]

    def gc_calls_list(self):
        if not self.gc_calls.exists():
            return []
        return [json.loads(x) for x in self.gc_calls.read_text("utf-8").splitlines() if x.strip()]

    def gc_stdin(self, messages, who=EMP):
        n = len(self.gc_calls_list())
        self.ok_llm(messages, who, engine="gigacode")
        calls = self.gc_calls_list()
        self.assertEqual(len(calls), n + 1, "gigacode не запускался")
        self.assertNotIn("auto-edit", calls[-1]["argv"])   # правило 7 CLAUDE.md
        return calls[-1]["stdin"]

    def hist(self, who=EMP):
        code, r = self.s.call("GET", API_HIST, session=self.sid[who])
        self.assertEqual(code, 200, r)
        return r


# ================================================================ критерий 6: доступ к /api/ai/file
class UploadAuthTest(Base):
    def test_requires_session(self):
        """Кр. 6: без сессии и с недействительной сессией — 401, без ключа страницы — 403."""
        body = {"name": "Записка.txt", "data": b64("Выдуманный текст".encode("utf-8"))}
        for sid in (None, "garbage-not-a-session"):
            with self.subTest(session=sid):
                code, r = self.s.call("POST", API_FILE, body, session=sid)
                self.assertEqual(code, 401, r)
                self.assertRegex(r.get("error", ""), RU)
        code, r = self.s.call("POST", API_FILE, body, session=self.sid[EMP], token=False)
        self.assertEqual(code, 403, r)

    def test_any_logged_in_user(self):
        """Кр. 6: прикладывать может любой вошедший — сотрудник, тимлид, админ."""
        for who in (EMP, LEAD, "admin"):
            with self.subTest(who=who):
                r = self.ok_file("Записка.txt", "Выдуманный текст для всех ролей".encode("utf-8"), who=who)
                self.assertEqual(r["text"], "Выдуманный текст для всех ролей")


# ================================================================ критерий 2: извлечение .docx
class DocxExtractTest(Base):
    def test_paragraphs_runs_and_entities(self):
        """Кр. 2: абзацы с кириллицей — каждый своей строкой; прогоны одного абзаца склеены; &lt; &amp; раскрыты."""
        body = (P("Выдуманный договор поставки № 7-Т")
                + par(run(wt("Поставщик: ")), run(wt("ООО «Ромашка-Тест»")))
                + P("Сумма < 100 & без НДС")
                + P("Подписано выдуманными сторонами"))
        name = "Договор поставки № 7.docx"
        r = self.ok_file(name, docx(body))
        text = r["text"]
        lines = [x.strip() for x in text.split("\n")]
        for want in ("Выдуманный договор поставки № 7-Т", "Поставщик: ООО «Ромашка-Тест»", "Сумма < 100 & без НДС",
                     "Подписано выдуманными сторонами"):
            self.assertIn(want, lines, text)
        self.assertLess(text.index("Выдуманный договор"), text.index("Подписано"))
        self.assertNotIn("<w:", text)
        self.assertEqual(r["name"], name)
        self.assertIs(r["truncated"], False)
        self.assertEqual(r["chars"], len(text))

    def test_table_cells_joined_with_pipe(self):
        """Кр. 2: таблица — строка таблицы = строка текста, ячейки через « | »; порядок документа сохранён."""
        body = P("Таблица выдуманных сроков") + table([["Сторона", "Срок"], ["Поставщик", "31.12.2030"]]) \
            + P("После таблицы")
        text = self.ok_file("Сроки.docx", docx(body))["text"]
        rows = [x for x in text.split("\n") if "|" in x]
        self.assertTrue(any("Сторона | Срок" in x for x in rows), text)
        self.assertTrue(any("Поставщик | 31.12.2030" in x for x in rows), text)
        self.assertFalse(any("Срок" in x and "31.12.2030" in x for x in rows), "строки таблицы слиплись: " + text)
        self.assertLess(text.index("Таблица выдуманных"), text.index("Сторона"))
        self.assertLess(text.index("31.12.2030"), text.index("После таблицы"))

    def test_list_items_prefixed(self):
        """Кр. 2: пункты списка — с префиксом «- », обычные абзацы — без него."""
        body = (P("Перечень выдуманных работ") + par(run(wt("Собрать требования")), ppr=LI)
                + par(run(wt("Согласовать сроки")), ppr=LI) + P("Конец перечня"))
        text = self.ok_file("Работы.docx", docx(body))["text"]
        self.assertRegex(text, r"(?m)^\s*- Собрать требования\s*$")
        self.assertRegex(text, r"(?m)^\s*- Согласовать сроки\s*$")
        self.assertNotRegex(text, r"(?m)^\s*- (Перечень|Конец)")

    def test_tab_and_break_inside_paragraph(self):
        """Кр. 2: табуляция внутри абзаца не склеивает слова, разрыв строки <w:br/> — перевод строки."""
        body = par(run(wt("До таба"), TAB, wt("после таба"), BR, wt("новая строка")))
        text = self.ok_file("Таб.docx", docx(body))["text"]
        self.assertRegex(text, r"До таба[\t ]+после таба")
        self.assertIn("после таба\nновая строка", text)

    def test_uppercase_extension(self):
        """Кр. 2: файлы из Windows бывают с расширением в верхнем регистре — .DOCX тоже .docx."""
        r = self.ok_file("ВЫДУМАННЫЙ ПРИКАЗ.DOCX", docx(P("Приказ о выдуманном мероприятии")))
        self.assertIn("Приказ о выдуманном мероприятии", r["text"])

    # ---- сверх минимума: так устроены настоящие файлы Word (договоры с правками, шаблоны)
    def test_hyperlink_text_kept(self):
        """Кр. 2 (настоящие файлы Word): текст гиперссылки — часть абзаца и не теряется."""
        body = par(run(wt("См. ")), f'<w:hyperlink r:id="rId9">{run(wt("выдуманную ссылку"))}</w:hyperlink>',
                   run(wt(" в приложении")))
        text = self.ok_file("Ссылки.docx", docx(body))["text"]
        self.assertIn("См. выдуманную ссылку в приложении", text)

    def test_tracked_changes(self):
        """Кр. 2 (настоящие файлы Word): вставленный правкой текст есть, удалённый правкой (w:delText) — нет."""
        body = par(run(wt("Срок поставки ")),
                   f'<w:ins w:id="1" w:author="Тестов">{run(wt("30 дней"))}</w:ins>',
                   '<w:del w:id="2" w:author="Тестов"><w:r><w:delText>90 суток</w:delText></w:r></w:del>')
        text = self.ok_file("Правки.docx", docx(body))["text"]
        self.assertIn("Срок поставки 30 дней", text)
        self.assertNotIn("90 суток", text)

    def test_content_control_text_kept(self):
        """Кр. 2 (настоящие файлы Word): абзац внутри элемента управления содержимым (w:sdt) не теряется."""
        body = P("Шапка шаблона") + f"<w:sdt><w:sdtPr/><w:sdtContent>{P('Поле шаблона: выдуманное значение')}" \
                                    "</w:sdtContent></w:sdt>"
        text = self.ok_file("Шаблон.docx", docx(body))["text"]
        self.assertIn("Поле шаблона: выдуманное значение", text)


# ================================================================ критерий 2: кодировки .txt
TXT = "Выдуманная служебная записка № 12\nо закупке тестовых скрепок — «синих», ещё ёлочных\nИтого: 3 коробки"
TXT_ENCODINGS = {
    "utf-8": b"" + TXT.encode("utf-8"),
    "utf-8 с BOM": codecs.BOM_UTF8 + TXT.encode("utf-8"),
    "utf-16 LE с BOM": codecs.BOM_UTF16_LE + TXT.encode("utf-16-le"),
    "utf-16 BE с BOM": codecs.BOM_UTF16_BE + TXT.encode("utf-16-be"),
    "cp1251": TXT.encode("cp1251"),
}


class TxtExtractTest(Base):
    def test_encodings(self):
        """Кр. 2: UTF-8 (с BOM и без), UTF-16 с BOM (LE и BE), иначе cp1251 — текст без искажений и без BOM."""
        for label, data in TXT_ENCODINGS.items():
            with self.subTest(enc=label):
                r = self.ok_file(f"Записка {label}.txt", data)
                self.assertEqual(r["text"], TXT)
                self.assertEqual(r["chars"], len(TXT))
                self.assertIs(r["truncated"], False)
                self.assertEqual(r["name"], f"Записка {label}.txt")

    def test_windows_line_breaks(self):
        """Кр. 2: переводы строк Windows (CRLF) -> \\n во всех кодировках; chars — после замены."""
        crlf = TXT.replace("\n", "\r\n")
        for label, data in (("utf-8", crlf.encode("utf-8")), ("utf-8 с BOM", codecs.BOM_UTF8 + crlf.encode("utf-8")),
                            ("utf-16 LE с BOM", codecs.BOM_UTF16_LE + crlf.encode("utf-16-le")),
                            ("cp1251", crlf.encode("cp1251"))):
            with self.subTest(enc=label):
                r = self.ok_file(f"Записка CRLF {label}.txt", data)
                self.assertNotIn("\r", r["text"])
                self.assertEqual(r["text"], TXT)
                self.assertEqual(r["chars"], len(TXT))

    def test_uppercase_extension(self):
        """Кр. 2: .TXT из Windows — тоже .txt."""
        r = self.ok_file("ЗАМЕТКИ.TXT", "Выдуманные заметки".encode("cp1251"))
        self.assertEqual(r["text"], "Выдуманные заметки")


# ================================================================ критерий 2: понятные ошибки
class UploadErrorsTest(Base):
    def test_doc_and_pdf_rejected_with_hint(self):
        """Кр. 2: .doc и .pdf — 400 по-русски с подсказкой сохранить как .docx (допущение T-009)."""
        for name, data in (("Старый договор.doc", OLE), ("Скан договора.pdf", PDF), ("СКАН.PDF", PDF)):
            with self.subTest(name=name):
                self.assertIn("docx", self.bad_file(name, data).lower())

    def test_other_formats_rejected(self):
        """Кр. 2: другие форматы (по расширению) — 400 по-русски."""
        xlsx = io.BytesIO()
        with zipfile.ZipFile(xlsx, "w") as z:
            z.writestr("[Content_Types].xml", CONTENT_TYPES)
            z.writestr("xl/workbook.xml", "<workbook/>")
        for name, data in (("Таблица.xlsx", xlsx.getvalue()), ("Письмо.rtf", b"{\\rtf1\\ansi Vydumka}"),
                           ("Картинка.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64),
                           ("Отчёт.docx.exe", b"MZ" + b"\x00" * 64),
                           ("Без расширения", "Выдуманный текст без расширения".encode("utf-8"))):
            with self.subTest(name=name):
                self.bad_file(name, data)

    def test_corrupted_docx(self):
        """Кр. 2: повреждённый или не-docx файл с расширением .docx — 400 по-русски, не 500."""
        good = docx(P("Выдуманный абзац для порчи"))
        crc = docx(P("CRC-MARK-1 выдуманный текст"), stored=True)
        self.assertEqual(crc.count(b"CRC-MARK-1"), 1)
        cases = {
            "не zip": "Выдуманный текст, а не docx".encode("utf-8"),
            "старый .doc под именем .docx": OLE,
            "обрезанный zip": good[:len(good) // 2],
            "не сходится CRC": crc.replace(b"CRC-MARK-1", b"CRC-MARK-2"),
            "zip без word/document.xml": docx(members={"word/other.xml": "<x/>"}),
            "битый XML": docx(document=wdoc(P("Выдуманный абзац"))[:-40]),
        }
        for label, data in cases.items():
            with self.subTest(case=label):
                self.bad_file(f"Повреждённый ({label}).docx", data)

    def test_empty_files(self):
        """Кр. 2: пустой файл или файл без текста — 400 по-русски."""
        cases = {
            "пустой .txt": ("Пустой.txt", b""),
            ".txt из пробелов": ("Пробелы.txt", b"   \r\n\t  \r\n"),
            ".txt только BOM": ("Только BOM.txt", codecs.BOM_UTF8),
            ".docx без абзацев": ("Пустой.docx", docx("")),
            ".docx с пустыми абзацами": ("Пустые абзацы.docx", docx("<w:p/>" + P("") + P("   "))),
        }
        for label, (name, data) in cases.items():
            with self.subTest(case=label):
                self.bad_file(name, data)

    def test_bad_base64(self):
        """Кр. 2: некорректный base64 — 400 по-русски."""
        for raw in ("это не base64!!!", "QUJ", "@@@"):
            with self.subTest(data=raw):
                self.bad_file("Записка.txt", raw=raw)

    def test_bad_body(self):
        """Кривое тело запроса — 400, а не 500."""
        data = b64("Выдуманный текст".encode("utf-8"))
        for body in ({}, {"name": "Записка.txt"}, {"data": data}, {"name": 5, "data": data},
                     {"name": "Записка.txt", "data": 123}, {"name": "", "data": data}, {"name": None, "data": None}):
            with self.subTest(body=str(body)[:60]):
                code, r = self.s.call("POST", API_FILE, body, session=self.sid[EMP])
                self.assertEqual(code, 400, r)
                self.assertRegex(r.get("error", ""), RU)

    def test_server_alive_after_errors(self):
        """После серии ошибок сервер принимает нормальный файл."""
        self.bad_file("Битый.docx", b"PK\x03\x04 obrezok")
        self.bad_file("Старый.doc", OLE)
        r = self.ok_file("Живой.docx", docx(P("Сервер жив после ошибок")))
        self.assertIn("Сервер жив после ошибок", r["text"])


# ================================================================ критерий 6: безопасность разбора и диск
DOCTYPE_ENT = '<!DOCTYPE w:document [<!ENTITY ent "ВЫДУМАННАЯ-СУЩНОСТЬ">]>'
LAUGHS = ('<!DOCTYPE w:document [<!ENTITY a "ха"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
          '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;"><!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">]>')


class SecurityTest(Base):
    def test_doctype_rejected(self):
        """Кр. 6: XML с DOCTYPE отклоняется (400), сущности не раскрываются — в том числе в UTF-16."""
        ent_p = "<w:p><w:r><w:t>Начало &ent; конец</w:t></w:r></w:p>"
        cases = {
            "внутренняя сущность": (wdoc(ent_p, DOCTYPE_ENT).encode("utf-8"), "ВЫДУМАННАЯ-СУЩНОСТЬ"),
            "лавина сущностей": (wdoc("<w:p><w:r><w:t>&d;</w:t></w:r></w:p>", LAUGHS).encode("utf-8"), "хахаха"),
            "DOCTYPE без сущностей": (wdoc(P("Абзац при DOCTYPE"), "<!DOCTYPE w:document>").encode("utf-8"), None),
            "DOCTYPE в UTF-16": (wdoc(ent_p, DOCTYPE_ENT.replace("ВЫДУМАННАЯ", "UTF16"),
                                      decl='<?xml version="1.0" encoding="UTF-16"?>').encode("utf-16"),
                                 "UTF16-СУЩНОСТЬ"),
        }
        for label, (doc, leak) in cases.items():
            with self.subTest(case=label):
                code, r = self.upload(f"DOCTYPE {label}.docx", docx(document=doc))
                if leak:
                    self.assertNotIn(leak, json.dumps(r, ensure_ascii=False), "сущность раскрыта")
                self.assertEqual(code, 400, r)
                self.assertRegex(r.get("error", ""), RU)

    def test_external_entity_not_read(self):
        """Кр. 6: внешняя сущность (XXE) не читает файлы сервера."""
        token = "XXE-SECRET-" + secrets.token_hex(6)
        secret = Path(self.work.name) / "xxe-secret.txt"
        secret.write_text(token, "utf-8")
        doc = wdoc("<w:p><w:r><w:t>Начало &xxe; конец</w:t></w:r></w:p>",
                   f'<!DOCTYPE w:document [<!ENTITY xxe SYSTEM "{secret.as_uri()}">]>')
        code, r = self.upload("Внешняя сущность.docx", docx(document=doc))
        self.assertNotIn(token, json.dumps(r, ensure_ascii=False))
        self.assertEqual(code, 400, r)

    def test_zip_bomb_rejected_server_alive(self):
        """Кр. 6: zip-бомба — десятки и сотни МБ в сжатом document.xml (сжатие ~1000:1) — 400 быстро; сервер жив.
        «Пустые абзацы» — та же бомба, но дерево XML раздувает память сервера (30 МБ XML — сотни МБ памяти)."""
        for label, mb, unit in (("пробелы 40 МБ", 40, b" "), ("пробелы 60 МБ", 60, b" "),
                                ("пробелы 200 МБ", 200, b" "), ("пустые абзацы 30 МБ", 30, b"<w:p/>")):
            with self.subTest(case=label):
                data = bomb_docx(mb, unit)
                self.assertLess(len(data), 1 << 20)   # сам файл маленький — лимит размера не спасает
                t0 = time.monotonic()
                err = self.bad_file(f"Бомба ({label}).docx", data)
                self.assertLess(time.monotonic() - t0, 10, "бомба разбиралась слишком долго")
                self.assertRegex(err, r"zip-бомб")
                self.assertRegex(err, r"\(сжатие \d+:1\)")        # без склонения «в N раз(а)»
                self.assertNotRegex(err, r"сжат в \d+ раз")
                r = self.ok_file("Жив.txt", "Сервер жив после бомбы".encode("utf-8"))
                self.assertEqual(r["text"], "Сервер жив после бомбы")

    def test_repetitive_docx_not_a_bomb(self):
        """Кр. 2, 6: однообразный, но обычный .docx (сжатие 150–230:1 при объёме до 1 МБ) — не бомба, принимается."""
        long_text = filler(60_000, "НАЧАЛО-ПОВТОРОВ ", " КОНЕЦ-ПОВТОРОВ")
        r = self.ok_file("Повторы.docx", docx(P(long_text)))
        self.assertIs(r["truncated"], True)
        self.assertTrue(r["text"].startswith("НАЧАЛО-ПОВТОРОВ"))
        r = self.ok_file("Большая таблица.docx", docx(table([["Пункт", "да", "да"]] * 2000)))
        self.assertIn("Пункт | да | да", r["text"])

    def test_deep_nesting_rejected_server_alive(self):
        """Кр. 2, 6: XML с огромной вложенностью — понятная 400, сервер не падает."""
        for depth in (100_000, 1_000_000):
            with self.subTest(depth=depth):
                self.bad_file(f"Вложенность {depth}.docx", nested_docx(depth))
                self.assertIsNone(self.s.proc.poll(), "сервер упал")
        r = self.ok_file("Жив.txt", "Сервер жив после вложенности".encode("utf-8"))
        self.assertEqual(r["text"], "Сервер жив после вложенности")

    def test_upload_writes_nothing_to_disk(self):
        """Кр. 6: файл не пишется на диск — ни в хранилище, ни во временную папку, ни в журнал сервера
        (start.sh пишет журнал в /tmp/ck_registry.log)."""
        marker = "T009-DISK-MARKER-" + secrets.token_hex(6)
        before = files_under(self.s.dir, self.tmpdir)
        self.ok_file("Записка с меткой.txt", f"Выдуманный текст {marker}".encode("cp1251"))
        self.ok_file("Документ с меткой.docx", docx(P(f"Выдуманный абзац {marker}"), stored=True))
        after = files_under(self.s.dir, self.tmpdir)
        self.assertEqual(sorted(after), sorted(before), "появились новые файлы")
        for path, data in after.items():
            self.assertNotIn(marker.encode(), data, path)
        self.assertNotIn(marker, self.s.output(), "текст файла попал в журнал сервера")

    def test_no_extract_to_disk_in_code(self):
        """Кр. 6: в app.py нет распаковки zip на диск и временных файлов. Единственная временная папка —
        клон репозитория БЗ на прокси сотрудника (T-010, bb_upload); код помощника tempfile не трогает."""
        for bad in ("extractall", ".extract(", "NamedTemporaryFile", "mkstemp"):
            self.assertNotIn(bad, APP_SRC, bad)
        tree = ast.parse(APP_SRC)
        self.assertFalse([n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "tempfile"],
                         "from tempfile import … — обход проверки ниже")
        fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        where = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "tempfile":
                owner = [f for f in fns if f.lineno <= n.lineno <= f.end_lineno]
                where.add(min(owner, key=lambda f: f.end_lineno - f.lineno).name if owner else "<модуль>")
        self.assertLessEqual(where, {"bb_upload"}, "временные файлы вне клона BB (T-010)")


# ================================================================ критерий 5: лимиты (маленькие — через окружение)
SMALL_CHARS, SMALL_TOTAL = 2000, 3000


class SmallLimitsTest(Base):
    env = {ENV_MB: "1", ENV_CHARS: str(SMALL_CHARS), ENV_TOTAL: str(SMALL_TOTAL)}

    def test_limits_from_env_reach_client(self):
        """Кр. 5: лимиты из registry.env/окружения; если GET /api/llm отдаёт их клиенту (file_limits) — те же."""
        code, r = self.s.call("GET", "/api/llm", session=self.sid[EMP])
        self.assertEqual(code, 200, r)
        lim = r.get("file_limits")
        if lim is None:
            self.skipTest("сервер не отдаёт file_limits")
        self.assertEqual((float(lim["max_mb"]), lim["max_chars"], lim["total_chars"], lim["per_msg"]),
                         (1.0, SMALL_CHARS, SMALL_TOTAL, MAX_FILES))

    def test_file_over_size_limit(self):
        """Кр. 5: файл больше REG_AI_FILE_MAX_MB (1 МБ) — 400 по-русски с указанием предела."""
        line = "Выдуманная строка большого файла\n".encode("utf-8")
        big_txt = line * (int(1.2 * 1024 * 1024) // len(line) + 1)
        big_docx = docx(P("Выдуманный большой документ " * 45000), stored=True)
        self.assertGreater(len(big_docx), int(1.2 * 1024 * 1024))
        for name, data in (("Большой.txt", big_txt), ("Большой.docx", big_docx)):
            with self.subTest(name=name):
                self.assertRegex(self.bad_file(name, data), r"М[Бб]|MB|мегабайт")

    def test_file_under_size_limit(self):
        """Кр. 5: файл 0,9 МБ при лимите 1 МБ принимается (лимит — на сам файл, не на base64)."""
        line = "Выдуманная строка файла поменьше\n".encode("utf-8")
        data = line * (int(0.9 * 1024 * 1024) // len(line))
        r = self.ok_file("Поменьше.txt", data)
        self.assertIs(r["truncated"], True)
        text = data.decode("utf-8")
        self.assertTrue(len(text.strip()) <= r["chars"] <= len(text), r["chars"])

    def check_truncated(self, r, full, limit):
        self.assertIs(r["truncated"], True, str(r)[:300])
        self.assertLessEqual(abs(r["chars"] - len(full)), 2, "chars — длина всего извлечённого текста")
        self.assertLessEqual(len(r["text"]), limit + SLACK)
        self.assertTrue(r["text"].startswith(full[:limit - SLACK]))
        self.assertNotIn("ХВОСТ-ФАЙЛА", r["text"])

    def test_text_truncated_txt_and_docx(self):
        """Кр. 5: текст длиннее REG_AI_FILE_MAX_CHARS обрезается: truncated, chars — исходная длина."""
        full = filler(5000, "НАЧАЛО-ФАЙЛА ", " ХВОСТ-ФАЙЛА")
        self.check_truncated(self.ok_file("Длинный.txt", full.encode("utf-8")), full, SMALL_CHARS)
        self.check_truncated(self.ok_file("Длинный.docx", docx(P(full))), full, SMALL_CHARS)

    def test_text_at_limit_not_truncated(self):
        """Кр. 5: ровно REG_AI_FILE_MAX_CHARS символов — не обрезается."""
        full = filler(SMALL_CHARS, "НАЧАЛО-РОВНО ", " КОНЕЦ-РОВНО")
        r = self.ok_file("Ровно.txt", full.encode("utf-8"))
        self.assertIs(r["truncated"], False)
        self.assertEqual(r["text"], full)
        self.assertEqual(r["chars"], SMALL_CHARS)

    def test_truncated_mark_for_model(self):
        """Кр. 5: обрезанный файл уходит модели с пометкой об обрезке; необрезанный — без неё."""
        full = filler(5000, "НАЧАЛО-ФАЙЛА ", " ХВОСТ-ФАЙЛА")
        up = self.ok_file("Длинный.txt", full.encode("utf-8"))
        n = len(self.mock.requests)
        self.ok_llm([umsg("Что в длинном файле?", ufile(up["name"], up["text"], up["truncated"]))])
        self.assertRegex(self.last_user(self.model_req(n)), TRUNC_MARK)
        n = len(self.mock.requests)
        self.ok_llm([umsg("Что в коротком файле?", ufile("Короткий.txt", "Выдуманный короткий текст"))])
        self.assertNotRegex(self.last_user(self.model_req(n)), TRUNC_MARK)

    def test_server_cuts_long_client_text(self):
        """Кр. 5: клиенту не доверяем — текст файла длиннее лимита в /api/llm сервер режет сам (модель и история)."""
        full = filler(5000, "НАЧАЛО-ФАЙЛА ", " ХВОСТ-ФАЙЛА")
        n = len(self.mock.requests)
        self.ok_llm([umsg("Перескажи файл", ufile("Подделка.txt", full, truncated=False))])
        sent = dialog_text(self.model_req(n))
        self.assertIn("НАЧАЛО-ФАЙЛА", sent)
        self.assertNotIn("ХВОСТ-ФАЙЛА", sent)
        f = self.hist()["msgs"][0]["files"][0]
        self.assertLessEqual(len(f.get("text") or ""), SMALL_CHARS + SLACK)
        self.assertNotIn("ХВОСТ-ФАЙЛА", f.get("text") or "")
        self.assertIs(f.get("truncated"), True)

    def budget_dialog(self):
        old = filler(1800, "СТАРЫЙ-ФАЙЛ-НАЧАЛО ", " СТАРЫЙ-ФАЙЛ-КОНЕЦ")
        new = filler(1800, "НОВЫЙ-ФАЙЛ-НАЧАЛО ", " НОВЫЙ-ФАЙЛ-КОНЕЦ")
        return [umsg("Вот старый выдуманный документ", ufile("Старый.txt", old)), amsg(),
                umsg("А вот новый выдуманный документ", ufile("Новый.txt", new))], new

    def check_budget(self, sent, where):
        self.assertIn("НОВЫЙ-ФАЙЛ-НАЧАЛО", sent, where)
        self.assertIn("НОВЫЙ-ФАЙЛ-КОНЕЦ", sent, where)
        self.assertNotIn("СТАРЫЙ-ФАЙЛ-НАЧАЛО", sent, f"{where}: старый файл не влезает в бюджет, а его текст ушёл")
        self.assertNotIn("СТАРЫЙ-ФАЙЛ-КОНЕЦ", sent, where)
        self.assertIn("Старый.txt", sent, f"{where}: у не влезшего файла должно остаться имя")
        self.assertIn(DROPPED_NOTE, sent, where)

    def test_total_budget_new_to_old_api(self):
        """Кр. 5: общий бюджет REG_AI_FILES_MAX_CHARS считается от новых к старым: старый — только имя с пометкой."""
        msgs, new = self.budget_dialog()
        n = len(self.mock.requests)
        self.ok_llm(msgs)
        req = self.model_req(n)
        self.check_budget(dialog_text(req), "модель")
        old_msg = [u for u in user_texts(req) if "Вот старый" in u]
        self.assertEqual(len(old_msg), 1, user_texts(req))
        self.assertIn("Старый.txt", old_msg[0], "имя не влезшего файла — в его сообщении")
        self.assertIn(DROPPED_NOTE, old_msg[0])

    def test_total_budget_in_history(self):
        """Кр. 5: в сохранённой истории старый файл — без текста и с признаком; новый — с текстом.
        Историю можно отправить обратно (F5 и продолжение диалога) — старый файл снова только именем."""
        msgs, new = self.budget_dialog()
        self.ok_llm(msgs)
        h = self.hist()["msgs"]
        old_f, new_f = h[0]["files"][0], h[2]["files"][0]
        self.assertEqual(old_f["name"], "Старый.txt")
        self.assertFalse(old_f.get("text"), "текст не влезшего файла сохранён в истории")
        self.assertEqual(new_f["text"], new)
        n = len(self.mock.requests)
        self.ok_llm(h + [umsg("И ещё вопрос по документам")])
        self.check_budget(dialog_text(self.model_req(n)), "модель после возврата истории")

    def test_total_budget_dropped_flag(self):
        """Кр. 5 (контракт истории): у не влезшего файла — признак DROPPED_KEY."""
        msgs, _ = self.budget_dialog()
        self.ok_llm(msgs)
        self.assertIs(self.hist()["msgs"][0]["files"][0].get(DROPPED_KEY), True)

    def test_total_budget_gigacode(self):
        """Кр. 3, 5: в GigaCode бюджет тот же — старый файл только именем с пометкой."""
        msgs, _ = self.budget_dialog()
        stdin = self.gc_stdin(msgs)
        self.check_budget(stdin[stdin.index(GC_DIALOG):], "GigaCode")


# ================================================================ критерий 5: лимиты по умолчанию
class DefaultLimitsTest(Base):
    def test_default_file_chars(self):
        """Кр. 5: по умолчанию текст файла — до 50 000 символов, длиннее — обрезается."""
        full = filler(60_000, "НАЧАЛО-60К ", " ХВОСТ-ФАЙЛА")
        r = self.ok_file("Длинный.txt", full.encode("utf-8"))
        self.assertIs(r["truncated"], True)
        self.assertEqual(r["chars"], 60_000)
        self.assertLessEqual(len(r["text"]), DEF_CHARS + SLACK)
        self.assertTrue(r["text"].startswith(full[:DEF_CHARS - SLACK]))
        self.assertNotIn("ХВОСТ-ФАЙЛА", r["text"])
        full = filler(DEF_CHARS, "НАЧАЛО-50К ", " КОНЕЦ-50К")
        r = self.ok_file("Ровно.txt", full.encode("utf-8"))
        self.assertIs(r["truncated"], False)
        self.assertEqual(r["text"], full)

    def test_default_total_budget(self):
        """Кр. 5: по умолчанию в запросе к модели — до 100 000 символов файлов, от новых к старым;
        файл в 40 000 символов уходит модели целиком (до конца)."""
        self.assertTrue(2 * 40_000 <= DEF_TOTAL < 3 * 40_000)   # два новых влезают, третий (старый) — нет
        texts = {k: filler(40_000, f"НАЧАЛО-{k} ", f" КОНЕЦ-{k}") for k in ("ПЕРВЫЙ", "ВТОРОЙ", "ТРЕТИЙ")}
        msgs = [umsg("Первый документ", ufile("Первый.txt", texts["ПЕРВЫЙ"])), amsg(),
                umsg("Второй документ", ufile("Второй.txt", texts["ВТОРОЙ"])), amsg(),
                umsg("Третий документ", ufile("Третий.txt", texts["ТРЕТИЙ"]))]
        n = len(self.mock.requests)
        self.ok_llm(msgs)
        sent = dialog_text(self.model_req(n))
        for k in ("ВТОРОЙ", "ТРЕТИЙ"):
            self.assertIn(f"НАЧАЛО-{k}", sent)
            self.assertIn(f"КОНЕЦ-{k}", sent, f"файл «{k}» в 40 000 символов дошёл до модели не целиком")
        self.assertNotIn("НАЧАЛО-ПЕРВЫЙ", sent)
        self.assertIn("Первый.txt", sent)
        self.assertIn(DROPPED_NOTE, sent)

    def test_default_size_10mb(self):
        """Кр. 5: по умолчанию файл — до 10 МБ: 10,5 МБ — 400 по-русски, 9,5 МБ — принимается."""
        line = "Выдуманная строка десятимегабайтного файла\n".encode("utf-8")
        big = line * (int(10.5 * 1024 * 1024) // len(line) + 1)
        self.assertRegex(self.bad_file("Огромный.txt", big), r"М[Бб]|MB|мегабайт")
        ok = line * (int(9.5 * 1024 * 1024) // len(line))
        r = self.ok_file("Большой.txt", ok)
        self.assertIs(r["truncated"], True)


# ================================================================ критерий 3: передача модели (API)
class ModelTest(Base):
    def test_docx_to_model_end_to_end(self):
        """Кр. 3: .docx -> /api/ai/file -> /api/llm: в сообщении пользователя — имя файла, его текст и сам вопрос."""
        name = "Выдуманный регламент.docx"
        up = self.ok_file(name, docx(P("Пункт 1. Отчёт сдаётся по пятницам")
                                     + table([["Роль", "Срок"], ["Исполнитель", "пятница"]])))
        n = len(self.mock.requests)
        r = self.ok_llm([umsg("Когда сдавать отчёт?", ufile(up["name"], up["text"], up["truncated"]))])
        self.assertTrue(r["reply"].startswith("Ответ мока Т9"))
        last = self.last_user(self.model_req(n))
        for want in (name, "Отчёт сдаётся по пятницам", "Исполнитель | пятница", "Когда сдавать отчёт?"):
            self.assertIn(want, last)

    def test_file_label_markup(self):
        """Кр. 3: явная разметка «Файл «имя»»."""
        n = len(self.mock.requests)
        self.ok_llm([umsg("Что тут?", ufile("Записка о скрепках.txt", "Выдуманный текст о скрепках"))])
        self.assertIn(FILE_LABEL.format("Записка о скрепках.txt"), self.last_user(self.model_req(n)))

    def test_file_blocks_before_message_text(self):
        """Кр. 3 (контракт): блоки файлов — перед текстом сообщения и отдельно от него."""
        n = len(self.mock.requests)
        self.ok_llm([umsg("ВОПРОС-ПОЛЬЗОВАТЕЛЯ про файлы",
                          ufile("Первый.txt", "ТЕКСТ-ПЕРВОГО-ФАЙЛА"), ufile("Второй.txt", "ТЕКСТ-ВТОРОГО-ФАЙЛА"))])
        last = self.last_user(self.model_req(n))
        q = last.index("ВОПРОС-ПОЛЬЗОВАТЕЛЯ")
        for marker in ("ТЕКСТ-ПЕРВОГО-ФАЙЛА", "ТЕКСТ-ВТОРОГО-ФАЙЛА"):
            end = last.index(marker) + len(marker)
            self.assertLess(end, q, "текст файла после текста сообщения")
            self.assertIn("\n", last[end:q], "текст файла слит с текстом сообщения")

    def test_only_files_message(self):
        """Кр. 1, 3: сообщение без текста, только с файлами, принимается и уходит модели."""
        for content in ("", "   "):
            with self.subTest(content=repr(content)):
                n = len(self.mock.requests)
                self.ok_llm([umsg(content, ufile("Только файл.txt", "ТЕКСТ-БЕЗ-ВОПРОСА выдуманный"))])
                last = self.last_user(self.model_req(n))
                self.assertIn("ТЕКСТ-БЕЗ-ВОПРОСА", last)
                self.assertIn("Только файл.txt", last)

    def test_empty_without_files_still_400(self):
        """Регрессия T-006: пустое сообщение без файлов (или с пустым списком) — 400, модель не вызывается."""
        n = len(self.mock.requests)
        for msg in (umsg(""), umsg("   "), {"role": "user", "content": "", "files": []}):
            with self.subTest(msg=msg):
                code, r = self.llm([msg])
                self.assertEqual(code, 400, r)
        self.assertEqual(len(self.mock.requests), n)

    def test_file_from_previous_message(self):
        """Кр. 4: приложенное остаётся в контексте — файл из прошлого сообщения диалога уходит модели."""
        msgs = [umsg("Вот выдуманный договор", ufile("Договор.docx", "СРОК-ПОСТАВКИ-30-ДНЕЙ по договору")),
                amsg("Договор прочитан"), umsg("Какой срок поставки?")]
        n = len(self.mock.requests)
        self.ok_llm(msgs)
        req = self.model_req(n)
        self.assertIn("СРОК-ПОСТАВКИ-30-ДНЕЙ", dialog_text(req))
        self.assertIn("Договор.docx", dialog_text(req))
        self.assertIn("Какой срок поставки?", self.last_user(req))

    def test_five_files_ok_six_400(self):
        """Кр. 1 (контракт): до 5 файлов в сообщении; 6 — 400 по-русски, модель не вызывается, история не меняется."""
        five = [ufile(f"Файл {i}.txt", f"ТЕКСТ-ФАЙЛА-{i}") for i in range(1, MAX_FILES + 1)]
        n = len(self.mock.requests)
        self.ok_llm([umsg("Пять файлов", *five)])
        sent = self.last_user(self.model_req(n))
        for f in five:
            self.assertIn(f["text"], sent)
        before = self.hist()
        n = len(self.mock.requests)
        code, r = self.llm([umsg("Шесть файлов", *five, ufile("Файл 6.txt", "ТЕКСТ-ФАЙЛА-6"))])
        self.assertEqual(code, 400, r)
        self.assertRegex(r.get("error", ""), RU)
        self.assertEqual(len(self.mock.requests), n)
        self.assertEqual(self.hist(), before)

    def test_malformed_files_no_500(self):
        """Кривое поле files от клиента — 200 или понятная 400, но не 500."""
        for files in ("строка вместо списка", [1, 2], [{"text": "без имени"}], [{"name": "a.txt", "text": 5}],
                      {"name": "a.txt", "text": "словарь вместо списка"}, [{"name": ["a"], "text": "x"}], [None]):
            with self.subTest(files=str(files)[:50]):
                code, r = self.llm([{"role": "user", "content": "Вопрос с кривыми файлами", "files": files}])
                self.assertIn(code, (200, 400), r)
                if code == 400:
                    self.assertRegex(r.get("error", ""), RU)


# ================================================================ критерий 3: передача в GigaCode
class GigaCodeTest(Base):
    def test_file_in_dialog_section(self):
        """Кр. 3: GigaCode получает имя и текст файла и сам вопрос в разделе диалога stdin."""
        stdin = self.gc_stdin([umsg("ВОПРОС-ДЛЯ-GIGACODE", ufile("Положение.docx", "ТЕКСТ-ПОЛОЖЕНИЯ выдуманный"))])
        self.assertIn(GC_DIALOG, stdin)
        dialog = stdin[stdin.index(GC_DIALOG):]
        for want in ("Положение.docx", "ТЕКСТ-ПОЛОЖЕНИЯ", "ВОПРОС-ДЛЯ-GIGACODE"):
            self.assertIn(want, dialog)
        self.assertLess(dialog.index("ТЕКСТ-ПОЛОЖЕНИЯ"), dialog.index("ВОПРОС-ДЛЯ-GIGACODE"))

    def test_only_files(self):
        """Кр. 1, 3: в GigaCode сообщение только с файлами тоже уходит."""
        stdin = self.gc_stdin([umsg("", ufile("Только файл.txt", "ТЕКСТ-БЕЗ-ВОПРОСА для GigaCode"))])
        self.assertIn("ТЕКСТ-БЕЗ-ВОПРОСА", stdin[stdin.index(GC_DIALOG):])

    def test_previous_message_file(self):
        """Кр. 4: файл из прошлого сообщения остаётся в диалоге GigaCode."""
        stdin = self.gc_stdin([umsg("Вот документ", ufile("Ранний.txt", "ТЕКСТ-РАННЕГО-ФАЙЛА")), amsg(),
                               umsg("Что было в документе?")])
        dialog = stdin[stdin.index(GC_DIALOG):]
        self.assertIn("ТЕКСТ-РАННЕГО-ФАЙЛА", dialog)
        self.assertIn("Что было в документе?", dialog)


# ================================================================ критерий 4: история на сервере
class HistoryTest(Base):
    def test_files_saved_with_message(self):
        """Кр. 4: в истории — сообщение с files (name, text, truncated); сообщения без файлов — как в T-006."""
        f = ufile("История.docx", "Выдуманный текст для истории")
        r = self.ok_llm([umsg("Без файла"), amsg("Ответ без файла"), umsg("С файлом", f)])
        h = self.hist()["msgs"]
        self.assertEqual([m["role"] for m in h], ["user", "assistant", "user", "assistant"])
        self.assertEqual(h[0], {"role": "user", "content": "Без файла"})
        self.assertEqual(h[1], {"role": "assistant", "content": "Ответ без файла"})
        self.assertEqual(h[2]["content"].strip(), "С файлом")
        self.assertEqual(len(h[2]["files"]), 1)
        hf = h[2]["files"][0]
        self.assertEqual(hf["name"], f["name"])
        self.assertEqual(hf["text"], f["text"])
        self.assertIn("truncated", hf)
        self.assertFalse(hf["truncated"])
        self.assertEqual(h[3], {"role": "assistant", "content": r["reply"]})

    def test_only_files_message_saved_queries_unchanged(self):
        """Кр. 4: сообщение только с файлами сохраняется; в «Историю запросов» пустой запрос не попадает
        и прежние запросы не переставляются."""
        self.ok_llm([umsg("Первый вопрос")])
        self.ok_llm([umsg("Второй вопрос")])
        before = self.hist()["queries"]
        self.assertEqual(before, ["Второй вопрос", "Первый вопрос"])
        self.ok_llm([umsg("Первый вопрос"), amsg(), umsg("", ufile("Без текста.txt", "Выдуманный текст файла"))])
        h = self.hist()
        self.assertEqual(h["queries"], before)
        self.assertEqual(h["msgs"][2]["content"].strip(), "")
        self.assertEqual(h["msgs"][2]["files"][0]["name"], "Без текста.txt")

    def test_queries_only_request_text(self):
        """Кр. 4: в «Истории запросов» — только текст запроса, без имён и текста файлов."""
        self.ok_llm([umsg("  Что в документе?  ", ufile("Документ.txt", "ТЕКСТ-НЕ-ДЛЯ-ЗАПРОСОВ"))])
        q = self.hist()["queries"]
        self.assertEqual(q, ["Что в документе?"])

    def test_reset_removes_files(self):
        """Кр. 4: «Новый диалог» убирает и файлы."""
        self.ok_llm([umsg("Запомни", ufile("До сброса.txt", "ТЕКСТ-ДО-СБРОСА"))])
        code, r = self.s.call("POST", API_HIST, {"action": "reset"}, session=self.sid[EMP])
        self.assertEqual(code, 200, r)
        self.assertEqual(r["msgs"], [])
        _, raw = self.s.raw("GET", API_HIST, session=self.sid[EMP])
        self.assertNotIn("ТЕКСТ-ДО-СБРОСА", raw)

    def test_new_session_sees_files(self):
        """Кр. 4: на другом компьютере (новая сессия) файлы в истории на месте."""
        self.ok_llm([umsg("Вопрос с файлом", ufile("Другой ПК.txt", "ТЕКСТ-ДЛЯ-ДРУГОГО-ПК"))])
        code, r = self.s.call("POST", "/api/login", {"name": EMP, "pin": PINS[EMP]})
        self.assertEqual(code, 200, r)
        code, h = self.s.call("GET", API_HIST, session=r["session"])
        self.assertEqual(code, 200, h)
        self.assertEqual(h, self.hist())
        self.assertEqual(h["msgs"][0]["files"][0]["text"], "ТЕКСТ-ДЛЯ-ДРУГОГО-ПК")

    def test_isolation(self):
        """Кр. 4 (как T-006): файлы сотрудника не видны другому — ни в истории, ни в /api/state."""
        self.ok_llm([umsg("Мой документ", ufile("Личное.txt", "СЕКРЕТ-ФАЙЛА-ВЛОЖЕНЦЕВОЙ"))])
        _, raw = self.s.raw("GET", API_HIST, session=self.sid[EMP2])
        self.assertNotIn("СЕКРЕТ-ФАЙЛА-ВЛОЖЕНЦЕВОЙ", raw)
        _, raw = self.s.raw("GET", "/api/state", session=self.sid[EMP2])
        self.assertNotIn("СЕКРЕТ-ФАЙЛА-ВЛОЖЕНЦЕВОЙ", raw)

    def test_model_error_saves_nothing(self):
        """Кр. 4 (как T-006): при ошибке модели история и запросы не меняются."""
        self.ok_llm([umsg("Удачный вопрос")])
        before = self.hist()
        self.mock.fail = True
        code, r = self.llm([umsg("Неудачный вопрос", ufile("Неудача.txt", "ТЕКСТ-ПРИ-ОШИБКЕ"))])
        self.assertEqual(code, 400, r)
        self.assertEqual(self.hist(), before)

    def test_history_roundtrip(self):
        """Кр. 4: после F5 клиент шлёт диалог из истории — сервер принимает свой же формат, файл снова у модели."""
        self.ok_llm([umsg("Запомни документ", ufile("Возврат.txt", "ТЕКСТ-ДЛЯ-ВОЗВРАТА"))])
        msgs = self.hist()["msgs"]
        n = len(self.mock.requests)
        self.ok_llm(msgs + [umsg("Что было в документе?")])
        self.assertIn("ТЕКСТ-ДЛЯ-ВОЗВРАТА", dialog_text(self.model_req(n)))

    def test_llm_reply_carries_saved_dialog(self):
        """Кр. 4: если /api/llm отдаёт сохранённый диалог (msgs — клиент показывает его вместо своего),
        он совпадает с историей на сервере, включая пометки не влезших файлов."""
        r = self.ok_llm([umsg("Сверка", ufile("Сверка.txt", "ТЕКСТ-СВЕРКИ"))])
        if "msgs" not in r:
            self.skipTest("сервер не отдаёт msgs — клиент дописывает ответ сам")
        self.assertEqual(r["msgs"], self.hist()["msgs"])

    def test_gigacode_saves_files(self):
        """Кр. 4: движок GigaCode — история с файлами сохраняется так же."""
        self.ok_llm([umsg("Через GigaCode", ufile("Для GigaCode.txt", "ТЕКСТ-ЧЕРЕЗ-GIGACODE"))], engine="gigacode")
        h = self.hist()
        self.assertEqual(h["msgs"][0]["files"][0]["text"], "ТЕКСТ-ЧЕРЕЗ-GIGACODE")
        self.assertEqual(h["queries"], ["Через GigaCode"])


class SqliteHistoryTest(HistoryTest):
    storage = "sqlite"


# ================================================================ коллеги: через прокси connect.sh (app.py proxy)
class _Via:
    """Запросы к реестру через другой адрес (прокси коллеги) тем же способом, что helpers.Server.call."""
    call = Server.call

    def __init__(self, base, token):
        self.base, self.token = base, token


class ColleagueProxyTest(Base):
    """Коллега открывает реестр через `app.py proxy <хост>:<порт>` (connect.sh); хост коллеги — 127.0.0.1."""
    env = {ENV_MB: "1"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        port = free_port()
        cls.proxy_log = open(Path(cls.work.name) / "proxy.log", "w+b")
        cls.proxy = subprocess.Popen([sys.executable, str(APP), "proxy", f"127.0.0.1:{cls.s.port}", "--port", str(port)],
                                     env=clean_env(), cwd=ROOT, stdin=subprocess.DEVNULL, stdout=cls.proxy_log,
                                     stderr=subprocess.STDOUT)
        cls.via = _Via(f"http://127.0.0.1:{port}", cls.s.token)
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.via.base + "/", timeout=1).read()
                return
            except Exception:
                if cls.proxy.poll() is not None:
                    break
                time.sleep(0.1)
        cls.tearDownClass()
        raise RuntimeError("прокси не поднялся")

    @classmethod
    def tearDownClass(cls):
        proxy = getattr(cls, "proxy", None)
        if proxy is not None and proxy.poll() is None:   # строго по PID
            proxy.terminate()
            try:
                proxy.wait(5)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait(5)
        if getattr(cls, "proxy_log", None):
            cls.proxy_log.close()
        super().tearDownClass()

    def test_upload_through_proxy(self):
        """Кр. 1, 2: файл прикладывается и через прокси коллеги."""
        body = {"name": "Через прокси.docx", "data": b64(docx(P("Выдуманный текст через прокси")))}
        code, r = self.via.call("POST", API_FILE, body, session=self.sid[EMP])
        self.assertEqual(code, 200, r)
        self.assertEqual(r["text"], "Выдуманный текст через прокси")
        code, r = self.via.call("POST", API_FILE, body)
        self.assertEqual(code, 401, r)

    def test_oversize_through_proxy(self):
        """Кр. 5: слишком большой файл через прокси — понятная 400 по-русски, а не обрыв связи или 502."""
        line = "Выдуманная строка большого файла\n".encode("utf-8")
        body = {"name": "Большой.txt", "data": b64(line * (int(1.2 * 1024 * 1024) // len(line) + 1))}
        try:
            code, r = self.via.call("POST", API_FILE, body, session=self.sid[EMP])
        except (ConnectionError, urllib.error.URLError) as e:
            self.fail(f"обрыв связи вместо понятной ошибки: {e!r}")
        self.assertEqual(code, 400, r)
        self.assertRegex(r.get("error", ""), r"М[Бб]")

    def test_llm_with_files_through_proxy(self):
        """Кр. 3: сообщение с файлом через прокси доходит до модели."""
        n = len(self.mock.requests)
        code, r = self.via.call("POST", "/api/llm", {"messages": [umsg("Вопрос коллеги",
                                                                       ufile("Коллеги.txt", "ТЕКСТ-ЧЕРЕЗ-ПРОКСИ"))]},
                                session=self.sid[EMP])
        self.assertEqual(code, 200, r)
        self.assertIn("ТЕКСТ-ЧЕРЕЗ-ПРОКСИ", self.last_user(self.model_req(n)))


# ================================================================ критерий 4: переживает перезапуск сервера
class PersistTest(unittest.TestCase):
    def check(self, storage):
        mock = MockModel()
        tmp = tempfile.TemporaryDirectory()
        s = None
        try:
            env = {"GIGACHAT_API_URL": mock.url}
            s = Server(storage, args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN], env=env, tmp=tmp)
            sid = setup_people(s)
            body = {"messages": [umsg("Перескажи файл", ufile("Перезапуск.txt", "ТЕКСТ-ДО-ПЕРЕЗАПУСКА"))]}
            code, r = s.call("POST", "/api/llm", body, session=sid[EMP])
            self.assertEqual(code, 200, r)
            code, want = s.call("GET", API_HIST, session=sid[EMP])
            self.assertEqual(code, 200, want)
            self.assertEqual(want["msgs"][0]["files"][0]["text"], "ТЕКСТ-ДО-ПЕРЕЗАПУСКА")
            s.stop()
            s = Server(storage, args=ADMIN_ARGS, stdin_lines=[ADMIN_PIN], env=env, tmp=tmp)
            sid = login_all(s)
            self.assertEqual(s.call("GET", API_HIST, session=sid[EMP]), (200, want))
        finally:
            if s:
                s.stop()
            mock.stop()
            tmp.cleanup()

    def test_json(self):
        self.check("json")

    def test_sqlite(self):
        self.check("sqlite")


# ================================================================ критерии 1, 3, 4: интерфейс (статически)
def _fn_body(name):
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", HTML) \
        or re.search(r"const\s+" + re.escape(name) + r"\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>\s*\{", HTML)
    if not m:
        return ""
    i, depth = m.end(), 1
    while depth and i < len(HTML):
        depth += {"{": 1, "}": -1}.get(HTML[i], 0)
        i += 1
    return HTML[m.end():i - 1]


def _with_calls(code):
    """Код и тела функций, которые он вызывает (на один уровень)."""
    return code + "".join(_fn_body(n) for n in set(re.findall(r"\b(\w+)\(", code)))


def _near(word, span=700):
    """Код рядом с каждым упоминанием события ('drop', 'dragover'…) вместе с вызываемыми функциями."""
    return [_with_calls(HTML[m.start():m.start() + span])
            for m in re.finditer(r"['\"]" + word + r"['\"]|\bon" + word + r"\b", HTML)]


def _hiding_classes():
    """Классы с «голым» правилом display:none (как .busy у заставки) — новым элементам их давать нельзя."""
    css = re.sub(r"/\*.*?\*/", "", frontend_css(), flags=re.S)
    out = set()
    for sel, decl in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if re.search(r"display\s*:\s*none", decl):
            out |= {m.group(1) for m in (re.fullmatch(r"\s*\.([\w-]+)\s*", s) for s in sel.split(",")) if m}
    return out


class StaticUiTest(unittest.TestCase):
    """Без браузера (playwright не установлен) — проверки index.html; поведение — вручную в браузере."""

    def test_attach_button(self):
        """Кр. 1: кнопка «Приложить файл»."""
        self.assertIn("Приложить файл", HTML)

    def test_file_input_accepts_docx_txt(self):
        """Кр. 1, 2: выбор файла — только .docx и .txt."""
        self.assertRegex(HTML, r"type\s*=\s*['\"]file['\"]|type\s*:\s*['\"]file['\"]")
        acc = " ".join(re.findall(r"accept\s*[=:]\s*['\"]([^'\"]+)['\"]", HTML))
        self.assertIn(".docx", acc)
        self.assertIn(".txt", acc)

    def test_drag_and_drop(self):
        """Кр. 1: перетаскивание — обработчик drop и dragover с preventDefault (иначе браузер откроет файл сам)."""
        self.assertTrue(_near("drop"), "нет обработчика drop")
        self.assertTrue(any("preventDefault" in c for c in _near("dragover")), "dragover без preventDefault")

    def test_drag_classes_no_conflict(self):
        """Урок CLAUDE.md: класс подсветки при перетаскивании не совпадает с занятыми — .dragging (перенос окна
        помощника), .busy и прочие с display:none."""
        code = "".join(_near("dragover") + _near("dragenter") + _near("dragleave") + _near("drop"))
        used = set(re.findall(r"classList\.(?:add|toggle|remove)\(\s*['\"]([\w-]+)['\"]", code))
        self.assertFalse(used & ({"dragging", "busy", "show"} | _hiding_classes()), used)

    def test_file_ui_classes_no_conflict(self):
        """Урок CLAUDE.md: классы плашек файлов не совпадают с занятыми (.chip/.chips — фильтры статусов с цветной
        точкой, .busy — заставка, прочие с display:none) и у каждого есть свои стили."""
        fns = [n for n in set(re.findall(r"function\s+(\w+)\s*\(", HTML))
               if re.search(r"обрезан|не передаётся", _fn_body(n))]
        self.assertTrue(fns, "нет функций отрисовки плашек")
        classes = set()
        for n in fns:
            for attr in re.findall(r"class=\"([^\"$]+)", _fn_body(n)):
                classes |= set(attr.split())
        self.assertTrue(classes, fns)
        self.assertFalse(classes & ({"chip", "chips", "busy", "dragging", "show"} | _hiding_classes()), classes)
        css = frontend_css()
        self.assertEqual([c for c in classes if not re.search(r"\." + re.escape(c) + r"\b", css)], [],
                         "классы без стилей")

    def test_upload_via_api_helper(self):
        """Кр. 2: файл уходит на /api/ai/file через api() или от BASE — относительный путь (урок про …/proxy/39246)."""
        self.assertRegex(HTML, r"api\(\s*['\"`]/?api/ai/file['\"`]|BASE\s*\+\s*['\"`]/?api/ai/file")
        for arg in re.findall(r"(?:fetch\(|\.open\(\s*['\"]\w+['\"]\s*,)\s*([^,)]+)", HTML):
            self.assertIn("BASE", arg, f"fetch мимо BASE: {arg}")

    def test_send_without_text_when_files(self):
        """Кр. 1: отправка без текста — если приложены файлы (раньше aiSend выходил при пустом тексте)."""
        body = _fn_body("aiSend")
        self.assertTrue(body, "нет aiSend")
        guard = re.search(r"if\s*\(([^)]*!\s*text\b[^)]*)\)\s*return", body)
        self.assertTrue(guard is None or re.search(r"length|files|att|pend", guard.group(1), re.I),
                        f"пустой текст отсекается без учёта файлов: {guard and guard.group(0)}")

    def test_files_sent_with_user_message(self):
        """Кр. 3, 4: файлы — в сообщении пользователя (AI.msgs), которое уходит в /api/llm и в историю."""
        code = _with_calls(_fn_body("aiSend"))
        self.assertRegex(code, r"role\s*:\s*['\"]user['\"]")
        self.assertRegex(code, r"\bfiles\b")

    def test_chip_texts(self):
        """Кр. 1, 5: на плашке — пометка «обрезан» и крестик «убрать»."""
        self.assertRegex(HTML, r"обрезан")
        self.assertRegex(HTML, r"(?i)убрать")

    def test_limit_5_on_client(self):
        """Кр. 1: в интерфейсе — не больше 5 файлов к сообщению."""
        self.assertRegex(HTML, r"(?:больше|более|до|максимум)\s+(?:5|пяти)\s+файл"
                               r"|\b\w*(?:MAX|max|Max)\w*(?:FILE|File|file)\w*\s*=\s*5\b"
                               r"|\b\w*(?:FILE|File|file)\w*(?:MAX|max|Max)\w*\s*=\s*5\b")

    def test_bubble_renders_files(self):
        """Кр. 3, 4: пузырь сообщения показывает файлы сообщения (после F5 — из истории)."""
        self.assertRegex(_with_calls(_fn_body("aiRender")), r"\.files\b")

    def test_toast_closes_on_click_action_once(self):
        """Ошибки файлов — тостами: клик по тосту закрывает его, кнопка действия срабатывает один раз
        (её клик тост не перехватывает, после действия тост убирается)."""
        body = _fn_body("toast")
        self.assertIn("${esc(msg)}", body)
        listener = re.search(r"addEventListener\(\s*['\"]click['\"](.*?)\);\n", body, re.S)
        self.assertTrue(listener, "клик по тосту не обрабатывается")
        self.assertIn("remove()", listener.group(1))
        self.assertRegex(listener.group(1), r"closest\(\s*['\"]button['\"]\s*\)", "клик по кнопке действия закрывает дважды")
        self.assertEqual(body.count("action.run("), 1)
        self.assertRegex(body, r"action\.run\(\);\s*el\.remove\(\)")

    def test_file_names_escaped(self):
        """Имена файлов в HTML-разметке — через esc() (имя приходит от пользователя). Текст для toast()
        не в счёт: toast сам экранирует сообщение."""
        self.assertIn("${esc(msg)}", _fn_body("toast"), "toast() перестал экранировать — проверьте сообщения о файлах")
        fns = [n for n in set(re.findall(r"function\s+(\w+)\s*\(", HTML)) if re.search(r"обрезан|\.files\b",
                                                                                       _fn_body(n))]
        self.assertTrue(fns, "нет функций отрисовки файлов")
        bad = []
        for n in fns:
            body = _fn_body(n)
            for m in re.finditer(r"\$\{([^{}]*\bname\b[^{}]*)\}", body):
                tpl = body[body.rfind("`", 0, m.start()):m.start()]   # начало шаблона до подстановки
                if "<" in tpl and "esc(" not in m.group(1):
                    bad.append((n, m.group(1)))
        self.assertEqual(bad, [])


class DocsTest(unittest.TestCase):
    def test_readme_contract(self):
        """Кр. 7: контракт /api/ai/file и лимиты — в README."""
        missing = [v for v in ("/api/ai/file", "files", ENV_MB, ENV_CHARS, ENV_TOTAL) if v not in README]
        self.assertEqual(missing, [], "нет в README.md")

    def test_env_example_limits(self):
        """Кр. 5: лимиты настраиваются в registry.env — перечислены в registry.env.example."""
        missing = [v for v in (ENV_MB, ENV_CHARS, ENV_TOTAL) if v not in ENV_EXAMPLE]
        self.assertEqual(missing, [], "нет в registry.env.example")


if __name__ == "__main__":
    unittest.main()
