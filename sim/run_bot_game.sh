#!/usr/bin/env bash
# Play N bot games against a running sim server and print sim-side time vs wall time.
#   sim/run_bot_game.sh PORT LOGFILE [games] [seed]
PORT=$1; LOG=$2; GAMES=${3:-1}; SEED=${4:-B2WTM8V4}
rpc() { curl -s -m 30 -X POST "http://127.0.0.1:$PORT/" -d "$1"; }
until rpc '{"jsonrpc":"2.0","method":"health","id":1}' | grep -q ok; do sleep 1; done
rpc '{"jsonrpc":"2.0","method":"sim.stats","params":{"reset":true},"id":1}' > /dev/null
s=$(date +%s%N)
"/c/github vs/balatro/.venv/Scripts/python.exe" -m balatro_bot.cli --port "$PORT" --start --deck BLUE --stake WHITE \
  --seed "$SEED" --games "$GAMES" --log "$LOG" > "$LOG.stdout" 2>&1
echo "bot exit $? wall $(( ($(date +%s%N) - s) / 1000000 )) ms"
grep "Game over" "$LOG" | sed 's/.*Game over/Game over/'
rpc '{"jsonrpc":"2.0","method":"sim.stats","id":1}'; echo
