"""Онтология базы знаний — фактические папки репозитория (T-014).

Узлы — папки и подпапки в клоне БЗ, не начинающиеся на точку (`.git` и прочие скрытые — не узлы).
Симлинки-папки пропускаем: дерево не должно выводить за пределы клона. Git не хранит пустые папки —
узел появляется в дереве, только когда в папке есть хотя бы один файл (`.gitkeep` или карточка).
Формат дерева для браузера прежний: {nodes: [{name, path, children}]}.
"""
import os

from registry.errors import BadRequest

KB_MAX_NODES = 500   # санити-кап: дерево из безумного репозитория не утащим в браузер


def _dir_ok(name):
    """Папка — узел онтологии: имя не начинается с точки (скрытые — не узлы)."""
    return not name.startswith(".")


def tree_from_clone(clone):
    """Дерево узлов из папок клона (обход вглубь, порядок алфавитный). Папок нет — BadRequest
    с подсказкой создать их в репозитории."""
    root = os.path.realpath(str(clone))

    def bad(why):
        return BadRequest(f"Онтология базы знаний: {why}")

    def walk(dirpath, rel):
        """[узлы] папок внутри dirpath; rel — относительный путь папки ('' для корня)."""
        try:
            entries = sorted(os.scandir(dirpath), key=lambda e: e.name)
        except OSError as e:
            raise bad(f"не удалось прочитать папку «{rel or '.'}»: {e.strerror or e}")
        out = []
        for e in entries:
            if not e.is_dir() or e.name.startswith(".") or e.is_symlink():
                continue   # скрытые и симлинки — не узлы; файлы — не узлы
            if len(out) >= KB_MAX_NODES:
                raise bad(f"больше {KB_MAX_NODES} папок — проверьте структуру репозитория")
            path = f"{rel}/{e.name}" if rel else e.name
            out.append({"name": e.name, "path": path, "children": walk(e.path, path)})
        return out

    nodes = walk(root, "")
    if not nodes:
        raise bad("в репозитории нет папок — узлы онтологии это папки и подпапки (не с точки); "
                  "создайте папки в репозитории базы знаний (в пустую папку положите любой файл, "
                  "например .gitkeep — git не хранит пустые папки)")
    return nodes
