-- =====================================================================
-- Power News Aggregator —— 采集层数据库结构（MySQL 8.0+ / utf8mb4）
-- ---------------------------------------------------------------------
-- 由 `powernews init-db` 执行（newsagg/db.py::Database.ensure_schema）。
-- 语句按 ";" 拆分后逐条执行，因此：
--   * 不使用 DELIMITER / 存储过程 / 触发器；
--   * 每张表都写 IF NOT EXISTS，重复执行安全（幂等）。
--
-- 表清单（8 张）
--   1. provinces      34 个省级行政区字典
--   2. sources        39 条电力新闻来源（一源一适配器）
--   3. source_slots   来源采集配额槽位
--   4. articles       文章正文 + 五类字段（核心表）
--   5. urls           URL 指纹全局登记表（跨来源 URL 去重）
--   6. article_tasks  列表页候选处理台账（发现 → 抓取 → 落库全链路留痕）
--   7. crawl_jobs     单来源单任务（窗口、终态、产出计数）
--   8. crawl_runs     一次协调器批次（聚合 crawl_jobs）
--
-- 时间约定
--   * 所有 *_cst 字段为**北京时间 naive**，与 cleaning.parse_published 输出一致；
--   * 所有 *_utc 字段为 **UTC naive**，由 UTC_TIMESTAMP() 产生；
--   * 因此聚合查询只用 UTC_TIMESTAMP() / *_utc 配对，不依赖服务器时区设置
--     （本机 MySQL 实测为 default-time-zone=+00:00，用 NOW() 去比 *_cst
--      会整体差 8 小时，见 newsagg/stats.py::overview 的注释）。
-- =====================================================================

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------
-- 1. provinces —— 省级行政区字典
--    code 取国家标准行政区划代码（GB/T 2260）前 6 位。
--    港澳台同样收录（is_special=1），采集层共用同一套字段与流程。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS provinces (
    code        CHAR(6)      NOT NULL COMMENT '行政区划代码，如 440000',
    name        VARCHAR(32)  NOT NULL COMMENT '全称，如 广东省',
    short_name  VARCHAR(16)  NOT NULL DEFAULT '' COMMENT '简称，如 广东',
    region      VARCHAR(16)  NOT NULL DEFAULT '' COMMENT '所属大区：华北/东北/华东/华中/华南/西南/西北/港澳台',
    is_special  TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '1=港澳台等特殊行政单位',
    created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (code),
    KEY idx_provinces_region (region)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '省级行政区字典（34 条）';

-- ---------------------------------------------------------------------
-- 2. sources —— 来源清单
--    一条记录 = 一个适配器（newsagg/adapters/<pkg>/<adapter_name>.py）。
--    **引擎优先使用库内 list_url**，因此改栏目页只需 UPDATE 本表，
--    无需改代码（见 docs/开发说明.md「栏目页改址」）。
--    status 与 enabled 的分工：
--      enabled=0  → 人工停用，协调器不会建批；
--      status     → 运行期状态（active / blocked / degraded），由采集侧回写。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    id                          INT UNSIGNED  NOT NULL AUTO_INCREMENT,
    adapter_name                VARCHAR(64)   NOT NULL COMMENT '适配器名，如 sgcc_gd / csg_gd / bjx',
    source_id                   VARCHAR(64)   NOT NULL DEFAULT '' COMMENT '来源业务 id（与 adapter_name 同源，供对外引用）',
    name                        VARCHAR(128)  NOT NULL COMMENT '来源名称，如 国网广东省电力公司',
    province_code               CHAR(6)       NULL COMMENT '所属省份；行业站/全国站为 NULL',
    base_url                    VARCHAR(512)  NOT NULL DEFAULT '' COMMENT '站点根地址',
    news_path                   VARCHAR(255)  NOT NULL DEFAULT '' COMMENT '新闻栏目路径',
    list_url                    VARCHAR(512)  NOT NULL DEFAULT '' COMMENT '栏目页完整 URL（引擎优先用此值）',
    category                    VARCHAR(16)   NOT NULL DEFAULT 'sgcc'
                                COMMENT 'sgcc=国家电网 / csg=南方电网 / independent=蒙东 / hmt=港澳台 / industry=行业媒体',
    priority                    TINYINT       NOT NULL DEFAULT 0 COMMENT '0=高频(1h) 1=中频(2-4h) 2=低频(4-6h)',
    crawl_interval_hours        TINYINT       NOT NULL DEFAULT 1 COMMENT '采集间隔（小时）',
    enabled                     TINYINT(1)    NOT NULL DEFAULT 1 COMMENT '1=启用',
    status                      VARCHAR(16)   NOT NULL DEFAULT 'active' COMMENT 'active / blocked / degraded',
    note                        VARCHAR(512)  NOT NULL DEFAULT '' COMMENT '来源特例说明（栏目页改址、反爬、编码等）',
    cooldown_until_utc          DATETIME      NULL COMMENT '封禁冷却到期（UTC），冷却期内协调器跳过',
    last_discovery_success_at_utc DATETIME    NULL COMMENT '最近一次列表发现成功时间（UTC），增量窗口依据',
    lease_token                 CHAR(36)      NULL COMMENT '采集租约令牌（同时只有一个任务能抓该来源）',
    lease_owner                 VARCHAR(128)  NULL COMMENT '租约持有者（主机名-PID）',
    lease_until_utc             DATETIME      NULL COMMENT '租约到期（UTC），过期可被他人抢占',
    created_at                  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_sources_adapter (adapter_name),
    KEY idx_sources_province (province_code),
    KEY idx_sources_category (category),
    KEY idx_sources_enabled (enabled, priority),
    KEY idx_sources_lease (lease_until_utc)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '电力新闻来源清单（39 条）';

-- ---------------------------------------------------------------------
-- 3. source_slots —— 来源采集配额槽位
--    ★ 架构演进说明 ★
--    旧项目为「一省一槽」（PK=province_code），导致同省第二个来源永远是
--    candidate、拿不到槽位。本项目升级为「每省 N 源」，槽位语义随之改为
--    「一来源一槽」：PK = adapter_name，province_code 仅作分组维度。
--    是否参与采集由 sources.enabled 决定，槽位只用于表达并发配额归属。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_slots (
    adapter_name  VARCHAR(64) NOT NULL COMMENT '适配器名（一来源一槽）',
    province_code CHAR(6)     NULL COMMENT '所属省份；行业站为 NULL',
    enabled       TINYINT(1)  NOT NULL DEFAULT 1 COMMENT '该来源是否占用采集配额',
    note          VARCHAR(512) NOT NULL DEFAULT '',
    created_at    TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (adapter_name),
    KEY idx_slots_province (province_code)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '来源采集配额槽位';

-- ---------------------------------------------------------------------
-- 4. articles —— 文章主表（五类字段：标题 / 正文 / 发布时间 / 来源 / 链接）
--    唯一身份 = (source_id, url_hash)
--      url_hash = SHA-256(normalize_url(url)) 前 40 位（newsagg/urls.py）
--      同一篇文章被同一来源在不同任务里再次发现时走 UPDATE 而非 INSERT，
--      因此抽取规则改进可直接修好历史记录（见 repo.save_article）。
--    content_hash = 正文 SimHash（crawl/simhash.py），跨来源同文打
--      quality_flags 里的 duplicate_of:<province_code> 标记。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS articles (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_id       INT UNSIGNED    NOT NULL COMMENT '来源 id → sources.id',
    url_hash        CHAR(40)        NOT NULL COMMENT 'URL 指纹（SHA-256 前 40 位）',
    url             VARCHAR(768)    NOT NULL COMMENT '原文链接（规范化后）',
    title           VARCHAR(512)    NOT NULL DEFAULT '' COMMENT '标题',
    body_text       MEDIUMTEXT      NULL COMMENT '正文纯文本（段落以 \\n\\n 分隔）',
    source_name     VARCHAR(128)    NOT NULL DEFAULT '' COMMENT '原文来源（站点自述的稿件来源，非站点名）',
    published_at_utc DATETIME       NULL COMMENT '发布时间（UTC naive）',
    published_at_cst DATETIME       NULL COMMENT '发布时间（北京时间 naive）',
    time_precision  VARCHAR(16)     NOT NULL DEFAULT 'unknown'
                    COMMENT 'second / minute / hour / day / unknown（时间精度，缺到日时不可参与小时级窗口判定）',
    content_hash    CHAR(40)        NOT NULL DEFAULT '' COMMENT '正文 SimHash（十六进制 16 位，右侧补零对齐 CHAR(40)）',
    quality_flags   VARCHAR(512)    NOT NULL DEFAULT '[]' COMMENT 'JSON 数组：empty_body / no_time_from_list / duplicate_of:440000 等',
    created_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_articles_source_hash (source_id, url_hash),
    KEY idx_articles_published (published_at_cst),
    KEY idx_articles_source_time (source_id, published_at_cst),
    KEY idx_articles_content_hash (content_hash),
    KEY idx_articles_flags (quality_flags(64)),
    CONSTRAINT fk_articles_source FOREIGN KEY (source_id) REFERENCES sources (id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '文章正文与五类字段';

-- ---------------------------------------------------------------------
-- 5. urls —— URL 指纹全局登记表
--    去重第一层（跨来源 URL 级）。articles 的唯一键只管「同一来源内」，
--    本表记录 URL 首次出现于哪个来源、被看过几次，供运营侧排查
--    「同一篇稿子被多家转载时哪个是首发来源」。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS urls (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    url_hash        CHAR(40)        NOT NULL COMMENT 'URL 指纹',
    url             VARCHAR(768)    NOT NULL COMMENT '规范化 URL',
    host            VARCHAR(128)    NOT NULL DEFAULT '' COMMENT '主机名',
    first_source_id INT UNSIGNED    NULL COMMENT '首次发现该 URL 的来源',
    seen_count      INT UNSIGNED    NOT NULL DEFAULT 1 COMMENT '累计被发现的次数',
    first_seen_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_urls_hash (url_hash),
    KEY idx_urls_host (host),
    KEY idx_urls_first_source (first_source_id)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = 'URL 指纹登记（跨来源 URL 去重）';

-- ---------------------------------------------------------------------
-- 6. article_tasks —— 列表页候选处理台账
--    列表页每条候选先落台账（status=pending），抓完回写终态。
--    价值：任务中断后可精确知道「哪些候选抓过、哪些没抓」，重跑不必全量重来。
--    list_title 是详情页标题缺失时的兜底（列表页标题）。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS article_tasks (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_id           INT UNSIGNED    NOT NULL COMMENT '来源 id',
    job_id              BIGINT UNSIGNED NOT NULL COMMENT '发现该候选的任务 id',
    discovered_url_hash CHAR(40)        NOT NULL COMMENT '候选 URL 指纹',
    url                 VARCHAR(768)    NOT NULL COMMENT '候选 URL',
    list_title          VARCHAR(512)    NULL COMMENT '列表页标题（详情页标题为空时兜底）',
    status              VARCHAR(16)     NOT NULL DEFAULT 'pending'
                        COMMENT 'pending / done / skipped / failed',
    failure_class       VARCHAR(64)     NULL COMMENT '失败归类：http_404 / timeout / parse_empty / blocked 等',
    skip_reason         VARCHAR(64)     NULL COMMENT '跳过原因：out_of_window / non_article / duplicate 等',
    attempts            INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '已尝试次数',
    created_at          TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_tasks_source_hash (source_id, discovered_url_hash),
    KEY idx_tasks_job (job_id),
    KEY idx_tasks_status (source_id, status),
    CONSTRAINT fk_tasks_source FOREIGN KEY (source_id) REFERENCES sources (id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '列表页候选处理台账';

-- ---------------------------------------------------------------------
-- 7. crawl_jobs —— 采集任务（单来源 × 单窗口）
--    幂等靠唯一键 (source_id, trigger_key)：
--      trigger_key = "hourly:2026101019:sgcc_gd" / "manual:20261010190000:sgcc_gd"
--    多实例同时跑协调器也只会建出一份任务（INSERT IGNORE）。
--    状态机：queued → running → succeeded / failed / partial
--                            ↘ retry_wait（retry_at_utc 到点可重投）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_jobs (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_id        INT UNSIGNED    NOT NULL,
    trigger_key      VARCHAR(128)    NOT NULL COMMENT '批次键（含小时戳与适配器名）',
    job_type         VARCHAR(32)     NOT NULL COMMENT 'full_rescan / incremental',
    status           VARCHAR(16)     NOT NULL DEFAULT 'queued'
                     COMMENT 'queued / running / succeeded / partial / failed / retry_wait / skipped',
    priority         TINYINT         NOT NULL DEFAULT 0,
    window_start_cst DATETIME        NOT NULL COMMENT '窗口起（北京时间）',
    window_end_cst   DATETIME        NOT NULL COMMENT '窗口止（北京时间）',
    discovered_count INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '列表页发现候选数',
    saved_count      INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '落库文章数',
    request_count    INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '实际发出的请求数',
    stop_reason      VARCHAR(64)     NOT NULL DEFAULT '' COMMENT '停止原因：done / window_bottom / budget / blocked / failure_ratio',
    retry_at_utc     DATETIME        NULL COMMENT '可重试时间（UTC）',
    started_at_utc   DATETIME        NULL,
    finished_at_utc  DATETIME        NULL,
    created_at       TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_jobs_trigger (source_id, trigger_key),
    KEY idx_jobs_status (status),
    KEY idx_jobs_source_finished (source_id, finished_at_utc),
    KEY idx_jobs_retry (retry_at_utc),
    CONSTRAINT fk_jobs_source FOREIGN KEY (source_id) REFERENCES sources (id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '采集任务（单来源单窗口）';

-- ---------------------------------------------------------------------
-- 8. crawl_runs —— 一次协调器批次
--    trigger_key = "hourly:2026101019"，聚合当轮建出的所有 crawl_jobs。
--    用于回答「19 点这一轮总共建了多少任务、跑得怎么样」。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_runs (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    trigger_key     VARCHAR(128)    NOT NULL COMMENT '批次键（小时粒度）',
    status          VARCHAR(16)     NOT NULL DEFAULT 'running' COMMENT 'running / finished',
    job_count       INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '本轮建出的任务数',
    created_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at_utc DATETIME        NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_runs_trigger (trigger_key),
    KEY idx_runs_created (created_at)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '协调器批次';

-- =====================================================================
-- 视图
-- =====================================================================

-- 干净文章视图：排除被标记为转载的文章，供对外接口/导出的默认口径使用。
-- 用 REPLACE 保证重复执行时视图定义可更新（CREATE OR REPLACE 在 MySQL 8 可用）。
CREATE OR REPLACE VIEW v_articles_clean AS
SELECT a.id,
       a.source_id,
       s.province_code,
       s.category,
       s.name   AS source_name_full,
       a.title,
       a.body_text,
       a.source_name,
       a.published_at_cst,
       a.time_precision,
       a.url,
       a.quality_flags,
       a.created_at,
       a.updated_at
FROM articles a
         JOIN sources s ON s.id = a.source_id
WHERE a.quality_flags NOT LIKE '%duplicate_of:%';

-- 省份覆盖视图：34 个省级行政区的来源数 / 文章数 / 字段完整率，
-- 直接支撑「覆盖 27 个国网省份 + 南方五省 + 蒙东」的验收口径。
CREATE OR REPLACE VIEW v_province_coverage AS
SELECT p.code                                                        AS province_code,
       p.name                                                        AS province_name,
       p.region,
       p.is_special,
       COALESCE(src.source_count, 0)                                 AS source_count,
       COALESCE(src.enabled_count, 0)                                AS enabled_source_count,
       COALESCE(art.article_count, 0)                                AS article_count,
       COALESCE(art.empty_body, 0)                                   AS empty_body,
       COALESCE(art.no_time, 0)                                      AS no_time,
       art.last_published_at
FROM provinces p
         LEFT JOIN (SELECT province_code,
                           COUNT(*)                                   AS source_count,
                           SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled_count
                    FROM sources
                    WHERE province_code IS NOT NULL
                    GROUP BY province_code) src ON src.province_code = p.code
         LEFT JOIN (SELECT s.province_code,
                           COUNT(*)                                             AS article_count,
                           SUM(CASE WHEN a.body_text IS NULL OR a.body_text = ''
                                    THEN 1 ELSE 0 END)                          AS empty_body,
                           SUM(CASE WHEN a.published_at_cst IS NULL
                                    THEN 1 ELSE 0 END)                          AS no_time,
                           MAX(a.published_at_cst)                              AS last_published_at
                    FROM articles a
                             JOIN sources s ON s.id = a.source_id
                    GROUP BY s.province_code) art ON art.province_code = p.code;

-- 来源产出视图：任务健康度 + 产出计数，供 /api/v1/sources 与质量看板使用。
CREATE OR REPLACE VIEW v_source_health AS
SELECT s.id,
       s.adapter_name,
       s.name,
       s.province_code,
       s.category,
       s.priority,
       s.crawl_interval_hours,
       s.enabled,
       s.status,
       s.cooldown_until_utc,
       s.last_discovery_success_at_utc,
       COALESCE(art.article_count, 0) AS article_count,
       COALESCE(job.job_total, 0)     AS job_total,
       COALESCE(job.job_succeeded, 0) AS job_succeeded,
       COALESCE(job.job_failed, 0)    AS job_failed,
       job.last_finished_at_utc,
       art.last_published_at
FROM sources s
         LEFT JOIN (SELECT source_id,
                           COUNT(*)                   AS article_count,
                           MAX(published_at_cst)      AS last_published_at
                    FROM articles
                    GROUP BY source_id) art ON art.source_id = s.id
         LEFT JOIN (SELECT source_id,
                           COUNT(*)                                                   AS job_total,
                           SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END)       AS job_succeeded,
                           SUM(CASE WHEN status IN ('failed', 'retry_wait') THEN 1 ELSE 0 END) AS job_failed,
                           MAX(finished_at_utc)                                       AS last_finished_at_utc
                    FROM crawl_jobs
                    GROUP BY source_id) job ON job.source_id = s.id;
