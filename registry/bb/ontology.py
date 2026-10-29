"""Разбор ontology.yml — формат пользователя (эталон — ontology.yml ЦК GenAI в корне реестра):
дерево узлов с отступами, строка «Имя узла:», минимальный отступ — корень. Каждый узел — папка.
Настоящему pyyaml реестру не нужен: формат узкий, парсер маленький и обкатан на эталоне (103 узла)."""
import re

from registry.errors import BadRequest


def parse_ontology(text):
    """ontology.yml → {"nodes": [{name, children}]} (пути папок дополнит вызывающий).
    Ошибки формата — BadRequest по-русски."""
    def bad(why):
        return BadRequest(f"ontology.yml: {why}")

    prep = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if not s.endswith(":"):
            raise bad(f"строка без «:» — {s[:60]}")
        name = s[:-1].strip()
        if not name:
            raise bad("пустое имя узла")
        prep.append((len(raw) - len(raw.lstrip(" ")), name))
    if not prep:
        raise bad("нет узлов")
    pos = [0]

    def parse_items(indent):
        out = []
        while pos[0] < len(prep):
            ind, name = prep[pos[0]]
            if ind < indent:
                break
            if ind > indent:
                raise bad(f"неожиданный отступ у «{name[:60]}»")
            pos[0] += 1
            kids = []
            if pos[0] < len(prep) and prep[pos[0]][0] > ind:   # дети — все строки глубже, до их отступа
                kids = parse_items(prep[pos[0]][0])
            out.append({"name": name, "children": kids})
        return out

    items = parse_items(prep[0][0])
    add_paths(items, "")
    return items


def add_paths(items, prefix):
    for it in items:
        it["path"] = f"{prefix}/{it['name']}" if prefix else it["name"]
        add_paths(it["children"], it["path"])


def ontology_from_clone(clone, branch):
    """Прочитать онтологию из клона (файл ontology.yml на выбранной ветке) → дерево с путями.
    Файла нет — BadRequest с именем файла."""
    import os
    p = os.path.join(str(clone), "ontology.yml")
    if not os.path.isfile(p):
        raise BadRequest(f"в базе знаний нет ontology.yml (ветка {branch}) — "
                         "добавьте онтологию в репозиторий базы знаний")
    return parse_ontology(open(p, encoding="utf-8").read())
