#!/usr/bin/env bash
# Install the unread Telegram bot with Docker — one command on a fresh server.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/maxbolgarin/unread/main/scripts/install-bot-docker.sh | bash
#
# Or, from a checkout:
#   bash scripts/install-bot-docker.sh [--dir PATH] [--reconfigure] [--image REF]
#
# What this does, in order:
#   1. Installs Docker if it's missing (Docker's official get.docker.com
#      script; asks first).
#   2. Asks for what the bot needs: a @BotFather token (checked with
#      Telegram), who may use the bot (found automatically when you press
#      Start in your new bot), an AI key, and a Telegram API id + hash.
#      No Telegram user session is needed — add one later with
#      /upload_session if you want the bot to read t.me chat links.
#   3. Writes ~/unread-bot/docker-compose.yml + ~/unread-bot/.env (0600),
#      pulls the image, starts the bot and waits until it reports ready.
#   4. Adds an `unread-bot` command: status, logs, restart, update,
#      config, stop, start, doctor, uninstall.
#
# Safe to re-run: answers already in ~/unread-bot/.env are kept and only
# missing ones are asked for, the compose file is refreshed and the image
# updated. `--reconfigure` asks everything again (Enter keeps a value).
#
# Unattended (cloud-init, CI, no terminal): export the answers and nothing
# is asked — UNREAD_BOT_TOKEN, UNREAD_BOT_OWNER_ID, TELEGRAM_API_ID,
# TELEGRAM_API_HASH and one of OPENAI_API_KEY / ANTHROPIC_API_KEY /
# GOOGLE_API_KEY / OPENROUTER_API_KEY (plus UNREAD_AI_CHAT_PROVIDER when
# more than one key is set). Environment values win over the .env file.
#
# Options:
#   --dir PATH      where the bot's files live (default ~/unread-bot;
#                   env UNREAD_BOT_DIR)
#   --reconfigure   ask every question again
#   --image REF     image to run instead of ghcr.io/maxbolgarin/unread:latest,
#                   e.g. ghcr.io/maxbolgarin/unread:1.10.0 (env UNREAD_BOT_IMAGE)
#   -h, --help      show this help
#
# Everything runs from main(), which starts only after bash has read the
# whole file: a download cut short never runs half a script. Prompts read
# /dev/tty, so `curl | bash` can still ask questions.

set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/maxbolgarin/unread"
REF="${UNREAD_REF:-main}"
# Override for a self-hosted Bot API server, or a mock in tests.
TG_API="${UNREAD_TELEGRAM_API:-https://api.telegram.org}"
SERVICE="unread-bot"
READY_TIMEOUT="${UNREAD_BOT_READY_TIMEOUT:-180}"

if [[ -t 1 ]]; then
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'
  C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'; C_RST=$'\033[0m'
else
  C_GREEN=""; C_YELLOW=""; C_RED=""; C_DIM=""; C_BOLD=""; C_RST=""
fi
say()  { printf '  %s\n' "$*"; }
hint() { printf '  %s%s%s\n' "$C_DIM" "$*" "$C_RST"; }
ok()   { printf '%s✓%s %s\n' "$C_GREEN" "$C_RST" "$*"; }
warn() { printf '%s!%s %s\n' "$C_YELLOW" "$C_RST" "$*"; }
err()  { printf '%s✖%s %s\n' "$C_RED" "$C_RST" "$*" >&2; }
step() { printf '\n%s→ %s%s\n' "$C_BOLD" "$*" "$C_RST"; }
die()  { err "$*"; exit 1; }

usage() {
  cat <<'EOF'
Install the unread Telegram bot with Docker.

  curl -fsSL https://raw.githubusercontent.com/maxbolgarin/unread/main/scripts/install-bot-docker.sh | bash
  curl -fsSL …/install-bot-docker.sh | bash -s -- --reconfigure

Options:
  --dir PATH      where the bot's files live (default ~/unread-bot)
  --reconfigure   ask every question again (Enter keeps the current value)
  --image REF     image to run (default ghcr.io/maxbolgarin/unread:latest)
  -h, --help      show this help

Unattended: export UNREAD_BOT_TOKEN, UNREAD_BOT_OWNER_ID, TELEGRAM_API_ID,
TELEGRAM_API_HASH and an AI key (OPENAI_API_KEY, ANTHROPIC_API_KEY,
GOOGLE_API_KEY or OPENROUTER_API_KEY) before running it.

Guide: https://github.com/maxbolgarin/unread/blob/main/docs/bot-vm-deploy.md
EOF
}

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

trim() {
  local s=${1//$'\r'/}
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

mask() {
  local s=$1
  if (( ${#s} <= 10 )); then printf '****'; else printf '%s…%s' "${s:0:4}" "${s:${#s}-4}"; fi
}

# Run as root: directly when we are root, through sudo otherwise.
as_root() {
  if [[ $EUID -eq 0 || -z $SUDO ]]; then "$@"; else sudo "$@"; fi
}

require_tty() {
  [[ $INTERACTIVE == 1 ]] && return 0
  die "No terminal to ask for $1. Export it before running the installer (see --help)."
}

# ask VAR "Prompt" [default] — visible input; Enter takes the default.
ask() {
  local __var=$1 __prompt=$2 __default=${3-} __reply=""
  [[ -n $__default ]] && __prompt+=" [$__default]"
  printf '  %s: ' "$__prompt" >/dev/tty
  IFS= read -r __reply </dev/tty || true
  __reply=$(trim "$__reply")
  printf -v "$__var" '%s' "${__reply:-$__default}"
}

# read_masked VAR — read a line from the terminal, echoing one * per
# character so typing or pasting shows something happened. Backspace and
# Ctrl-U edit; arrow keys and other escape sequences are dropped.
read_masked() {
  local __out=$1 __buf="" __c="" __rest=""
  while IFS= read -rsn1 __c </dev/tty; do
    case $__c in
      '' | $'\n' | $'\r') break ;;
      $'\x7f' | $'\b')
        if [[ -n $__buf ]]; then
          __buf=${__buf%?}
          printf '\b \b' >/dev/tty
        fi
        ;;
      $'\x15')
        while [[ -n $__buf ]]; do
          __buf=${__buf%?}
          printf '\b \b' >/dev/tty
        done
        ;;
      $'\e') IFS= read -rsn2 -t 0.05 __rest </dev/tty || true ;;
      [[:cntrl:]]) ;;
      *)
        __buf+=$__c
        printf '*' >/dev/tty
        ;;
    esac
  done
  printf -v "$__out" '%s' "$__buf"
}

