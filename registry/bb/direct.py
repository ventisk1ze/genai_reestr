"""Прямая работа с Bitbucket — без MCP: git (клон/ветка/коммит/push) и REST API (pull request).

Учётка BB — только в памяти прокси (правила проекта: ни в аргументах, ни в окружении, ни в файлах):
git получает её через askpass-помощник — маленький скрипт без секретов читает строку из
унаследованного канала (pipe в памяти ядра): первый вызов git — логин, второй — пароль.
REST-запрос pull request — заголовок Authorization в памяти процесса.
Локальные пути (не http) git ходит без учётки — поэтому тесты гоняются на локальных
bare-репозиториях без сети; http(s)-адрес принимается только с хоста REG_BB_BASE_URL.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from registry import config, creds

# askpass-помощник: учётки в нём нет — только номер канала, откуда читать ответ
ASKPASS_TEMPLATE = """#!/bin/sh
# GIT_ASKPASS реестра: ответ читается из унаследованного канала (fd {fd}); учётки в файле нет.
IFS= read -r answer <&{fd}
printf '%s\\n' "$answer"
"""

BB_STEP = {"clone": "не удалось клонировать репозиторий", "fetch": "не удалось получить изменения",
           "checkout": "не удалось перейти на ветку", "новая ветка": "не удалось создать ветку",
           "add": "не удалось добавить файлы в git", "status": "не удалось проверить изменения",
           "commit": "не удалось сделать коммит", "push": "не удалось отправить ветку в Bitbucket",
           "pull request": "не удалось создать pull request"}
BB_HINTS = (   # первая подходящая подсказка — по тексту ошибки (уже с маской учётки)
    (r"please tell me who you are|user\.email|user\.name|empty ident|auto-detect email",
     "задайте git-идентичность в своём контейнере — git config --global user.name \"Фамилия Имя\" и "
     "git config --global user.email \"ваша почта\" — и повторите"),
    (r"no such file or directory: '?git'?|git: not found|git: command not found",
     "в вашем контейнере не найден git — он нужен для работы с Bitbucket"),
    (r"authentication failed|\b401\b|unauthori[sz]ed|invalid (username|password|credentials)|"
     r"incorrect (username|password)|could not read (username|password)|authentication required",
     "Bitbucket не принял логин или пароль BB — перезапустите bash connect.sh и введите их заново"),
    (r"\b403\b|forbidden|permission denied|not permitted|access denied|insufficient permission",
     "у вашей учётки BB нет прав на этот репозиторий — попросите доступ у владельца репозитория"),
    (r"certificate|\bssl\b",
     "Bitbucket не прошёл проверку SSL-сертификата в вашем контейнере"),
    (r"could not resolve host|name or service not known|connection refused|timed out|network is unreachable|"
     r"failed to connect|connection reset",
     "Bitbucket недоступен из вашего контейнера — проверьте сеть и REG_BB_BASE_URL"),
    (r"did not match any|pathspec|unknown revision|invalid reference|couldn't find remote ref",
     "в репозитории нет целевой ветки — проверьте ветку в настройках у владельца реестра"),
    (r"repository not found|not found|\b404\b|does not exist",
     "репозиторий не найден — проверьте адрес и права у владельца реестра"),
)


class BbFail(Exception):
    """Ошибка шага работы с Bitbucket: step — где упало, text — суть (маска учётки — при выводе)."""

    def __init__(self, step, text=""):
        super().__init__(text)
        self.step, self.text = step, str(text or "")


class BbNothing(Exception):
    """Изменений нет — коммитить и создавать pull request нечего."""


def bb_message(step, text):
    """Ошибка для человека: по-русски, с шагом и подсказкой, без учётки."""
    text = creds.bb_mask(text).strip()
    if len(text) > 700:
        text = text[:700].rstrip() + "…"
    hint = next((h for rx, h in BB_HINTS if re.search(rx, text, re.I)), "")
    return (f"BB, шаг «{step}»: {BB_STEP.get(step, 'ошибка')}" + (f": {text}" if text else "")
            + (f"\nЧто сделать: {hint}." if hint else ""))


def repo_url_parts(url):
    """(project, repo) из адреса: последние два сегмента пути, .git отбрасывается —
    работает и для …/scm/<project>/<repo>.git, и для локальных путей тестов …/<project>/<repo>.git."""
    m = re.search(r"/(~?[\w.\-]+)/([\w.\-]+?)(?:\.git)?/?$", url) if isinstance(url, str) else None
    return (m.group(1), m.group(2)) if m else None


def url_ok_for_creds(url):
    """Адрес безопасен для учётки: локальный путь (учётка не отправляется) или свой Bitbucket
    (REG_BB_BASE_URL) — чужому хосту Basic-учётку не отправляем."""
    if not isinstance(url, str) or not url.strip():
        return False
    if not re.match(r"^https?://", url.strip()):
        return True   # локальный путь / ssh — Basic-учётку git не отправляет
    from registry.bb import bb_url_parts
    return bb_url_parts(url.strip()) == bb_url_parts(config.BB_BASE_URL)


def kb_file_url(project, repo, branch, path):
    """Ссылка на файл в Bitbucket (browse): после влития PR тот же путь в целевой ветке."""
    seg = "/".join(quote(s, safe="") for s in path.split("/"))
    return (f"{config.BB_BASE_URL}/projects/{quote(project, safe='~')}/repos/{quote(repo, safe='')}"
            f"/browse/{seg}?at={quote(branch, safe='')}")


def git(args, cwd=None, step="git", check_creds=True):
    """git-команда; учётка — через askpass-канал (в памяти), не в аргументах/окружении/файлах.
    Возвращает stdout; ненулевой код — BbFail с шагом и замаскированным текстом."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("BITBUCKET_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    r = helper = None
    if check_creds and creds.BB_USER:
        r, w = os.pipe()
        os.write(w, f"{creds.BB_USER}\n{creds.BB_PASS}\n".encode("utf-8", "surrogateescape"))
        os.close(w)   # учётка — в буфере канала (память ядра), читает askpass-помощник
        helper = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8")
        helper.write(ASKPASS_TEMPLATE.format(fd=r))
        helper.close()
        os.chmod(helper.name, 0o700)
        env["GIT_ASKPASS"] = helper.name
    try:
        p = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=cwd, env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=config.BB_TIMEOUT,
                           pass_fds=(r,) if r is not None else ())
        if p.returncode:
            raise BbFail(step, (p.stderr or p.stdout or "").strip()[-700:])
        return p.stdout
    except subprocess.TimeoutExpired:
        raise BbFail(step, f"git не завершился за {config.BB_TIMEOUT} с (предел — REG_BB_TIMEOUT)")
    finally:
        if r is not None:
            os.close(r)
        if helper:
            os.unlink(helper.name)


