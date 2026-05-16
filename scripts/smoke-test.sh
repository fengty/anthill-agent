#!/usr/bin/env bash
# Anthill — end-to-end smoke test.
#
# Walks every user-facing surface in a clean tmpdir so a successful
# run proves the v0.2.x closed-loop logic actually works for a new
# user. No real model API key is required for the offline portion;
# pass --live to also run a real `anthill ask` (needs a working key).
#
# Usage:
#   scripts/smoke-test.sh             # offline checks only
#   scripts/smoke-test.sh --live      # also exercise a real ask
#
# Exits 0 on success, 1 on any failed check.

set -euo pipefail

LIVE=0
if [ "${1:-}" = "--live" ]; then
  LIVE=1
fi

# --- pretty output ---
BOLD=$'\033[1m'
GREEN=$'\033[32m'
YELLOW=$'\033[33m'
RED=$'\033[31m'
DIM=$'\033[2m'
RESET=$'\033[0m'

PASS=0
FAIL=0
SKIP=0

pass() { printf "${GREEN}✓${RESET} %s\n" "$*"; PASS=$((PASS + 1)); }
warn() { printf "${YELLOW}⚠${RESET} %s\n" "$*"; SKIP=$((SKIP + 1)); }
fail() { printf "${RED}✗${RESET} %s\n" "$*"; FAIL=$((FAIL + 1)); }
info() { printf "${DIM}→${RESET} %s\n" "$*"; }
banner() { printf "\n${BOLD}%s${RESET}\n" "$*"; }

# --- isolate ---
TMP=$(mktemp -d -t anthill-smoke-XXXXXX)
export ANTHILL_HOME="$TMP"

cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

banner "Anthill smoke test"
info "ANTHILL_HOME=$ANTHILL_HOME"
info "anthill binary: $(command -v anthill || echo 'NOT FOUND')"

if ! command -v anthill >/dev/null 2>&1; then
  fail "anthill not on PATH"
  exit 1
fi

# 1. Empty install: doctor reports misses but exits 0
banner "1. anthill doctor on a clean home"
DOC_OUT=$(COLUMNS=200 anthill doctor 2>&1 || true)
if echo "$DOC_OUT" | grep -q "no models"; then
  pass "doctor identifies missing models"
else
  fail "doctor should warn about missing models"
fi

# 2. model add (non-interactive)
banner "2. anthill model add (non-interactive)"
if anthill model add demo \
     --provider deepseek \
     --model deepseek-chat \
     --key "sk-test-fake-key-for-smoke" \
     --set-default > /dev/null; then
  pass "model add accepted"
else
  fail "model add failed"
fi

if anthill model list | grep -q "demo"; then
  pass "model list shows 'demo'"
else
  fail "model list does not show 'demo'"
fi

SHOW_OUT=$(COLUMNS=200 anthill model show demo 2>&1)
if echo "$SHOW_OUT" | grep -q "sk-t"; then
  pass "model show masks key (prefix visible)"
else
  fail "model show does not display masked key"
fi

if echo "$SHOW_OUT" | grep -q "sk-test-fake-key-for-smoke"; then
  fail "model show LEAKED the full key"
else
  pass "model show does not leak full key"
fi

# 3. secrets file integrity
banner "3. secrets.toml chmod + .gitignore"
SECRETS_FILE="$ANTHILL_HOME/secrets.toml"
if [ -f "$SECRETS_FILE" ]; then
  pass "secrets.toml created"
else
  fail "secrets.toml missing"
fi

if [ "$(stat -f '%A' "$SECRETS_FILE" 2>/dev/null || stat -c '%a' "$SECRETS_FILE")" = "600" ]; then
  pass "secrets.toml chmod 600"
else
  fail "secrets.toml NOT chmod 600"
fi

GI="$ANTHILL_HOME/.gitignore"
if [ -f "$GI" ] && grep -q "secrets.toml" "$GI"; then
  pass ".gitignore exists and hides secrets.toml"
else
  fail ".gitignore missing or does not hide secrets.toml"
fi

# 4. nation lifecycle
banner "4. anthill nation"
if anthill nation create kingdom --citizens 2 > /dev/null; then
  pass "nation create 'kingdom' with 2 citizens"
else
  fail "nation create failed"
fi

if anthill nation list | grep -q "kingdom"; then
  pass "nation list shows 'kingdom'"
else
  fail "nation list does not show 'kingdom'"
fi

if anthill nation switch kingdom > /dev/null; then
  pass "nation switch kingdom"
else
  fail "nation switch failed"
fi

# 5. channel CRUD
banner "5. anthill channel"
if anthill channel add testbot \
     --kind telegram \
     --bot-token "123:abc-test-token-not-real" > /dev/null; then
  pass "channel add telegram"
else
  fail "channel add telegram failed"
fi

CH_OUT=$(COLUMNS=200 anthill channel show testbot 2>&1)
if echo "$CH_OUT" | grep -q "123"; then
  pass "channel show includes (masked) token"
else
  fail "channel show missing token display"
fi
if echo "$CH_OUT" | grep -q "123:abc-test-token-not-real"; then
  fail "channel show LEAKED full token"
else
  pass "channel show does not leak full token"
fi

# 6. doctor sees the configured model
banner "6. anthill doctor after setup"
DOC2=$(COLUMNS=200 anthill doctor 2>&1 || true)
if echo "$DOC2" | grep -q "default_model"; then
  pass "doctor reports default_model row"
else
  fail "doctor missing default_model row"
fi
if echo "$DOC2" | grep -q "demo (deepseek/deepseek-chat)"; then
  pass "doctor displays the configured model"
else
  fail "doctor does not display 'demo (deepseek/...)'"
fi

# 7. live ask (only with --live)
banner "7. live anthill ask"
if [ "$LIVE" = "1" ]; then
  if [ -z "${ANTHILL_DEEPSEEK_KEY:-}" ]; then
    warn "skipping live ask: ANTHILL_DEEPSEEK_KEY not set"
  else
    # Replace fake key with the real one for this run only.
    anthill model remove demo --yes > /dev/null
    anthill model add demo \
      --provider deepseek \
      --model deepseek-chat \
      --key "$ANTHILL_DEEPSEEK_KEY" \
      --set-default > /dev/null
    info "calling deepseek (60s budget)..."
    set +e
    OUTPUT=$(
      ( anthill ask "What is two plus two? Reply with just the number." \
          --nation kingdom 2>&1 ) \
      & PID=$!
      ( sleep 60 && kill $PID 2>/dev/null ) &
      wait $PID 2>/dev/null
    )
    EC=$?
    set -e
    if [ "$EC" != "0" ]; then
      fail "live ask timed out or exited non-zero"
      echo "$OUTPUT" | head -10 | sed 's/^/    /'
    elif echo "$OUTPUT" | grep -q "4"; then
      pass "live ask returned the number 4"
    else
      fail "live ask did not contain 4"
      echo "$OUTPUT" | head -10 | sed 's/^/    /'
    fi
  fi
else
  warn "skipping live ask (pass --live to enable)"
fi

# --- summary ---
printf "\n"
TOTAL=$((PASS + FAIL + SKIP))
printf "${BOLD}smoke test: %s${RESET}  pass=%d  fail=%d  skip=%d\n" \
  "$([ "$FAIL" = "0" ] && echo "${GREEN}OK${RESET}" || echo "${RED}FAILED${RESET}")" \
  "$PASS" "$FAIL" "$SKIP"

exit "$FAIL"