# ask_secret VAR "Prompt" [current] — masked input (* per character);
# Enter keeps the current value.
ask_secret() {
  local __var=$1 __prompt=$2 __current=${3-} __reply=""
  [[ -n $__current ]] && __prompt+=" [Enter keeps $(mask "$__current")]"
  printf '  %s: ' "$__prompt" >/dev/tty
  read_masked __reply
  printf '\n' >/dev/tty
  __reply=$(trim "$__reply")
  printf -v "$__var" '%s' "${__reply:-$__current}"
}

# confirm "Question" [Y|N] — the answer Enter gives.
confirm() {
  local def=${2:-Y} reply="" choices="[Y/n]"
  [[ $def == N ]] && choices="[y/N]"
  printf '  %s %s ' "$1" "$choices" >/dev/tty
  IFS= read -r reply </dev/tty || true
  reply=$(trim "$reply")
  [[ -z $reply ]] && reply=$def
  [[ $reply == [Yy]* ]]
}

valid_ids()   { [[ $1 =~ ^[0-9]{3,15}(,[0-9]{3,15})*$ ]]; }
valid_token() { [[ $1 =~ ^[0-9]{5,15}:[A-Za-z0-9_-]{30,}$ ]]; }
# Values end up single-quoted in .env: no quotes, backslashes or spaces.
valid_secret() { [[ -n $1 && $1 != *[[:space:]\'\"\\]* ]]; }

# ---------------------------------------------------------------------------
# .env handling — values are single-quoted so neither Compose (`$`
# interpolation) nor unread's own loader changes them.
# ---------------------------------------------------------------------------

env_get() {
  [[ -f $ENV_FILE ]] || return 0
  local line first last
  line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?$1=" "$ENV_FILE" | tail -n1 || true)
  [[ -n $line ]] || return 0
  line=$(trim "${line#*=}")
  if (( ${#line} >= 2 )); then
    first=${line:0:1}
    last=${line:${#line}-1}
    if [[ $first == "$last" && ( $first == "'" || $first == '"' ) ]]; then
      line=${line:1:${#line}-2}
    fi
  fi
  printf '%s' "$line"
}

# current KEY — the installer's own environment wins over the .env file.
current() {
  local v
  v=$(printenv "$1" 2>/dev/null || true)
  if [[ -n $v ]]; then printf '%s' "$v"; else env_get "$1"; fi
}

env_set() {
  local tmp
  tmp=$(mktemp "$ENV_FILE.XXXXXX")
  KEY="$1" LINE="$1='$2'" awk '
    $0 ~ "^[ \t]*(export[ \t]+)?" ENVIRON["KEY"] "=" { if (!done) print ENVIRON["LINE"]; done = 1; next }
    { print }
    END { if (!done) print ENVIRON["LINE"] }
  ' "$ENV_FILE" >"$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

env_unset() {
  local tmp
  grep -qE "^[[:space:]]*(export[[:space:]]+)?$1=" "$ENV_FILE" 2>/dev/null || return 0
  tmp=$(mktemp "$ENV_FILE.XXXXXX")
  KEY="$1" awk '$0 ~ "^[ \t]*(export[ \t]+)?" ENVIRON["KEY"] "=" { next } { print }' "$ENV_FILE" >"$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

write_env_header() {
  (
    umask 077
    cat >"$ENV_FILE" <<'EOF'
# unread Telegram bot — settings, written by install-bot-docker.sh.
# Change them with `unread-bot config` (opens an editor, then applies), or
# edit this file and run `unread-bot restart`. Keep comments on their own
# lines: everything after `=` is the value.
#
# UNREAD_BOT_OWNER_ID lists who may use the bot (comma-separated ids; the
# first is the owner). An UNREAD_AI_*_PROVIDER set here outranks the bot's
# /settings menu. Every option, explained:
# https://github.com/maxbolgarin/unread/blob/main/.env.bot.example

EOF
  )
}

# ---------------------------------------------------------------------------
# Telegram Bot API (HTTPS) — used only during setup: check the token, find
# the owner's id, say hello. The bot itself talks MTProto. The token goes
# to curl through its config on stdin, never through the process list.
# ---------------------------------------------------------------------------

tg_call() {  # tg_call TOKEN METHOD [extra curl args…]
  local token=$1 method=$2
  shift 2
  curl -sS --max-time 20 "$@" -K - <<EOF
url = "$TG_API/bot$token/$method"
EOF
}

# check_bot_token TOKEN → 0 valid (sets BOT_USERNAME), 1 rejected, 2 unknown
check_bot_token() {
  local resp
  local ok_re='"ok":[[:space:]]*true' bad_re='"error_code":[[:space:]]*40[14]'
  resp=$(tg_call "$1" getMe 2>/dev/null) || return 2
  if [[ $resp =~ $ok_re ]]; then
    BOT_USERNAME=$(printf '%s' "$resp" | sed -n 's/.*"username":[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)
    return 0
  fi
  [[ $resp =~ $bad_re ]] && return 1
  return 2
}

# getUpdates JSON on stdin → "max|N", then one line per private message from
# a person: update_id|user id|unix date|username|name ("-" when empty).
parse_updates() {
  python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
if not data.get("ok"):
    sys.exit(2)
updates = data.get("result") or []
print("max|%d" % max([u.get("update_id", 0) for u in updates] or [0]))
clean = lambda s: (s or "").replace("|", " ").replace("\n", " ").strip() or "-"
for u in updates:
    msg = u.get("message") or {}
    who = msg.get("from") or {}
    if (msg.get("chat") or {}).get("type") != "private" or who.get("is_bot") or not who.get("id"):
        continue
    name = " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x)
    print("%d|%d|%d|%s|%s" % (u["update_id"], who["id"], msg.get("date", 0), clean(who.get("username")), clean(name)))
'
}

# Wait for the owner to message the new bot, and read their id off it.
detect_owner() {
  local resp parsed max offset line typed uid uname name who deadline now
  resp=$(tg_call "$BOT_TOKEN" "getUpdates?offset=-1&timeout=0" 2>/dev/null) || return 1
  parsed=$(printf '%s' "$resp" | parse_updates 2>/dev/null) || return 1
  max=$(printf '%s\n' "$parsed" | awk -F'|' '$1 == "max" { print $2 }')
  offset=$(( ${max:-0} + 1 ))
  # Pressed Start before getting here? A message from the last 10 minutes counts.
  now=$(date +%s)
  line=$(printf '%s\n' "$parsed" | awk -F'|' -v since=$((now - 600)) '$1 != "max" && $3 >= since' | tail -n1)

  say "Open ${C_BOLD}https://t.me/$BOT_USERNAME${C_RST} in Telegram and press ${C_BOLD}Start${C_RST} (or send it any message)."
  hint "Waiting for it here… or type your numeric Telegram id and press Enter."
  deadline=$((SECONDS + 300))
  while (( SECONDS < deadline )); do
    if [[ -n $line ]]; then
      IFS='|' read -r _ uid _ uname name <<<"$line"
      who=$name
      [[ $who == - ]] && who="someone"
      [[ $uname != - ]] && who+=" (@$uname)"
      ok "Got a message from $who — id $uid."
      if confirm "Is that you?" Y; then
        OWNER_ID=$uid
        tg_call "$BOT_TOKEN" "getUpdates?offset=$offset&timeout=0" >/dev/null 2>&1 || true
        return 0
      fi
      hint "OK — still waiting for your message…"
      line=""
    fi
    if resp=$(tg_call "$BOT_TOKEN" "getUpdates?offset=$offset&timeout=4" 2>/dev/null) \
      && parsed=$(printf '%s' "$resp" | parse_updates 2>/dev/null); then
      max=$(printf '%s\n' "$parsed" | awk -F'|' '$1 == "max" { print $2 }')
      if (( ${max:-0} >= offset )); then offset=$((max + 1)); fi
      line=$(printf '%s\n' "$parsed" | awk -F'|' '$1 != "max"' | tail -n1)
      [[ -n $line ]] && continue
    else
      sleep 2
    fi
    if IFS= read -r -t 1 typed </dev/tty; then
      typed=$(trim "$typed")
      if valid_ids "$typed"; then
        OWNER_ID=$typed
        return 0
      fi
      if [[ -n $typed ]]; then warn "Type digits only, like 123456789."; fi
    fi
  done
  warn "No message arrived in 5 minutes."
  return 1
}

say_hello() {
  [[ -n $BOT_USERNAME ]] || return 0
  tg_call "$BOT_TOKEN" sendMessage \
    --data-urlencode "chat_id=${OWNER_ID%%,*}" \
    --data-urlencode "text=✅ Your unread bot is running.

Send me a voice message, a PDF or any file, a web link or a YouTube link and I'll summarize it. /help lists everything; /settings changes the language, report format and AI model." \
    >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# AI key check — one cheap authenticated GET. Only a clear "unauthorized"
# counts as a bad key; anything else (offline, rate limit) is let through.
# → 0 accepted, 1 rejected, 2 couldn't tell
# ---------------------------------------------------------------------------

check_ai_key() {
  local provider=$1 key=$2 code config
  case $provider in
    openai) config="url = \"https://api.openai.com/v1/models\""$'\n'"header = \"Authorization: Bearer $key\"" ;;
    anthropic) config="url = \"https://api.anthropic.com/v1/models\""$'\n'"header = \"x-api-key: $key\""$'\n'"header = \"anthropic-version: 2023-06-01\"" ;;
    google) config="url = \"https://generativelanguage.googleapis.com/v1beta/models\""$'\n'"header = \"x-goog-api-key: $key\"" ;;
    openrouter) config="url = \"https://openrouter.ai/api/v1/key\""$'\n'"header = \"Authorization: Bearer $key\"" ;;
    *) return 2 ;;
  esac
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -K - <<<"$config" 2>/dev/null) || return 2
  case $code in
    2??) return 0 ;;
    401 | 403) return 1 ;;
    400) if [[ $provider == google ]]; then return 1; fi; return 2 ;;
    *) return 2 ;;
  esac
}

provider_key_var() {
  case $1 in
    openai) echo OPENAI_API_KEY ;;
    anthropic) echo ANTHROPIC_API_KEY ;;
    google) echo GOOGLE_API_KEY ;;
    openrouter) echo OPENROUTER_API_KEY ;;
  esac
}

provider_label() {
  case $1 in
    openai) echo "OpenAI" ;;
    anthropic) echo "Anthropic" ;;
    google) echo "Google Gemini" ;;
    openrouter) echo "OpenRouter" ;;
  esac
}

