# FILE: db_connector.py
# VERSION: 3.18 - "The Network Faucet Patch"
# RESPONSIBILITY: Unified database access with robust Cloud-first logic.
# UPDATED: Added schema migration for max_gb_per_hour to network_quotas to act as a hard data rate limiter.

import sqlite3
import json
import logging
import os
import time
import random
import re
from pathlib import Path
from contextlib import contextmanager

# Try to import psycopg2 for Postgres
try:
    import psycopg2
    from psycopg2 import pool
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False

# --- Configuration ---
ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "birdnet_config.json"
LOCAL_DB_FILE = ROOT / "detections.db"

# --- GLOBAL CIRCUIT BREAKER ---
# If Cloud fails, we lock it out for 5 minutes so the GUI/WebMap doesn't freeze.
CLOUD_CIRCUIT_BREAKER_TS = 0

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [DB] - %(message)s')

def translate_sqlite_to_postgres(sql):
    work_sql = sql.strip().replace('\n', ' ').replace('  ', ' ')
    work_sql = work_sql.replace('?', '%s')
    
    if "IGNORE" in work_sql.upper():
        match = re.search(r"INSERT\s+OR\s+IGNORE\s+INTO\s+(\w+)\s*\((.*?)\)\s*VALUES\s*\((.*?)\)", work_sql, re.IGNORECASE | re.DOTALL)
        if match:
            table_name = match.group(1).lower()
            cols, vals = match.group(2), match.group(3)
            pk = "url" if table_name == "stream_queue" else "listener_id" if table_name == "scheduler_status" else "hash_text, stream_url" if table_name == "audio_hashes" else "stream_url, species_name" if table_name == "species_stream_profiles" else "id"
            return f"INSERT INTO {table_name} ({cols}) VALUES ({vals}) ON CONFLICT ({pk}) DO NOTHING"

    if "REPLACE" in work_sql.upper():
        work_sql = work_sql.replace("INSERT OR REPLACE INTO", "REPLACE INTO")
        match = re.search(r"REPLACE\s+INTO\s+(\w+)\s*\((.*?)\)\s*VALUES\s*\((.*?)\)", work_sql, re.IGNORECASE | re.DOTALL)
        if match:
            table_name, columns_str, values_str = match.group(1).lower(), match.group(2), match.group(3)
            columns =[c.strip() for c in columns_str.split(',')]
            pk = "url" if table_name == "stream_queue" else "listener_id" if table_name == "scheduler_status" else "stream_url" if table_name == "stream_noise_profiles" else "species_name" if table_name == "species_traits" else "email" if table_name == "users" else "interface_name" if table_name == "network_quotas" else "id"
            update_set = ", ".join([f"{col} = EXCLUDED.{col}" for col in columns if col not in pk])
            return f"INSERT INTO {table_name} ({columns_str}) VALUES ({values_str}) ON CONFLICT ({pk}) DO UPDATE SET {update_set}"

    if work_sql.upper().startswith("INSERT INTO DETECTIONS") and "RETURNING ID" not in work_sql.upper():
        work_sql += " RETURNING id"

    return work_sql

class SQLiteConnectionWrapper:
    def __init__(self, connection):
        self.conn = connection
        self.db_type = 'sqlite'
    def cursor(self): return self.conn.cursor()
    def commit(self): self.conn.commit()
    def rollback(self): self.conn.rollback()
    def close(self): self.conn.close()
    def execute(self, sql, params=None):
        return self.conn.execute(sql, params) if params else self.conn.execute(sql)
    def executemany(self, sql, params): return self.conn.executemany(sql, params)
    def __enter__(self): return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type: self.conn.rollback()
        else: self.conn.commit()
        self.conn.close()

class PostgresCursorWrapper:
    def __init__(self, real_cursor):
        self.cursor = real_cursor
        self._last_id = None
    
    def execute(self, sql, params=None):
        pg_sql = translate_sqlite_to_postgres(sql)
        if isinstance(params, list): params = tuple(params)
        
        result = self.cursor.execute(pg_sql, params) if params else self.cursor.execute(pg_sql)
        
        if "RETURNING ID" in pg_sql.upper():
            try:
                row = self.cursor.fetchone()
                if row: self._last_id = row[0]
            except: pass
        return result

    def executemany(self, sql, params_seq):
        pg_sql = translate_sqlite_to_postgres(sql)
        return self.cursor.executemany(pg_sql, params_seq)

    def fetchone(self): return self.cursor.fetchone()
    def fetchall(self): return self.cursor.fetchall()
    def close(self): self.cursor.close()
    
    @property
    def rowcount(self): return self.cursor.rowcount
    
    @property
    def lastrowid(self):
        return self._last_id

