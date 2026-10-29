"""Файлы в лава-болле (T-009): .docx и .txt, текст извлекает сервер в памяти.

На диск файлы не пишутся. Лимиты применяет сервер, клиенту не доверяет: размер файла,
текст одного файла, текст всех файлов диалога. Защита от zip-бомб и внешних сущностей XML —
разбор потоком (pyexpat), дерево в памяти не строится.
"""
import base64
import codecs
import io
import os
import re
import threading
import xml.parsers.expat
import zipfile

from registry import config
from registry.errors import BadRequest

_W_URIS = ("http://schemas.openxmlformats.org/wordprocessingml/2006/main",
           "http://purl.oclc.org/ooxml/wordprocessingml/main")                 # обычный OOXML и Strict
_MC_URI = "http://schemas.openxmlformats.org/markup-compatibility/2006"        # mc:Fallback — дубль надписей
# не текст документа: свойства, удалённое и перемещённое при рецензировании, коды полей
_DOCX_SKIP = {"rPr", "sectPr", "tblPr", "tblGrid", "trPr", "tcPr", "sdtPr", "sdtEndPr", "del", "moveFrom",
              "delText", "instrText", "delInstrText", "fldData"}
_DOCX_CHARS = {"tab": "\t", "ptab": "\t", "br": "\n", "cr": "\n", "noBreakHyphen": "-"}
_CTRL = re.compile(r"[\x01-\x08\x0e-\x1f\x7f]")
_NOT_TEXT = ((b"PK\x03\x04", "это документ Word или архив — если это документ Word, приложите его как .docx"),
             (b"%PDF", "это PDF — сохраните документ как .docx или .txt"),
             (b"\xd0\xcf\x11\xe0", "это документ старого формата Office (.doc) — сохраните его как .docx"),
             (b"\x89PNG", "это картинка"), (b"\xff\xd8\xff", "это картинка"), (b"GIF8", "это картинка"))

DOCX_PART_MAX = 50 * 1024 * 1024        # XML внутри .docx после распаковки — больше не читаем (zip-бомба)
DOCX_RATIO_MAX = 100                    # XML сжат сильнее (у документов Word — единицы-десятки раз) —
DOCX_RATIO_FROM = 10 * 1024 * 1024      # … и после распаковки больше 10 МБ — похоже на zip-бомбу
DOCX_DEPTH_MAX = 256                    # вложенность XML: у настоящих документов — десятки уровней
FILE_SLOTS = threading.BoundedSemaphore(1)   # разбор файлов — по одному: это чистая работа процессора


def _mb(x):
    return f"{x:g}".replace(".", ",")


def _num(n):
    return f"{n:,}".replace(",", " ")


def clean_file_name(name):
    """Имя файла для плашки и разметки: без пути и управляющих символов, не длиннее 150 символов."""
    if not isinstance(name, str):
        return ""
    name = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", re.split(r"[\\/]", name)[-1]).split())
    if len(name) > 150:
        stem, ext = os.path.splitext(name)
        name = stem[:150 - len(ext)] + ext if len(ext) <= 10 else name[:150]
    return name


def ai_file_type(name):
    """Расширение .docx или .txt (регистр не важен); другое — понятная ошибка с подсказкой."""
    ext = os.path.splitext(name)[1].lower()
    if ext in config.AI_FILE_TYPES:
        return ext
    if ext == ".doc":
        raise BadRequest(f"Файл «{name}»: формат .doc (старый Word) не поддерживается — "
                         "откройте его в Word и сохраните как .docx")
    if ext == ".pdf":
        raise BadRequest(f"Файл «{name}»: PDF не поддерживается — сохраните документ как .docx или .txt")
    if not ext:
        raise BadRequest(f"У файла «{name}» нет расширения — приложите .docx или .txt")
    raise BadRequest(f"Файл «{name}»: формат {ext} не поддерживается — приложите .docx или .txt")


