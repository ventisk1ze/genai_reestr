"""Скиллы ассистента: один источник для веб-чата и GigaCode.

<REG_SKILLS_DIR>/<имя>/SKILL.md — frontmatter (name, title, description, roles, greeting,
examples, placeholder) + текст (системный промпт). roles — кому доступен скилл:
teamlead = лид, зам и админ; admin; employee; через запятую, нет поля — всем.
"""
import re

from registry import config
from registry.errors import BadRequest, Forbidden


def load_skills():
    """Все скиллы из папки: {name: {name, title, description, body, roles, greeting, placeholder, examples}}."""
    skills = {}
    if not config.SKILLS_DIR.is_dir():
        return skills
    for d in sorted(config.SKILLS_DIR.iterdir()):
        f = next((d / n for n in ("SKILL.md", "skill.md") if (d / n).is_file()), None) if d.is_dir() else None
        if not f:
            continue
        text = f.read_text("utf-8")
        meta, body = {}, text
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
        if m:
            body = m.group(2)
            for line in m.group(1).splitlines():
                k, _, v = line.partition(":")
                if v.strip():
                    meta[k.strip()] = v.strip().strip('"\'')
        name = meta.get("name") or d.name
        skills[name] = {"name": name, "title": meta.get("title") or name,
                        "description": meta.get("description", ""), "body": body.strip(),
                        "roles": [r for r in re.split(r"[,\s]+", meta.get("roles", "")) if r],
                        "greeting": meta.get("greeting", ""), "placeholder": meta.get("placeholder", ""),
                        "examples": [x.strip() for x in meta.get("examples", "").split(" | ") if x.strip()]}
    return skills


def user_roles(user):
    """Роли для доступа к скиллам: админ — admin и teamlead, лид или зам — teamlead, остальные — employee."""
    if user is None:
        return set()
    if user["admin"]:
        return {"admin", "teamlead"}
    return {"teamlead"} if user.get("lead_teams") else {"employee"}


def skill_allowed(sk, user):
    return not sk["roles"] or bool(set(sk["roles"]) & user_roles(user))


def skills_for(user):
    """Доступные пользователю скиллы и скилл по умолчанию (без сессии — только общедоступные)."""
    allowed = {k: v for k, v in load_skills().items() if skill_allowed(v, user)}
    want = config.DEFAULT_SKILL if "teamlead" in user_roles(user) else config.DEFAULT_SKILL_EMPLOYEE
    return allowed, (want if want in allowed else next(iter(allowed), ""))


def pick_skill(name, user):
    """Скилл запроса: не указан — по умолчанию для роли; чужой — 403, неизвестный — 400."""
    skills = load_skills()
    if not skills:
        raise BadRequest(f"Нет ни одного скилла в {config.SKILLS_DIR} (ожидается <имя>/SKILL.md)")
    name = str(name or "").strip() or skills_for(user)[1]
    sk = skills.get(name)
    if sk is None:
        raise BadRequest(f"Нет скилла «{name}» — выберите скилл из списка помощника")
    if not skill_allowed(sk, user):
        who = " и ".join(config.SKILL_ROLES.get(r, r) for r in sk["roles"])
        raise Forbidden(f"Скилл «{sk['title']}» вам недоступен: он для {who}")
    return sk
