#!/usr/bin/env bash
# 启动本地 PostgreSQL（conda 环境，免 root）。数据目录 $HOME/pgdata，socket 在 /tmp:55432
set -euo pipefail
source "$HOME/miniforge3/etc/profile.d/conda.sh"
conda activate pg
export PGDATA="${PGDATA:-$HOME/pgdata}"
export PGPORT="${PGPORT:-55432}"
if [ ! -d "$PGDATA" ]; then
  initdb -D "$PGDATA" -U app --auth=trust --no-locale --encoding=UTF8
fi
pg_ctl -D "$PGDATA" -l "$HOME/pg.log" -o "-p $PGPORT -k /tmp" start
sleep 1
psql -h /tmp -p "$PGPORT" -U app -d postgres -c "SELECT current_database(), version();"
