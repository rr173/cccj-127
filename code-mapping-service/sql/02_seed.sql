-- ============================================================================
-- 种子数据：三套小型系统代码表 + 目标口径三版本 + 映射三版本
-- 区间均为半开 [from, to)，to=NULL 表示至今
-- ============================================================================

INSERT INTO source_system(code, name) VALUES
    ('CRM', '客户关系管理系统'),
    ('ERP', '企业资源计划系统'),
    ('POS', '门店收银系统')
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- 源系统代码表（每套系统按自身节奏维护生效区间）
-- ---------------------------------------------------------------------------
-- CRM：2025-07-01 代码体系整体换码
INSERT INTO source_code(system_code, code_domain, source_code, source_name, effective_from, effective_to) VALUES
    ('CRM','CUSTOMER_STATUS','A',  '活跃', DATE '2023-01-01', DATE '2025-07-01'),
    ('CRM','CUSTOMER_STATUS','F',  '冻结', DATE '2023-01-01', DATE '2025-07-01'),
    ('CRM','CUSTOMER_STATUS','C',  '注销', DATE '2023-01-01', DATE '2025-07-01'),
    ('CRM','CUSTOMER_STATUS','ACT','活跃', DATE '2025-07-01', NULL),
    ('CRM','CUSTOMER_STATUS','FRO','冻结', DATE '2025-07-01', NULL),
    ('CRM','CUSTOMER_STATUS','CLS','注销', DATE '2025-07-01', NULL);

-- ERP：E30 挂起 2026 年停用，新增 E40 观察
INSERT INTO source_code(system_code, code_domain, source_code, source_name, effective_from, effective_to) VALUES
    ('ERP','CUSTOMER_STATUS','E10','在用', DATE '2023-01-01', NULL),
    ('ERP','CUSTOMER_STATUS','E20','停用', DATE '2023-01-01', NULL),
    ('ERP','CUSTOMER_STATUS','E30','挂起', DATE '2023-01-01', DATE '2026-01-01'),
    ('ERP','CUSTOMER_STATUS','E40','观察', DATE '2026-01-01', NULL);

-- POS：2026-01-01 数字码换字母码；2 睡眠 全周期有效
INSERT INTO source_code(system_code, code_domain, source_code, source_name, effective_from, effective_to) VALUES
    ('POS','CUSTOMER_STATUS','1',       '正常', DATE '2023-01-01', DATE '2026-01-01'),
    ('POS','CUSTOMER_STATUS','0',       '停用', DATE '2023-01-01', DATE '2026-01-01'),
    ('POS','CUSTOMER_STATUS','2',       '睡眠', DATE '2023-01-01', NULL),
    ('POS','CUSTOMER_STATUS','ACTIVE',  '正常', DATE '2026-01-01', NULL),
    ('POS','CUSTOMER_STATUS','INACTIVE','停用', DATE '2026-01-01', NULL);

-- ---------------------------------------------------------------------------
-- 目标分析口径与版本
-- ---------------------------------------------------------------------------
INSERT INTO target_dimension(code, name) VALUES ('CUSTOMER_STATUS','统一客户状态分析口径')
ON CONFLICT DO NOTHING;

INSERT INTO target_version(dimension_code, version_label, effective_from, effective_to) VALUES
    ('CUSTOMER_STATUS','v2024', DATE '2024-01-01', DATE '2025-01-01'),
    ('CUSTOMER_STATUS','v2025', DATE '2025-01-01', DATE '2026-01-01'),
    ('CUSTOMER_STATUS','v2026', DATE '2026-01-01', NULL);

INSERT INTO target_code(target_version_id, target_code, target_name)
SELECT id, c, n FROM target_version tv,
    LATERAL (VALUES
        ('ACTIVE','活跃'),('INACTIVE','非活跃'),('CHURNED','流失')
    ) AS v(c,n)
WHERE tv.version_label='v2024';

INSERT INTO target_code(target_version_id, target_code, target_name)
SELECT id, c, n FROM target_version tv,
    LATERAL (VALUES
        ('ACTIVE','活跃'),('INACTIVE','非活跃'),('CHURNED','流失'),('DORMANT','沉睡')
    ) AS v(c,n)
WHERE tv.version_label='v2025';

