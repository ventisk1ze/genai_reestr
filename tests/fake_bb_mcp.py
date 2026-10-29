"""Фейковый MCP-сервер Bitbucket для тестов T-010/T-011 (только stdlib) — подменяет server.py пользователя.

Тест копирует этот файл в свою временную папку как server.py и кладёт рядом fake_bb.json:
  bare        — путь к локальному bare-репозиторию: любой URL клонирования ведёт туда
  repos_root  — папка репозиториев баз знаний (T-011): <repos_root>/<project>/<repo>.git; URL вида
                /scm/<project>/<repo>.git резолвится сюда, остальное — в bare
  journal     — журнал (JSON Lines): старт процесса, initialize, каждый вызов инструмента
  counter     — файл-счётчик номеров pull request
  scan        — папки, где при старте и на каждом вызове ищутся файлы с паролем (утечки во временные файлы)
  fail        — режим отказа: "" | "start" | "fail:<инструмент>" | "fail_quoted:<инструмент>" (пароль в URL
                через quote(safe="")) | "fail_quoted_slash:<инструмент>" (quote() по умолчанию — «/» не кодируется) |
                "hang:<инструмент>" | "hang:initialize" | "exit:<инструмент>"
  pr_url      — подменить ссылку в ответе bitbucket_create_pull_request (проверка, что прокси ей не верит вслепую)
  chatty      — перед каждым ответом на tools/call: уведомление notifications/message и запрос ping серверу-клиенту
                (как SDK при логировании инструмента), а при старте — строка не-JSON в stdout (print в server.py)
  status      — формат bitbucket_status: "porcelain" (по умолчанию; пусто — изменений нет) или "long"
  git_env     — GIT_AUTHOR_*/GIT_COMMITTER_* и пр. для git (глобальный конфиг git не читается и не трогается)
FAKE_BB_CONFIG в окружении — другой путь к fake_bb.json (запасной вариант).

Протокол — как у настоящего SDK (mcp 2.3.0): JSON-RPC 2.0 по stdio, одна строка — одно сообщение;
initialize -> ответ с protocolVersion 2025-06-18; запросы до notifications/initialized — ошибка;
tools/call -> result.content [{type: "text", text}], result.isError; ошибка инструмента —
isError: true и текст «Error executing tool <имя>: …».

Учётка: лаунчер реестра кладёт её в os.environ этого процесса (BITBUCKET_USERNAME, BITBUCKET_PASSWORD).
В журнал пишутся только sha256 значений, а также исходные argv и окружение процесса так, как их видит ОС
(Linux — /proc/self/cmdline и /proc/self/environ, macOS — sysctl KERN_PROCARGS2): по ним тест проверяет,
что учётки нет ни в аргументах, ни в исходном окружении процесса. Все данные выдуманные."""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PROTOCOL = "2025-06-18"
HERE = Path(__file__).resolve().parent
CFG_PATH = Path(os.environ.get("FAKE_BB_CONFIG") or HERE / "fake_bb.json")
STARTED = time.time()

try:   # «в его папке»: python server.py кладёт папку скрипта в sys.path[0] — соседние модули импортируются
    import fake_bb_sibling  # noqa: F401
    SIBLING_OK = True
except Exception:   # noqa
    SIBLING_OK = False


def cfg():
    try:
        return json.loads(CFG_PATH.read_text("utf-8"))
    except Exception:   # noqa
        return {}


def sha(v):
    return hashlib.sha256(v.encode("utf-8")).hexdigest() if v is not None else None