def txt_text(raw, name="файл.txt"):
    """Текст .txt: BOM UTF-8 / UTF-16 (UTF-32) — по нему, иначе строгий UTF-8, не вышло — cp1251.
    Переводы строк Windows → \\n, NUL убираются; двоичный файл под видом .txt — ошибка."""
    for sig, what in _NOT_TEXT:
        if raw.startswith(sig):
            raise BadRequest(f"Файл «{name}» — не текст: {what}")
    if raw.startswith(codecs.BOM_UTF8):
        text = raw[3:].decode("utf-8", "replace")
    elif raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        text = raw.decode("utf-32", "replace")
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = raw.decode("utf-16", "replace")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1251", "replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    sample = text[:1 << 20]   # двоичный файл двоичный везде — хватит начала
    if len(_CTRL.findall(sample)) > max(16, len(sample) // 20):
        raise BadRequest(f"Файл «{name}» не похож на текст — сохраните его как .txt (UTF-8) или приложите .docx")
    return text


def _xml_parser(name):
    """pyexpat: разбор потоком, дерево в памяти не строится; на первом же DOCTYPE или ENTITY — отказ
    (защита от «миллиарда смешков» и внешних сущностей)."""
    p = xml.parsers.expat.ParserCreate(namespace_separator=" ")

    def no_dtd(*_):
        raise BadRequest(f"Файл «{name}» отклонён: в XML внутри него есть DOCTYPE или ENTITY — "
                         "в документах Word такого не бывает")
    p.StartDoctypeDeclHandler = p.EntityDeclHandler = no_dtd
    p.buffer_text = True
    return p


def _zip_feed(zf, part, parser, name, cap=DOCX_PART_MAX):
    """Часть .docx — в парсер кусками прямо из архива. До чтения — размер после распаковки и степень сжатия
    (zip-бомба: больше cap или больше DOCX_RATIO_FROM при сжатии сильнее DOCX_RATIO_MAX); больше объявленного
    размера zipfile не отдаст — ошибка CRC."""
    info = zf.getinfo(part)
    ratio = info.file_size // max(info.compress_size, 1)
    if info.file_size > cap or (info.file_size > DOCX_RATIO_FROM and ratio > DOCX_RATIO_MAX):
        raise BadRequest(f"Файл «{name}» отклонён: похоже на zip-бомбу — документ внутри распаковывается "
                         f"в {_mb(round(info.file_size / 2 ** 20, 1))} МБ (сжатие {ratio}:1)")
    with zf.open(info) as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            parser.Parse(chunk, False)
    parser.Parse(b"", True)


def _docx_main_part(zf, name):
    """Основная часть документа — по _rels/.rels (обычно word/document.xml)."""
    names, found = set(zf.namelist()), []

    def rel(tag, attrs):
        if str(attrs.get("Type", "")).endswith("/officeDocument"):
            found.append(str(attrs.get("Target", "")).lstrip("/"))
    if "_rels/.rels" in names:
        p = _xml_parser(name)
        p.StartElementHandler = rel
        _zip_feed(zf, "_rels/.rels", p, name, cap=2 ** 20)
    return next((t for t in found if t in names), "word/document.xml")


class _DocxText:
    """Текст document.xml по событиям pyexpat: дерево не строится, память не растёт с размером документа.
    В порядке документа: абзацы (w:t; w:tab — таб; w:br, w:cr — перевод строки; пустые схлопываются), абзацы
    списков (w:numPr) — с «- », вложенные — с отступом; таблицы — строка на строку, ячейки через « | »,
    вложенная таблица сплющивается в текст ячейки; надписи — своими строками после абзаца.
    Хранится только начало текста (limit символов), длина всего текста (chars) считается точно."""

    def __init__(self, name, limit):
        self.name, self.limit = name, limit
        self.depth = self.skip = self.ppr = self.num = self.t_end = 0   # 0 — «нет»: глубина корня — 1
        self.t_buf = None        # куда идёт текст открытого w:t
        self.open = []           # открытые абзацы, таблицы, строки, ячейки, надписи
        self.body = False
        self.parts, self.kept, self.chars, self.lines = [], 0, 0, 0

    # --- строки тела документа: хранятся, пока набралось меньше limit символов, дальше только считаются
    def append(self, line):
        if not self.lines:
            line = line.lstrip()
        self.chars += len(line) + (1 if self.lines else 0)
        self.lines += 1
        if self.kept <= self.limit:
            self.parts.append(line)
            self.kept += len(line) + 1

    def extend(self, lines):
        for line in lines:
            self.append(line)

    def text(self):
        return "\n".join(self.parts)[:self.limit]

    def _sink(self, cur):
        """Куда идут строки блоков: тело документа, ячейка таблицы или надпись."""
        if cur is None:
            return self
        return cur["lines"] if cur["kind"] in ("tc", "txbx") else cur["out"]

    def start(self, name, attrs):
        self.depth += 1
        d = self.depth
        if d > DOCX_DEPTH_MAX:
            raise BadRequest(f"Файл «{self.name}» отклонён: слишком глубокая вложенность XML — "
                             "документы Word так не устроены")
        if self.skip:
            return
        uri, _, tag = name.rpartition(" ")
        if uri not in _W_URIS:   # чужие элементы (рисунки, mc:AlternateContent…) прозрачны, кроме mc:Fallback
            if d == 1:
                raise ValueError("корень — не w:document")
            if uri == _MC_URI and tag == "Fallback":
                self.skip = d
            return
        if self.ppr:   # свойства абзаца: нужен только w:numPr — уровень (ilvl) и номер списка (numId)
            if tag == "numPr" and d == self.ppr + 1:
                self.num = d
                self.open[-1]["list"] = True
            elif self.num and d == self.num + 1 and tag in ("ilvl", "numId"):
                self.open[-1][tag] = next((v for k, v in attrs.items() if k.rpartition(" ")[2] == "val"), "")
            return
        cur = self.open[-1] if self.open else None
        in_p = cur is not None and cur["kind"] == "p"
        if d == 1:
            if tag != "document":
                raise ValueError("корень — не w:document")
        elif tag == "t":
            if in_p:
                self.t_end, self.t_buf = d, cur["buf"]
        elif tag in _DOCX_CHARS:
            if in_p:
                cur["buf"].append(_DOCX_CHARS[tag])
        elif tag == "p":
            if not in_p:   # абзац прямо в абзаце (не в надписи) — часть внешнего
                self.open.append({"kind": "p", "d": d, "out": self._sink(cur), "buf": [], "extra": [],
                                  "list": False, "ilvl": "0", "numId": ""})
        elif tag == "pPr":
            if in_p and d == cur["d"] + 1:
                self.ppr = d
            else:
                self.skip = d
        elif tag in _DOCX_SKIP:
            self.skip = d
        elif tag == "body":
            self.body = self.body or d == 2
        elif tag == "tbl":
            if not in_p:
                self.open.append({"kind": "tbl", "d": d, "out": self._sink(cur),
                                  "flat": any(f["kind"] == "tc" for f in self.open)})
        elif tag == "tr":
            if cur is not None and cur["kind"] == "tbl":
                self.open.append({"kind": "tr", "d": d, "out": cur["out"], "flat": cur["flat"], "cells": []})
        elif tag == "tc":
            if cur is not None and cur["kind"] == "tr":
                self.open.append({"kind": "tc", "d": d, "lines": [], "row": cur["cells"]})
        elif tag == "txbxContent":
            if in_p:
                self.open.append({"kind": "txbx", "d": d, "lines": cur["extra"]})

    def end(self, name):
        d = self.depth
        self.depth -= 1
        if self.skip:
            if d == self.skip:
                self.skip = 0
            return
        if self.ppr:
            if d == self.ppr:
                self.ppr = self.num = 0
            elif d == self.num:
                self.num = 0
            return
        if d == self.t_end:
            self.t_end, self.t_buf = 0, None
        elif self.open and self.open[-1]["d"] == d:
            f = self.open.pop()
            if f["kind"] == "p":
                self._end_p(f)
            elif f["kind"] == "tc":
                f["row"].append(" ".join(" ".join(f["lines"]).split()))
            elif f["kind"] == "tr" and any(f["cells"]):
                cells = f["cells"]
                f["out"].append(" ".join(c for c in cells if c) if f["flat"] else " | ".join(cells).strip())

    def data(self, s):
        if self.t_buf is not None:
            self.t_buf.append(s.replace("\r", "\n"))

    def _end_p(self, f):
        if not f["buf"] and not f["extra"]:   # пустой абзац — частый случай, без лишней работы
            return
        lines = [s.strip() for s in "".join(f["buf"]).split("\n")]
        lines = [s for s in lines if s]   # пустые абзацы и пустые строки схлопываются
        if lines:
            if f["list"] and f["numId"] != "0":   # numId = 0 — нумерация снята
                try:
                    lvl = max(0, min(int(f["ilvl"]), 8))
                except ValueError:
                    lvl = 0
                lines[0] = "  " * lvl + "- " + lines[0]
            f["out"].append("\n".join(lines))
        f["out"].extend(f["extra"])


def docx_text(raw, name="файл.docx", limit=None):
    """Текст .docx → (начало текста до limit символов, длина всего текста). zipfile поверх BytesIO, XML — потоком
    (_DocxText): на диск ничего не пишется. Колонтитулы, сноски и примечания не берутся."""
    if raw.startswith(b"\xd0\xcf\x11\xe0"):
        raise BadRequest(f"Файл «{name}» — не .docx: это старый формат Word (.doc) или документ с паролем. "
                         "Снимите пароль и сохраните документ как .docx")
    if raw.startswith(b"%PDF"):
        raise BadRequest(f"Файл «{name}» — не .docx, а PDF: сохраните документ как .docx или .txt")
    bad = BadRequest(f"Файл «{name}» повреждён или это не .docx — откройте его в Word и сохраните как .docx")
    doc = _DocxText(name, config.AI_FILE_MAX_CHARS if limit is None else limit)
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        p = _xml_parser(name)
        p.StartElementHandler, p.EndElementHandler, p.CharacterDataHandler = doc.start, doc.end, doc.data
        _zip_feed(zf, _docx_main_part(zf, name), p, name)
    except BadRequest:
        raise
    except Exception:   # не zip, нет word/document.xml, не документ Word, битый XML, шифрование
        raise bad
    if not doc.body:
        raise bad
    return doc.text(), doc.chars


def extract_text(name, raw, max_chars=None):
    """Текст приложенного файла → (текст до max_chars, длина всего извлечённого текста, обрезан ли).
    Формат — по расширению (.docx, .txt; регистр не важен) и по содержимому; ошибки — BadRequest по-русски."""
    limit = config.AI_FILE_MAX_CHARS if max_chars is None else int(max_chars)
    name = clean_file_name(name) or "файл"
    ext = ai_file_type(name)
    if len(raw) > config.AI_FILE_MAX_BYTES:
        raise BadRequest(f"Файл «{name}» больше {_mb(config.AI_FILE_MAX_MB)} МБ — приложите файл поменьше "
                         "или только нужную часть")
    if not raw:
        raise BadRequest(f"Файл «{name}» пустой")
    if ext == ".docx":
        text, chars = docx_text(raw, name, limit)
    else:
        full = txt_text(raw, name).strip()
        text, chars = full[:limit], len(full)
    if not chars:
        raise BadRequest(f"В файле «{name}» нет текста"
                         + (" — возможно, в нём только картинки или скан" if ext == ".docx" else ""))
    return text, chars, chars > limit


def op_ai_file(body):
    """POST /api/ai/file {name, data: base64} → {name, text, chars, truncated}; text — уже до лимита одного файла."""
    name = clean_file_name(body.get("name"))
    if not name:
        raise BadRequest("Не указано имя файла")
    ai_file_type(name)
    data = body.get("data")
    if not isinstance(data, str):
        raise BadRequest(f"Нет содержимого файла «{name}» — приложите его ещё раз")
    data = data.strip()
    if data.startswith("data:"):   # data URL из FileReader — берём только base64
        data = data.partition(",")[2]
    data = re.sub(r"\s+", "", data)
    if len(data) > (config.AI_FILE_MAX_BYTES + 2) // 3 * 4:
        raise BadRequest(f"Файл «{name}» больше {_mb(config.AI_FILE_MAX_MB)} МБ — приложите файл поменьше "
                         "или только нужную часть")
    try:
        raw = base64.b64decode(data, validate=True)
    except ValueError:
        raise BadRequest(f"Файл «{name}» пришёл повреждённым (некорректный base64) — приложите его ещё раз")
    with FILE_SLOTS:
        text, chars, truncated = extract_text(name, raw)
    return {"name": name, "text": text, "chars": chars, "truncated": truncated}


def ai_files(files):
    """Файлы сообщения пользователя из /api/llm: проверка и обрезка текста каждого до AI_FILE_MAX_CHARS.
    Файл, не влезший раньше в общий лимит, приходит с dropped: true и без текста — так и остаётся."""
    if not isinstance(files, list):
        raise BadRequest("files — список приложенных файлов")
    if len(files) > config.AI_FILES_PER_MSG:
        raise BadRequest(f"К одному сообщению — не больше {config.AI_FILES_PER_MSG} файлов")
    out = []
    for f in files:
        name = clean_file_name(f.get("name")) if isinstance(f, dict) else ""
        if not name:
            raise BadRequest("У приложенного файла нет имени — приложите его заново")
        if f.get("dropped") is True:
            out.append({"name": name, "truncated": f.get("truncated") is True, "dropped": True})
            continue
        text = f.get("text")
        if not isinstance(text, str):
            raise BadRequest(f"Нет текста файла «{name}» — приложите его заново")
        out.append({"name": name, "text": text[:config.AI_FILE_MAX_CHARS],
                    "truncated": f.get("truncated") is True or len(text) > config.AI_FILE_MAX_CHARS})
    return out


def ai_dialog(body):
    """Диалог из запроса /api/llm: сообщения user/assistant с текстом (T-006), у user — files (T-009).
    Текст всех файлов — до AI_FILES_MAX_CHARS, от новых сообщений к старым: не влезший файл теряет текст,
    имя остаётся с dropped: true. Один и тот же диалог уходит модели (оба движка) и в историю."""
    raw = body.get("messages")
    msgs = []
    for m in raw if isinstance(raw, list) else []:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        files = m.get("files") if m["role"] == "user" else None
        content = "" if m.get("content") is None and files else m.get("content")
        if not isinstance(content, str):
            continue
        x = {"role": m["role"], "content": content}
        if files is not None and files != []:
            x["files"] = ai_files(files)
        msgs.append(x)
    left = config.AI_FILES_MAX_CHARS
    for x in reversed(msgs):
        for f in x.get("files", ()):
            if f.get("dropped"):
                continue
            if len(f["text"]) <= left:
                left -= len(f["text"])
            else:
                del f["text"]
                f["dropped"] = True
    return msgs


def ai_file_block(f):
    """Файл в сообщении для модели: имя и границы видны явно, пометки об обрезке — внутри разметки."""
    name = f["name"]
    if f.get("dropped"):
        return (f"[Файл «{name}» — текст не передаётся: не поместился в общий лимит текста файлов диалога "
                f"({_num(config.AI_FILES_MAX_CHARS)} символов)]")
    note = f" — текст обрезан, переданы первые {_num(len(f['text']))} символов" if f.get("truncated") else ""
    return f"[Файл «{name}»{note}]\n{f['text']}\n[Конец файла «{name}»]"


def ai_content(m):
    """Содержимое сообщения для модели: сначала приложенные файлы в разметке, потом текст сообщения
    (до 20 000 символов — лимит относится к тексту, не к файлам)."""
    text = m["content"][:20000]
    if not m.get("files"):
        return text
    return "\n\n".join([ai_file_block(f) for f in m["files"]]
                       + [text.strip() or "(Текста в сообщении нет — только приложенные файлы.)"])


def chat_messages(body):
    """Сообщения для модели (API и GigaCode): последние 30 непустых из диалога, последнее — от пользователя."""
    msgs = [{"role": m["role"], "content": ai_content(m)} for m in ai_dialog(body)
            if m["content"].strip() or m.get("files")][-30:]
    if not msgs or msgs[-1]["role"] != "user":
        raise BadRequest("Пустой запрос к ассистенту")
    return msgs
