#!/usr/bin/env bash
# Самопочинка после переноса через Windows: убрать CRLF и перезапуститься (хвостовой # гасит \r в этой строке)
if grep -q $'\r' "$0"; then sed -i 's/\r$//' "$0"; exec bash "$0" "$@"; fi #
# ============================================================================
# start.sh — запуск реестра задач ЦК в DataLab Pro (в фоне, идемпотентно)
#
#   bash start.sh                     # хранилище из REG_STORAGE, по умолчанию json (запись в БД выключена)
#   REG_STORAGE=gp bash start.sh      # писать в Greenplum
#   REG_PORT=39245 bash start.sh      # другой порт (для команды — только 39235–39246)
#
# Сервер слушает порт 39246 — он доступен участникам команды как http://jlab-<ваш табельный>:39246,
# коллеги открывают реестр у себя командой  bash connect.sh <ваш табельный>.
# Несекретные настройки (REG_STORAGE, GP_HOST, GP_PORT, GP_DATABASE, GP_SCHEMA, GIGACHAT_MODEL, REG_PORT)
# можно положить в registry.env рядом; адрес шлюза моделей DataLab Pro уже задан по умолчанию.
# Логины, пароли и PIN НИГДЕ не хранятся: логин/пароль GP и админский PIN спрашиваются при каждом запуске
# и передаются серверу через канал на stdin — не через файл и не через переменные окружения.
# ============================================================================

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${REG_PORT:-${PORT:-}}"
LOG="/tmp/ck_registry.log"
PIDF="/tmp/ck_registry.pid"

ok()   { echo "  ✓ $1"; }
warn() { echo "  ⚠ $1"; }
die()  { echo "  ✗ $1"; exit 1; }

cd "$DIR" || exit 1

