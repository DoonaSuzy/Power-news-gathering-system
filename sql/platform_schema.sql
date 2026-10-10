-- =====================================================================
-- Power News Aggregator —— 数据中台库 powernews_platform（MySQL 8.0+ / utf8mb4）
-- ---------------------------------------------------------------------
-- 设计依据：docs/字段分析报告.md（基于 826 篇真实文章的实测结论）
--
-- 与采集库 sql/schema.sql 的分工
--   powernews          采集库：写多读少，保留任务/租约/台账等运行态
--   powernews_platform 中台库：读多写少，面向查询/分析/对外 API 的「数据集市」
--   本库是**自包含**的：维度（省份/来源）+ 事实（文章）+ 平台自有表 + 视图。
--
-- 相比采集库的 6 项优化（均由字段实测驱动，详见报告 §七）
--   1. province_code 允许 NULL：行业源（北极星/中国电力新闻网/国际能源网）
--      共 120 篇无行政区划，同时冗余 province_name 保证展示与可读性。
--   2. 日期拆成 published_date(DATE) + year_month(CHAR7)：
--      DATE 列可直接建索引，按月/按日聚合不再走函数、不失效索引。
--   3. body_text 用 MEDIUMTEXT：实测正文 0 ~ 40842 字，TEXT(64KB 字符)
--      在极端长文下有截断风险。
--   4. body_length / title_length 落列：质量看板免去每次 LENGTH() 全表扫描。
--   5. crawl_method 落列：记录该篇是 http 直连还是 CDP 真实浏览器攻克
--      （瑞数 412 站），对溯源与「哪些源需要浏览器」有直接价值。
--   6. quality_flags 用 JSON：替代采集库的 VARCHAR(512) 字符串，
--      可直接用 JSON_CONTAINS 查询，无需 LIKE。
--
-- 幂等：全部 IF NOT EXISTS / CREATE OR REPLACE VIEW，可重复执行。
-- =====================================================================

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------
-- 1. provinces —— 省级行政区维度（34 条，含港澳台）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS provinces (
    province_code  CHAR(6)      NOT NULL COMMENT '行政区划代码，如 440000',
    province_name  VARCHAR(32)  NOT NULL COMMENT '全称，如 广东省',
    short_name     VARCHAR(16)  NOT NULL DEFAULT '' COMMENT '简称，如 广东',
    region         VARCHAR(16)  NOT NULL DEFAULT '' COMMENT '大区：华北/东北/华东/华中/华南/西南/西北/港澳台',
    is_special     TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '1=港澳台',
    sort_order     SMALLINT     NOT NULL DEFAULT 0 COMMENT '展示排序（按国标顺序）',
    created_at     TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (province_code),
    KEY idx_provinces_region (region),
    KEY idx_provinces_name (province_name)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci
  COMMENT = '省级行政区维度';

-- ---------------------------------------------------------------------
-- 2. sources —— 来源维度（45 条）
--    crawl_method  记录攻克方式：http=直连 / cdp-browser=真实浏览器（过瑞数）
--    blocker       记录卡点类型：none/ruishu(412)/http202/spa/waf403/unreachable/parse
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    source_id       VARCHAR(64)   NOT NULL COMMENT '来源业务 id，如 sgcc_bj',
    source_name     VARCHAR(128)  NOT NULL COMMENT '来源名称',
    province_code   CHAR(6)       NULL COMMENT '所属省份；行业源为 NULL',
    category        VARCHAR(24)   NOT NULL DEFAULT 'sgcc'
                    COMMENT 'sgcc/csg/independent/hmt/industry/gov',
    category_label  VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '类别中文名',
    base_url        VARCHAR(512)  NOT NULL DEFAULT '',
    list_url        VARCHAR(512)  NOT NULL DEFAULT '' COMMENT '实际生效的栏目页 URL',
    crawl_method    VARCHAR(16)   NOT NULL DEFAULT 'http' COMMENT 'http / cdp-browser',
    status          VARCHAR(16)   NOT NULL DEFAULT 'active' COMMENT 'ok / blocked / pending',
    blocker         VARCHAR(32)   NOT NULL DEFAULT 'none' COMMENT '卡点类型',
    article_count   INT UNSIGNED  NOT NULL DEFAULT 0 COMMENT '已入库文章数',
    first_published DATE          NULL COMMENT '最早发布日期',
    last_published  DATE          NULL COMMENT '最晚发布日期',
    note            VARCHAR(512)  NOT NULL DEFAULT '',
    created_at      TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (source_id),
    KEY idx_sources_province (province_code),
    KEY idx_sources_category (category),
    KEY idx_sources_status (status)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci
  COMMENT = '来源维度（含攻克方式与卡点）';

-- ---------------------------------------------------------------------
-- 3. articles —— 文章事实表（核心）
--    唯一身份 = url_hash = SHA-256(normalize_url(url)) 前 40 位，
--    跨来源全局去重（同一篇被多家转载只保留一条，来源记为首次发现者）。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS articles (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    url_hash        CHAR(40)        NOT NULL COMMENT 'URL 指纹（SHA-256 前 40 位）',
    source_id       VARCHAR(64)     NOT NULL COMMENT '来源 id → sources.source_id',
    province_code   CHAR(6)         NULL COMMENT '省份代码；行业源为 NULL',
    province_name   VARCHAR(32)     NOT NULL DEFAULT '' COMMENT '省份全称（冗余，便于直查）',
    category        VARCHAR(24)     NOT NULL DEFAULT '' COMMENT '来源类别',
    title           VARCHAR(512)    NOT NULL DEFAULT '' COMMENT '标题',
    body_text       MEDIUMTEXT      NULL COMMENT '正文纯文本（段落以换行分隔）',
    body_length     INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '正文长度（字符）',
    title_length    SMALLINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '标题长度',
    published_date  DATE            NULL COMMENT '发布日期（北京时区，日粒度）',
    published_at_cst DATETIME       NULL COMMENT '发布时间（北京时间）',
    published_at_utc DATETIME       NULL COMMENT '发布时间（UTC）',
    time_precision  VARCHAR(16)     NOT NULL DEFAULT 'day'
                    COMMENT 'second/minute/hour/day/unknown',
    published_ym    CHAR(7)         NOT NULL DEFAULT '' COMMENT '年-月，如 2026-10（月度聚合用；避免用保留字 year_month）',
    url             VARCHAR(768)    NOT NULL COMMENT '原文链接',
    source_host     VARCHAR(128)    NOT NULL DEFAULT '' COMMENT '来源域名',
    crawl_method    VARCHAR(16)     NOT NULL DEFAULT 'http' COMMENT '采集方式 http / cdp-browser',
    quality_flags   JSON            NULL COMMENT '质量标记数组，如 ["empty_body"]',
    crawled_at      DATETIME        NULL COMMENT '采集时间',
    created_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_articles_url_hash (url_hash),
    KEY idx_articles_province_date (province_code, published_date),
    KEY idx_articles_source_date (source_id, published_date),
    KEY idx_articles_ym (published_ym),
    KEY idx_articles_date (published_date),
    KEY idx_articles_category (category),
    KEY idx_articles_method (crawl_method),
    KEY idx_articles_title (title(64))
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci
  COMMENT = '文章事实表（标题/正文/发布时间/来源/链接 + 分析派生列）';

-- ---------------------------------------------------------------------
-- 4. etl_runs —— 写入批次留痕（可追溯「这批数据什么时候、怎么灌进来的」）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS etl_runs (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    batch_key     VARCHAR(64)     NOT NULL COMMENT '批次键，如 20261010-211500',
    source_file   VARCHAR(255)    NOT NULL DEFAULT '' COMMENT '数据来源文件',
    rows_read     INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '读取行数',
    rows_inserted INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '新增行数',
    rows_updated  INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '更新行数',
    rows_skipped  INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '跳过（重复/无效）行数',
    status        VARCHAR(16)     NOT NULL DEFAULT 'running' COMMENT 'running/succeeded/failed',
    message       VARCHAR(512)    NOT NULL DEFAULT '',
    started_at    DATETIME        NOT NULL,
    finished_at   DATETIME        NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_etl_batch (batch_key),
    KEY idx_etl_started (started_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci
  COMMENT = '数据写入批次留痕';

-- ---------------------------------------------------------------------
-- 5. 平台自有表（与 newsplatform/models/orm.py 的 PlatformBase 一致）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_keys (
    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    name         VARCHAR(64)     NOT NULL COMMENT '用途名称',
    key_hash     CHAR(64)        NOT NULL COMMENT 'sha256(明文 Key) 十六进制',
    scopes       VARCHAR(255)    NOT NULL DEFAULT 'articles:read',
    enabled      TINYINT(1)      NOT NULL DEFAULT 1,
    expires_at   DATETIME        NULL,
    last_used_at DATETIME        NULL,
    created_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_api_keys_hash (key_hash),
    KEY idx_api_keys_enabled (enabled)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci
  COMMENT = 'API Key（只存哈希）';

CREATE TABLE IF NOT EXISTS audit_logs (
    id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    request_id   CHAR(32)        NOT NULL DEFAULT '',
    api_key_id   BIGINT UNSIGNED NULL,
    api_key_name VARCHAR(64)     NOT NULL DEFAULT '',
    method       VARCHAR(10)     NOT NULL,
    path         VARCHAR(255)    NOT NULL,
    status_code  INT             NOT NULL DEFAULT 200,
    client_ip    VARCHAR(64)     NOT NULL DEFAULT '',
    user_agent   VARCHAR(255)    NOT NULL DEFAULT '',
    duration_ms  INT             NOT NULL DEFAULT 0,
    detail       VARCHAR(512)    NOT NULL DEFAULT '',
    created_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_audit_created (created_at),
    KEY idx_audit_key (api_key_id),
    KEY idx_audit_path (path)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci
  COMMENT = '写操作审计日志';

CREATE TABLE IF NOT EXISTS stats_snapshots (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    snapshot_date DATE            NOT NULL,
    scope         VARCHAR(32)     NOT NULL COMMENT 'overview/province/source/quality',
    scope_key     VARCHAR(64)     NOT NULL DEFAULT '',
    payload       JSON            NOT NULL,
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_snapshot (snapshot_date, scope, scope_key),
    KEY idx_snapshot_scope (scope, snapshot_date)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci
  COMMENT = '统计快照（按日物化聚合结果）';

-- =====================================================================
-- 视图
-- =====================================================================

-- 省份覆盖视图：一省一行，直接支撑「覆盖 27 国网省 + 南方五省 + 蒙东」验收
CREATE OR REPLACE VIEW v_province_coverage AS
SELECT p.province_code,
       p.province_name,
       p.region,
       p.is_special,
       s.source_total,
       s.source_ok,
       s.category_list,
       COALESCE(a.article_count, 0)                    AS article_count,
       COALESCE(a.empty_body, 0)                       AS empty_body,
       COALESCE(a.avg_body_length, 0)                  AS avg_body_length,
       a.first_published,
       a.last_published
FROM provinces p
         LEFT JOIN (SELECT province_code,
                           COUNT(*)                                         AS source_total,
                           SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END)    AS source_ok,
                           GROUP_CONCAT(DISTINCT category ORDER BY category) AS category_list
                    FROM sources
                    WHERE province_code IS NOT NULL
                    GROUP BY province_code) s ON s.province_code = p.province_code
         LEFT JOIN (SELECT province_code,
                           COUNT(*)                                  AS article_count,
                           SUM(CASE WHEN body_length = 0 THEN 1 ELSE 0 END) AS empty_body,
                           ROUND(AVG(NULLIF(body_length, 0)), 0)     AS avg_body_length,
                           MIN(published_date)                       AS first_published,
                           MAX(published_date)                       AS last_published
                    FROM articles
                    WHERE province_code IS NOT NULL
                    GROUP BY province_code) a ON a.province_code = p.province_code;

-- 来源健康视图：含攻克方式与卡点，回答「这个源是怎么拿下的、现在什么状态」
CREATE OR REPLACE VIEW v_source_health AS
SELECT source_id,
       source_name,
       province_code,
       category,
       category_label,
       crawl_method,
       status,
       blocker,
       article_count,
       first_published,
       last_published,
       list_url,
       SUBSTRING_INDEX(SUBSTRING_INDEX(base_url, '//', -1), '/', 1) AS source_host
FROM sources;

-- 日度统计：按发布日期统计入库量（趋势看板）
CREATE OR REPLACE VIEW v_daily_stats AS
SELECT published_date,
       COUNT(*)                                          AS article_count,
       COUNT(DISTINCT province_code)                     AS province_count,
       COUNT(DISTINCT source_id)                         AS source_count,
       ROUND(AVG(NULLIF(body_length, 0)), 0)             AS avg_body_length
FROM articles
WHERE published_date IS NOT NULL
GROUP BY published_date;

-- 月度趋势
CREATE OR REPLACE VIEW v_monthly_trend AS
SELECT published_ym,
       COUNT(*)                      AS article_count,
       COUNT(DISTINCT province_code) AS province_count,
       COUNT(DISTINCT source_id)     AS source_count
FROM articles
WHERE published_ym <> ''
GROUP BY published_ym
ORDER BY published_ym;

-- 类别分布
CREATE OR REPLACE VIEW v_category_stats AS
SELECT category,
       COUNT(*)                      AS article_count,
       COUNT(DISTINCT province_code) AS province_count,
       COUNT(DISTINCT source_id)     AS source_count,
       ROUND(AVG(NULLIF(body_length, 0)), 0) AS avg_body_length
FROM articles
GROUP BY category;

-- 数据质量视图：一屏看清字段完整率
CREATE OR REPLACE VIEW v_data_quality AS
SELECT COUNT(*)                                                     AS total_articles,
       SUM(CASE WHEN title = '' THEN 1 ELSE 0 END)                   AS missing_title,
       SUM(CASE WHEN body_length = 0 THEN 1 ELSE 0 END)              AS empty_body,
       SUM(CASE WHEN published_date IS NULL THEN 1 ELSE 0 END)       AS missing_date,
       SUM(CASE WHEN url = '' THEN 1 ELSE 0 END)                     AS missing_url,
       SUM(CASE WHEN province_code IS NULL THEN 1 ELSE 0 END)        AS no_province_code,
       ROUND(100.0 * SUM(CASE WHEN body_length > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS body_rate,
       ROUND(100.0 * SUM(CASE WHEN published_date IS NOT NULL THEN 1 ELSE 0 END) / COUNT(*), 1) AS date_rate
FROM articles;
