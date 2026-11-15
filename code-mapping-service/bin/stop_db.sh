#!/usr/bin/env bash
set -euo pipefail
source "$HOME/miniforge3/etc/profile.d/conda.sh"
conda activate pg
pg_ctl -D "${PGDATA:-$HOME/pgdata}" stop
