#!/usr/bin/env bash
# Самопочинка после переноса через Windows: убрать CRLF и перезапуститься (хвостовой # гасит \r в этой строке)
if grep -q $'\r' "$0"; then sed -i 's/\r$//' "$0"; exec bash "$0" "$@"; fi #
# ============================================================================
# connect.sh — подключиться к общему реестру задач ЦК коллеги (DataLab Pro)
#
#   bash connect.sh <табельный коллеги> [порт реестра, по умолчанию 39246]
#   bash connect.sh 127.0.0.1:39246             # владелец реестра — к своему серверу (чтобы выгружать в BB)
#   PROXY_PORT=9001 bash connect.sh 1656671     # другой порт прокси у себя
#
# Запускает у вас прокси (по умолчанию порт 9000) к http://jlab-<табельный>:<порт>
# и настраивает GigaCode работать с тем же реестром. Ничего секретного не сохраняет.
# Выгрузка карточек знаний в BB (Bitbucket) идёт из вашего контейнера и с вашей учёткой BB: задайте в registry.env
# рядом REG_BB_MCP (путь к server.py MCP-сервера BB) — connect.sh при каждом запуске спросит логин и пароль BB
# и передаст их прокси через канал на stdin (не через файл и не через окружение): они только в памяти прокси.
# ============================================================================

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAB="$1"
TPORT="${2:-39246}"
LOG="/tmp/ck_registry_proxy.log"
PIDF="/tmp/ck_registry_proxy.pid"

ok()   { echo "  ✓ $1"; }
warn() { echo "  ⚠ $1"; }
die()  { echo "  ✗ $1"; exit 1; }

[ -n "$TAB" ] || die "укажите табельный номер коллеги, у которого запущен реестр: bash connect.sh 1656671"
[ -f "$DIR/app.py" ] || die "нет app.py рядом с connect.sh"
# файлы после переноса через Windows: CRLF -> LF
for f in "$DIR/app.py" "$DIR"/registry/*.py "$DIR"/registry/*/*.py "$DIR"/registry/*/*/*.py \
         "$DIR"/templates/*.html "$DIR"/static/css/*.css "$DIR"/static/js/*.js "$DIR"/static/js/*/*.js; do
    [ -f "$f" ] || continue
    grep -q $'\r' "$f" && sed -i 's/\r$//' "$f"
done

# --- несекретные настройки (registry.env рядом) — с той же проверкой на секреты, что в start.sh
if [ -f "$DIR/registry.env" ]; then
    grep -q $'\r' "$DIR/registry.env" && sed -i 's/\r$//' "$DIR/registry.env" && ok "registry.env: убраны переводы строк Windows"
    python3 -c "import sys; open(sys.argv[1], encoding='utf-8').read()" "$DIR/registry.env" 2>/dev/null || \
        die "registry.env не в UTF-8. Исправить: iconv -f cp1251 -t utf-8 \"$DIR/registry.env\" > /tmp/fix && mv /tmp/fix \"$DIR/registry.env\""
    if grep -Eiq '^[[:space:]]*(export[[:space:]]+)?[A-Z_]*(PASSWORD|PASSWD|PASS|USERNAME|LOGIN|SECRET|TOKEN)[A-Z_]*[[:space:]]*=' "$DIR/registry.env"; then
        die "в registry.env есть логин, пароль или токен — удалите эти строки: их нельзя хранить в файлах"
    fi
    set -a; . "$DIR/registry.env"; set +a
    ok "настройки из registry.env"
fi
PPORT="${PROXY_PORT:-9000}"
TARGET="http://jlab-$TAB:$TPORT"
[[ "$TAB" == *:* || "$TAB" == http* ]] && TARGET="$TAB" && [[ "$TARGET" != http* ]] && TARGET="http://$TARGET"

# реестр коллеги отвечает?
if python3 -c "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('$TARGET/', timeout=10)" 2>/dev/null; then
    ok "реестр коллеги отвечает: $TARGET"
else
    die "реестр $TARGET не отвечает — попросите коллегу запустить bash start.sh (порт должен быть 39235–39246)"
fi

# перезапуск прокси (останавливаем только свой прокси: PID из файла и в его команде — app.py proxy)
port_up() { python3 -c "import socket; socket.create_connection(('127.0.0.1', $PPORT), 1)" 2>/dev/null; }
OLD="$( [ -f "$PIDF" ] && cat "$PIDF" )"
if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null && ps -p "$OLD" -o args= 2>/dev/null | grep -q "app.py proxy"; then
    kill "$OLD" && sleep 1