provider_key_url() {
  case $1 in
    openai) echo "https://platform.openai.com/api-keys" ;;
    anthropic) echo "https://console.anthropic.com/settings/keys" ;;
    google) echo "https://aistudio.google.com/apikey" ;;
    openrouter) echo "https://openrouter.ai/keys" ;;
  esac
}

# prompt_ai_key PROVIDER CURRENT [optional] → prints the key ("" if skipped).
# Runs in $(…), so everything meant for the user goes to /dev/tty.
prompt_ai_key() {
  local provider=$1 key=$2 optional=${3:-0} label rc
  label="$(provider_label "$provider") API key"
  while :; do
    if [[ $optional == 1 ]]; then
      ask_secret key "$label (Enter to skip)" "$key"
      [[ -z $key ]] && break
    else
      ask_secret key "$label" "$key"
    fi
    if ! valid_secret "$key"; then
      warn "That doesn't look like an API key." >/dev/tty
      key=""
      continue
    fi
    rc=0
    check_ai_key "$provider" "$key" || rc=$?
    case $rc in
      0) ok "Key accepted ($(mask "$key"))." >/dev/tty; break ;;
      1)
        warn "$(provider_label "$provider") rejected this key." >/dev/tty
        confirm "Use it anyway?" N && break
        key=""
        ;;
      *) hint "Couldn't check the key from here — using it as is." >/dev/tty; break ;;
    esac
  done
  printf '%s' "$key"
}

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

# Docker CLI, through sudo when this user can't reach the daemon directly.
dk() {
  if [[ $DOCKER_SUDO == 1 ]]; then sudo "$@"; else "$@"; fi
}
compose() { (cd "$DIR" && dk "${COMPOSE_CMD[@]}" "$@"); }
container_id() {
  compose ps -a -q "$SERVICE" 2>/dev/null | head -n1 || compose ps -q "$SERVICE" 2>/dev/null | head -n1
}

install_docker() {
  warn "Docker isn't installed."
  if [[ $INTERACTIVE == 1 ]]; then
    confirm "Install it now with Docker's official script (get.docker.com)?" Y \
      || die "Docker is required: https://docs.docker.com/engine/install/ — then run this again."
  else
    say "Installing it with Docker's official script (get.docker.com)…"
  fi
  [[ $EUID -eq 0 || -n $SUDO ]] || die "Installing Docker needs root. Re-run as root, or install Docker first."
  local script
  script=$(mktemp)
  curl -fsSL https://get.docker.com -o "$script" || die "Couldn't download get.docker.com."
  as_root sh "$script" </dev/null || { rm -f "$script"; die "Docker installation failed — see the output above."; }
  rm -f "$script"
  if command -v systemctl >/dev/null 2>&1; then
    as_root systemctl enable --now docker >/dev/null 2>&1 || true
  fi
  ok "Docker installed."
}

