-- =====================================================================
-- 气象数据表（24 张 = 8 大区 × 3 数据产品）
--
-- ⚠ 本文件由 `scripts/gen_weather_schema.py` 生成，请勿手改。
--   列定义唯一来源于 `newsagg/weather/models.py`；
--   改列请改源码后重新生成，并用 `--check` 验证同步。
--
-- 命名：wx_{大区}_{产品}
--   大区：huabei/dongbei/huadong/huazhong/huanan/xinan/xibei/gangat
--   产品：hist_rt(历史实时) / hist_fc(历史预测) / daily_fc(每日预测)
--
-- 时间类别：同表内由 `granularity` 列区分逐时/逐日，两类行的数值列同名但口径不同，
--   逐日口径见每个数值列注释里的「逐日=…」。
-- 时区：一律 Asia/Shanghai，逐时为整点、逐日为当日零点。
-- 幂等：主键 (station_id, granularity, ts)，重复采集覆盖而非追加。
-- =====================================================================

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------
-- 站点主表：全国行政区中心点（由 scripts/build_weather_stations.py 产出）
-- 大区口径与 newsagg/provinces.py 一致，供数据表按大区路由。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wx_stations (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键，CN + 行政区划代码',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码（唯一）',
    name           VARCHAR(64)   NOT NULL COMMENT '区县/市级名称',
    level          VARCHAR(16)   NOT NULL DEFAULT '' COMMENT 'district/city/province',
    lon            DECIMAL(9,6)  NOT NULL COMMENT '经度（官方质心，°E）',
    lat            DECIMAL(9,6)  NOT NULL COMMENT '纬度（官方质心，°N）',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    province       VARCHAR(32)   NOT NULL COMMENT '省级全称',
    region         VARCHAR(16)   NOT NULL COMMENT '大区：华北/东北/华东/华中/华南/西南/西北/港澳台',
    is_special     TINYINT(1)    NOT NULL DEFAULT 0 COMMENT '1=港澳台',
    created_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id),
    UNIQUE KEY uk_wx_stations_adcode (adcode),
    KEY idx_wx_stations_region (region)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象站点主表';