def raw_procargs(pid=None):
    """Исходные argv и окружение процесса глазами ОС -> (способ, [argv], [env]) байтами; не вышло — (None, None, None).
    Значения, положенные в os.environ после старта, сюда не попадают — это и есть аналог /proc/<pid>/environ."""
    pid = pid or os.getpid()
    try:
        if sys.platform.startswith("linux"):
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            return "proc", [a for a in argv[:-1]], [e for e in env if e]
        if sys.platform == "darwin":
            import ctypes
            import ctypes.util
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            mib = (ctypes.c_int * 3)(1, 49, pid)   # CTL_KERN, KERN_PROCARGS2
            n = ctypes.c_size_t(0)
            if libc.sysctl(mib, 3, None, ctypes.byref(n), None, 0) != 0:
                return None, None, None
            buf = ctypes.create_string_buffer(n.value)
            if libc.sysctl(mib, 3, buf, ctypes.byref(n), None, 0) != 0:
                return None, None, None
            raw = buf.raw[:n.value]
            argc = int.from_bytes(raw[:4], sys.byteorder)
            rest = raw[4:]
            rest = rest[rest.index(b"\0"):].lstrip(b"\0")   # путь к исполняемому файлу и выравнивание
            parts = rest.split(b"\0")
            return "sysctl", parts[:argc], [p for p in parts[argc:] if p]
    except Exception:   # noqa
        pass
    return None, None, None


def leaks():
    """Файлы в папках scan, где встречается пароль (в имени или содержимом) — только пути, без содержимого."""
    pw = os.environ.get("BITBUCKET_PASSWORD") or ""
    if not pw:
        return []
    needles = {pw.encode("utf-8")}
    try:
        from urllib.parse import quote
        needles.add(quote(pw, safe="").encode("utf-8"))
    except Exception:   # noqa
        pass
    found = []
    for d in cfg().get("scan") or []:
        for root, _dirs, files in os.walk(d):
            for name in files:
                p = os.path.join(root, name)
                try:
                    with open(p, "rb") as f:
                        data = f.read(4 * 1024 * 1024)
                except OSError:
                    continue
                if any(x in data or x in p.encode("utf-8", "replace") for x in needles):
                    found.append(p)
    return found


def journal(rec):
    path = cfg().get("journal")
    if not path:
        return
    rec = {"t": round(time.time(), 3), "pid": os.getpid(), **rec}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply(mid, result):
    send({"jsonrpc": "2.0", "id": mid, "result": result})


def rpc_error(mid, code, message):
    send({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})


class ToolError(Exception):
    pass


def git(args, cwd=None, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("BITBUCKET_")}
    env.update(cfg().get("git_env") or {})
    p = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if check and p.returncode:
        raise ToolError(f"Command 'git {' '.join(args)}' returned non-zero exit status {p.returncode}. "
                        f"{(p.stderr or p.stdout).strip()}")
    return p.stdout


def _repo(args):
    rp = args.get("repo_path")
    if not isinstance(rp, str) or not rp:
        raise ToolError("repo_path is required")
    if not (Path(rp) / ".git").is_dir():
        raise ToolError(f"Not a git repository: {rp}")
    return rp


def repo_for_url(url):
    """URL клонирования → bare-репозиторий: базы знаний (T-011) — из repos_root/<project>/<repo>.git,
    остальное — единый bare (тесты T-010)."""
    if not isinstance(url, str) or not url:
        raise ToolError("url is required")
    root = cfg().get("repos_root")
    m = re.search(r"/scm/(~?[\w.\-]+)/([\w.\-]+?)(?:\.git)?/?$", url) if root else None
    if m:
        p = Path(root) / m.group(1) / f"{m.group(2)}.git"
        if p.is_dir():
            return str(p)
        raise ToolError(f"Repository {m.group(1)}/{m.group(2)} not found")
    return str(cfg()["bare"])