start_docker_daemon() {
  if command -v systemctl >/dev/null 2>&1; then
    as_root systemctl start docker >/dev/null 2>&1 || true
  elif command -v service >/dev/null 2>&1; then
    as_root service docker start >/dev/null 2>&1 || true
  fi
  sleep 2
}

ensure_docker() {
  step "Checking Docker"
  if ! command -v docker >/dev/null 2>&1; then
    case "$(uname -s)" in
      Linux) install_docker ;;
      Darwin) die "Install Docker Desktop (https://docs.docker.com/desktop/), start it, and run this again." ;;
      *) die "Unsupported system: $(uname -s). Install Docker yourself, then run this again." ;;
    esac
  fi

  DOCKER_SUDO=0
  if ! docker info >/dev/null 2>&1; then
    if [[ $EUID -eq 0 ]]; then
      start_docker_daemon
      docker info >/dev/null 2>&1 \
        || die "Docker is installed but not running. Start it (systemctl start docker) and run this again."
    elif [[ -n $SUDO ]]; then
      # Not in the docker group (normal right after installing it): use sudo.
      if ! sudo docker info >/dev/null 2>&1; then
        start_docker_daemon
        sudo docker info >/dev/null 2>&1 \
          || die "Can't reach Docker even with sudo. Start it (sudo systemctl start docker) and run this again."
      fi
      DOCKER_SUDO=1
    else
      die "Can't reach Docker. Run this as root, or add your user to the docker group."
    fi
  fi

  if dk docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD=(docker-compose)
  else
    warn "Docker Compose isn't installed — trying the docker-compose-plugin package."
    if command -v apt-get >/dev/null 2>&1; then
      as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-compose-plugin </dev/null >/dev/null 2>&1 \
        || as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-compose-v2 </dev/null >/dev/null 2>&1 \
        || true
    elif command -v dnf >/dev/null 2>&1; then
      as_root dnf install -y -q docker-compose-plugin </dev/null >/dev/null 2>&1 || true
    fi
    dk docker compose version >/dev/null 2>&1 \
      || die "Couldn't install Docker Compose. See https://docs.docker.com/compose/install/linux/ and run this again."
    COMPOSE_CMD=(docker compose)
  fi
  ok "$(dk docker --version 2>/dev/null | head -n1) · $(dk "${COMPOSE_CMD[@]}" version 2>/dev/null | head -n1)"
}

# Pull the image. On failure, retry once without this machine's registry
# login: a ghcr.io login for some other account makes GHCR answer "denied"
# even for this public image.
pull_image() {
  local anon rc=0
  compose pull </dev/null || rc=$?
  if (( rc != 0 )); then
    hint "Retrying without this machine's registry login…"
    anon=$(mktemp -d)
    echo '{}' >"$anon/config.json"
    rc=0
    (cd "$DIR" && dk env "DOCKER_CONFIG=$anon" "${COMPOSE_CMD[@]}" pull </dev/null) || rc=$?
    rm -rf "$anon"
  fi
  if (( rc != 0 )); then
    case "$(uname -m)" in
      x86_64 | amd64) ;;
      *) warn "This server is $(uname -m); the prebuilt image may only exist for x86_64. On ARM, use the native installer: https://github.com/maxbolgarin/unread/blob/main/docs/bot-vm-deploy.md" ;;
    esac
  fi
  return "$rc"
}

# → 0 ready, 1 crashed, 2 still starting after READY_TIMEOUT
wait_for_ready() {
  local cid logs restarts state deadline=$((SECONDS + READY_TIMEOUT))
  while (( SECONDS < deadline )); do
    cid=$(container_id)
    if [[ -n $cid ]]; then
      logs=$(dk docker logs "$cid" 2>&1 | tail -n 300 || true)
      if grep -q "bot ready" <<<"$logs"; then return 0; fi
      restarts=$(dk docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null || echo 0)
      state=$(dk docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || echo "")
      if (( ${restarts:-0} > 0 )) || [[ $state == exited || $state == dead ]]; then
        return 1
      fi
    fi
    sleep 2
  done
  return 2
}

explain_failure() {
  local logs=$1
  if grep -qiE 'rate-limit|FloodWait' <<<"$logs"; then
    say "Telegram is rate-limiting this bot token. Wait the time shown above, then: $HELPER start"
    return
  elif grep -qiE 'api_id/api_hash|ApiIdInvalid' <<<"$logs"; then
    say "Telegram rejected the API id / hash — re-check them on https://my.telegram.org."
  elif grep -qiE 'AccessTokenInvalid|token is invalid' <<<"$logs"; then
    say "Telegram rejected the bot token — copy it again from @BotFather."
  elif grep -qiE 'owner allowlist|UNREAD_BOT_OWNER_ID' <<<"$logs"; then
    say "UNREAD_BOT_OWNER_ID is missing or invalid."
  fi
  say "Fix the setting with ${C_BOLD}$HELPER config${C_RST} (it restarts the bot), or re-run this installer with --reconfigure."
}

warn_native_service() {
  if command -v systemctl >/dev/null 2>&1 && systemctl --user is-active --quiet unread-bot.service 2>/dev/null; then
    warn "The native unread-bot service (scripts/install-bot.sh) is also running here."
    say "Two copies of one bot answer every message twice. Stop the old one:"
    say "  systemctl --user disable --now unread-bot"
  fi
}

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