class PostgresConnectionWrapper:
    def __init__(self, connection): self.conn = connection; self.db_type = 'postgres'
    def cursor(self): return PostgresCursorWrapper(self.conn.cursor())
    def commit(self): self.conn.commit()
    def rollback(self): self.conn.rollback()
    def close(self): self.conn.close()
    def execute(self, sql, params=None):
        cur = self.cursor(); cur.execute(sql, params); return cur
    def __enter__(self): return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type: self.conn.rollback()
        else: self.conn.commit()
        self.conn.close()

def _get_local_connection():
    try:
        con = sqlite3.connect(LOCAL_DB_FILE, timeout=60)
        con.execute("PRAGMA journal_mode=WAL")
        return SQLiteConnectionWrapper(con)
    except Exception as e:
        logging.error(f"FATAL: Could not connect to local SQLite database: {e}")
        raise e

def _get_cloud_config():
    if not CONFIG_FILE.exists(): return None
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f: return json.load(f).get('database_cloud', None)
    except: return None

def get_db_connection(force_local=True):
    global CLOUD_CIRCUIT_BREAKER_TS
    if force_local:
        return _get_local_connection()

    cloud_cfg = _get_cloud_config()

    if cloud_cfg and cloud_cfg.get('enabled', False):
        if not POSTGRES_AVAILABLE:
            return _get_local_connection()
            
        # --- THE CIRCUIT BREAKER ---
        if time.time() < CLOUD_CIRCUIT_BREAKER_TS:
            return _get_local_connection()

        try:
            conn = psycopg2.connect(
                host=cloud_cfg.get('host'),
                database=cloud_cfg.get('dbname'),
                user=cloud_cfg.get('user'),
                password=cloud_cfg.get('password'),
                port=cloud_cfg.get('port', 5432),
                connect_timeout=3
            )
            CLOUD_CIRCUIT_BREAKER_TS = 0 
            return PostgresConnectionWrapper(conn)
        except Exception as e:
            CLOUD_CIRCUIT_BREAKER_TS = time.time() + 300 
            logging.error(f"CRITICAL: Cloud DB connection failed. Tripping Circuit Breaker for 5 minutes to prevent lag.")
            return _get_local_connection()
    else:
        return _get_local_connection()

