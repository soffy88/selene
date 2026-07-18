#!/usr/bin/env bash
# One-shot scheduled re-run, 2026-08-05 (optimization A4, arranged 2026-07-12).
#
# By then v2_ticks AND v2_liquidations (both start 2026-07-06) have >= 30 days,
# so the studies' data-sufficiency guards flip from PENDING to real verdicts:
#   - lens_study:        H-ICT1a formal VPIN verdict (H-ICT1b stays untestable
#                        while Cascade=0)
#   - ict_advanced_study: ICT-7 liquidation-sweep association
#
# Both studies are deterministic and observation-only; reports are written into
# the repo working tree (analysis/*.md). NO auto-commit — review the verdicts,
# update the candidate-pool 出池记录 per the pool's 失败标准, then commit.
# Installed via crontab: 23 9 5 8 * (self-removes after running).
set -euo pipefail
cd /data/soffy/projects/selene
PW=$(grep '^SELENE_APP_PASSWORD=' .env | cut -d= -f2)
export DB_URL="postgresql://selene_app:${PW}@localhost:5434/selene"
PY=/home/soffy/.venvs/selene-analytics/bin/python
LOG=sel_v2/reports/rerun_20260805.log
{
    echo "=== scheduled rerun $(date -u '+%Y-%m-%d %H:%M UTC') ==="
    "$PY" -m sel_v2.offline.lens_study
    "$PY" -m sel_v2.offline.ict_advanced_study
    echo "=== done — review analysis/lens_*_v1.md + analysis/ict_advanced_v1.md,"
    echo "=== apply pool 失败标准 to H-ICT1a / ICT-7, update 出池记录, commit. ==="
} >>"$LOG" 2>&1
# one-shot: remove our own crontab line so it never fires again
crontab -l 2>/dev/null | grep -v 'rerun_20260805' | crontab -