parse_args() {
  DIR="${UNREAD_BOT_DIR:-$HOME/unread-bot}"
  RECONFIGURE=0
  IMAGE_OVERRIDE="${UNREAD_BOT_IMAGE:-}"
  while (( $# )); do
    case $1 in
      --dir) [[ $# -ge 2 ]] || die "--dir needs a path"; DIR=$2; shift 2 ;;
      --dir=*) DIR=${1#*=}; shift ;;
      --reconfigure) RECONFIGURE=1; shift ;;
      --image) [[ $# -ge 2 ]] || die "--image needs a value"; IMAGE_OVERRIDE=$2; shift 2 ;;
      --image=*) IMAGE_OVERRIDE=${1#*=}; shift ;;
      -h | --help) usage; exit 0 ;;
      *) die "Unknown option: $1 (see --help)" ;;
    esac
  done
}

preflight() {
  command -v curl >/dev/null 2>&1 || die "curl is required — install it and run this again."
  INTERACTIVE=0
  if (exec </dev/tty) 2>/dev/null; then INTERACTIVE=1; fi
  HAVE_PYTHON=0
  if command -v python3 >/dev/null 2>&1; then HAVE_PYTHON=1; fi
  SUDO=""
  if [[ $EUID -ne 0 ]] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
  BOT_TOKEN=""
  BOT_USERNAME=""
  OWNER_ID=""
  BOT_READY=0
  HELPER="unread-bot"
}

intro() {
  printf '\n%sunread%s · Telegram bot installer (Docker)\n' "$C_BOLD" "$C_RST"
  say "You'll need three things — each step says where to get it:"
  say "  • a bot token from @BotFather"
  say "  • an API key for OpenAI, Anthropic, Google Gemini or OpenRouter"
  say "  • a Telegram API id + hash from my.telegram.org (free)"
}

prepare_dir() {
  mkdir -p "$DIR"
  DIR=$(cd "$DIR" && pwd)
  chmod 700 "$DIR" 2>/dev/null || true
  ENV_FILE="$DIR/.env"
  if [[ -f $ENV_FILE ]]; then
    ok "Using the existing setup in $DIR (answers you gave before are kept)."
  else
    write_env_header
  fi
  chmod 600 "$ENV_FILE"
}

collect_bot_token() {
  step "Step 1/4 · Bot token"
  local token rc
  token=$(current UNREAD_BOT_TOKEN)
  if [[ -z $token || $RECONFIGURE == 1 ]]; then
    require_tty UNREAD_BOT_TOKEN
    say "In Telegram, open @BotFather, send /newbot and follow the prompts."
    say "It replies with a token like 123456789:AAH…"
    ask_secret token "Bot token" "$token"
  fi
  while :; do
    if ! valid_token "$token"; then
      [[ $INTERACTIVE == 1 ]] || die "UNREAD_BOT_TOKEN doesn't look like a bot token."
      warn "That doesn't look like a bot token (digits, a colon, then letters)."
      ask_secret token "Bot token"
      continue
    fi
    rc=0
    check_bot_token "$token" || rc=$?
    case $rc in
      0) ok "Token works — your bot is @$BOT_USERNAME."; break ;;
      1)
        [[ $INTERACTIVE == 1 ]] || die "Telegram rejected UNREAD_BOT_TOKEN."
        warn "Telegram rejected this token. Copy it again from @BotFather."
        ask_secret token "Bot token"
        ;;
      *) warn "Couldn't reach Telegram to check the token — going on without the check."; break ;;
    esac
  done
  BOT_TOKEN=$token
  env_set UNREAD_BOT_TOKEN "$token"
}

collect_owner() {
  step "Step 2/4 · Who may use the bot"
  local owner
  owner=$(current UNREAD_BOT_OWNER_ID)
  owner=${owner// /}
  if [[ -n $owner && $RECONFIGURE == 0 ]] && valid_ids "$owner"; then
    OWNER_ID=$owner
  else
    if [[ -n $owner && $INTERACTIVE == 0 ]]; then
      die "UNREAD_BOT_OWNER_ID must be numeric Telegram ids, like 123456789 or 111,222."
    fi
    require_tty UNREAD_BOT_OWNER_ID
    say "The bot answers only the people you list here and ignores everyone else."
    if [[ -n $BOT_USERNAME && $HAVE_PYTHON == 1 ]] && detect_owner; then
      :
    else
      say "Find your numeric id: message @userinfobot in Telegram and it replies with it."
      while :; do
        ask OWNER_ID "Your Telegram id" "$owner"
        OWNER_ID=${OWNER_ID// /}
        valid_ids "$OWNER_ID" && break
        warn "Type digits only, like 123456789 (several: 111,222)."
      done
    fi
    ask_more_people "$owner"
  fi
  if [[ $OWNER_ID == *,* ]]; then
    ok "Allowed: $OWNER_ID (the first one manages AI settings)"
  else
    ok "Owner: $OWNER_ID"
  fi
  env_set UNREAD_BOT_OWNER_ID "$OWNER_ID"
}

# More people besides the owner. Each gets their own chat with the bot;
# only the first id can change the AI provider, model and key.
# $1 is the list from before a --reconfigure: its other people are the
# default, so re-detecting the owner doesn't silently drop them.
ask_more_people() {
  local more="" before="" id
  local IFS=,
  for id in $1; do
    [[ -n $id && $id != "$OWNER_ID" ]] && before+=${before:+,}$id
  done
  unset IFS
  say "Anyone else? Their numeric ids (they can message @userinfobot to get theirs)."
  hint "Change this any time with: $HELPER users add ID / $HELPER users remove ID"
  while :; do
    if [[ -n $before ]]; then
      ask more "Other people's ids, comma-separated (\"none\" removes them)" "$before"
    else
      ask more "Other people's ids, comma-separated (Enter to skip)"
    fi
    more=${more// /}
    [[ -z $more || $more == none ]] && return 0
    if valid_ids "$more"; then
      OWNER_ID=$(merge_ids "$OWNER_ID" "$more")
      return 0
    fi
    warn "Type digits only, like 123456789 (several: 111,222)."
  done
}

# merge_ids "1,2" "2,3" → "1,2,3": order kept, duplicates dropped.
merge_ids() {
  local out="" id
  local IFS=,
  for id in $1 $2; do
    [[ -n $id && ,$out, != *,"$id",* ]] && out+=${out:+,}$id
  done
  printf '%s' "$out"
}

collect_ai() {
  step "Step 3/4 · AI provider"
  local provider choice old_provider key openai_key p var have=""
  provider=$(current UNREAD_AI_CHAT_PROVIDER)
  old_provider=$(env_get UNREAD_AI_CHAT_PROVIDER)
  for p in openai anthropic google openrouter; do
    if [[ -n $(current "$(provider_key_var "$p")") ]]; then have+="$p "; fi
  done

  if [[ -z $provider && -n $have ]]; then
    # No provider named: OpenAI when its key is there, else the only key given.
    if [[ $have == *openai* ]]; then
      provider=openai
    elif [[ $(wc -w <<<"$have") -eq 1 ]]; then
      provider=$(trim "$have")
    elif [[ $INTERACTIVE == 0 ]]; then
      die "Several AI keys are set — pick one with UNREAD_AI_CHAT_PROVIDER."
    fi
  fi

  if [[ $RECONFIGURE == 0 && -n $provider && -n $(current "$(provider_key_var "$provider")") ]]; then
    ok "Using $(provider_label "$provider") ($(mask "$(current "$(provider_key_var "$provider")")"))."
  else
    require_tty "an AI key (OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY or OPENROUTER_API_KEY)"
    say "Which AI should read things for you?"
    say "  1) OpenAI       recommended — also transcribes voice messages and video"
    say "  2) Anthropic    Claude"
    say "  3) Google       Gemini"
    say "  4) OpenRouter   one key, many models"
    case ${provider:-openai} in openai) choice=1 ;; anthropic) choice=2 ;; google) choice=3 ;; *) choice=4 ;; esac
    while :; do
      ask choice "Choose 1-4" "$choice"
      case $choice in
        1) provider=openai ;;
        2) provider=anthropic ;;
        3) provider=google ;;
        4) provider=openrouter ;;
        *) warn "Type a number from 1 to 4."; continue ;;
      esac
      break
    done
    hint "Get a key: $(provider_key_url "$provider")"
    key=$(prompt_ai_key "$provider" "$(current "$(provider_key_var "$provider")")")
    env_set "$(provider_key_var "$provider")" "$key"
    if [[ $provider != openai && -z $(current OPENAI_API_KEY) ]]; then
      say "Voice messages and video are transcribed by OpenAI (Whisper). Add an OpenAI key for that?"
      hint "Get a key: $(provider_key_url openai)"
      openai_key=$(prompt_ai_key openai "" 1)
      if [[ -n $openai_key ]]; then env_set OPENAI_API_KEY "$openai_key"; fi
    fi
  fi

  # Keys that came from the environment must reach the container too.
  for p in openai anthropic google openrouter; do
    var=$(provider_key_var "$p")
    key=$(printenv "$var" 2>/dev/null || true)
    if [[ -n $key ]]; then env_set "$var" "$key"; fi
  done

  # OpenAI is every slot's default, so it needs no routing at all — and
  # leaving it unset keeps the bot's /settings provider switch in charge.
  if [[ $provider == openai ]]; then
    env_unset UNREAD_AI_CHAT_PROVIDER
    env_unset UNREAD_AI_FILTER_PROVIDER
    env_unset UNREAD_AI_VISION_PROVIDER
  else
    env_set UNREAD_AI_CHAT_PROVIDER "$provider"
    env_set UNREAD_AI_FILTER_PROVIDER "$provider"
    env_set UNREAD_AI_VISION_PROVIDER "$provider"
  fi
  if [[ -n $old_provider && $old_provider != "$provider" ]]; then
    # A model picked for the previous provider would be a foreign id now.
    env_unset UNREAD_AI_CHAT_MODEL
    env_unset UNREAD_AI_FILTER_MODEL
    env_unset UNREAD_AI_VISION_MODEL
  fi
}

