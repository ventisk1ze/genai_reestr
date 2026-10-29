#!/usr/bin/env bash
# Ворота качества: задачу нельзя закрыть, пока проект не собирается и тесты не зелёные.
# Вызывается хуком TaskCompleted (код выхода 2 — задача не закрывается, текст уходит агенту)
# и вручную: bash .claude/hooks/quality-gate.sh < /dev/null
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" || exit 0
cat > /dev/null 2>&1          # входные данные хука не нужны
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fail() { printf 'Задачу закрывать рано: %b\n' "$1" >&2; exit 2; }

python3 -m compileall -q app.py registry 2> "$T/py" || fail "бэкенд не компилируется:\n$(tail -5 "$T/py")"
for f in start.sh connect.sh; do [ -f "$f" ] && { bash -n "$f" 2> "$T/sh" || fail "ошибка синтаксиса в $f:\n$(cat "$T/sh")"; }; done
if command -v node > /dev/null; then
    found=0
    while IFS= read -r -d '' f; do
        found=1
        node --check "$f" 2> "$T/js" || fail "ошибка синтаксиса JS в $f:\n$(head -8 "$T/js")"
    done < <(find static/js -name '*.js' -print0 2>/dev/null)
    [ "$found" = 1 ] || fail "не найдено ни одного static/js/*.js"
fi
if ls tests/test_*.py > /dev/null 2>&1; then
    python3 -m unittest discover -s tests -q > "$T/ut" 2>&1 || fail "тесты красные:\n$(tail -30 "$T/ut")"
fi
exit 0
