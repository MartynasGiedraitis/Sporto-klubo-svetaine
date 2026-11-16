CREATE DATABASE IF NOT EXISTS gym;

CREATE TABLE IF NOT EXISTS gym.analytics_events
(
    event_id String,
    event_type LowCardinality(String),
    occurred_at DateTime64(3, 'UTC'),
    session_id String DEFAULT '',
    booking_id String DEFAULT '',
    trainer_id String DEFAULT '',
    payload String,
    INDEX event_type_index event_type TYPE set(32) GRANULARITY 1,
    INDEX session_id_index session_id TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(occurred_at)
ORDER BY event_id;
