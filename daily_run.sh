#!/bin/zsh
# ApplyPilot nightly round: discover -> enrich/score/tailor/cover/pdf -> apply until the queue is empty.
# Run a round by hand:   zsh ~/ApplyPilot/daily_run.sh     (settings: ~/ApplyPilot/run_settings.env)
# Stop a running round:  pkill -INT -f "applypilot apply"; pkill -INT -f "applypilot run"

set -u
export HOME="${HOME:?}"
export APPLYPILOT_DIR="$HOME/ApplyPilot"
export PATH="$HOME/.local/bin:$(dirname "$(command -v node 2>/dev/null || echo /usr/local/bin/node)"):/Library/TeX/texbin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export LANG="en_US.UTF-8"
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT

AP="$HOME/.local/bin/applypilot"
DB="$APPLYPILOT_DIR/applypilot.db"
LOGDIR="$APPLYPILOT_DIR/logs"
LOG="$LOGDIR/nightly-$(date +%F).log"
LOCK="$APPLYPILOT_DIR/.nightly.lock"
[ -f "$APPLYPILOT_DIR/run_settings.env" ] && source "$APPLYPILOT_DIR/run_settings.env"
MIN_SCORE="${MIN_SCORE:-7}"; MAX_APPLY="${MAX_APPLY:-60}"; WORKERS="${WORKERS:-2}"; APPLY_MODEL="${APPLY_MODEL:-sonnet}"; HEADLESS="${HEADLESS:-0}"
HEADLESS_FLAG=""; [ "$HEADLESS" = "1" ] && HEADLESS_FLAG="--headless"
mkdir -p "$LOGDIR"

# One round at a time: skip if a previous round (or a manual run) is still going.
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "SKIPPED: another ApplyPilot round is running (lock $LOCK)"; exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT
if pgrep -f "python.*applypilot (apply|run)" >/dev/null; then
  echo "SKIPPED: an applypilot process is already running"; exit 0
fi

counts() { sqlite3 "$DB" "select sum(apply_status='applied'), sum(apply_status='failed'), sum(fit_score>=7), count(*) from jobs"; }
before=$(counts)
start=$(date +%s)
{
  echo "===== nightly round $(date '+%F %T') ====="
  echo "--- discover";  "$AP" run discover
  echo "--- pipeline";  "$AP" run enrich score tailor cover pdf -w 2 --min-score "$MIN_SCORE" --validation strict
  echo "--- apply";     "$AP" apply --limit "$MAX_APPLY" --min-score "$MIN_SCORE" --workers "$WORKERS" --model "$APPLY_MODEL" $HEADLESS_FLAG
  echo "===== done $(date '+%F %T') ====="
} >> "$LOG" 2>&1 < /dev/null &
round=$!
if command -v caffeinate >/dev/null; then caffeinate -i -w "$round"; fi   # macOS: keep awake until the round finishes
wait "$round"; rc=$?

after=$(counts)
IFS='|' read -r a0 f0 g0 t0 <<< "$before"
IFS='|' read -r a1 f1 g1 t1 <<< "$after"
mins=$(( ($(date +%s) - start) / 60 ))
echo "ApplyPilot nightly round finished in ${mins} min (exit ${rc})"
echo "new postings: $(( t1 - t0 )) | new matches (score>=7): $(( g1 - g0 ))"
echo "applied tonight: $(( a1 - a0 )) | failed tonight: $(( f1 - f0 )) | total applied: ${a1}"
echo "log: $LOG"
exit "$rc"
