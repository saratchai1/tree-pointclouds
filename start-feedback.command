#!/bin/bash
set -e
cd "$(dirname "$0")"
exec python3 scripts/start_feedback.py