INSERT INTO target_code(target_version_id, target_code, target_name)
SELECT id, c, n FROM target_version tv,
    LATERAL (VALUES
        ('ACTIVE','活跃'),('INACTIVE','非活跃'),('CHURNED','流失'),
        ('DORMANT','沉睡'),('PENDING','待定')
    ) AS v(c,n)
WHERE tv.version_label='v2026';

-- ---------------------------------------------------------------------------
-- 映射版本 m2024（对应目标 v2024）
-- g1 为 MERGE：CRM:A 与 POS:1 合并到 ACTIVE；g6/g9 为 UNMAPPED
-- ---------------------------------------------------------------------------
INSERT INTO mapping_version(target_version_id, version_label, effective_from, effective_to, status, published_at)
SELECT id, 'm2024', DATE '2024-01-01', DATE '2025-01-01', 'published', now()
FROM target_version WHERE version_label='v2024';

INSERT INTO mapping_row(mapping_version_id, system_code, code_domain, source_code, target_code,
                        mapping_group_id, group_type, effective_from, effective_to, business_reason, approver)
SELECT mv.id, d.sys, 'CUSTOMER_STATUS', d.src, d.tgt, d.grp, d.gtype,
       DATE '2024-01-01', DATE '2025-01-01', d.reason, d.approver
FROM mapping_version mv
CROSS JOIN (VALUES
    ('CRM','A',  'ACTIVE',   'g1','MERGE',     '线下门店与线上活跃客户统一归入活跃口径', '张敏'),
    ('POS','1',  'ACTIVE',   'g1','MERGE',     '线下门店与线上活跃客户统一归入活跃口径', '张敏'),
    ('CRM','F',  'INACTIVE', 'g2','ONE_TO_ONE','冻结客户按非活跃统计',                   '张敏'),
    ('CRM','C',  'CHURNED',  'g3','ONE_TO_ONE','注销即流失',                             '张敏'),
    ('POS','0',  'INACTIVE', 'g5','ONE_TO_ONE','停用门店客户按非活跃统计',               '张敏'),
    ('POS','2',  NULL,       'g6','UNMAPPED',   'v2024 目标口径尚未发布沉睡码，暂不对应','张敏'),
    ('ERP','E10','ACTIVE',   'g7','ONE_TO_ONE','在用客户视为活跃',                       '李华'),
    ('ERP','E20','INACTIVE', 'g8','ONE_TO_ONE','停用客户视为非活跃',                     '李华'),
    ('ERP','E30',NULL,       'g9','UNMAPPED',   '挂起态缺少口径定义，待 v2025 评审',     '李华')
) AS d(sys, src, tgt, grp, gtype, reason, approver)
WHERE mv.version_label='m2024';

-- ---------------------------------------------------------------------------
-- 映射版本 m2025（对应目标 v2025）：新增 DORMANT，承接两个历史 UNMAPPED；
-- CRM 7 月换码，用映射行自身的生效区间衔接；合并组 g1 源码集合保持不变
-- ---------------------------------------------------------------------------
INSERT INTO mapping_version(target_version_id, version_label, effective_from, effective_to, status, published_at)
SELECT id, 'm2025', DATE '2025-01-01', DATE '2026-01-01', 'published', now()
FROM target_version WHERE version_label='v2025';

INSERT INTO mapping_row(mapping_version_id, system_code, code_domain, source_code, target_code,
                        mapping_group_id, group_type, effective_from, effective_to, business_reason, approver)
