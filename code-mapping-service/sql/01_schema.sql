-- ============================================================================
-- 多业务系统代码表映射服务 - 数据库结构
-- 区间约定：半开区间 [effective_from, effective_to)，effective_to 为 NULL 表示至今
-- ============================================================================

CREATE TABLE IF NOT EXISTS source_system (
    code        text PRIMARY KEY,          -- CRM / ERP / POS
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- 每个源系统自己维护的代码表（SCD2，按生效区间）
CREATE TABLE IF NOT EXISTS source_code (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    system_code     text NOT NULL REFERENCES source_system(code),
    code_domain     text NOT NULL,         -- 代码域，如 CUSTOMER_STATUS / ORDER_STATUS
    source_code     text NOT NULL,
    source_name     text NOT NULL,
    effective_from  date NOT NULL,
    effective_to    date,                  -- NULL = 至今
    CONSTRAINT source_code_half_open CHECK (effective_to IS NULL OR effective_to > effective_from)
);
-- 同一系统同一域同一代码在时间轴上不允许重叠
CREATE UNIQUE INDEX IF NOT EXISTS ux_source_code_nodup
    ON source_code (system_code, code_domain, source_code, effective_from);

-- 目标分析口径（如统一客户状态）
CREATE TABLE IF NOT EXISTS target_dimension (
    code        text PRIMARY KEY,          -- CUSTOMER_STATUS
    name        text NOT NULL
);

-- 目标口径的发布版本
CREATE TABLE IF NOT EXISTS target_version (
    id              smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dimension_code  text NOT NULL REFERENCES target_dimension(code),
    version_label   text NOT NULL UNIQUE,  -- v2024 / v2025 / v2026
    effective_from  date NOT NULL,
    effective_to    date,
    CONSTRAINT target_ver_half_open CHECK (effective_to IS NULL OR effective_to > effective_from)
);

-- 目标版本内的代码清单
CREATE TABLE IF NOT EXISTS target_code (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    target_version_id   smallint NOT NULL REFERENCES target_version(id),
    target_code         text NOT NULL,
    target_name         text NOT NULL,
    UNIQUE (target_version_id, target_code)
);

-- 映射版本：目标口径每次重新发布映射时生成一行
CREATE TABLE IF NOT EXISTS mapping_version (
    id                  smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    target_version_id   smallint NOT NULL REFERENCES target_version(id),
    version_label       text NOT NULL UNIQUE,      -- 映射自身的版本号
    effective_from      date NOT NULL,
    effective_to        date,
    status              text NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','published')),
    created_by          text NOT NULL DEFAULT current_user,
    created_at          timestamptz NOT NULL DEFAULT now(),
    published_at        timestamptz,
    CONSTRAINT map_ver_half_open CHECK (effective_to IS NULL OR effective_to > effective_from)
);

-- 映射明细。group_type:
--   ONE_TO_ONE 一对一；MERGE 多源码合并（同 group_id 多行 -> 同一目标码）；
--   SPLIT 拆分；UNMAPPED 暂时无对应项（target_code 为空，必须填业务原因）
CREATE TABLE IF NOT EXISTS mapping_row (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mapping_version_id  smallint NOT NULL REFERENCES mapping_version(id),
    system_code         text NOT NULL REFERENCES source_system(code),
    code_domain         text NOT NULL,
    source_code         text NOT NULL,
    target_code         text,               -- NULL = 无对应项
    mapping_group_id    text NOT NULL,      -- 合并/拆分的血缘分组，跨版本保持稳定
    group_type          text NOT NULL CHECK (group_type IN
                            ('ONE_TO_ONE','MERGE','SPLIT','UNMAPPED')),
    effective_from      date NOT NULL,
    effective_to        date,
    business_reason     text NOT NULL,
    approver            text NOT NULL,
    CONSTRAINT map_row_half_open CHECK (effective_to IS NULL OR effective_to > effective_from),
    CONSTRAINT unmapped_requires_reason CHECK (
        (target_code IS NOT NULL) OR
        (target_code IS NULL AND group_type = 'UNMAPPED' AND length(btrim(business_reason)) > 0)
    )
);
CREATE INDEX IF NOT EXISTS ix_mapping_row_lookup
    ON mapping_row (system_code, code_domain, source_code, effective_from);
CREATE INDEX IF NOT EXISTS ix_mapping_row_version
    ON mapping_row (mapping_version_id);
CREATE INDEX IF NOT EXISTS ix_mapping_row_target
    ON mapping_row (mapping_version_id, target_code);

-- 已封存报表：记录报表使用的映射版本；封存后该版本映射行不可改写
CREATE TABLE IF NOT EXISTS report_snapshot (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_code         text NOT NULL,
    report_period       date NOT NULL,
    mapping_version_id  smallint NOT NULL REFERENCES mapping_version(id),
    sealed_by           text NOT NULL DEFAULT current_user,
    sealed_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (report_code, report_period)
);

-- ============================================================================
-- 函数 1：发布前校验，返回全部冲突（不放行任何一条错误发布）
-- ============================================================================
CREATE OR REPLACE FUNCTION validate_mapping_publish(p_mapping_version_id int)
RETURNS TABLE(violation_code text, message text)
LANGUAGE plpgsql AS $$
DECLARE
    v_target_version int;
    v_eff_from       date;
    v_prev_id        int;
BEGIN
    SELECT target_version_id, effective_from
      INTO v_target_version, v_eff_from
      FROM mapping_version WHERE id = p_mapping_version_id;

    -- 1) 同一源系统同一时段存在多个有效映射（区间重叠）
    RETURN QUERY
    SELECT 'OVERLAPPING_MAPPING'::text,
           format('系统 %s 域 %s 源码 %s 在区间 [%s, %s) 存在 %s 条重叠映射: group=%s',
                  a.system_code, a.code_domain, a.source_code,
                  greatest(a.effective_from, b.effective_from),
                  least(coalesce(a.effective_to, DATE '9999-12-31'),
                        coalesce(b.effective_to, DATE '9999-12-31')),
                  2, a.mapping_group_id)
      FROM mapping_row a
      JOIN mapping_row b
        ON a.mapping_version_id = b.mapping_version_id
       AND a.system_code = b.system_code
       AND a.code_domain = b.code_domain
       AND a.source_code = b.source_code
       AND a.id < b.id
       AND a.effective_from < coalesce(b.effective_to, DATE '9999-12-31')
       AND b.effective_from < coalesce(a.effective_to, DATE '9999-12-31')
     WHERE a.mapping_version_id = p_mapping_version_id;

    -- 2) 目标码无效：引用了目标版本中不存在的代码
    RETURN QUERY
    SELECT 'INVALID_TARGET_CODE'::text,
           format('系统 %s 源码 %s 引用的目标码 %s 在目标版本中不存在',
                  mr.system_code, mr.source_code, mr.target_code)
      FROM mapping_row mr
     WHERE mr.mapping_version_id = p_mapping_version_id
       AND mr.target_code IS NOT NULL
       AND NOT EXISTS (
             SELECT 1 FROM target_code tc
              WHERE tc.target_version_id = v_target_version
                AND tc.target_code = mr.target_code);

    -- 3) 合并关系拆分不完整：与上一已发布版本相比，同 group 的源码集合必须一致
    SELECT mv.id INTO v_prev_id
      FROM mapping_version mv
     WHERE mv.target_version_id = v_target_version
       AND mv.status = 'published'
       AND mv.effective_from <= v_eff_from
       AND mv.id <> p_mapping_version_id
     ORDER BY mv.effective_from DESC, mv.id DESC
     LIMIT 1;

    IF v_prev_id IS NOT NULL THEN
        RETURN QUERY
        WITH prev AS (
            SELECT mapping_group_id, array_agg(DISTINCT system_code || ':' || source_code
                                               ORDER BY system_code || ':' || source_code) AS s
              FROM mapping_row WHERE mapping_version_id = v_prev_id
             GROUP BY mapping_group_id
        ), cur AS (
            SELECT mapping_group_id, array_agg(DISTINCT system_code || ':' || source_code
                                               ORDER BY system_code || ':' || source_code) AS s
              FROM mapping_row WHERE mapping_version_id = p_mapping_version_id
             GROUP BY mapping_group_id
        )
        SELECT 'INCOMPLETE_MERGE_SPLIT'::text,
               format('分组 %s 源码集合发生变化: 上一版本=%s, 当前版本=%s（合并/拆分不完整）',
                      p.mapping_group_id, p.s, c.s)
          FROM prev p JOIN cur c ON c.mapping_group_id = p.mapping_group_id
         WHERE c.s <> p.s;
    END IF;

    -- 4) 覆盖缺口：事件时点仍有效的源码，没有任何映射行覆盖（无对应项也必须显式登记）
    RETURN QUERY
    SELECT 'MISSING_MAPPING'::text,
           format('系统 %s 域 %s 源码 %s 在 %s 起生效但无任何映射行覆盖',
                  sc.system_code, sc.code_domain, sc.source_code, v_eff_from)
      FROM source_code sc
     WHERE sc.effective_from <= v_eff_from
       AND v_eff_from < coalesce(sc.effective_to, DATE '9999-12-31')
       AND NOT EXISTS (
             SELECT 1 FROM mapping_row mr
              WHERE mr.mapping_version_id = p_mapping_version_id
                AND mr.system_code = sc.system_code
                AND mr.code_domain = sc.code_domain
                AND mr.source_code = sc.source_code
                AND mr.effective_from < coalesce(mr.effective_to, DATE '9999-12-31')
                AND v_eff_from < coalesce(mr.effective_to, DATE '9999-12-31'));
END;
$$;

-- ============================================================================
-- 函数 2：封存版本不可改写（行级触发器）
-- ============================================================================
CREATE OR REPLACE FUNCTION enforce_sealed_version()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_id int;
    v_label text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        v_id := OLD.mapping_version_id;
    ELSE
        v_id := NEW.mapping_version_id;
    END IF;

    IF EXISTS (SELECT 1 FROM report_snapshot rs
                WHERE rs.mapping_version_id = v_id) THEN
        SELECT version_label INTO v_label FROM mapping_version WHERE id = v_id;
        RAISE EXCEPTION '映射版本 % (id=%) 已被封存报表引用，映射行不可改写',
                        v_label, v_id
              USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END;
$$;

DROP TRIGGER IF EXISTS trg_sealed_mapping_immutable ON mapping_row;
CREATE TRIGGER trg_sealed_mapping_immutable
    BEFORE UPDATE OR DELETE ON mapping_row
    FOR EACH ROW EXECUTE FUNCTION enforce_sealed_version();