def parse_ontology(text):
    """Разбор ontology.yml — формат пользователя (T-011): дерево узлов с отступами, строка «Имя узла:»;
    минимальный отступ — корень. Возвращает {"nodes": [{name, path, children}]} — как kb_ontology
    настоящего server.py (pyyaml). Вместо pyyaml — разбор по отступам, фейку достаточно."""
    prep = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if not s.endswith(":"):
            raise ToolError(f"ontology.yml: строка без «:» — {s[:60]}")
        name = s[:-1].strip()
        if not name:
            raise ToolError("ontology.yml: пустое имя узла")
        prep.append((len(raw) - len(raw.lstrip(" ")), name))
    if not prep:
        raise ToolError("ontology.yml: нет узлов")
    pos = [0]

    def parse_items(indent):
        out = []
        while pos[0] < len(prep):
            ind, name = prep[pos[0]]
            if ind < indent:
                break
            if ind > indent:
                raise ToolError(f"ontology.yml: неожиданный отступ у «{name[:60]}»")
            pos[0] += 1
            kids = []
            if pos[0] < len(prep) and prep[pos[0]][0] > ind:   # дети — все строки глубже, до их отступа
                kids = parse_items(prep[pos[0]][0])
            out.append({"name": name, "children": kids})
        return out

    items = parse_items(prep[0][0])

    def add_paths(items, prefix):
        for it in items:
            it["path"] = f"{prefix}/{it['name']}" if prefix else it["name"]
            add_paths(it["children"], it["path"])

    add_paths(items, "")
    return {"nodes": items}


def failing_text(tool, quoted=""):
    """Текст отказа с учёткой внутри — как у git, когда логин и пароль попали в URL (проверка маскировки).
    quoted: "" — как есть, "all" — quote(safe=""), "slash" — quote() по умолчанию (safe="/")."""
    from urllib.parse import quote
    user = os.environ.get("BITBUCKET_USERNAME") or ""
    pw = os.environ.get("BITBUCKET_PASSWORD") or ""
    if quoted == "all":
        user, pw = quote(user, safe=""), quote(pw, safe="")
    elif quoted == "slash":
        user, pw = quote(user), quote(pw)
    base = (os.environ.get("BITBUCKET_BASE_URL") or "https://bb.example.test").split("://", 1)[-1]
    url = f"https://{user}:{pw}@{base}/scm/kbproj/kb-repo.git"
    return (f"Command '['git', '{tool.removeprefix('bitbucket_')}', '{url}']' returned non-zero exit status 128. "
            f"remote: Invalid username or password. fatal: Authentication failed for '{url}' "
            f"(user {user}, password {pw})")