fi
port_up && die "порт $PPORT занят — PROXY_PORT=9001 bash connect.sh $*"

# --- выгрузка карточек в BB: логин и пароль BB спрашиваем при каждом запуске, нигде не храним
BB_USER=""; BB_PASS=""
if [ -n "${REG_BB_MCP:-}" ]; then
    BBF="${REG_BB_MCP/#\~/$HOME}"; [[ "$BBF" == /* ]] || BBF="$DIR/$BBF"
    ok "выгрузка карточек в BB: MCP-сервер $BBF, Bitbucket ${REG_BB_BASE_URL:-https://df-bitbucket.ca.sbrf.ru}"
    [ -f "$BBF" ] || warn "нет файла $BBF — поправьте REG_BB_MCP в registry.env, иначе выгрузка будет недоступна"
    read -rp  "    Логин BB (Enter — без выгрузки в BB): " BB_USER
    if [ -n "$BB_USER" ]; then
        read -rsp "    Пароль BB: " BB_PASS; echo
    else
        warn "логин BB не введён — в этом запуске выгрузка карточек в BB выключена"
    fi
else
    echo "  Выгрузка карточек в BB выключена: не задан REG_BB_MCP в registry.env"
fi

if [ -n "${REG_BB_MCP:-}" ]; then
    # логин и пароль BB уходят прокси через канал на stdin (printf — встроенная команда bash, в аргументах процессов
    # не видна); не через export: окружение процесса читается из /proc/<pid>/environ. Канал — подстановкой процесса:
    # подоболочка сразу заменяется на python (exec) и не держит пароль в памяти, пока работает прокси
    ( exec setsid python3 "$DIR/app.py" proxy "$TARGET" --port "$PPORT" --bb-creds-stdin \
        < <(printf '%s\n%s\n' "$BB_USER" "$BB_PASS") ) > "$LOG" 2>&1 < /dev/null &
else
    ( exec setsid python3 "$DIR/app.py" proxy "$TARGET" --port "$PPORT" ) > "$LOG" 2>&1 < /dev/null &
fi
BG=$!
BB_ON=""; [ -n "$BB_USER" ] && BB_ON=1
BB_PASS=""; unset BB_PASS; BB_USER=""; unset BB_USER
for i in $(seq 1 10); do port_up && break; sleep 0.5; done
# PID самого python — по уникальному шаблону с портом (как в start.sh): $! — PID подоболочки, а setsid может
# запустить python отдельным процессом; не нашли — тогда $!
PID="$(pgrep -f "[a]pp.py proxy .*--port $PPORT( |\$)" | head -1)"
echo "${PID:-$BG}" > "$PIDF"
port_up && ok "прокси слушает :$PPORT (pid $(cat "$PIDF"))" || { tail -5 "$LOG"; die "прокси не поднялся"; }
if [ -n "$BB_ON" ]; then
    for i in 1 2 3 4 5; do grep -q "Выгрузка карточек в BB" "$LOG" && break; sleep 0.2; done
    if grep -q "Выгрузка карточек в BB: недоступна" "$LOG"; then
        warn "$(grep -m1 'Выгрузка карточек в BB' "$LOG")"; BB_ON=""
    else
        ok "выгрузка карточек в BB готова: учётка BB — только в памяти прокси, до его остановки"
    fi
fi

# GigaCode коллеги работает с тем же реестром (адрес и путь — несекретные)
BRC="$HOME/.bashrc"
touch "$BRC"
sed -i '/^export CK_REGISTRY=/d; /^export REG_SERVER=/d' "$BRC"
printf 'export CK_REGISTRY="%s"\nexport REG_SERVER="%s"\n' "$DIR" "$TARGET" >> "$BRC"
ok "GigaCode: CK_REGISTRY и REG_SERVER прописаны в ~/.bashrc (действуют в новых терминалах)"

echo ""
echo "  Открыть реестр: в адресе своего Jupyter замените всё после /jserver на /proxy/$PPORT/"
echo "  Войдите: выберите себя и введите свой PIN (PIN выдаёт владелец реестра в админ-панели)."
[ -n "$BB_ON" ] && echo "  Карточки в BB: задача «Согласована» → раздел «База знаний» → «Выгрузить карточки в BB»."
echo "  Лог: $LOG    Отключиться: kill \$(cat $PIDF)"
