"""handoff-load 终端命令测试。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import psycopg

from handoff.cli import main
from tests.conftest import DEMO, loader_dsn
from tests.helpers import demo_files, snapshot_all


def _args(schema: str, files: dict[str, list[str]], tmp_dir: Path) -> list[str]:
    args = ["--dsn", loader_dsn(), "--contract", str(DEMO / "contract.json")]
    # CLI 读契约后用 schema 替换：写一个临时契约文件到测试临时目录。
    tmp_contract = tmp_dir / f"cli_contract_{schema}.json"
    doc = json.loads((DEMO / "contract.json").read_text("utf-8"))
    doc["schema"] = schema
    for t in doc["tables"]:
        t["schema"] = schema
    tmp_contract.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    args[3] = str(tmp_contract)
    for logical, paths in files.items():
        for p in paths:
            args += ["--files", f"{logical}={p}"]
    return args


def test_cli_success_outputs_json_report(fresh_schema, tmp_path):
    files = demo_files("handoff")
    rc = main(_args(fresh_schema, files, tmp_path))
    assert rc == 0
    after = snapshot_all(fresh_schema)
    assert len(after["member"]) == 6


def test_cli_failure_nonzero_and_rollback(fresh_schema, capsys, tmp_path):
    files = demo_files("handoff")
    before = snapshot_all(fresh_schema)
    # 制造一个目录不匹配：库里加列。
    with psycopg.connect(loader_dsn().replace("handoff_loader", "handoff")) as admin:
        admin.autocommit = True
        admin.execute(
            psycopg.sql.SQL("ALTER TABLE {s}.member ADD COLUMN rogue_cli text").format(
                s=psycopg.sql.Identifier(fresh_schema))
        )
    rc = main(_args(fresh_schema, files, tmp_path))
    assert rc == 1
    err = capsys.readouterr().err
    assert "额外列" in err
    assert snapshot_all(fresh_schema) == before


def test_cli_subprocess_entrypoint(fresh_schema, tmp_path):
    # 以独立进程方式调用（模拟操作员在另一个 shell 执行 load）。
    files = demo_files("handoff")
    proc = subprocess.run(
        [sys.executable, "-m", "handoff.cli", *_args(fresh_schema, files, tmp_path)],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1]),
             "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["committed"] is True
    assert report["tables"]["member"]["inserted"] == 3
