-- 演示仓库 schema：organization / member / assignment 三个参与装载的关系表，
-- 加上一个不参与装载、只从外部引用 member 代理键的事实表 fact_usage。
--
-- 约定：
--   * 每张参与表恰有一列 bigint 代理主键 id，由独立序列 nextval 分配；
--   * 自然键为有序非空 text 列（member 用复合自然键，分量里故意含 '|' 与
--     引号，证明不会用分隔符拼假键）；
--   * 所有外键与业务唯一约束都 DEFERRABLE，id 主键不延迟；
--   * 外键动作一律 NO ACTION（含可延迟），不使用任何级联/置空。
-- 标识符里特意保留中文、空格、点与引号，验证产品的安全引用能力。

DROP SCHEMA IF EXISTS handoff_demo CASCADE;
CREATE SCHEMA handoff_demo;

-- 序列必须独立存在并被 id 列默认值引用。
CREATE SEQUENCE handoff_demo.organization_id_seq;
CREATE SEQUENCE handoff_demo.member_id_seq;
CREATE SEQUENCE handoff_demo.assignment_id_seq;

-- member 先建（自引用外键可直接建）；指向 organization 的循环外键在下方
-- organization 建好后用 ALTER TABLE 补，避免前向引用不存在的表。
CREATE TABLE handoff_demo.member (
    id            bigint PRIMARY KEY DEFAULT nextval('handoff_demo.member_id_seq'),
    tenant_id     text NOT NULL,
    member_code   text NOT NULL,
    display_name  text NOT NULL,
    alias         text NOT NULL,
    score         bigint NOT NULL,
    vip           boolean,
    manager_id    bigint,
    org_id        bigint,
    -- 复合自然键；列名带点与空格，要求正确引用。
    CONSTRAINT member_nk UNIQUE (tenant_id, member_code) DEFERRABLE INITIALLY DEFERRED,
    -- 可交换的业务唯一别名。
    CONSTRAINT member_alias_uk UNIQUE (alias) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT member_manager_fk
        FOREIGN KEY (manager_id) REFERENCES handoff_demo.member(id)
        ON DELETE NO ACTION ON UPDATE NO ACTION
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE handoff_demo.organization (
    id              bigint PRIMARY KEY DEFAULT nextval('handoff_demo.organization_id_seq'),
    org_code        text NOT NULL,
    org_tenant      text NOT NULL,
    legal_name      text NOT NULL,
    tier            bigint,
    active          boolean NOT NULL,
    contact_member  bigint NOT NULL,
    CONSTRAINT organization_nk UNIQUE (org_code, org_tenant) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT organization_legal_name_uk UNIQUE (legal_name) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT organization_contact_fk
        FOREIGN KEY (contact_member)
        REFERENCES handoff_demo.member(id)
        ON DELETE NO ACTION ON UPDATE NO ACTION
        DEFERRABLE INITIALLY DEFERRED
);

-- 循环外键后补：member -> organization。
ALTER TABLE handoff_demo.member
    ADD CONSTRAINT member_org_fk
        FOREIGN KEY (org_id) REFERENCES handoff_demo.organization(id)
        ON DELETE NO ACTION ON UPDATE NO ACTION
        DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE handoff_demo.assignment (
    id            bigint PRIMARY KEY DEFAULT nextval('handoff_demo.assignment_id_seq'),
    project_key   text NOT NULL,
    role_code     text NOT NULL,
    note          text,
    member_id     bigint NOT NULL,
    CONSTRAINT assignment_nk UNIQUE (project_key, role_code) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT assignment_member_fk
        FOREIGN KEY (member_id) REFERENCES handoff_demo.member(id)
        ON DELETE NO ACTION ON UPDATE NO ACTION
        DEFERRABLE INITIALLY DEFERRED
);

-- 不参与装载的外部事实表：它持有的 member_id 是维度身份的硬引用。
CREATE TABLE handoff_demo.fact_usage (
    fact_id    bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    event_name text NOT NULL,
    member_id  bigint NOT NULL,
    CONSTRAINT fact_usage_member_fk
        FOREIGN KEY (member_id) REFERENCES handoff_demo.member(id)
        ON DELETE NO ACTION ON UPDATE NO ACTION
        -- 事实表外键也只允许 NO ACTION；可延迟与否不影响“禁止删除被引用身份”。
        DEFERRABLE INITIALLY DEFERRED
);

-- 一个物理名含空格、引号、点和中文的参与表，验证标识符安全引用与数据参数化。
-- 标准 SQL 中双引号标识符内的引号用双写表示，实际表名含一个字面双引号：
-- weird 名.表 "x"
CREATE SEQUENCE handoff_demo."weird 名 表_id_seq";
CREATE TABLE handoff_demo."weird 名.表 ""x""" (
    id          bigint PRIMARY KEY DEFAULT nextval('handoff_demo."weird 名 表_id_seq"'),
    kc          text NOT NULL,
    label       text,
    CONSTRAINT "weird 名.表 ""x""_nk" UNIQUE (kc) DEFERRABLE INITIALLY DEFERRED
);
