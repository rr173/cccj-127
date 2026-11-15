#!/usr/bin/env python3
"""离线可启动的 PostgreSQL 演示环境管理。

不依赖宿主已有服务：在项目目录下维护独立的数据目录、独立端口和专用数据库，
所有路径/端口/角色都显式配置。支持的动作：

  init     初始化本地集群（若已存在则报错退出，避免误覆盖）
  start    启动集群并创建专用演示数据库/角色
  stop     停止集群
  status   查看状态
  reset    重新执行 schema + 种子（先 DROP SCHEMA，数据回到交接前）
  destroy  删除整个本地数据目录（清理演示环境）
  psql     用演示连接参数打开 psql
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PG_HOME = Path("/workspace/.pglocal/usr/lib/postgresql/15")
PG_HOME = Path(os.environ.get("PG_HOME", DEFAULT_PG_HOME))
PGDATA = Path(os.environ.get("HANDOFF_PGDATA", PROJECT_ROOT / ".demo-pgdata"))
PORT = int(os.environ.get("HANDOFF_PGPORT", "55432"))
SOCKET_DIR = PROJECT_ROOT / ".demo-pgsocket"
ROLE = "handoff"
# 实际执行装载的非超级用户、非复制角色（超级用户只负责建库）。
LOAD_ROLE = "handoff_loader"
DB = "handoff_demo_db"
LOG = PGDATA / "server.log"
SQL_DIR = PROJECT_ROOT / "demo" / "sql"


def bin_path(name: str) -> Path:
    p = PG_HOME / "bin" / name
    if not p.exists():
        sys.exit(f"找不到 {p}；设置 PG_HOME 指向解压后的 PostgreSQL 安装目录")
    return p


def run(cmd: list[str], env: dict[str, str] | None = None, check: bool = True) -> int:
    print("+", " ".join(str(c) for c in cmd))
    merged = {**os.environ, **(env or {})}
    proc = subprocess.run([str(c) for c in cmd], env=merged)
    if check and proc.returncode != 0:
        sys.exit(proc.returncode)
    return proc.returncode


def socket_args() -> list[str]:
    return ["-h", str(SOCKET_DIR), "-p", str(PORT)]


def is_listening() -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(str(SOCKET_DIR / f".s.PGSQL.{PORT}"))
            return True
        except OSError:
            return False


def action_init() -> None:
    if PGDATA.exists():
        sys.exit(f"数据目录已存在: {PGDATA}（如需重建请先 destroy）")
    PGDATA.parent.mkdir(parents=True, exist_ok=True)
    SOCKET_DIR.mkdir(parents=True, exist_ok=True)
    run([
        bin_path("initdb"), "-D", PGDATA, "-U", ROLE,
        "--auth=trust", "--no-locale", "--encoding=UTF8",
    ])
    # 独立端口与 unix socket 目录，与宿主任何服务隔离。
    with (PGDATA / "postgresql.conf").open("a", encoding="utf-8") as fh:
        fh.write("\n# handoff demo 专用配置\n")
        fh.write(f"port = {PORT}\n")
        fh.write(f"unix_socket_directories = '{SOCKET_DIR}'\n")
        fh.write("listen_addresses = ''\n")  # 只走 unix socket，不暴露 TCP
        fh.write("max_connections = 30\n")
        # 本地精简安装不含 LLVM 运行库，关闭 JIT，避免大表达式查询加载 llvmjit 失败。
        fh.write("jit = off\n")


def action_start() -> None:
    if not PGDATA.exists():
        action_init()
    SOCKET_DIR.mkdir(parents=True, exist_ok=True)
    if is_listening():
        print("演示集群已在运行")
    else:
        run([
            bin_path("pg_ctl"), "-D", PGDATA, "-l", LOG,
            "-o", f"-p {PORT} -k {SOCKET_DIR}", "start",
        ])
        for _ in range(50):
            if is_listening():
                break
    psql = bin_path("psql")
    # 专用演示数据库（不存在才创建）。
    run([psql, *socket_args(), "-U", ROLE, "-d", "postgres",
         "-tc", f"SELECT 1 FROM pg_database WHERE datname = '{DB}'"], check=False)
    exists = subprocess.run(
        [psql, *socket_args(), "-U", ROLE, "-d", "postgres", "-tAc",
         f"SELECT 1 FROM pg_database WHERE datname = '{DB}'"],
        capture_output=True, text=True,
    ).stdout.strip()
    if exists != "1":
        run([psql, *socket_args(), "-U", ROLE, "-d", "postgres", "-c",
             f"CREATE DATABASE {DB} OWNER {ROLE}"])
    # 非特权装载角色：NOSUPERUSER / NOREPLICATION / NOBYPASSRLS。
    role_exists = subprocess.run(
        [psql, *socket_args(), "-U", ROLE, "-d", "postgres", "-tAc",
         f"SELECT 1 FROM pg_roles WHERE rolname = '{LOAD_ROLE}'"],
        capture_output=True, text=True,
    ).stdout.strip()
    if role_exists != "1":
        run([psql, *socket_args(), "-U", ROLE, "-d", "postgres", "-c",
             f"CREATE ROLE {LOAD_ROLE} LOGIN NOSUPERUSER NOREPLICATION "
             "NOBYPASSRLS PASSWORD NULL"])
    print(f"演示数据库就绪: {dsn()}")


def action_stop() -> None:
    if not PGDATA.exists() or not is_listening():
        print("演示集群未在运行")
        return
    run([bin_path("pg_ctl"), "-D", PGDATA, "stop", "-m", "fast"])


def action_reset() -> None:
    action_start()
    psql = bin_path("psql")
    for sql in sorted(SQL_DIR.glob("*.sql")):
        run([psql, *socket_args(), "-U", ROLE, "-d", DB, "-v", "ON_ERROR_STOP=1",
             "-f", sql])
    # schema/表/序列权限授予非特权装载角色（DDL 仍由库属主超级用户执行）。
    run([psql, *socket_args(), "-U", ROLE, "-d", DB, "-v", "ON_ERROR_STOP=1", "-c",
         f"GRANT USAGE ON SCHEMA handoff_demo TO {LOAD_ROLE}; "
         f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA handoff_demo "
         f"TO {LOAD_ROLE}; "
         f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA handoff_demo TO {LOAD_ROLE};"])
    print("schema 与种子数据已重置为交接前状态")


def action_status() -> None:
    print(f"PGDATA={PGDATA}")
    print(f"PORT={PORT} SOCKET={SOCKET_DIR}")
    print("running=" + ("yes" if is_listening() else "no"))
    if is_listening():
        print("DSN=" + dsn())


def action_destroy() -> None:
    if is_listening():
        action_stop()
    if PGDATA.exists():
        shutil.rmtree(PGDATA)
    if SOCKET_DIR.exists():
        shutil.rmtree(SOCKET_DIR)
    print("演示环境已清理")


def action_psql() -> None:
    os.execv(str(bin_path("psql")), [
        str(bin_path("psql")), *socket_args(), "-U", ROLE, "-d", DB, *sys.argv[2:]
    ])


def dsn() -> str:
    return f"host={SOCKET_DIR} port={PORT} dbname={DB} user={LOAD_ROLE}"


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    {"init": action_init, "start": action_start, "stop": action_stop,
     "status": action_status, "reset": action_reset, "destroy": action_destroy,
     "psql": action_psql}.get(sys.argv[1], lambda: sys.exit(__doc__))()


if __name__ == "__main__":
    main()