def check_and_migrate_schema():
    # 1. ALWAYS patch the Local SQLite Database
    try:
        with get_db_connection(force_local=True) as con:
            con.execute("CREATE TABLE IF NOT EXISTS network_hardware_logs (id INTEGER PRIMARY KEY, interface_name TEXT, timestamp REAL, bytes_sent INTEGER, bytes_recv INTEGER)")
            con.execute("CREATE TABLE IF NOT EXISTS network_app_logs (id INTEGER PRIMARY KEY, interface_name TEXT, engine_type TEXT, timestamp REAL, bytes_used INTEGER)")
            con.execute("CREATE TABLE IF NOT EXISTS network_quotas (interface_name TEXT PRIMARY KEY, limit_gb REAL, reset_day INTEGER)")

            cur = con.cursor()
            cur.execute("PRAGMA table_info(detections)")
            cols = [row[1] for row in cur.fetchall()]

            if 'listener_id' not in cols: con.execute("ALTER TABLE detections ADD COLUMN listener_id TEXT")
            if 'network_interface' not in cols: con.execute("ALTER TABLE detections ADD COLUMN network_interface TEXT")
            if 'detection_method' not in cols: con.execute("ALTER TABLE detections ADD COLUMN detection_method TEXT DEFAULT 'audio'")
            if 'vision_path' not in cols: con.execute("ALTER TABLE detections ADD COLUMN vision_path TEXT")
            if 'human_verified' not in cols: con.execute("ALTER TABLE detections ADD COLUMN human_verified TEXT DEFAULT 'pending'")
            if 'ai_notes' not in cols: con.execute("ALTER TABLE detections ADD COLUMN ai_notes TEXT")
            if 'frame_size' not in cols: con.execute("ALTER TABLE detections ADD COLUMN frame_size TEXT")
            if 'filter_reason' not in cols: con.execute("ALTER TABLE detections ADD COLUMN filter_reason TEXT")
            
            cur.execute("PRAGMA table_info(network_quotas)")
            quota_cols = [row[1] for row in cur.fetchall()]
            if 'plan_size_gb' not in quota_cols: con.execute("ALTER TABLE network_quotas ADD COLUMN plan_size_gb REAL DEFAULT 0")
            if 'allowed_percent' not in quota_cols: con.execute("ALTER TABLE network_quotas ADD COLUMN allowed_percent INTEGER DEFAULT 100")
            if 'max_gb_per_hour' not in quota_cols: con.execute("ALTER TABLE network_quotas ADD COLUMN max_gb_per_hour REAL DEFAULT 0")
            
    except Exception as e: 
        logging.error(f"Local Schema migration failed: {e}")

    # 2. Patch the Cloud PostgreSQL Database if it is active
    try:
        with get_db_connection(force_local=False) as con:
            if getattr(con, 'db_type', 'sqlite') == 'postgres':
                con.execute("CREATE TABLE IF NOT EXISTS network_hardware_logs (id SERIAL PRIMARY KEY, interface_name TEXT, timestamp DOUBLE PRECISION, bytes_sent BIGINT, bytes_recv BIGINT)")
                con.execute("CREATE TABLE IF NOT EXISTS network_app_logs (id SERIAL PRIMARY KEY, interface_name TEXT, engine_type TEXT, timestamp DOUBLE PRECISION, bytes_used BIGINT)")
                con.execute("CREATE TABLE IF NOT EXISTS network_quotas (interface_name TEXT PRIMARY KEY, limit_gb DOUBLE PRECISION, reset_day INTEGER)")

                cur = con.cursor()
                cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'detections'")
                cols = [row[0] for row in cur.fetchall()]

                if 'listener_id' not in cols: con.execute("ALTER TABLE detections ADD COLUMN listener_id TEXT")
                if 'network_interface' not in cols: con.execute("ALTER TABLE detections ADD COLUMN network_interface TEXT")
                if 'detection_method' not in cols: con.execute("ALTER TABLE detections ADD COLUMN detection_method TEXT DEFAULT 'audio'")
                if 'vision_path' not in cols: con.execute("ALTER TABLE detections ADD COLUMN vision_path TEXT")
                if 'human_verified' not in cols: con.execute("ALTER TABLE detections ADD COLUMN human_verified TEXT DEFAULT 'pending'")
                if 'ai_notes' not in cols: con.execute("ALTER TABLE detections ADD COLUMN ai_notes TEXT")
                if 'frame_size' not in cols: con.execute("ALTER TABLE detections ADD COLUMN frame_size TEXT")
                if 'filter_reason' not in cols: con.execute("ALTER TABLE detections ADD COLUMN filter_reason TEXT")
                
                cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'network_quotas'")
                quota_cols =[row[0] for row in cur.fetchall()]
                if 'plan_size_gb' not in quota_cols: con.execute("ALTER TABLE network_quotas ADD COLUMN plan_size_gb DOUBLE PRECISION DEFAULT 0")
                if 'allowed_percent' not in quota_cols: con.execute("ALTER TABLE network_quotas ADD COLUMN allowed_percent INTEGER DEFAULT 100")
                if 'max_gb_per_hour' not in quota_cols: con.execute("ALTER TABLE network_quotas ADD COLUMN max_gb_per_hour DOUBLE PRECISION DEFAULT 0")
    except Exception as e: 
        logging.error(f"Cloud Schema migration failed: {e}")

def init_stream_queue(streams_list):
    check_and_migrate_schema()
    try:
        with get_db_connection(force_local=True) as con:
            con.execute('CREATE TABLE IF NOT EXISTS stream_queue (url TEXT PRIMARY KEY, check_count INTEGER DEFAULT 0, last_checked_ts DOUBLE PRECISION DEFAULT 0, next_eligible_ts DOUBLE PRECISION DEFAULT 0, status_note TEXT)')
            con.execute('CREATE TABLE IF NOT EXISTS turnstile_state (id INTEGER PRIMARY KEY, last_release_ts DOUBLE PRECISION DEFAULT 0)')
            cur = con.cursor(); cur.execute("SELECT count(*) FROM turnstile_state")
            if cur.fetchone()[0] == 0: con.execute("INSERT INTO turnstile_state (id, last_release_ts) VALUES (1, 0)")
            active_urls =[s['page_url'] for s in streams_list if s.get('enabled', True)]
            if active_urls:
                placeholders = ','.join('?' for _ in active_urls)
                con.execute(f"DELETE FROM stream_queue WHERE url NOT IN ({placeholders})", active_urls)
                for url in active_urls:
                    con.execute("INSERT INTO stream_queue (url, check_count, last_checked_ts, next_eligible_ts) VALUES (?, 0, 0, 0) ON CONFLICT (url) DO NOTHING", (url,))
            else: con.execute("DELETE FROM stream_queue")
    except Exception as e: logging.error(f"Failed to initialize stream queue: {e}")