collect_telegram_api() {
  step "Step 4/4 · Telegram API id and hash"
  local api_id api_hash
  api_id=$(current TELEGRAM_API_ID)
  api_hash=$(current TELEGRAM_API_HASH)
  if [[ $RECONFIGURE == 0 && $api_id =~ ^[0-9]{3,12}$ && $api_hash =~ ^[0-9a-fA-F]{32}$ ]]; then
    ok "Using API id $api_id."
  else
    require_tty "TELEGRAM_API_ID / TELEGRAM_API_HASH"
    say "The bot connects to Telegram the way apps do (MTProto), not via the plain"
    say "Bot API, which can't download files over 20 MB. That connection needs"
    say "an app id and hash, even though you never log in to your own account."
    say "They're free:"
    say "  1. Open https://my.telegram.org and log in with your phone number."
    say "  2. Open \"API development tools\" and create an app (any name will do)."
    say "  3. Copy \"App api_id\" and \"App api_hash\" here."
    while :; do
      ask api_id "api_id" "$api_id"
      [[ $api_id =~ ^[0-9]{3,12}$ ]] && break
      warn "api_id is a number, like 1234567."
      api_id=""
    done
    while :; do
      ask_secret api_hash "api_hash" "$api_hash"
      [[ $api_hash =~ ^[0-9a-fA-F]{32}$ ]] && break
      warn "api_hash is 32 characters: digits and the letters a-f."
      api_hash=""
    done
    ok "API id $api_id, hash $(mask "$api_hash")."
  fi
  env_set TELEGRAM_API_ID "$api_id"
  env_set TELEGRAM_API_HASH "$api_hash"
}

write_compose_file() {
  step "Writing $DIR/docker-compose.yml"
  local tmp src local_copy=""
  tmp=$(mktemp "$DIR/.compose.XXXXXX")
  # Run from a checkout? Use its compose file, so local edits get tested.
  # (Under `curl | bash`, BASH_SOURCE[0] is just "main".)
  src="${BASH_SOURCE[0]:-}"
  if [[ $(basename "$src") == install-bot-docker.sh && -f $src ]]; then
    local_copy="$(cd "$(dirname "$src")/.." && pwd)/docker-compose.bot.yml"
  fi
  if [[ -n $local_copy && -f $local_copy ]]; then
    cp "$local_copy" "$tmp"
  elif ! curl -fsSL "$REPO_RAW/$REF/docker-compose.bot.yml" -o "$tmp"; then
    rm -f "$tmp"
    die "Couldn't download docker-compose.bot.yml from GitHub."
  fi
  chmod 644 "$tmp"
  if [[ -f $DIR/docker-compose.yml ]] && ! cmp -s "$tmp" "$DIR/docker-compose.yml"; then
    cp "$DIR/docker-compose.yml" "$DIR/docker-compose.yml.bak"
    hint "Refreshed it; the previous copy is docker-compose.yml.bak. Keep your own changes in"
    hint "docker-compose.override.yml — Compose merges that file in automatically."
  fi
  mv "$tmp" "$DIR/docker-compose.yml"
  if [[ -n $IMAGE_OVERRIDE ]]; then env_set UNREAD_BOT_IMAGE "$IMAGE_OVERRIDE"; fi
  ok "Settings saved to $ENV_FILE"
}

start_bot() {
  step "Starting the bot"
  warn_native_service
  pull_image || die "Couldn't download the bot image — see the error above."
  compose up -d --remove-orphans </dev/null
  hint "Waiting for it to connect to Telegram…"
  local rc=0 cid logs
  wait_for_ready || rc=$?
  cid=$(container_id)
  logs=$(dk docker logs "$cid" 2>&1 | tail -n 25 || true)
  case $rc in
    0)
      ok "The bot is up and listening."
      BOT_READY=1
      ;;
    1)
      compose stop >/dev/null 2>&1 || true
      err "The bot stopped right after starting. Its last log lines:"
      printf '%s\n' "$logs" | sed 's/^/    /' >&2
      explain_failure "$logs"
      exit 1
      ;;
    *)
      warn "The bot hasn't reported ready after ${READY_TIMEOUT}s. Its last log lines:"
      printf '%s\n' "$logs" | sed 's/^/    /'
      say "If it's waiting out a Telegram rate limit it carries on by itself. Otherwise make"
      say "sure this server can reach Telegram, and follow along with: $HELPER logs"
      ;;
  esac
}