def tool_call(name, args):
    c = cfg()
    mode = c.get("fail") or ""
    if mode == f"hang:{name}":
        time.sleep(3600)
    if mode == f"exit:{name}":
        sys.stderr.write(f"fake bb: падение на {name}, пароль {os.environ.get('BITBUCKET_PASSWORD')}\n")
        sys.stderr.flush()
        os._exit(3)
    if mode in (f"fail:{name}", f"fail_quoted:{name}", f"fail_quoted_slash:{name}"):
        text = failing_text(name, quoted={"fail_quoted": "all", "fail_quoted_slash": "slash"}.get(mode.split(":")[0], ""))
        sys.stderr.write(f"ERROR {text}\n")   # SDK пишет ошибки инструмента и в stderr
        sys.stderr.flush()
        raise ToolError(text)
    if name == "bitbucket_clone":
        url, path = args.get("url"), args.get("path")
        if not isinstance(url, str) or not isinstance(path, str) or not url or not path:
            raise ToolError("url and path are required")
        if os.path.exists(path) and os.listdir(path):
            raise ToolError(f"destination path '{path}' already exists and is not an empty directory")
        git(["clone", "--quiet", repo_for_url(url), path])
        return f"Cloned repository into {path}"
    if name == "bitbucket_checkout":
        rp = _repo(args)
        br = args.get("branch")
        if not isinstance(br, str) or not br:
            raise ToolError("branch is required")
        git(["checkout", "--quiet", *(["-b"] if args.get("create") else []), br], cwd=rp)
        return f"Switched to {'a new ' if args.get('create') else ''}branch '{br}'"
    if name == "bitbucket_add":
        rp = _repo(args)
        paths = args.get("paths")
        if isinstance(paths, str):
            paths = [paths]
        if not paths or not all(isinstance(p, str) and p for p in paths):
            raise ToolError("paths is required")
        git(["add", "--", *paths], cwd=rp)
        return f"Added {len(paths)} path(s)"
    if name == "bitbucket_status":
        rp = _repo(args)
        if c.get("status") == "long":
            return git(["status"], cwd=rp)
        return git(["status", "--porcelain"], cwd=rp)
    if name == "bitbucket_commit":
        rp = _repo(args)
        msg = args.get("message")
        if not isinstance(msg, str) or not msg.strip():
            raise ToolError("message is required")
        return git(["commit", "--quiet", "-m", msg], cwd=rp) or "Committed"
    if name == "bitbucket_push":
        rp = _repo(args)
        br = args.get("branch")
        if not isinstance(br, str) or not br:
            raise ToolError("branch is required")
        git(["push", "--quiet", args.get("remote") or "origin", br], cwd=rp)
        return f"Pushed {br}"
    if name == "bitbucket_create_pull_request":
        need = ("project", "repo", "source_branch", "target_branch", "title")
        if not all(isinstance(args.get(k), str) and args.get(k) for k in need):
            raise ToolError("project, repo, source_branch, target_branch and title are required")
        base = (os.environ.get("BITBUCKET_BASE_URL") or "").rstrip("/")
        pr_repo = repo_for_url(f"{base}/scm/{args['project']}/{args['repo']}.git")
        for br in (args["source_branch"], args["target_branch"]):
            if git(["--git-dir", pr_repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{br}"],
                   check=False).strip() == "":
                raise ToolError(f"Branch {br} does not exist in {args['project']}/{args['repo']}")
        if not git(["--git-dir", pr_repo, "diff", "--name-only",
                    args["target_branch"], args["source_branch"]]).strip():
            raise ToolError("Pull request has no changes (source branch is already up to date)")   # как Bitbucket
        counter = Path(c["counter"])
        n = int(counter.read_text() or 0) + 1 if counter.exists() else 1
        counter.write_text(str(n))
        base = (os.environ.get("BITBUCKET_BASE_URL") or "").rstrip("/")
        return {"id": n, "title": args["title"], "state": "OPEN",
                "url": c.get("pr_url") or f"{base}/projects/{args['project']}/repos/{args['repo']}/pull-requests/{n}",
                # лишнее поле, как в REST Bitbucket: прокси не должен передавать его серверу реестра (там логин)
                "author": {"user": {"name": os.environ.get("BITBUCKET_USERNAME") or ""}}}
    if name == "kb_ontology":
        url, branch = args.get("url"), args.get("branch") or "master"
        bare = repo_for_url(url)
        try:
            text = git(["--git-dir", bare, "show", f"{branch}:ontology.yml"], check=False)
        except Exception:   # noqa
            raise ToolError(f"path 'ontology.yml' does not exist in branch {branch}")
        if not text.strip():
            raise ToolError(f"path 'ontology.yml' does not exist in branch {branch}")
        return parse_ontology(text)
    raise ToolError(f"Unknown tool: {name}")


TOOLS = [{"name": n, "description": f"fake {n}", "inputSchema": {"type": "object"}} for n in (
    "bitbucket_clone", "bitbucket_checkout", "bitbucket_add", "bitbucket_status", "bitbucket_commit",
    "bitbucket_push", "bitbucket_create_pull_request", "kb_ontology")]


def b64(parts):
    return base64.b64encode(b"\0".join(parts)).decode("ascii") if parts is not None else None


def main():
    kind, raw_argv, raw_env = raw_procargs()
    journal({"ev": "start", "ppid": os.getppid(), "name": __name__, "argv": sys.argv, "cwd": os.getcwd(),
             "path0": sys.path[0] if sys.path else "", "sibling": SIBLING_OK, "raw_kind": kind,
             "raw_argv": [a.decode("utf-8", "replace") for a in raw_argv] if raw_argv is not None else None,
             "raw_argv_b64": b64(raw_argv), "raw_env_b64": b64(raw_env),
             "user_sha": sha(os.environ.get("BITBUCKET_USERNAME")), "pass_sha": sha(os.environ.get("BITBUCKET_PASSWORD")),
             "base_url": os.environ.get("BITBUCKET_BASE_URL"),
             "tmpdir": os.environ.get("TMPDIR"), "leaks": leaks()})
    if cfg().get("fail") == "start":   # исключение при импорте/старте, учётка — в тексте (traceback уйдёт в stderr)
        raise RuntimeError(f"fake bb: не удалось войти как {os.environ.get('BITBUCKET_USERNAME')} "
                           f"с паролем {os.environ.get('BITBUCKET_PASSWORD')}")
    if cfg().get("chatty"):
        print("fake bb: отладочный print в stdout — не JSON-RPC", flush=True)
    initialized, srv_seq = False, 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            journal({"ev": "bad_json", "line": line[:200]})
            rpc_error(None, -32700, "Parse error")
            continue
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            journal({"ev": "bad_message", "msg": msg})
            rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
            continue
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method is None:   # ответ клиента на наш запрос (ping) — записываем, не ждём
            journal({"ev": "client_response", "id": mid, "result": msg.get("result"), "error": msg.get("error")})
            continue
        if method == "initialize":
            info = params.get("clientInfo") if isinstance(params, dict) else None
            ok = (isinstance(params, dict) and isinstance(params.get("protocolVersion"), str)
                  and isinstance(params.get("capabilities"), dict) and isinstance(info, dict)
                  and isinstance(info.get("name"), str) and isinstance(info.get("version"), str))
            journal({"ev": "initialize", "params": params, "valid": ok})
            if not ok:
                rpc_error(mid, -32602, "Invalid request parameters")
                continue
            if cfg().get("fail") == "hang:initialize":
                time.sleep(3600)
            reply(mid, {"protocolVersion": PROTOCOL, "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "fake-bitbucket", "version": "0.1.0"}})
            continue
        if method == "notifications/initialized":
            initialized = True
            journal({"ev": "initialized"})
            continue
        if mid is None:   # прочие уведомления
            journal({"ev": "notification", "method": method})
            continue
        if method == "ping":
            reply(mid, {})
            continue
        if not initialized:   # как ServerSession SDK: запросы до notifications/initialized не принимаются
            journal({"ev": "before_initialized", "method": method})
            rpc_error(mid, -32602, "Received request before initialization was complete")
            continue
        if method == "tools/list":
            reply(mid, {"tools": TOOLS})
            continue
        if method == "tools/call":
            name = params.get("name") if isinstance(params, dict) else None
            args = params.get("arguments") or {}
            journal({"ev": "call", "name": name, "args": args, "leaks": leaks()})
            if cfg().get("chatty"):
                srv_seq += 1
                send({"jsonrpc": "2.0", "method": "notifications/message",
                      "params": {"level": "info", "logger": "fake-bb", "data": f"вызов {name}"}})
                send({"jsonrpc": "2.0", "id": f"srv-{srv_seq}", "method": "ping"})
            try:
                out = tool_call(name, args)
                if isinstance(out, dict):
                    reply(mid, {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                                "structuredContent": out, "isError": False})
                else:
                    reply(mid, {"content": [{"type": "text", "text": out}], "isError": False})
            except ToolError as e:
                journal({"ev": "tool_error", "name": name})
                reply(mid, {"content": [{"type": "text", "text": f"Error executing tool {name}: {e}"}],
                            "isError": True})
            continue
        rpc_error(mid, -32601, "Method not found")
    journal({"ev": "eof"})


if __name__ == "__main__":
    main()
