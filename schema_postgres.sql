-- FILE: schema_postgres.sql
-- VERSION: 1.2 (V40.0 The 2D/3D Overhaul Edition)
-- DESCRIPTION: PostgreSQL Schema for Global Birdsong Radio Cloud Node.

-- 1. CORE DATA: The Detections
CREATE TABLE IF NOT EXISTS detections (
    id SERIAL PRIMARY KEY,
    timestamp DOUBLE PRECISION NOT NULL,
    channel_url TEXT NOT NULL,
    species TEXT NOT NULL,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    distance_category TEXT,
    snr DOUBLE PRECISION,
    alert_sent BOOLEAN DEFAULT FALSE,
    listener_id TEXT,
    network_interface TEXT,
    detection_method TEXT DEFAULT 'audio',
    vision_path TEXT,
    human_verified TEXT DEFAULT 'pending',
    ai_notes TEXT,
    frame_size TEXT,          -- Added for V40.0 2D/3D Split
    filter_reason TEXT        -- Added for V40.0 Curation Studio Transparency
);

-- Indexes for fast API queries
CREATE INDEX IF NOT EXISTS idx_detections_species ON detections(species);
CREATE INDEX IF NOT EXISTS idx_detections_timestamp ON detections(timestamp);
CREATE INDEX IF NOT EXISTS idx_detections_channel ON detections(channel_url);

-- 2. SCHEDULER: The Queue
CREATE TABLE IF NOT EXISTS stream_queue (
    url TEXT PRIMARY KEY,
    check_count INTEGER DEFAULT 0,
    last_checked_ts DOUBLE PRECISION DEFAULT 0,
    next_eligible_ts DOUBLE PRECISION DEFAULT 0,
    status_note TEXT
);

CREATE TABLE IF NOT EXISTS scheduler_status (
    listener_id TEXT PRIMARY KEY,
    cycle_start_time DOUBLE PRECISION,
    total_cycle_seconds DOUBLE PRECISION,
    managed_streams_json TEXT,
    last_updated DOUBLE PRECISION,
    cycle_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS turnstile_state (
    id INTEGER PRIMARY KEY,
    last_release_ts DOUBLE PRECISION DEFAULT 0
);

-- 3. INTELLIGENCE: Bayesian Profiling (Cooldowns)
CREATE TABLE IF NOT EXISTS species_stream_profiles (
    id SERIAL PRIMARY KEY,
    stream_url TEXT NOT NULL,
    species_name TEXT NOT NULL,
    max_snr_observed DOUBLE PRECISION DEFAULT 0,
    sample_count INTEGER DEFAULT 0,
    last_updated DOUBLE PRECISION NOT NULL,
    baseline_detection_id INTEGER,
    UNIQUE(stream_url, species_name)
);

-- 4. INTELLIGENCE: Stream Health & Audio Fingerprinting
CREATE TABLE IF NOT EXISTS stream_noise_profiles (
    stream_url TEXT PRIMARY KEY,
    sample_count INTEGER DEFAULT 0,
    average_noise_dbfs DOUBLE PRECISION DEFAULT -90.0,
    last_updated DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS stream_health_events (
    id SERIAL PRIMARY KEY,
    stream_url TEXT NOT NULL,
    timestamp DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL,
    message TEXT
);

CREATE TABLE IF NOT EXISTS audio_hashes (
    hash_text TEXT NOT NULL,
    stream_url TEXT NOT NULL,
    first_seen_timestamp DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (hash_text, stream_url)
);

-- 5. LOGGING: System Events
CREATE TABLE IF NOT EXISTS system_events (
    id SERIAL PRIMARY KEY,
    timestamp DOUBLE PRECISION NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    details TEXT
);

-- 6. FORENSIC PROFILER
CREATE TABLE IF NOT EXISTS species_traits (
    species_name TEXT PRIMARY KEY,
    family_group TEXT,
    size_class INTEGER,
    beak_type TEXT,
    color_primary TEXT,
    silhouette TEXT,
    call_pattern TEXT
);
CREATE INDEX IF NOT EXISTS idx_traits_beak ON species_traits(beak_type);

-- 7. TAXONOMY (eBird Data)
CREATE TABLE IF NOT EXISTS ebird_species (
    "speciesCode" TEXT PRIMARY KEY,
    "comName" TEXT NOT NULL,
    "sciName" TEXT NOT NULL,
    "familyComName" TEXT,
    "familySciName" TEXT
);

CREATE TABLE IF NOT EXISTS bird_groups (
    "groupName" TEXT PRIMARY KEY,
    "ebirdFamilyComName" TEXT NOT NULL UNIQUE
);

-- 8. USERS & ALERTS
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_interests (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    interest_type TEXT NOT NULL,   -- 'continent', 'species', 'sniper_match'
    interest_value TEXT NOT NULL,
    specific_context TEXT,         -- For 'sniper_match' (stream_url)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, interest_type, interest_value)
);

-- 9. VISUAL ASSETS (Added for Cloud Image Sync)
CREATE TABLE IF NOT EXISTS species_images (
    species_name TEXT PRIMARY KEY,
    image_data BYTEA,
    source_url TEXT,
    last_updated TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
);