write_helper() {
  local target=$1
  {
    printf '#!/usr/bin/env bash\n'
    printf '# unread-bot — manage the unread Telegram bot set up by install-bot-docker.sh.\n'
    printf "# Run \"unread-bot help\" for the commands.\n"
    printf 'DIR=%q\n' "$DIR"
    cat <<'HELPER'
set -euo pipefail
SERVICE=unread-bot

if [[ -t 1 ]]; then G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; B=$'\033[1m'; D=$'\033[2m'; N=$'\033[0m'
else G=""; Y=""; R=""; B=""; D=""; N=""; fi
die() { printf '%s✖%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

cd "$DIR" 2>/dev/null || die "$DIR is gone. Re-run the installer to set the bot up again."

# Docker through sudo when this user can't reach the daemon directly.
SUDO=0
if ! docker info >/dev/null 2>&1; then
  if [[ $EUID -ne 0 ]] && command -v sudo >/dev/null 2>&1; then SUDO=1
  else die "Can't reach Docker — is it running? (systemctl start docker)"; fi
fi
dk() { if [[ $SUDO == 1 ]]; then sudo "$@"; else "$@"; fi; }
if dk docker compose version >/dev/null 2>&1; then DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then DC=(docker-compose)
else die "Docker Compose isn't installed."; fi
dc() { dk "${DC[@]}" "$@"; }

confirm() {
  local def=${2:-N} reply="" choices="[y/N]"
  [[ $def == Y ]] && choices="[Y/n]"
  read -r -p "$1 $choices " reply || true
  [[ -z $reply ]] && reply=$def
  [[ $reply == [Yy]* ]]
}

container() { dc ps -a -q "$SERVICE" 2>/dev/null | head -n1 || true; }

# The allowlist in .env, as "111,222" (quotes and spaces stripped).
owner_ids() {
  sed -n "s/^UNREAD_BOT_OWNER_ID=//p" "$DIR/.env" | tail -n1 | tr -d "'\" "
}

set_owner_ids() {
  local tmp
  tmp=$(mktemp "$DIR/.env.XXXXXX")
  awk -v v="UNREAD_BOT_OWNER_ID='$1'" '
    /^UNREAD_BOT_OWNER_ID=/ { if (!done) print v; done = 1; next }
    { print }
    END { if (!done) print v }
  ' "$DIR/.env" >"$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$DIR/.env"
}

users() {
  local action=${1:-list} ids out="" id
  ids=$(owner_ids)
  if [[ $action == list ]]; then
    printf 'People who may use the bot (the first one manages AI settings):\n'
    tr ',' '\n' <<<"$ids" | sed 's/^/  /'
    printf '%sAdd or remove with: unread-bot users add ID / unread-bot users remove ID%s\n' "$D" "$N"
    return 0
  fi
  [[ $action == add || $action == remove ]] || die "Use: unread-bot users [list | add ID… | remove ID…]"
  shift
  [[ $# -gt 0 ]] || die "Which id? e.g. unread-bot users $action 123456789 (people get theirs from @userinfobot)"
  local args=" ${*//,/ } "
  for id in $args; do
    [[ $id =~ ^[0-9]{3,15}$ ]] || die "$id isn't a Telegram id — those are digits only, like 123456789."
  done
  if [[ $action == add ]]; then
    for id in ${ids//,/ } $args; do
      [[ ,$out, == *,"$id",* ]] || out+=${out:+,}$id
    done
  else
    for id in ${ids//,/ }; do
      [[ $args == *" $id "* ]] || out+=${out:+,}$id
    done
    [[ -n $out ]] || die "That would leave nobody who can use the bot."
  fi
  if [[ $out == "$ids" ]]; then
    printf 'Nothing to change: %s\n' "$ids"
    return 0
  fi
  set_owner_ids "$out"
  printf '%s✓%s Allowed now: %s\n' "$G" "$N" "$out"
  dc up -d --force-recreate "$SERVICE" && wait_ready
}

version() { dc exec -T "$SERVICE" unread --version 2>/dev/null | head -n1 | sed 's/^\[\*\] //' || true; }

wait_ready() {
  local cid restarts deadline=$((SECONDS + ${1:-90}))
  printf '%sWaiting for the bot to connect…%s\n' "$D" "$N"
  while (( SECONDS < deadline )); do
    cid=$(container)
    if [[ -n $cid ]]; then
      if dk docker logs "$cid" 2>&1 | tail -n 300 | grep -q "bot ready"; then
        printf '%s✓%s Running — %s\n' "$G" "$N" "$(version)"
        return 0
      fi
      restarts=$(dk docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null || echo 0)
      if (( ${restarts:-0} > 0 )); then
        printf '%s✖%s The bot keeps crashing. Its last log lines:\n' "$R" "$N" >&2
        dk docker logs --tail 20 "$cid" 2>&1 | sed 's/^/    /' >&2
        printf 'Fix the setting with: unread-bot config\n' >&2
        return 1
      fi
    fi
    sleep 2
  done
  printf '%s!%s Not ready yet — follow along with: unread-bot logs\n' "$Y" "$N"
}

pull() {
  local anon rc=0
  dc pull "$SERVICE" || rc=$?
  if (( rc != 0 )); then
    # A ghcr.io login on this machine can make GHCR refuse the public image.
    anon=$(mktemp -d)
    echo '{}' >"$anon/config.json"
    rc=0
    dk env "DOCKER_CONFIG=$anon" "${DC[@]}" pull "$SERVICE" || rc=$?
    rm -rf "$anon"
  fi
  return "$rc"
}

remove_links() {
  local link
  for link in /usr/local/bin/unread-bot "$HOME/.local/bin/unread-bot"; do
    [[ -L $link && $(readlink "$link") == "$DIR/unread-bot" ]] || continue
    if [[ -w $(dirname "$link") ]]; then rm -f "$link"; else sudo rm -f "$link" || true; fi
  done
}

usage() {
  cat <<EOF
${B}unread-bot${N} — manage your unread Telegram bot (files in $DIR)

  status      is it running, which version, is it connected
  logs        follow the bot's log (Ctrl-C stops watching, not the bot)
  restart     restart it — also applies edits to .env
  update      download the latest version and restart
  config      edit the settings (.env), then apply them
  users       who may use the bot: users add ID / users remove ID
  stop        stop the bot
  start       start it again
  doctor      check the bot's setup from inside the container
  uninstall   remove the bot (asks before deleting any data)
EOF
}

cmd=${1:-help}
[[ $# -gt 0 ]] && shift
case $cmd in
  status)
    cid=$(container)
    if [[ -z $cid ]]; then
      printf '%s●%s not running — start it with: unread-bot start\n' "$Y" "$N"
      exit 1
    fi
    IFS='|' read -r state started restarts image <<<"$(dk docker inspect -f '{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.Config.Image}}' "$cid")"
    if [[ $state != running ]]; then
      [[ $state == exited ]] && state=stopped
      printf '%s●%s %s — start it with: unread-bot start\n' "$R" "$N" "$state"
      printf '%sLast log lines:%s\n' "$D" "$N"
      dk docker logs --tail 5 "$cid" 2>&1 | sed 's/^/    /'
      exit 1
    fi
    printf '%s●%s running · %s · %s\n' "$G" "$N" "$(version)" "$image"
    printf '  started %s · restarts: %s\n' "${started%%.*}" "$restarts"
    ready=$(dk docker logs "$cid" 2>&1 | grep "bot ready" | tail -n1 || true)
    if [[ -n $ready ]]; then printf '  %s\n' "$ready"; fi
    printf '  settings: %s/.env\n' "$DIR"
    ;;
  logs) dc logs -f --tail "${1:-100}" "$SERVICE" ;;
  restart) dc up -d --force-recreate "$SERVICE" && wait_ready ;;
  start) dc up -d "$SERVICE" && wait_ready ;;
  stop) dc stop "$SERVICE" ;;
  update)
    before=$(version)
    pull || die "Couldn't download the update — see the error above."
    dc up -d "$SERVICE"
    # Drop the superseded image so updates don't slowly fill the disk.
    dk docker image prune -f --filter "label=org.opencontainers.image.title=unread" >/dev/null 2>&1 || true
    wait_ready
    if [[ -n $before && $before == "$(version)" ]]; then printf 'Already up to date.\n'; fi
    ;;
  config)
    editor=${VISUAL:-${EDITOR:-}}
    if [[ -z $editor ]]; then
      for e in nano vim vi; do
        if command -v "$e" >/dev/null 2>&1; then editor=$e; break; fi
      done
    fi
    [[ -n $editor ]] || die "No text editor found. Edit $DIR/.env, then run: unread-bot restart"
    # shellcheck disable=SC2086  # EDITOR may carry flags, e.g. "code -w"
    $editor "$DIR/.env"
    dc up -d "$SERVICE" && wait_ready
    ;;
  doctor) dc exec "$SERVICE" unread doctor ;;
  users) users "$@" ;;
  uninstall)
    confirm "Stop and remove the unread bot?" N || exit 0
    if confirm "Also delete its data — reports, settings, Telegram session? This can't be undone." N; then
      dc down -v --remove-orphans
    else
      dc down --remove-orphans
    fi
    remove_links
    if confirm "Delete $DIR too (your .env with the keys)?" N; then
      rm -rf "$DIR"
      printf '%s✓%s Removed everything.\n' "$G" "$N"
    else
      printf '%s✓%s Removed. Your settings are still in %s/.env\n' "$G" "$N" "$DIR"
    fi
    ;;
  help | -h | --help) usage ;;
  *) usage; exit 2 ;;
