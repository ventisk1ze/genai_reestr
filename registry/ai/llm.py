"""Лава-болл: два движка ответа (шлюз модели по API и агент GigaCode) и история диалогов.

История — у каждого своя, ключ — ФИО вошедшего (у админа — «Администратор»), хранится
в хранилище (T-006). GigaCode из веба — только --approval-mode default|plan (auto-edit запрещён).
"""
import json
import os
import re
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request

from registry import config, runtime
from registry.ai.context import registry_context, skill_prompt, speaker_label
from registry.ai.files import _mb, ai_dialog, chat_messages
from registry.ai.skills import pick_skill, skills_for
from registry.errors import BadRequest
from registry.util import dumps

GIGACODE_LOCK = threading.Lock()   # один запуск агента за раз, остальные ждут
AI_MAX_MSGS, AI_MAX_QUERIES = 40, 30


def llm_configured():
    return bool(os.environ.get("GIGACHAT_API_URL"))


def gigacode_path():
    return shutil.which(config.GIGACODE_BIN)


def op_llm_status(user=None):
    """Без сессии — статус и только общедоступные скиллы (без roles)."""
    skills, default = skills_for(user)
    return {"configured": llm_configured(), "model": config.LLM_MODEL, "gigacode": bool(gigacode_path()),
            "default": default,
            "skills": [{k: s[k] for k in ("name", "title", "description", "roles", "greeting", "examples",
                                          "placeholder")} for s in skills.values()],
            "file_limits": {"types": list(config.AI_FILE_TYPES), "max_mb": config.AI_FILE_MAX_MB,
                            "max_bytes": config.AI_FILE_MAX_BYTES,
                            "max_chars": config.AI_FILE_MAX_CHARS, "total_chars": config.AI_FILES_MAX_CHARS,
                            "per_msg": config.AI_FILES_PER_MSG}}


def op_gigacode(body, user, sk):
    """Ответ агента GigaCode на сервере реестра: `gigacode -p`, скилл и диалог — через stdin."""
    exe = gigacode_path()
    if not exe:
        raise BadRequest(f"GigaCode не найден на сервере реестра ({config.GIGACODE_BIN}). Переключитесь на режим API.")
    if config.GIGACODE_APPROVAL not in ("default", "plan"):
        raise BadRequest("REG_GIGACODE_APPROVAL: допустимо default или plan — в лава-болле агент не правит файлы")
    msgs = chat_messages(body)
    _, ctx = registry_context(user)
    who = speaker_label(user)
    dialog = "\n\n".join((f"{who}: " if m["role"] == "user" else "Помощник: ") + m["content"] for m in msgs)
    # Текст скилла передаём явно: так ответ совпадает со скиллом, даже если в GigaCode он подключён из другой папки
    stdin = ("# Где ты отвечаешь\nТы отвечаешь пользователю реестра задач ЦК в веб-чате (лава-болл), а не в терминале. "
             "Команды реестра и правка файлов здесь недоступны — выведи результат служебным блоком из скилла "
             "(```task```, ```result```, ```card```), пользователь перенесёт его в реестр кнопкой." + ctx
             + f"\n# Скилл «{sk['name']}»\nСледуй ему (если он подключён у тебя как скилл — это он же):\n\n{sk['body']}\n"
             + f"\n# Диалог\n{dialog}\n")
    cmd = [exe, "-p", "Ответь на последнее сообщение пользователя из диалога выше, следуя скиллу. "
                      "Выведи только сам ответ пользователю.",
           "--approval-mode", config.GIGACODE_APPROVAL, "-o", "text"]
    if config.GIGACODE_MODEL:
        cmd += ["-m", config.GIGACODE_MODEL]
    env = dict(os.environ, NO_COLOR="1", TERM="dumb", REG_NO_TTY="1")   # create-task из лава-болла — через веб-форму
    with GIGACODE_LOCK:
        try:
            p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               cwd=str(config.HERE), env=env, timeout=config.GIGACODE_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise BadRequest(f"GigaCode не ответил за {config.GIGACODE_TIMEOUT} с")
    text = re.sub(r"<think>.*?</think>", "", config.ANSI.sub("", p.stdout or ""), flags=re.S).strip()
    if p.returncode != 0 or not text:
        err = config.ANSI.sub("", (p.stderr or p.stdout or "").strip())[-600:]
        raise BadRequest(f"GigaCode завершился с ошибкой (код {p.returncode}): {err or 'пустой ответ'}")
    return {"reply": text, "model": "GigaCode" + (f" ({config.GIGACODE_MODEL})" if config.GIGACODE_MODEL else ""),
            "skill": sk["name"]}