SELECT mv.id, d.sys, 'CUSTOMER_STATUS', d.src, d.tgt, d.grp, d.gtype, d.ef, d.et, d.reason, d.approver
FROM mapping_version mv
CROSS JOIN (VALUES
    -- 合并组 g1：成员与 m2024 完全一致，CRM:A 随源码在 7 月终止
    ('CRM','A',  'ACTIVE',   'g1','MERGE',     DATE '2025-01-01', DATE '2025-07-01', '合并口径延续 m2024', '张敏'),
    ('POS','1',  'ACTIVE',   'g1','MERGE',     DATE '2025-01-01', NULL,               '合并口径延续 m2024', '张敏'),
    ('CRM','F',  'INACTIVE', 'g2','ONE_TO_ONE',DATE '2025-01-01', DATE '2025-07-01', '冻结客户按非活跃统计','张敏'),
    ('CRM','C',  'CHURNED',  'g3','ONE_TO_ONE',DATE '2025-01-01', DATE '2025-07-01', '注销即流失',         '张敏'),
    ('POS','0',  'INACTIVE', 'g5','ONE_TO_ONE',DATE '2025-01-01', NULL,               '停用门店客户按非活跃统计','张敏'),
    -- 沉睡口径发布，原 g6/g9 无对应项转正（源码集合不变，仅目标码变化，允许）
    ('POS','2',  'DORMANT',  'g6','ONE_TO_ONE',DATE '2025-01-01', NULL,               'v2025 新增沉睡口径，承接历史无对应项','王芳'),
    ('ERP','E30','DORMANT',  'g9','ONE_TO_ONE',DATE '2025-01-01', DATE '2026-01-01', '挂起客户评审后归入沉睡','李华'),
    ('ERP','E10','ACTIVE',   'g7','ONE_TO_ONE',DATE '2025-01-01', NULL,               '在用客户视为活跃',   '李华'),
    ('ERP','E20','INACTIVE', 'g8','ONE_TO_ONE',DATE '2025-01-01', NULL,               '停用客户视为非活跃', '李华'),
    -- CRM 新码（新分组，不影响历史分组成员）
    ('CRM','ACT','ACTIVE',   'g10','ONE_TO_ONE',DATE '2025-07-01', NULL,              '新码体系：活跃',     '张敏'),
    ('CRM','FRO','INACTIVE', 'g11','ONE_TO_ONE',DATE '2025-07-01', NULL,              '新码体系：冻结',     '张敏'),
    ('CRM','CLS','CHURNED',  'g12','ONE_TO_ONE',DATE '2025-07-01', NULL,              '新码体系：注销',     '张敏')
) AS d(sys, src, tgt, grp, gtype, ef, et, reason, approver)
WHERE mv.version_label='m2025';

-- ---------------------------------------------------------------------------
-- 映射版本 m2026（对应目标 v2026）：POS 换字母码、ERP E30->E40，新增 PENDING
-- 封存前处于已发布状态，供正向/反向/覆盖率查询
-- ---------------------------------------------------------------------------
INSERT INTO mapping_version(target_version_id, version_label, effective_from, effective_to, status, published_at)
SELECT id, 'm2026', DATE '2026-01-01', NULL, 'published', now()
FROM target_version WHERE version_label='v2026';

INSERT INTO mapping_row(mapping_version_id, system_code, code_domain, source_code, target_code,
                        mapping_group_id, group_type, effective_from, effective_to, business_reason, approver)
SELECT mv.id, d.sys, 'CUSTOMER_STATUS', d.src, d.tgt, d.grp, d.gtype,
       DATE '2026-01-01', NULL, d.reason, d.approver
FROM mapping_version mv
CROSS JOIN (VALUES
    ('CRM','ACT',     'ACTIVE',   'g10','ONE_TO_ONE','新码体系延续',                '张敏'),
    ('CRM','FRO',     'INACTIVE', 'g11','ONE_TO_ONE','新码体系延续',                '张敏'),
    ('CRM','CLS',     'CHURNED',  'g12','ONE_TO_ONE','新码体系延续',                '张敏'),
    ('POS','ACTIVE',  'ACTIVE',   'g13','ONE_TO_ONE','POS 换码后承接原 1 码',       '王芳'),
    ('POS','INACTIVE','INACTIVE', 'g14','ONE_TO_ONE','POS 换码后承接原 0 码',       '王芳'),
    ('POS','2',       'DORMANT',  'g6', 'ONE_TO_ONE','沉睡口径延续',                '王芳'),
    ('ERP','E10',     'ACTIVE',   'g7', 'ONE_TO_ONE','在用客户视为活跃',            '李华'),
    ('ERP','E20',     'INACTIVE', 'g8', 'ONE_TO_ONE','停用客户视为非活跃',          '李华'),
    ('ERP','E40',     'PENDING',  'g15','ONE_TO_ONE','观察态新码，先按待定口径统计','李华')
) AS d(sys, src, tgt, grp, gtype, reason, approver)
WHERE mv.version_label='m2026';
