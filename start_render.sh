#!/usr/bin/env bash
set -euo pipefail
python cloud_pilot_seed.py
exec python -m uvicorn app:app --host 0.0.0.0 --port "${PORT:-10000}" --workers 1 --proxy-headers --forwarded-allow-ips '*'