def ensure_clone(url, path):
    """Постоянный клон: папка с .git — переиспользуем; нет или сломан — клонируем заново.
    Клон — чистый кэш: живёт между отправками и перезапусками прокси, на диске удаляется только
    для переклонирования при сбое."""
    p = Path(path)
    if (p / ".git").is_dir():
        return p
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    git(["clone", "--quiet", url, str(p)], step="clone")
    return p


def refresh_clone(path, branch):
    """Обновить кэш-клон до свежего состояния удалённой ветки: fetch + сброс локальной ветки
    на origin/<ветка> (клон — кэш, локальных правок в нём не бывает)."""
    rp = str(path)
    git(["fetch", "--quiet", "origin", branch], cwd=rp, step="fetch")
    git(["checkout", "--quiet", "-f", "-B", branch, f"origin/{branch}"], cwd=rp, step="checkout")


def no_changes(path):
    """Рабочее дерево клона не отличается от HEAD — коммитить нечего."""
    return not git(["status", "--porcelain"], cwd=str(path), step="status").strip()


def rest_create_pr(project, repo, title, description, src, dst):
    """Pull request напрямую через REST API Bitbucket (Basic-авторизация — в памяти).
    Возвращает (ссылка, номер)."""
    if not creds.BB_USER:
        raise BbFail("pull request", "логин BB не введён — перезапустите bash connect.sh")
    url = (f"{config.BB_BASE_URL}/rest/api/1.0/projects/{quote(project, safe='~')}"
           f"/repos/{quote(repo, safe='')}/pull-requests")
    payload = {"title": title, "description": description,
               "fromRef": {"id": f"refs/heads/{src}"}, "toRef": {"id": f"refs/heads/{dst}"}}
    auth = base64.b64encode(f"{creds.BB_USER}:{creds.BB_PASS}".encode("utf-8", "surrogateescape")).decode("ascii")
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "Authorization": "Basic " + auth})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=config.BB_TIMEOUT) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise BbFail("pull request", f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    except Exception as e:   # сеть, таймаут, не-JSON
        raise BbFail("pull request", f"{type(e).__name__}: {e}")
    pid = d.get("id") if isinstance(d, dict) else None
    href = ""
    try:
        href = d["links"]["self"][0]["href"]
    except (KeyError, IndexError, TypeError):
        pass
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid < 10 ** 12 \
            or not isinstance(href, str) or not href:
        raise BbFail("pull request", "Bitbucket не вернул номер и ссылку pull request")
    return href, pid
