#!/usr/bin/env bash
cd /home/f_nisaakk529 || exit 1

source ~/.bashrc >/dev/null 2>&1 || true

while true; do
  echo "[SUPERVISOR] elite_divergence start $(date -u '+%Y-%m-%d %H:%M:%S UTC')" >> elite_divergence_hl_scanner_v3.log
  python3 -u elite_divergence_hl_scanner_v3.py >> elite_divergence_hl_scanner_v3.log 2>&1
  echo "[SUPERVISOR] elite_divergence crashed/exited; restart in 30s $(date -u '+%Y-%m-%d %H:%M:%S UTC')" >> elite_divergence_hl_scanner_v3.log
  sleep 30
done
