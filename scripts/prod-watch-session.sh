#!/bin/bash
# Keeps the watcher session running (docs/EPICPROD_WATCHER.md, The wake):
# a Claude Code session named swf-watch in tmux, started with its brief,
# restarted when it exits and once a day so its context starts fresh. Run
# by the user unit prod-watch-session.service.
set -u
NAME=swf-watch
REPO=/data/wenauseic/github/swf-epicprod
BRIEF=$REPO/docs/EPICPROD_WATCHER_BRIEF.md
STAMP=$HOME/.prod-watch-session.started
MAX_AGE_S=86400
CLAUDE=$HOME/.local/bin/claude

start() {
    tmux new-session -d -s "$NAME" -c /data/wenauseic/github \
        "bash -lc 'source ~/.env; export TJAI_SESSION_NAME=$NAME; exec $CLAUDE -n $NAME --permission-mode auto \"\$(cat $BRIEF)\"'"
    date +%s > "$STAMP"
    echo "$(date -u +%FT%TZ) started $NAME"
}

while true; do
    if ! tmux has-session -t "$NAME" 2>/dev/null; then
        start
    elif [ $(( $(date +%s) - $(cat "$STAMP" 2>/dev/null || echo 0) )) -gt $MAX_AGE_S ]; then
        echo "$(date -u +%FT%TZ) daily restart of $NAME"
        tmux kill-session -t "$NAME"
        sleep 5
        start
    fi
    sleep 60
done