def op_llm(body, user):
    """Ответ ассистента: скилл → движок (gigacode или API шлюза модели)."""
    sk = pick_skill(body.get("skill"), user)   # недоступный скилл — 403 до запуска модели или агента
    if body.get("engine") == "gigacode":
        return op_gigacode(body, user, sk)
    if not llm_configured():
        raise BadRequest("Ассистент не настроен: задайте GIGACHAT_API_URL (registry.env или export) "
                         "и перезапустите bash start.sh.")
    msgs = chat_messages(body)
    skill, system = skill_prompt(sk, user)

    payload = {"model": config.LLM_MODEL, "n": 1, "temperature": config.LLM_TEMPERATURE,
               "max_tokens": config.LLM_MAX_TOKENS,
               "messages": [{"role": "system", "content": system}] + msgs}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if config.LLM_TOKEN:
        headers["Authorization"] = f"Bearer {config.LLM_TOKEN}"
    url = os.environ["GIGACHAT_API_URL"].rstrip("/") + "/chat/completions"
    ctx = ssl.create_default_context()
    if not config.LLM_VERIFY_SSL:
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    last = ""
    for attempt in range(1, 4):
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT, context=ctx) as r:
                data = json.loads(r.read().decode("utf-8"))
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()   # рассуждения reasoning-моделей
            if text:
                return {"reply": text, "model": config.LLM_MODEL, "skill": skill}
            last = "модель вернула пустой ответ"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
            if e.code not in config.LLM_RETRY:
                break
        except Exception as e:  # сеть, таймаут, не-JSON
            last = f"{type(e).__name__}: {e}"
        time.sleep(3 * attempt)
    raise BadRequest(f"Модель недоступна ({config.LLM_MODEL}): {last}")


# --- история лава-болла
def ai_history(person):
    d = runtime.STORE.get_ai(person) or {}
    return {"msgs": list(d.get("msgs") or []), "queries": list(d.get("queries") or [])}


def op_ai_history(body, person):
    action = body.get("action")
    if action not in ("reset", "clear_queries"):
        raise BadRequest("Неизвестное действие с историей: допустимо reset или clear_queries")
    with config.LOCK:
        h = ai_history(person)
        h["msgs" if action == "reset" else "queries"] = []
        runtime.STORE.save_ai(person, h)
    return h


def remember_llm(person, body, reply):
    """После удачного ответа модели: диалог запроса (без error и прочего; файлы — как ушли модели, в том же
    общем лимите) + ответ; текст запроса — наверх списка (сообщение только с файлами туда не попадает).
    Возвращает сохранённый диалог — клиент показывает его вместо своего."""
    msgs = (ai_dialog(body) + [{"role": "assistant", "content": reply}])[-AI_MAX_MSGS:]
    last = next((m["content"].strip() for m in reversed(msgs) if m["role"] == "user"), "")
    with config.LOCK:
        h = ai_history(person)
        h["msgs"] = msgs
        if last:
            h["queries"] = ([last] + [q for q in h["queries"] if q != last])[:AI_MAX_QUERIES]
        runtime.STORE.save_ai(person, h)
    return msgs
