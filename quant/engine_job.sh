#!/usr/bin/env bash
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-QUANT
#  >>> FILE PATH:   quant/engine_job.sh
#  z169 - one engine job: runs quant.run, commits data/quant every 15 minutes WHILE it trades (the app reads
#  summary.json from GitHub - before, nothing appeared until a 5 h 45 m job ended), commits once more at the end,
#  then decides the next job: queue it when the session is still open or quant.run asked to bridge (exit 3);
#  stop on a weekend / holiday; fail loudly when the secrets are missing (exit 2).
# ============================================================
set -u
BRANCH="${GITHUB_REF_NAME:-main}"
git config user.name "valuatio-quant"
git config user.email "actions@github.com"

commit_data() {
  [ -e data/quant ] || return 0
  git add -A data/quant
  git diff --staged --quiet && return 0
  git commit -q -m "quant: $(TZ=America/New_York date +%Y-%m-%d\ %H%M) ET [skip ci]"
  for k in 1 2 3 4 5; do
    if git push -q origin "HEAD:$BRANCH"; then echo "  committed data/quant (attempt $k)"; return 0; fi
    git fetch -q origin "$BRANCH"; git merge -X ours "origin/$BRANCH" --no-edit -q 2>/dev/null || true; sleep $(( k * 3 ))
  done
  echo "::warning::push failed after 5 attempts"
}

chain_next() {
  [ -n "${GH_TOKEN:-}" ] || { echo "::notice::GH_TOKEN not set - not chaining"; return 0; }
  if gh workflow run engine.yml --ref "$BRANCH" -R "$GITHUB_REPOSITORY" >/dev/null 2>&1; then
    echo "chained: the next engine job is queued"
  else
    echo "::warning::could not queue the next engine job (does the workflow grant actions: write?)"
  fi
}

python -m quant.run &
PID=$!
while kill -0 "$PID" 2>/dev/null; do
  for _ in $(seq 1 90); do kill -0 "$PID" 2>/dev/null || break; sleep 10; done   # 15 minutes, checking every 10 s
  kill -0 "$PID" 2>/dev/null && commit_data
done
wait "$PID"; RC=$?
commit_data

if [ "$RC" -eq 2 ]; then echo "engine not configured - see the error above"; exit 2; fi
if [ "$RC" -eq 3 ]; then chain_next; exit 0; fi
OPEN=$(python -c "from quant import market_hours as m; s = m.session_now(); print(1 if s['open'] and (s['minutesToClose'] or 0) > 3 else 0)")
D=$(TZ=America/New_York date +%u)
if [ "$OPEN" = "1" ]; then
  chain_next
elif [ "$D" -le 4 ] && [ "${QUANT_BRIDGE:-1}" = "1" ]; then
  echo "session over - Monday-Thursday: queueing the next job, which waits for tomorrow's 0920 ET"
  chain_next
else
  echo "session over (Friday / weekend) - not chaining; Monday starts from the schedule or the heartbeat"
fi
[ "$RC" -eq 0 ] || exit "$RC"
