#!/bin/bash
# Started from cron @reboot (no sudo on this box -> no system systemd unit).
# flock guarantees a single instance.
cd "$(dirname "$0")"
exec flock -n /tmp/discord-signal.lock \
  ./.venv/bin/uvicorn ingest:app --host 0.0.0.0 --port 8787 >> uvicorn.log 2>&1
