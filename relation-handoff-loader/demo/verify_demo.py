#!/usr/bin/env python3
"""用独立连接核验演示数据库的实际装载结果（不只是打印拟执行 SQL）。

核验项：
1. 自然键 -> id 的身份保留关系（旧身份不变、新身份全局一致）；
2. 非空循环负责人、组织联系人、任务指派等真实外键，用普通 SQL JOIN 连出来；
3. 不参与装载的 fact_usage 仍能 JOIN 到保留身份；
4. bigint 边界、复合键分隔符/引号、中文引号标识符；
5. 以另一个文件/行顺序再次交相同数据，应全部 unchanged 且 id 不漂移。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from demo.pg_env import dsn  # noqa: E402

SCHEMA = "handoff_demo"


def q(conn, sql, *args):
    return conn.execute(sql, args).fetchall()


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        print(("  ✓ " if cond else "  ✗ ") + msg)
        if not cond:
            failures.append(msg)

    with psycopg.connect(dsn()) as conn:
        # 1) 身份保留
        member_id = {
            (r[0], r[1]): r[2]
            for r in q(conn, f"SELECT tenant_id, member_code, id FROM {SCHEMA}.member ORDER BY id")
        }
        print("== 身份保留 ==")
        check(member_id[("T1", "M1")] == 1001, "M1 保留旧 id 1001")
        check(member_id[("T1", "M2")] == 1002, "M2 保留旧 id 1002")
        check(member_id[("T1", "M4")] == 1004, "M4 保留旧 id 1004")
        check(("T1", "M3") not in member_id, "M3 已删除（无自然键即无身份）")
        new_a = member_id[("T1", "M-NEW-A")]
        new_b = member_id[("T1", "M-NEW-B")]
        check(new_a != new_b and new_a > 1004 and new_b > 1004,
              f"新成员取全局一致新 id（A={new_a}, B={new_b}）")

        print("== 非空循环与真实外键（普通 SQL JOIN）==")
        mgr = {
            r[0]: r[1]
            for r in q(conn, f"""
                SELECT m.member_code, mg.member_code
                FROM {SCHEMA}.member m LEFT JOIN {SCHEMA}.member mg ON mg.id=m.manager_id
                ORDER BY m.id""")
        }
        check(mgr["M-NEW-A"] == "M-NEW-B", "A 的负责人是 B（真实外键）")
        check(mgr["M-NEW-B"] == "M-NEW-A", "B 的负责人是 A（非空循环成立）")
        check(mgr["M1"] == "M2" and mgr["M2"] == "M1", "老成员负责人互换")

        org_contact = dict(q(conn, f"""
            SELECT o.org_code || '/' || o.org_tenant, m.member_code
            FROM {SCHEMA}.organization o JOIN {SCHEMA}.member m ON m.id=o.contact_member
            ORDER BY 1"""))
        check(org_contact["ORG/A"] == "M-NEW-A", "组织 A 联系人非空且指向新成员 A")
        check(org_contact["ORG/C"] == "M-NEW-A", "新组织 C 联系人指向 A")

        print("== 任务指派 JOIN ==")
        assign = dict(q(conn, f"""
            SELECT a.project_key || '/' || a.role_code, m.member_code
            FROM {SCHEMA}.assignment a JOIN {SCHEMA}.member m ON m.id=a.member_id
            ORDER BY 1"""))
        check(assign["P1/R1"] == "M-NEW-B", "P1/R1 改派给 B")
        check(assign["P2/R9"] == "M-NEW-A", "新任务 P2/R9 指向 A")

        print("== 外部事实连接（不参与装载）==")
        facts = q(conn, f"""
            SELECT f.event_name, m.member_code FROM {SCHEMA}.fact_usage f
            JOIN {SCHEMA}.member m ON m.id=f.member_id ORDER BY 1""")
        check(dict(facts) == {"login": "M2", "report": "M4"},
              "fact_usage 仍 JOIN 到保留身份 M2/M4")

        print("== 类型与特殊键 ==")
        edge = q(conn, f"""SELECT score FROM {SCHEMA}.member
                          WHERE member_code='M-NEW-A'""")[0][0]
        check(edge == 9223372036854775807, "bigint 上界不经浮点原样落库")
        edge2 = q(conn, f"""SELECT score FROM {SCHEMA}.member
                           WHERE member_code='M-NEW-B'""")[0][0]
        check(edge2 == -9223372036854775808, "bigint 下界原样落库")
        check(('T1', 'a|b"c,d') in member_id, "复合键分量含 | 引号 逗号 未被拼假键")
        weird = q(conn, f'SELECT kc FROM {SCHEMA}."weird 名.表 ""x""" ORDER BY kc')
        check(any("|" in r[0] for r in weird), "中文/引号/点/空格标识符表可正常查询")

    # 5) 独立进程幂等重交（换文件/行顺序），应全 unchanged、id 不漂移。
    print("== 独立进程幂等重交（打乱 member 行顺序）==")
    before = {k: v for k, v in member_id.items()}
    shuffled = ROOT / "demo/data/_verify_shuffled_member.jsonl"
    lines = []
    for p in ["member-1.jsonl", "member-2.jsonl"]:
        lines += (ROOT / "demo/data/handoff" / p).read_text("utf-8").splitlines()
    lines.reverse()
    shuffled.write_text("\n".join(lines) + "\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "handoff.cli",
         "--dsn", dsn(), "--contract", str(ROOT / "demo/contract.json"),
         "--files", f"organization={ROOT/'demo/data/handoff/organization.jsonl'}",
         "--files", f"member={shuffled}",
         "--files", f"assignment={ROOT/'demo/data/handoff/assignment.jsonl'}",
         "--files", f"weird_named={ROOT/'demo/data/handoff/weird.jsonl'}"],
        cwd=str(ROOT), capture_output=True, text=True,
        env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
    )
    check(proc.returncode == 0, "幂等重交退出码 0")
    if proc.returncode == 0:
        rep = json.loads(proc.stdout)
        all_unchanged = all(
            t["inserted"] == 0 and t["updated"] == 0 and t["deleted"] == 0
            and t["unchanged"] > 0
            for t in rep["tables"].values()
        )
        check(all_unchanged, "四表全部 unchanged")
    shuffled.unlink(missing_ok=True)
    with psycopg.connect(dsn()) as conn:
        after = {
            (r[0], r[1]): r[2]
            for r in q(conn, f"SELECT tenant_id, member_code, id FROM {SCHEMA}.member")
        }
    check(after == before, "重交后 id 不漂移")

    print("\n结果：" + ("全部通过 ✅" if not failures else f"{len(failures)} 项失败 ❌"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