# --- файлы после переноса через Windows: CRLF -> LF, проверка UTF-8
[ -f "$DIR/app.py" ] || die "нет app.py рядом со start.sh"
[ -f "$DIR/templates/index.html" ] || die "нет templates/index.html"
for f in "$DIR/app.py" "$DIR"/registry/*.py "$DIR"/registry/*/*.py "$DIR"/registry/*/*/*.py \
         "$DIR"/templates/*.html "$DIR"/static/css/*.css "$DIR"/static/js/*.js "$DIR"/static/js/*/*.js \
         "$DIR"/requirements.txt "$DIR"/skills/*/SKILL.md "$DIR/registry.env"; do
    [ -f "$f" ] || continue
    grep -q $'\r' "$f" && sed -i 's/\r$//' "$f" && ok "${f#$DIR/}: убраны переводы строк Windows"
    python3 -c "import sys; open(sys.argv[1], encoding='utf-8').read()" "$f" 2>/dev/null || \
        die "${f#$DIR/} не в UTF-8. Исправить: iconv -f cp1251 -t utf-8 \"$f\" > /tmp/fix && mv /tmp/fix \"$f\""
done

# --- несекретные настройки
if [ -f "$DIR/registry.env" ]; then
    if grep -Eiq '^[[:space:]]*(export[[:space:]]+)?[A-Z_]*(PASSWORD|PASSWD|PASS|USERNAME|LOGIN|SECRET|TOKEN)[A-Z_]*[[:space:]]*=' "$DIR/registry.env"; then
        die "в registry.env есть логин, пароль или токен — удалите эти строки: их нельзя хранить в файлах"
    fi
    # окружение команды важнее registry.env — как в app.py: заданное вручную возвращаем после source
    _keep=""
    for _v in $(compgen -v | grep -E '^(REG_|GP_|GIGACHAT_)' || true); do
        _keep="$_keep $_v=$(printf '%q' "${!_v}")"
    done
    set -a; . "$DIR/registry.env"; set +a
    [ -n "$_keep" ] && eval "export $_keep"
    unset _v _keep
    ok "настройки из registry.env"
fi
PORT="${REG_PORT:-${PORT:-39246}}"
[ "$PORT" = "8080" ] && die "порт 8080 занят JupyterHub"
if [ "$PORT" -lt 39235 ] || [ "$PORT" -gt 39246 ]; then
    warn "порт $PORT вне 39235–39246: реестр будет виден только вам, коллеги не подключатся"
fi

# --- хранилище (переключатель)
REG_STORAGE="${REG_STORAGE:-json}"
export REG_STORAGE
case "$REG_STORAGE" in
    json)   ok "хранилище: локальные JSON в ${REG_JSON_DIR:-$DIR/data} (запись в БД выключена)" ;;
    sqlite) ok "хранилище: SQLite ${REG_DB_PATH:-$DIR/registry.db} (отладка)" ;;
    gp)
        for v in GP_HOST GP_DATABASE GP_SCHEMA; do
            [ -n "${!v}" ] || die "REG_STORAGE=gp: не задан $v (в registry.env или export)"
        done
        python3 -c "import asyncpg" 2>/dev/null || die "нет asyncpg: pip install asyncpg (из зеркала PyPI)"
        if command -v klist >/dev/null 2>&1 && ! klist -s 2>/dev/null; then
            warn "нет действующего Kerberos-тикета; если подключение не пройдёт — выполните kinit и перезапустите"
        fi
        echo "  Подключение к Greenplum $GP_HOST/$GP_DATABASE, схема $GP_SCHEMA (логин и пароль не сохраняются):"
        read -rp  "    Логин GP: " GP_USERNAME
        read -rsp "    Пароль GP (Enter — без пароля, по Kerberos): " GP_PASSWORD; echo
        # логин/пароль уходят в Python через канал (printf — встроенная команда bash, в аргументах процессов
        # не видна); не через export: окружение процесса читается из /proc/<pid>/environ
        creds() { printf '%s\n%s\n' "$GP_USERNAME" "$GP_PASSWORD"; }
        if creds | python3 "$DIR/app.py" check --storage gp --creds-stdin > /tmp/ck_check.log 2>&1; then
            ok "$(tail -1 /tmp/ck_check.log)"
        else
            sed 's/^/    /' /tmp/ck_check.log; die "подключение к Greenplum не прошло"
        fi
        ;;
    *) die "REG_STORAGE=$REG_STORAGE — допустимо json, gp, sqlite" ;;
esac

# --- админский PIN: владелец сервера — администратор реестра (люди, PIN сотрудников, справочники).
# Спрашивается при каждом запуске, нигде не хранится: уходит в Python через канал на stdin, там остаётся только хеш.
ADMIN_PIN=""
for i in 1 2 3; do
    read -rsp "  Админский PIN для этого запуска (4–8 цифр): " p1; echo
    if ! [[ "$p1" =~ ^[0-9]{4,8}$ ]]; then warn "PIN — от 4 до 8 цифр"; continue; fi
    read -rsp "  Повторите админский PIN: " p2; echo
    [ "$p1" = "$p2" ] && { ADMIN_PIN="$p1"; break; }
    warn "PIN не совпали"
done
p1=""; p2=""
[ -n "$ADMIN_PIN" ] || die "админский PIN не задан — без него некому вести людей и PIN сотрудников"
ok "админский PIN принят (не сохраняется)"

# --- скиллы
SK="${REG_SKILLS_DIR:-$DIR/skills}"
n=$(find "$SK" -mindepth 2 -maxdepth 2 -iname 'skill.md' 2>/dev/null | wc -l)
[ "$n" -gt 0 ] && ok "скиллов: $n ($SK)" || warn "нет скиллов в $SK (ожидается <имя>/SKILL.md)"

# --- перезапуск сервиса
port_up() { python3 -c "import socket; socket.create_connection(('127.0.0.1', $PORT), 1)" 2>/dev/null; }
pids=$( { [ -f "$PIDF" ] && cat "$PIDF"; ss -tlnp 2>/dev/null | grep ":$PORT " | grep -o 'pid=[0-9]*' | cut -d= -f2; } | sort -u)
for p in $pids; do kill -0 "$p" 2>/dev/null && { echo "  останавливаю pid $p"; kill "$p"; }; done
for i in $(seq 1 10); do port_up || break; sleep 0.5; done
port_up && die "порт $PORT занят другим процессом — PORT=8766 bash start.sh"

# stdin сервера: [логин GP, пароль GP — только для gp], затем админский PIN — по строке
if [ "$REG_STORAGE" = "gp" ]; then
    ( { creds; printf '%s\n' "$ADMIN_PIN"; } | setsid python3 "$DIR/app.py" --storage gp --port "$PORT" \
        --creds-stdin --admin-pin-stdin ) > "$LOG" 2>&1 < /dev/null &
else
    ( printf '%s\n' "$ADMIN_PIN" | setsid python3 "$DIR/app.py" --storage "$REG_STORAGE" --port "$PORT" \
        --admin-pin-stdin ) > "$LOG" 2>&1 < /dev/null &
fi
GP_PASSWORD=""; unset GP_PASSWORD; ADMIN_PIN=""; unset ADMIN_PIN

for i in $(seq 1 20); do port_up && break; sleep 1; done
pgrep -f "[a]pp.py --storage $REG_STORAGE.*--port $PORT" | head -1 > "$PIDF"
port_up && ok "реестр слушает :$PORT (pid $(cat "$PIDF"))" || { warn "не поднялся, лог:"; tail -20 "$LOG"; exit 1; }

# --- GigaCode: путь к реестру прописываем сами (несекретно), новые терминалы подхватят
BRC="$HOME/.bashrc"
touch "$BRC"
sed -i '/^export CK_REGISTRY=/d; /^export REG_SERVER=/d' "$BRC"
echo "export CK_REGISTRY=\"$DIR\"" >> "$BRC"
ok "GigaCode: CK_REGISTRY прописан в ~/.bashrc (действует в новых терминалах)"

# --- табельный номер для ссылки коллегам
TAB="${REG_TAB_NUMBER:-}"
[ -z "$TAB" ] && [[ "$(hostname)" =~ ^jlab-([0-9]+) ]] && TAB="${BASH_REMATCH[1]}"
[ -z "$TAB" ] && [[ "${USER:-}" =~ ^([0-9]+)_ ]] && TAB="${BASH_REMATCH[1]}"

echo ""
cat "$LOG"
echo ""
echo "  Открыть реестр: в адресе своего Jupyter замените всё после /jserver на /proxy/$PORT/"
if [ "$PORT" -ge 39235 ] && [ "$PORT" -le 39246 ]; then
    echo "  Коллегам:       bash connect.sh ${TAB:-<ваш табельный>}$([ "$PORT" != 39246 ] && echo " $PORT")"
fi
echo "  Админ-панель:   войдите на странице реестра переключателем «Войти как администратор» (админский PIN)"
echo "  Лог: $LOG    Остановить: kill \$(cat $PIDF)"