def checkout_streams(batch_size=20):
    now = time.time()
    try:
        with get_db_connection(force_local=True) as con:
            con.conn.isolation_level = None 
            con.execute("BEGIN EXCLUSIVE")
            
            cur = con.cursor()
            cur.execute("SELECT url FROM stream_queue WHERE next_eligible_ts <= ? ORDER BY last_checked_ts ASC, RANDOM() LIMIT ?", (now, batch_size))
            rows = cur.fetchall()
            selected_urls =[row[0] for row in rows]
            
            if selected_urls:
                placeholders = ','.join('?' for _ in selected_urls)
                lease_time = now + 60.0 
                con.execute(f"UPDATE stream_queue SET check_count = check_count + 1, last_checked_ts = ?, next_eligible_ts = ? WHERE url IN ({placeholders})",[now, lease_time] + selected_urls)
            
            con.commit() 
            return selected_urls
    except Exception as e:
        if "database is locked" not in str(e).lower():
            logging.error(f"Failed to checkout streams: {e}")
        return[]

def update_stream_status(url, status_type, penalty_minutes=0):
    try:
        next_time = time.time() + (penalty_minutes * 60)
        with get_db_connection(force_local=True) as con:
            con.execute("UPDATE stream_queue SET next_eligible_ts = ?, status_note = ? WHERE url = ?", (next_time, status_type, url))
    except: pass

def get_turnstile_wait(target_cycle_s, num_listeners, jitter_s=0):
    try:
        now = time.time()
        ideal_gap = max(10, target_cycle_s) / max(1, num_listeners)
        with get_db_connection(force_local=True) as con:
            cur = con.cursor(); cur.execute("SELECT last_release_ts FROM turnstile_state WHERE id = 1")
            row = cur.fetchone(); last_ts = row[0] if row else 0
            target_time = last_ts + ideal_gap
            wait_time = max(0, target_time - now); final_wait = max(0, wait_time + random.uniform(-jitter_s, jitter_s))
            new_ts = max(now, target_time)
            con.execute("UPDATE turnstile_state SET last_release_ts = ? WHERE id = 1", (new_ts,))
            return final_wait
    except: return 5

def get_maintenance_candidates():
    try:
        cutoff_7d = time.time() - (7 * 24 * 60 * 60)
        with get_db_connection(force_local=True) as con:
            cur = con.cursor()
            query = """
                SELECT 
                    q.url, 
                    q.status_note, 
                    q.next_eligible_ts,
                    COALESCE(strike_counts.strikes, 0) as strikes,
                    last_successes.last_success
                FROM stream_queue q
                LEFT JOIN (
                    SELECT stream_url, COUNT(*) as strikes
                    FROM stream_health_events
                    WHERE timestamp > ? AND status NOT IN ('SUCCESS', 'CHECKING', 'INFO')
                    GROUP BY stream_url
                ) strike_counts ON q.url = strike_counts.stream_url
                LEFT JOIN (
                    SELECT stream_url, MAX(timestamp) as last_success
                    FROM stream_health_events
                    WHERE status = 'SUCCESS'
                    GROUP BY stream_url
                ) last_successes ON q.url = last_successes.stream_url
                WHERE q.status_note NOT IN ('SUCCESS', 'CHECKING', 'None') AND q.status_note IS NOT NULL
            """
            cur.execute(query, (cutoff_7d,))
            return cur.fetchall()
    except Exception as e:
        logging.error(f"Maintenance query failed: {e}")
        return[]

def get_consecutive_failures(url):
    try:
        with get_db_connection(force_local=True) as con:
            cur = con.cursor()
            cur.execute("SELECT status FROM stream_health_events WHERE stream_url = ? ORDER BY timestamp DESC LIMIT 20", (url,))
            count = 0
            for row in cur.fetchall():
                if row[0] in ('FAILURE', 'HICCUP'): 
                    count += 1
                else: 
                    break 
            return count
    except: return 0

def get_last_success(url):
    try:
        with get_db_connection(force_local=True) as con:
            cur = con.cursor()
            cur.execute("SELECT MAX(timestamp) FROM stream_health_events WHERE stream_url = ? AND status = 'SUCCESS'", (url,))
            row = cur.fetchone()
            return row[0] if row and row[0] else 0
    except: return 0