SELECT create_hypertable('telemetry.inverter_telemetry', 'time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE);

ALTER TABLE telemetry.inverter_telemetry SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'asset_id',
    timescaledb.compress_orderby = 'time DESC');

SELECT add_compression_policy('telemetry.inverter_telemetry',
    INTERVAL '30 days', if_not_exists => TRUE);

SELECT create_hypertable('telemetry.weather_telemetry', 'time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE);

ALTER TABLE telemetry.weather_telemetry SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'asset_id',
    timescaledb.compress_orderby = 'time DESC');

SELECT add_compression_policy('telemetry.weather_telemetry',
    INTERVAL '30 days', if_not_exists => TRUE);

SELECT create_hypertable('telemetry.block_telemetry', 'time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE);

ALTER TABLE telemetry.block_telemetry SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'asset_id',
    timescaledb.compress_orderby = 'time DESC');

SELECT add_compression_policy('telemetry.block_telemetry',
    INTERVAL '30 days', if_not_exists => TRUE);

SELECT create_hypertable('telemetry.transformer_telemetry', 'time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE);

ALTER TABLE telemetry.transformer_telemetry SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'asset_id',
    timescaledb.compress_orderby = 'time DESC');

SELECT add_compression_policy('telemetry.transformer_telemetry',
    INTERVAL '30 days', if_not_exists => TRUE);

SELECT create_hypertable('telemetry.plant_telemetry', 'time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE);

ALTER TABLE telemetry.plant_telemetry SET (
    timescaledb.compress,
    timescaledb.compress_orderby = 'time DESC');

SELECT add_compression_policy('telemetry.plant_telemetry',
    INTERVAL '30 days', if_not_exists => TRUE);

-- Built directly on raw telemetry.
CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry.inverter_5min
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '5 minutes', time) AS bucket,
    asset_id,
    avg(ac_power_kw) AS value,
    max(ac_power_kw) AS value_max,
    count(*) AS samples
FROM telemetry.inverter_telemetry
GROUP BY 1, 2;

-- Hierarchical: built on another continuous aggregate.
CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry.inverter_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 hour', bucket) AS bucket,
    asset_id,
    avg(value) AS value,
    max(value_max) AS value_max,
    sum(samples) AS samples
FROM telemetry.inverter_5min
GROUP BY 1, 2;

-- Built directly on raw telemetry.
CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry.plant_5min
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '5 minutes', time) AS bucket,
    avg(grid_export_power_kw) AS value,
    max(grid_export_power_kw) AS value_max,
    count(*) AS samples
FROM telemetry.plant_telemetry
GROUP BY 1;

-- Hierarchical: built on another continuous aggregate.
CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry.plant_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 hour', bucket) AS bucket,
    avg(value) AS value,
    max(value_max) AS value_max,
    sum(samples) AS samples
FROM telemetry.plant_5min
GROUP BY 1;

-- Hierarchical: built on another continuous aggregate.
CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry.plant_daily
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 day', bucket) AS bucket,
    avg(value) AS value,
    max(value_max) AS value_max,
    sum(samples) AS samples
FROM telemetry.plant_hourly
GROUP BY 1;

-- Built directly on raw telemetry.
CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry.weather_hourly
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '1 hour', time) AS bucket,
    asset_id,
    avg(ghi) AS value,
    max(ghi) AS value_max,
    count(*) AS samples
FROM telemetry.weather_telemetry
GROUP BY 1, 2;