esac
HELPER
  } >"$target.tmp"
  chmod 755 "$target.tmp"
  mv "$target.tmp" "$target"
}

install_helper() {
  local target="$DIR/unread-bot" link=""
  write_helper "$target"
  if [[ $EUID -eq 0 ]]; then
    if ln -sf "$target" /usr/local/bin/unread-bot 2>/dev/null; then link=/usr/local/bin/unread-bot; fi
  elif [[ ":$PATH:" == *":$HOME/.local/bin:"* ]]; then
    if mkdir -p "$HOME/.local/bin" && ln -sf "$target" "$HOME/.local/bin/unread-bot"; then
      link="$HOME/.local/bin/unread-bot"
    fi
  elif [[ -n $SUDO ]] && sudo -n true 2>/dev/null; then
    if sudo ln -sf "$target" /usr/local/bin/unread-bot 2>/dev/null; then link=/usr/local/bin/unread-bot; fi
  fi
  if [[ -z $link ]] && mkdir -p "$HOME/.local/bin" && ln -sf "$target" "$HOME/.local/bin/unread-bot"; then
    link="$HOME/.local/bin/unread-bot"
  fi
  HELPER="unread-bot"
  if [[ -z $link || ":$PATH:" != *":$(dirname "$link"):"* ]]; then HELPER="$target"; fi
}

summary() {
  local bar="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  printf '\n%s%s%s\n' "$C_GREEN" "$bar" "$C_RST"
  if [[ $BOT_READY == 1 ]]; then
    printf '%s✓ Done!%s' "$C_GREEN" "$C_RST"
    if [[ -n $BOT_USERNAME ]]; then printf ' Your bot: %shttps://t.me/%s%s' "$C_BOLD" "$BOT_USERNAME" "$C_RST"; fi
    printf '\n'
    say "Send it a voice message, a PDF, a web link or a YouTube link."
    say "In the bot, /help lists everything; /settings changes language and format,"
    say "and /settings → 🧠 Model picks the AI model."
  else
    printf '%s✓ Installed.%s Still starting — check on it with: %s status\n' "$C_GREEN" "$C_RST" "$HELPER"
  fi
  printf '\n'
  say "Manage it:  $HELPER status | logs | restart | update | config | users | stop"
  say "Settings:   $ENV_FILE"
  say "Optional:   to analyze t.me chat links too, send the bot /upload_session"
  if [[ $DOCKER_SUDO == 1 ]]; then
    hint "Docker needs sudo for your user. To drop it: sudo usermod -aG docker $USER, then log in again."
  fi
  printf '%s%s%s\n' "$C_GREEN" "$bar" "$C_RST"
}

main() {
  parse_args "$@"
  preflight
  intro
  ensure_docker
  prepare_dir
  collect_bot_token
  collect_owner
  collect_ai
  collect_telegram_api
  write_compose_file
  install_helper
  start_bot
  if [[ $BOT_READY == 1 ]]; then say_hello; fi
  summary
}

main "$@"
