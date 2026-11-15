-- 交接前仓库里已有的组织、成员、任务和一条事实用量。
-- 代理键在这里显式写入，便于文档与测试断言“身份保留”关系。

-- 旧成员（复合自然键 tenant_id, member_code）
INSERT INTO handoff_demo.member
    (id, tenant_id, member_code, display_name, alias, score, vip, manager_id, org_id)
VALUES
    (1001, 'T1', 'M1', '成员一（旧名）', 'alias-one',  10,  true,  NULL, NULL),
    (1002, 'T1', 'M2', '成员二',         'alias-two',  20,  false, 1001, NULL),
    (1003, 'T1', 'M3', '成员三（将删除）','alias-three',30, NULL, 1002, NULL),
    (1004, 'T1', 'M4', '成员四（保留原样）','alias-four',40, true, 1001, NULL);
SELECT setval('handoff_demo.member_id_seq', 1004);

-- 旧组织（contact_member 非空，直接指向已存在的成员）
INSERT INTO handoff_demo.organization (id, org_code, org_tenant, legal_name, tier, active, contact_member)
VALUES
    (101, 'ORG', 'A', '甲组织（旧）', 3, true,  1001),
    (102, 'ORG', 'B', '乙组织',       7, false, 1002);
SELECT setval('handoff_demo.organization_id_seq', 102);

-- 成员回填组织归属
UPDATE handoff_demo.member SET org_id = 101 WHERE id IN (1001, 1002);
UPDATE handoff_demo.member SET org_id = 102 WHERE id IN (1003, 1004);

-- 旧任务
INSERT INTO handoff_demo.assignment (id, project_key, role_code, note, member_id)
VALUES
    (5001, 'P1', 'R1', '旧任务一', 1001),
    (5002, 'P1', 'R2', '旧任务二（将删除）', 1003);
SELECT setval('handoff_demo.assignment_id_seq', 5002);

-- 外部事实表引用成员 1002 与 1004：这些身份在演示的失败场景里受保护，删除会整次拒绝。
INSERT INTO handoff_demo.fact_usage (event_name, member_id)
VALUES ('login', 1002), ('report', 1004);

-- 引号/中文/点标识符表的初始行
INSERT INTO handoff_demo."weird 名.表 ""x""" (id, kc, label)
VALUES (9001, 'w1', '旧标签');
SELECT setval('handoff_demo."weird 名 表_id_seq"', 9001);