-- ---------------------------------------------------------------------
-- 回填进度表：让「2024.1.1 → 今天」这种跨数小时的长回填可中断续跑。
-- 主键即一片工作单元（产品 × 大区 × 时间窗）；status='ok' 的片会被跳过。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wx_ingest_progress (
    product       VARCHAR(16)   NOT NULL COMMENT '数据产品',
    region        VARCHAR(16)   NOT NULL COMMENT '大区',
    period_start  DATE          NOT NULL COMMENT '窗口起（闭）',
    period_end    DATE          NOT NULL COMMENT '窗口止（闭）',
    hourly_rows   INT UNSIGNED  NOT NULL DEFAULT 0 COMMENT '写入的逐时行数',
    daily_rows    INT UNSIGNED  NOT NULL DEFAULT 0 COMMENT '写入的逐日行数',
    stations      INT UNSIGNED  NOT NULL DEFAULT 0 COMMENT '本片站点数',
    status        VARCHAR(16)   NOT NULL DEFAULT 'ok' COMMENT 'ok / partial / failed',
    note          VARCHAR(255)  NOT NULL DEFAULT '' COMMENT '失败原因摘要',
    updated_at    TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (product, region, period_start, period_end),
    KEY idx_wx_ingest_progress_status (status)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据回填进度（可续跑）';

-- =====================================================================
-- 数据表 24 张：华北=huabei；东北=dongbei；华东=huadong；华中=huazhong；华南=huanan；西南=xinan；西北=xibei；港澳台=gangat
-- =====================================================================

-- 华北 × 历史实时（ERA5 再分析实况）
CREATE TABLE IF NOT EXISTS wx_huabei_hist_rt (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_huabei_hist_rt_adcode_ts (adcode, ts),
    KEY idx_wx_huabei_hist_rt_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·华北·历史实时（ERA5 再分析实况）';

-- 华北 × 历史预测（归档的历史预报）
CREATE TABLE IF NOT EXISTS wx_huabei_hist_fc (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_huabei_hist_fc_adcode_ts (adcode, ts),
    KEY idx_wx_huabei_hist_fc_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·华北·历史预测（归档的历史预报）';

-- 华北 × 每日预测（滚动预报）
CREATE TABLE IF NOT EXISTS wx_huabei_daily_fc (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_huabei_daily_fc_adcode_ts (adcode, ts),
    KEY idx_wx_huabei_daily_fc_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·华北·每日预测（滚动预报）';

-- 东北 × 历史实时（ERA5 再分析实况）
CREATE TABLE IF NOT EXISTS wx_dongbei_hist_rt (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_dongbei_hist_rt_adcode_ts (adcode, ts),
    KEY idx_wx_dongbei_hist_rt_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·东北·历史实时（ERA5 再分析实况）';

-- 东北 × 历史预测（归档的历史预报）
CREATE TABLE IF NOT EXISTS wx_dongbei_hist_fc (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_dongbei_hist_fc_adcode_ts (adcode, ts),
    KEY idx_wx_dongbei_hist_fc_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·东北·历史预测（归档的历史预报）';

-- 东北 × 每日预测（滚动预报）
CREATE TABLE IF NOT EXISTS wx_dongbei_daily_fc (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_dongbei_daily_fc_adcode_ts (adcode, ts),
    KEY idx_wx_dongbei_daily_fc_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·东北·每日预测（滚动预报）';

-- 华东 × 历史实时（ERA5 再分析实况）
CREATE TABLE IF NOT EXISTS wx_huadong_hist_rt (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_huadong_hist_rt_adcode_ts (adcode, ts),
    KEY idx_wx_huadong_hist_rt_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·华东·历史实时（ERA5 再分析实况）';

-- 华东 × 历史预测（归档的历史预报）
CREATE TABLE IF NOT EXISTS wx_huadong_hist_fc (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_huadong_hist_fc_adcode_ts (adcode, ts),
    KEY idx_wx_huadong_hist_fc_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·华东·历史预测（归档的历史预报）';

-- 华东 × 每日预测（滚动预报）
CREATE TABLE IF NOT EXISTS wx_huadong_daily_fc (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_huadong_daily_fc_adcode_ts (adcode, ts),
    KEY idx_wx_huadong_daily_fc_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·华东·每日预测（滚动预报）';

-- 华中 × 历史实时（ERA5 再分析实况）
CREATE TABLE IF NOT EXISTS wx_huazhong_hist_rt (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_huazhong_hist_rt_adcode_ts (adcode, ts),
    KEY idx_wx_huazhong_hist_rt_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·华中·历史实时（ERA5 再分析实况）';

-- 华中 × 历史预测（归档的历史预报）
CREATE TABLE IF NOT EXISTS wx_huazhong_hist_fc (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_huazhong_hist_fc_adcode_ts (adcode, ts),
    KEY idx_wx_huazhong_hist_fc_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·华中·历史预测（归档的历史预报）';

-- 华中 × 每日预测（滚动预报）
CREATE TABLE IF NOT EXISTS wx_huazhong_daily_fc (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_huazhong_daily_fc_adcode_ts (adcode, ts),
    KEY idx_wx_huazhong_daily_fc_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·华中·每日预测（滚动预报）';

-- 华南 × 历史实时（ERA5 再分析实况）
CREATE TABLE IF NOT EXISTS wx_huanan_hist_rt (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_huanan_hist_rt_adcode_ts (adcode, ts),
    KEY idx_wx_huanan_hist_rt_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·华南·历史实时（ERA5 再分析实况）';

-- 华南 × 历史预测（归档的历史预报）
CREATE TABLE IF NOT EXISTS wx_huanan_hist_fc (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_huanan_hist_fc_adcode_ts (adcode, ts),
    KEY idx_wx_huanan_hist_fc_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·华南·历史预测（归档的历史预报）';

-- 华南 × 每日预测（滚动预报）
CREATE TABLE IF NOT EXISTS wx_huanan_daily_fc (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_huanan_daily_fc_adcode_ts (adcode, ts),
    KEY idx_wx_huanan_daily_fc_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·华南·每日预测（滚动预报）';

-- 西南 × 历史实时（ERA5 再分析实况）
CREATE TABLE IF NOT EXISTS wx_xinan_hist_rt (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_xinan_hist_rt_adcode_ts (adcode, ts),
    KEY idx_wx_xinan_hist_rt_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·西南·历史实时（ERA5 再分析实况）';

-- 西南 × 历史预测（归档的历史预报）
CREATE TABLE IF NOT EXISTS wx_xinan_hist_fc (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_xinan_hist_fc_adcode_ts (adcode, ts),
    KEY idx_wx_xinan_hist_fc_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·西南·历史预测（归档的历史预报）';

-- 西南 × 每日预测（滚动预报）
CREATE TABLE IF NOT EXISTS wx_xinan_daily_fc (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_xinan_daily_fc_adcode_ts (adcode, ts),
    KEY idx_wx_xinan_daily_fc_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·西南·每日预测（滚动预报）';

-- 西北 × 历史实时（ERA5 再分析实况）
CREATE TABLE IF NOT EXISTS wx_xibei_hist_rt (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_xibei_hist_rt_adcode_ts (adcode, ts),
    KEY idx_wx_xibei_hist_rt_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·西北·历史实时（ERA5 再分析实况）';

-- 西北 × 历史预测（归档的历史预报）
CREATE TABLE IF NOT EXISTS wx_xibei_hist_fc (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_xibei_hist_fc_adcode_ts (adcode, ts),
    KEY idx_wx_xibei_hist_fc_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·西北·历史预测（归档的历史预报）';

-- 西北 × 每日预测（滚动预报）
CREATE TABLE IF NOT EXISTS wx_xibei_daily_fc (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_xibei_daily_fc_adcode_ts (adcode, ts),
    KEY idx_wx_xibei_daily_fc_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·西北·每日预测（滚动预报）';

-- 港澳台 × 历史实时（ERA5 再分析实况）
CREATE TABLE IF NOT EXISTS wx_gangat_hist_rt (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_gangat_hist_rt_adcode_ts (adcode, ts),
    KEY idx_wx_gangat_hist_rt_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·港澳台·历史实时（ERA5 再分析实况）';

-- 港澳台 × 历史预测（归档的历史预报）
CREATE TABLE IF NOT EXISTS wx_gangat_hist_fc (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_gangat_hist_fc_adcode_ts (adcode, ts),
    KEY idx_wx_gangat_hist_fc_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·港澳台·历史预测（归档的历史预报）';

-- 港澳台 × 每日预测（滚动预报）
CREATE TABLE IF NOT EXISTS wx_gangat_daily_fc (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：hourly/daily',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
    temperature_2m               DECIMAL(5,2)         NULL COMMENT '2 米气温（°C）；逐日=日平均',
    relative_humidity_2m         DECIMAL(5,2)         NULL COMMENT '2 米相对湿度（%）；逐日=逐时均值（本地聚合）',
    precipitation                DECIMAL(7,2)         NULL COMMENT '降水量（mm）；逐日=日累计',
    wind_speed_10m               DECIMAL(6,2)         NULL COMMENT '10 米风速（km/h）；逐日=日最大',
    wind_direction_10m           SMALLINT UNSIGNED    NULL COMMENT '10 米风向（°）；逐日=主导风向',
    shortwave_radiation          DECIMAL(8,2)         NULL COMMENT '短波辐照度（W/m²）；逐日=日累计',
    cloud_cover                  DECIMAL(5,2)         NULL COMMENT '总云量（%）；逐日=逐时均值（本地聚合）',
    pressure_msl                 DECIMAL(7,2)         NULL COMMENT '海平面气压（hPa）；逐日=逐时均值（本地聚合）',
    et0_fao_evapotranspiration   DECIMAL(7,3)         NULL COMMENT '参考蒸散量 ET₀（mm）；逐日=日累计',
    weather_code                 SMALLINT UNSIGNED    NULL COMMENT '天气代码（WMO）；逐日=日主导天气代码',
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_wx_gangat_daily_fc_adcode_ts (adcode, ts),
    KEY idx_wx_gangat_daily_fc_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据·港澳台·每日预测（滚动预报）';

