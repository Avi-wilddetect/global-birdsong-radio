# FILE: api_server.py
# VERSION: 5.1 - "The Floodgate Patch"
# PURPOSE: Serves Web Map, Mobile API, HLS Proxy, securely accepts telemetry, and autonomously cleans Cloud Storage bloat.
# UPDATED: Fast-forwards the MasterGate pointer on startup to ignore historical backlogs. Re-introduced a generous 2-hour TTL to prevent map flooding while remaining immune to Windows hardware clock drift.

import sqlite3
import json
import logging
import os
import threading
import time
import re
import hashlib
import shutil
import random
import socket
from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlparse, quote, unquote, urljoin, quote_plus

from flask import Flask, jsonify, request, send_from_directory, Response, stream_with_context, make_response
from flask_bcrypt import Bcrypt
from flask_jwt_extended import create_access_token, get_jwt_identity, jwt_required, JWTManager

try:
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
except ImportError:
    pass 

ROOT = Path(__file__).resolve().parent
DATABASE_PATH = ROOT / "detections.db"
IMAGE_DB_PATH = ROOT / "image_database.db"
CLIPS_DIR = ROOT / "baseline_clips"
VAULT_DIR = ROOT / "vision_snapshots"
CONFIG_FILE = ROOT / "birdnet_config.json"
TEMPLATE_FILE = ROOT / "map_template.html"
VISION_TARGETS_FILE = ROOT / "vision_targets.json"

# Ensure the media directories exist on the server
CLIPS_DIR.mkdir(exist_ok=True)
VAULT_DIR.mkdir(exist_ok=True)

# --- STRICT CATEGORIZATION LOGIC ---
BIOACOUSTIC_KEYWORDS =[
    "elephant", "rhinoceros", "hippopotamus", "wolf", "coyote", "hyena", "fox", "bear",
    "lion", "tiger", "leopard", "jaguar", "panther", "panda", 
    "monkey", "gorilla", "ape", "chimpanzee", "gibbon", "baboon", "macaque", "lemur",
    "cow", "cattle", "sheep", "goat", "yak", "camel", "bovid", "zebra", "horse", "pig", "boar",
    "elk", "deer", "moose", "caribou", "antelope",
    "squirrel", "chipmunk", "bat", "mouse", "rat", "hare", "rabbit",
    "sea lion", "seal", "otter", "walrus", "whale", "dolphin", "porpoise",
    "frog", "toad", "peeper", "amphibian", "reptile", "snake", "alligator", "crocodile",
    "cricket", "cicada", "grasshopper", "katydid", "insect", "mammal"
]

BIRD_EXCEPTIONS =[
    "heron", "frogmouth", "cowbird", "egret", "finch", "dove", "tyrant", "woodpecker", 
    "duck", "goose", "swan", "sparrow", "weaver", "bunting", "warbler", "thrush", 
    "hawk", "eagle", "falcon", "owl", "gull", "tern", "wren", "bird"
]

def is_bird(species_name):
    name_lower = species_name.lower()
    if any(exc in name_lower for exc in BIRD_EXCEPTIONS):
        return True
    if any(kw in name_lower for kw in BIOACOUSTIC_KEYWORDS):
        return False
    return True # Default assumption is a bird

# --- THE FAILSAFE RADAR GRAPHIC ---
FALLBACK_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
    <rect width="200" height="200" fill="#111111"/>
    <circle cx="100" cy="100" r="80" fill="none" stroke="#00E5FF" stroke-width="4" stroke-dasharray="10 10"/>
    <circle cx="100" cy="100" r="40" fill="none" stroke="#00E5FF" stroke-width="2" opacity="0.5"/>
    <circle cx="100" cy="100" r="8" fill="#FF3D00"/>
    <line x1="100" y1="100" x2="160" y2="40" stroke="#00E5FF" stroke-width="3" opacity="0.8"/>
    <text x="100" y="160" font-family="Arial, sans-serif" font-size="16" font-weight="bold" fill="#00E5FF" text-anchor="middle" letter-spacing="2">NO VISUAL</text>
</svg>"""

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
app = Flask(__name__)
app.config["JWT_SECRET_KEY"] = "a-super-secret-key-that-you-should-change"
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(days=30)
bcrypt = Bcrypt(app)
jwt = JWTManager(app)
config_lock = threading.Lock()

# --- UNIFIED DATABASE EXECUTOR ---
def read_config():
    with config_lock:
        if not CONFIG_FILE.exists(): return None
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f: return json.load(f)

def query_db(query, params=(), fetch=True, many=False, force_local=False):
    """
    Executes a query. If fetch=False, returns the affected rowcount (for UPDATE/DELETE) 
    or the new ID (for INSERT RETURNING id).
    Guarantees connection closure via try...finally block to prevent exhaustion locks.
    """
    config = read_config()
    is_cloud = config and config.get('database_cloud', {}).get('enabled', False)
    
    if is_cloud and not force_local:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        c_cfg = config['database_cloud']
        conn = None
        try:
            try:
                conn = psycopg2.connect(
                    host='db', dbname=c_cfg.get('dbname'),
                    user=c_cfg.get('user'), password=c_cfg.get('password'), port=c_cfg.get('port', 5432),
                    connect_timeout=5
                )
            except psycopg2.OperationalError:
                conn = psycopg2.connect(
                    host=c_cfg.get('host'), dbname=c_cfg.get('dbname'),
                    user=c_cfg.get('user'), password=c_cfg.get('password'), port=c_cfg.get('port', 5432),
                    connect_timeout=5
                )
            
            pg_query = query.replace('?', '%s')
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            if many:
                cur.executemany(pg_query, params)
            else:
                if isinstance(params, list): params = tuple(params)
                cur.execute(pg_query, params)
                
            if fetch:
                res =[dict(r) for r in cur.fetchall()]
            else:
                res = None
                if "RETURNING ID" in pg_query.upper():
                    row = cur.fetchone()
                    if row:
                        res = row['id']
                else:
                    res = cur.rowcount
                conn.commit()
                
            cur.close()
            return res
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        
    else:
        conn = None
        try:
            conn = sqlite3.connect(DATABASE_PATH, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            cur = conn.cursor()
            
            if many:
                cur.executemany(query, params)
            else:
                cur.execute(query, params)
                
            if fetch:
                res =[dict(r) for r in cur.fetchall()]
            else:
                res = cur.lastrowid
                if query.strip().upper().startswith("UPDATE") or query.strip().upper().startswith("DELETE"):
                    res = cur.rowcount
                conn.commit()
                
            cur.close()
            return res
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

def init_broadcast_table():
    """Ensures the Master Gate broadcast table exists and is indexed on startup."""
    config = read_config()
    is_cloud = config and config.get('database_cloud', {}).get('enabled', False)
    
    if is_cloud:
        query = """
        CREATE TABLE IF NOT EXISTS map_feed_broadcast (
            id SERIAL PRIMARY KEY,
            detection_id INTEGER UNIQUE,
            release_timestamp DOUBLE PRECISION
        )
        """
    else:
        query = """
        CREATE TABLE IF NOT EXISTS map_feed_broadcast (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detection_id INTEGER UNIQUE,
            release_timestamp REAL
        )
        """
    try:
        query_db(query, fetch=False)
        
        count_res = query_db("SELECT COUNT(*) as c FROM map_feed_broadcast")
        if count_res and count_res[0]['c'] == 0:
            logging.info("Backfilling 500 recent historical timestamps into Broadcast Queue...")
            if is_cloud:
                query_db("""
                    INSERT INTO map_feed_broadcast (detection_id, release_timestamp) 
                    SELECT id, timestamp FROM detections 
                    ORDER BY timestamp DESC LIMIT 500
                    ON CONFLICT (detection_id) DO NOTHING
                """, fetch=False)
            else:
                query_db("""
                    INSERT OR IGNORE INTO map_feed_broadcast (detection_id, release_timestamp) 
                    SELECT id, timestamp FROM detections 
                    ORDER BY timestamp DESC LIMIT 500
                """, fetch=False)
        
        query_db("CREATE INDEX IF NOT EXISTS idx_broadcast_release ON map_feed_broadcast(release_timestamp)", fetch=False)
        query_db("CREATE INDEX IF NOT EXISTS idx_broadcast_detid ON map_feed_broadcast(detection_id)", fetch=False)
        
        logging.info("✅ Verified map_feed_broadcast table exists and is fully indexed.")
    except Exception as e:
        logging.error(f"Failed to create map_feed_broadcast table: {e}")

# --- UTILS ---
def generate_stable_id(url):
    return "id_" + hashlib.md5(url.encode('utf-8')).hexdigest()

def sanitize_url_for_matching(raw_url):
    if not raw_url: return ""
    clean = raw_url.replace(r"\u0026", "&").replace(r"\/", "/")
    clean = re.sub(r'[\?&]variant=\d+', '', clean)
    return clean.strip()

def get_fuzzy_base(url):
    if not url: return ""
    if "youtube.com" in url or "youtu.be" in url:
        return url
    if "?" in url:
        return url.split('?')[0]
    return url

def _no_cache_json(data):
    response = jsonify(data)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

def normalize_timestamp(ts, now_ts):
    if ts > now_ts + 60:
        drift_hours = max(1, round((ts - now_ts) / 3600.0))
        return ts - (drift_hours * 3600)
    return ts

# --- ROUTES ---

@app.route('/')
def root():
    return send_from_directory(ROOT, 'map_template.html')

@app.route('/data')
def map_data():
    try:
        config = read_config()
        if not config: return _no_cache_json({"nodes":[], "feed":[], "error": "Config missing", "maintenance_mode": False})
        
        is_maintenance = config.get('maintenance_mode', False)
        is_cloud = config.get('database_cloud', {}).get('enabled', False)
        
        alert_cond_sql = "d.alert_sent = TRUE" if is_cloud else "d.alert_sent = 1"
        
        streams_config = {s['page_url']: s for s in config.get('streams',[]) if s.get('enabled', True)}
        map_settings = config.get("map_settings", {})
        live_window = int(map_settings.get("live_window_seconds", 120))
        feed_capacity = int(map_settings.get("target_sidebar_capacity", 50))
        
        url_to_name = {}
        for s in config.get('streams',[]):
            name = s.get('name', 'Unknown Stream')
            
            p_url = s.get('page_url', '')
            if p_url:
                sanitized_p_url = sanitize_url_for_matching(p_url)
                url_to_name[p_url] = name
                url_to_name[sanitized_p_url] = name
                url_to_name[get_fuzzy_base(p_url)] = name 
                url_to_name[get_fuzzy_base(sanitized_p_url)] = name 
                
            o_url = s.get('original_url', '')
            if o_url:
                sanitized_o_url = sanitize_url_for_matching(o_url)
                url_to_name[o_url] = name
                url_to_name[sanitized_o_url] = name
                url_to_name[get_fuzzy_base(o_url)] = name 
                url_to_name[get_fuzzy_base(sanitized_o_url)] = name 

        # --- PRIVACY FILTERING LOGIC ---
        private_species = set()
        if VISION_TARGETS_FILE.exists():
            try:
                v_data = json.loads(VISION_TARGETS_FILE.read_text(encoding='utf-8'))
                for sp, details in v_data.get("registry", {}).items():
                    if not details.get("show_on_map", True):
                        private_species.add(sp.strip())
            except Exception as e:
                logging.error(f"Error reading vision targets for privacy filter: {e}")

        priv_list = list(private_species)
        if priv_list:
            placeholders = ','.join('?' for _ in priv_list)
            where_d = f"WHERE {alert_cond_sql} AND d.species NOT IN ({placeholders})"
            params_latest = tuple(priv_list)
            params_feed = tuple(priv_list)
        else:
            where_d = f"WHERE {alert_cond_sql}"
            params_latest = ()
            params_feed = ()
            
        # CLOCK DRIFT PATCH: Pulling b.release_timestamp into the queries to ensure Cloud-Atomic timing.
        query_latest_raw = f"""
            SELECT d.id, d.channel_url, d.species, d.timestamp, b.release_timestamp, d.distance_category, d.detection_method, d.frame_size, d.filter_reason, d.ai_notes 
            FROM map_feed_broadcast b
            JOIN detections d ON b.detection_id = d.id
            {where_d}
            ORDER BY b.release_timestamp DESC 
            LIMIT 2000
        """
        
        query_feed = f"""
            SELECT d.id, d.species, d.channel_url, d.timestamp, b.release_timestamp, d.distance_category, d.detection_method, d.frame_size, d.filter_reason, d.ai_notes 
            FROM map_feed_broadcast b
            JOIN detections d ON b.detection_id = d.id
            {where_d} 
            ORDER BY b.release_timestamp DESC LIMIT {feed_capacity}
        """

        try:
            raw_latest = query_db(query_latest_raw, params_latest)
            feed_rows = query_db(query_feed, params_feed)
        except Exception as e:
            if "relation \"map_feed_broadcast\" does not exist" in str(e) or "no such table" in str(e):
                logging.info("Table map_feed_broadcast missing! Creating it on the fly...")
                init_broadcast_table()
                try:
                    raw_latest = query_db(query_latest_raw, params_latest)
                    feed_rows = query_db(query_feed, params_feed)
                except Exception as retry_e:
                    logging.error(f"SQL Retry Error in map payload: {retry_e}")
                    return _no_cache_json({"nodes": [], "feed":[], "error": f"SQL Error: {retry_e}", "maintenance_mode": False})
            else:
                logging.error(f"SQL Error in map payload: {e}")
                return _no_cache_json({"nodes": [], "feed":[], "error": f"SQL Error: {e}", "maintenance_mode": False})

        # --- THE MAP BALANCE PATCH ---
        # Track Audio and Vision independently so Audio doesn't overwrite Vision candidates
        latest_rows =[]
        seen_audio_urls = set()
        seen_vision_urls = set()
        
        for r in raw_latest:
            if r['detection_method'] in ('audio', 'multimodal'):
                if r['channel_url'] not in seen_audio_urls:
                    seen_audio_urls.add(r['channel_url'])
                    latest_rows.append(r)
            elif r['detection_method'] == 'vision':
                if r['channel_url'] not in seen_vision_urls:
                    seen_vision_urls.add(r['channel_url'])
                    latest_rows.append(r)

        # =====================================================================
        # --- MAP BALANCE (AUDIO VS VISION) DYNAMIC SLOT-FILLING ---
        # =====================================================================
        target_audio_pct = map_settings.get("audio_vision_ratio", 70)
        target_map_capacity = int(map_settings.get("target_map_capacity", 50))
        
        def apply_dynamic_slots(rows, total_capacity, audio_pct):
            target_audio_slots = int(total_capacity * (audio_pct / 100.0))
            target_vision_slots = total_capacity - target_audio_slots
            
            audio_rows = [r for r in rows if r['detection_method'] in ('audio', 'multimodal')]
            vision_rows =[r for r in rows if r['detection_method'] == 'vision']
            
            a_count = len(audio_rows)
            v_count = len(vision_rows)
            
            allowed_a = target_audio_slots
            allowed_v = target_vision_slots
            
            # The Compensation Logic: Fill the Void
            if a_count < target_audio_slots:
                deficit = target_audio_slots - a_count
                allowed_v += deficit
                allowed_a = a_count
            elif v_count < target_vision_slots:
                deficit = target_vision_slots - v_count
                allowed_a += deficit
                allowed_v = v_count
                
            # Use release_timestamp for accurate sorting
            audio_rows.sort(key=lambda x: x.get('release_timestamp') or x['timestamp'], reverse=True)
            vision_rows.sort(key=lambda x: x.get('release_timestamp') or x['timestamp'], reverse=True)
            
            kept_rows = audio_rows[:allowed_a] + vision_rows[:allowed_v]
            kept_rows.sort(key=lambda x: x.get('release_timestamp') or x['timestamp'], reverse=True)
            return kept_rows

        latest_rows = apply_dynamic_slots(latest_rows, target_map_capacity, target_audio_pct)
        feed_rows = feed_rows[:feed_capacity]
        # =====================================================================

        # Safe grouping by URL for map nodes
        latest_detections = {}
        latest_detections_base = {}
        for row in latest_rows:
            row_time = row.get('release_timestamp') or row['timestamp']
            
            # We overwrite so the absolute newest (whether Audio or Vision) takes the single Map Pin slot
            if row['channel_url'] not in latest_detections or row_time > (latest_detections[row['channel_url']].get('release_timestamp') or latest_detections[row['channel_url']]['timestamp']):
                latest_detections[row['channel_url']] = row
                latest_detections_base[get_fuzzy_base(row['channel_url'])] = row 
        
        anchor_rows = query_db("SELECT stream_url, species_name, baseline_detection_id FROM species_stream_profiles WHERE baseline_detection_id IS NOT NULL")
        anchor_map = {(r['stream_url'], r['species_name']): r['baseline_detection_id'] for r in anchor_rows}
        
        def get_valid_audio_id(stream_url, species, current_det_id=None):
            if current_det_id:
                if (CLIPS_DIR / f"detection_{current_det_id}.mp3").exists() or (CLIPS_DIR / f"detection_{current_det_id}.wav").exists():
                    return current_det_id
            anchor_id = anchor_map.get((stream_url, species))
            if not anchor_id: 
                return None
            if (CLIPS_DIR / f"detection_{anchor_id}.mp3").exists() or (CLIPS_DIR / f"detection_{anchor_id}.wav").exists():
                return anchor_id
            return None
        
        nodes =[]
        now = time.time()
        name_to_canonical = {}

        for url, conf in streams_config.items():
            raw_orig_lat = conf.get('original_lat')
            raw_orig_lon = conf.get('original_lon')
            if raw_orig_lat is not None and raw_orig_lon is not None:
                lat = float(raw_orig_lat); lon = float(raw_orig_lon)
            else:
                lat = float(conf.get('lat') or 0.0); lon = float(conf.get('lon') or 0.0)

            clean_url = sanitize_url_for_matching(url)
            explicit_type = conf.get('stream_type')
            human_url = conf.get('original_url') or clean_url
            yt_id = None; audio_url = None; stream_type = 'unknown'
            
            if explicit_type:
                stream_type = explicit_type
                if stream_type == 'youtube' or stream_type == 'video':
                    match = re.search(r'(?:v=|\/live\/|\/embed\/|\/v\/|youtu\.be\/)([^&?#\/]+)', clean_url)
                    yt_id = match.group(1) if match else None
                elif stream_type in['hls', 'direct_stream', 'audio']:
                    audio_url = clean_url
            else:
                is_youtube = "youtu" in clean_url.lower()
                if is_youtube:
                    stream_type = 'video'
                    match = re.search(r'(?:v=|\/live\/|\/embed\/|\/v\/|youtu\.be\/)([^&?#\/]+)', clean_url)
                    yt_id = match.group(1) if match else None
                elif ".m3u8" in clean_url:
                    stream_type = 'hls'
                    audio_url = clean_url
                else: 
                    stream_type = 'audio'
                    audio_url = clean_url

            if ".m3u8" in clean_url and stream_type == 'audio': stream_type = 'hls'
            
            name_lower = conf['name'].lower()
            if "(audio" in name_lower or "(mp3" in name_lower:
                stream_type = 'audio'
                audio_url = clean_url
            
            stable_id = generate_stable_id(clean_url)
            name_to_canonical[conf['name']] = {'id': stable_id, 'type': stream_type}

            node = {
                'id': stable_id, 'name': conf['name'],
                'lat': lat, 'lon': lon,
                'yt_id': yt_id, 'audio_url': audio_url,
                'page_url': clean_url, 'external_url': human_url,
                'stream_type': stream_type,
                'species': 'Online', 'status': 'listening', 'time_ago_str': 'Scanning...',
                'image': None, 'links': {'wiki': '#', 'secondary': '#', 'secondary_name': 'ALL ABOUT BIRDS'}, 
                'audio_id': None, 'detection_method': 'audio', 'dist': 'Unknown',
                'frame_size': 'N/A', 'filter_reason': '', 'ai_notes': ''
            }

            det = (latest_detections.get(url) or 
                   latest_detections.get(clean_url) or 
                   latest_detections_base.get(get_fuzzy_base(url)) or 
                   latest_detections_base.get(get_fuzzy_base(clean_url)))

            if det:
                # CLOCK DRIFT PATCH: Using atomic release_timestamp from the Cloud db directly!
                norm_ts = det.get('release_timestamp') or det['timestamp']
                diff = max(0, now - norm_ts)
                
                node['species'] = det['species']
                node['audio_id'] = get_valid_audio_id(det['channel_url'], det['species'], det['id'])
                node['detection_method'] = det.get('detection_method', 'audio')
                node['dist'] = det.get('distance_category', 'Unknown')
                node['frame_size'] = det.get('frame_size', 'N/A')
                node['filter_reason'] = det.get('filter_reason', '')
                node['ai_notes'] = det.get('ai_notes', '')
                
                safe_name = det['species'].replace(" ", "_")
                
                is_b = is_bird(det['species'])
                sec_url = f"https://www.allaboutbirds.org/guide/{quote(det['species'])}" if is_b else f"https://www.inaturalist.org/search?q={quote_plus(det['species'])}"
                sec_name = "ALL ABOUT BIRDS" if is_b else "INATURALIST DATABASE"
                
                node['links'] = {
                    'wiki': f"https://en.wikipedia.org/wiki/{safe_name}", 
                    'secondary': sec_url,
                    'secondary_name': sec_name
                }
                
                if diff < live_window: node['status'] = 'live'; node['time_ago_str'] = 'LIVE NOW'
                elif diff < 3600: node['status'] = 'recent'; node['time_ago_str'] = f"{int(diff/60)}m ago"
                else: node['status'] = 'dormant'; node['time_ago_str'] = f"{int(diff/3600)}h ago"
                
                node['image'] = f"/api/image/{quote(det['species'])}" 

            nodes.append(node)

        feed =[]
        for f in feed_rows:
            # CLOCK DRIFT PATCH: Using atomic release_timestamp
            norm_ts = f.get('release_timestamp') or f['timestamp']
            diff = max(0, now - norm_ts)
            urgency = 'red' if diff < live_window else 'orange' if diff < 3600 else 'grey'
            
            raw_url = f['channel_url']
            clean_url = sanitize_url_for_matching(raw_url)
            base_url = get_fuzzy_base(raw_url)
            clean_base_url = get_fuzzy_base(clean_url)
            
            stream_name = (url_to_name.get(raw_url) or 
                           url_to_name.get(clean_url) or 
                           url_to_name.get(base_url) or 
                           url_to_name.get(clean_base_url) or 
                           "Unknown Stream")
            
            if stream_name in name_to_canonical:
                tid = name_to_canonical[stream_name]['id']
                stype = name_to_canonical[stream_name]['type']
            else:
                tid = generate_stable_id(raw_url); stype = 'unknown'
            
            feed.append({
                'audio_id': get_valid_audio_id(raw_url, f['species'], f['id']),
                'timestamp': norm_ts,
                'species': f['species'], 
                'time': datetime.fromtimestamp(norm_ts).strftime('%H:%M'), 
                'stream': stream_name, 'stream_type': stype,
                'dist': f['distance_category'] or "?", 'target_id': tid,
                'urgency': urgency, 'is_close': "Near" in (f['distance_category'] or ""), 'has_migration': False,
                'detection_method': f.get('detection_method', 'audio'),
                'frame_size': f.get('frame_size', 'N/A'),
                'filter_reason': f.get('filter_reason', ''),
                'ai_notes': f.get('ai_notes', '')
            })

        return _no_cache_json({"nodes": nodes, "feed": feed, "maintenance_mode": is_maintenance})
    except Exception as e:
        logging.error("MAP DATA ERROR", exc_info=True)
        return _no_cache_json({"nodes":[], "feed":[], "error": str(e), "maintenance_mode": False})

@app.route('/proxy')
def proxy_stream():
    target_url = request.args.get('url')
    if not target_url: return "Missing URL", 400
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        req = requests.get(target_url, headers=headers, stream=True, timeout=10, verify=False)
        return Response(stream_with_context(req.iter_content(chunk_size=1024)), content_type=req.headers['content-type'])
    except Exception as e:
        return f"Proxy Error: {e}", 502

@app.route('/api/inject_detection', methods=['POST'])
def inject_detection():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    client_token = request.form.get('secret_token', "")
    
    if not server_token or client_token != server_token:
        return "Unauthorized", 401

    try:
        # CLOCK DRIFT PATCH: Removed artificial clamping. Allow raw timestamp straight from Edge.
        timestamp = float(request.form.get('timestamp', time.time()))
            
        url = request.form.get('channel_url')
        species = request.form.get('species')
        lat = request.form.get('latitude')
        lon = request.form.get('longitude')
        dist = request.form.get('distance_category')
        snr = float(request.form.get('snr', 0.0))
        alert_sent = request.form.get('alert_sent', 'false').lower() == 'true'
        listener_id = request.form.get('listener_id')
        network_interface = request.form.get('network_interface')
        detection_method = request.form.get('detection_method')
        
        frame_size = request.form.get('frame_size', 'N/A')
        filter_reason = request.form.get('filter_reason', '')
        ai_notes = request.form.get('ai_notes', '')
        
        if not detection_method:
            if listener_id == "VISION_ENGINE":
                detection_method = "multimodal" if dist and "Multimodal" in dist else "vision"
            else:
                detection_method = "audio"
        
        if not url or not species:
            return "Missing mandatory fields", 400
            
        lat = float(lat) if lat else None
        lon = float(lon) if lon else None

        query = """
            INSERT INTO detections 
            (timestamp, channel_url, species, latitude, longitude, distance_category, snr, alert_sent, listener_id, network_interface, detection_method, frame_size, filter_reason, ai_notes) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
        """
        
        det_id = query_db(query, (timestamp, url, species, lat, lon, dist, snr, alert_sent, listener_id, network_interface, detection_method, frame_size, filter_reason, ai_notes), fetch=False)
        
        if 'audio_file' in request.files and det_id:
            file = request.files['audio_file']
            filename = f"detection_{det_id}.mp3"
            save_path = CLIPS_DIR / filename
            file.save(str(save_path))
            
        return jsonify({"status": "success", "detection_id": det_id}), 200

    except Exception as e:
        logging.error(f"HTTPS Injection Failed: {e}")
        return str(e), 500

@app.route('/api/upgrade_multimodal', methods=['POST'])
def upgrade_multimodal():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    client_token = request.form.get('secret_token', "")
    
    if not server_token or client_token != server_token:
        return "Unauthorized", 401

    try:
        original_timestamp_str = request.form.get('original_timestamp')
        url = request.form.get('channel_url')
        
        if not original_timestamp_str or not url:
            return "Missing mandatory fields", 400
            
        # CLOCK DRIFT PATCH: Removed artificial clamping.
        orig_ts = float(original_timestamp_str)
        
        # Expanded safety window to +/- 15 seconds to catch drifted Edge node acoustic signatures
        t_min = orig_ts - 15.0
        t_max = orig_ts + 15.0
        
        dist = request.form.get('distance_category')
        frame_size = request.form.get('frame_size', 'N/A')
        filter_reason = request.form.get('filter_reason', '')
        ai_notes = request.form.get('ai_notes', '')
        alert_sent = request.form.get('alert_sent', 'false').lower() == 'true'
        
        query = """
            UPDATE detections 
            SET detection_method = 'multimodal',
                distance_category = ?,
                frame_size = ?,
                filter_reason = ?,
                ai_notes = ?,
                alert_sent = alert_sent OR ?
            WHERE id IN (
                SELECT id FROM detections 
                WHERE channel_url = ? AND timestamp >= ? AND timestamp <= ?
                ORDER BY ABS(timestamp - ?) ASC LIMIT 1
            )
        """
        
        rowcount = query_db(query, (dist, frame_size, filter_reason, ai_notes, alert_sent, url, t_min, t_max, orig_ts), fetch=False)
        
        if rowcount == 0:
            return jsonify({"status": "failed", "message": "Original audio detection not found on cloud."}), 404
            
        logging.info(f"Multimodal Upgrade Success: Patched {rowcount} closest audio record on {url}")
        return jsonify({"status": "success", "message": f"Upgraded {rowcount} records to multimodal."}), 200

    except Exception as e:
        logging.error(f"HTTPS Upgrade Failed: {e}")
        return str(e), 500

@app.route('/api/migrate_detection_urls', methods=['POST'])
def migrate_detection_urls():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    client_token = request.form.get('secret_token', "")
    
    if not server_token or client_token != server_token:
        return "Unauthorized", 401

    try:
        old_url = request.form.get('old_url')
        new_url = request.form.get('new_url')
        
        if not old_url or not new_url:
            return "Missing mandatory fields (old_url and new_url are required)", 400
            
        query = "UPDATE detections SET channel_url = ? WHERE channel_url = ?"
        rowcount = query_db(query, (new_url, old_url), fetch=False)
        
        logging.info(f"Cloud URL Migration Success: Redirected {rowcount} history records from '{old_url}' to '{new_url}'")
        return jsonify({
            "status": "success", 
            "message": f"Successfully migrated {rowcount} historical detections to the new URL."
        }), 200

    except Exception as e:
        logging.error(f"HTTPS Migration Failed: {e}")
        return str(e), 500

@app.route('/api/curate_detection', methods=['POST'])
def curate_detection():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    client_token = request.form.get('secret_token', "")
    
    if not server_token or client_token != server_token:
        return "Unauthorized", 401

    try:
        timestamp_str = request.form.get('timestamp')
        url = request.form.get('channel_url')
        new_species = request.form.get('new_species')
        
        if not timestamp_str or not url or not new_species:
            return "Missing mandatory fields", 400
            
        timestamp = float(timestamp_str)
        
        # CLOCK DRIFT PATCH: Expanded to +/- 15.0 seconds
        t_min = timestamp - 15.0
        t_max = timestamp + 15.0
        
        query = """
            UPDATE detections 
            SET species = ?
            WHERE id IN (
                SELECT id FROM detections 
                WHERE channel_url = ? AND timestamp >= ? AND timestamp <= ?
                ORDER BY ABS(timestamp - ?) ASC LIMIT 1
            )
        """
        
        rowcount = query_db(query, (new_species, url, t_min, t_max, timestamp), fetch=False)
        
        if rowcount == 0:
            return jsonify({"status": "failed", "message": "Cloud Record not found. Timestamp drift too large."}), 404
            
        logging.info(f"Pinpoint Curation Success: Patched {rowcount} record near {timestamp} to '{new_species}'")
        return jsonify({"status": "success", "message": f"Successfully updated {rowcount} record(s) to {new_species}"}), 200

    except Exception as e:
        logging.error(f"HTTPS Curation Failed: {e}")
        return str(e), 500

@app.route('/api/delete_detection', methods=['POST'])
def delete_detection():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    client_token = request.form.get('secret_token', "")
    
    if not server_token or client_token != server_token:
        return "Unauthorized", 401

    try:
        timestamp_str = request.form.get('timestamp')
        url = request.form.get('channel_url')
        
        if not timestamp_str or not url:
            return "Missing mandatory fields", 400
            
        timestamp = float(timestamp_str)
        
        # CLOCK DRIFT PATCH: Expanded to +/- 15.0 seconds
        t_min = timestamp - 15.0
        t_max = timestamp + 15.0
        
        query = """
            DELETE FROM detections 
            WHERE id IN (
                SELECT id FROM detections 
                WHERE channel_url = ? AND timestamp >= ? AND timestamp <= ?
                ORDER BY ABS(timestamp - ?) ASC LIMIT 1
            )
        """
        
        rowcount = query_db(query, (url, t_min, t_max, timestamp), fetch=False)
        
        if rowcount == 0:
            return jsonify({"status": "failed", "message": "Cloud Record not found. Timestamp drift too large."}), 404
            
        logging.info(f"Pinpoint Delete Success: Erased {rowcount} record near {timestamp}")
        return jsonify({"status": "success", "message": f"Successfully deleted {rowcount} record(s)"}), 200

    except Exception as e:
        logging.error(f"HTTPS Delete Failed: {e}")
        return str(e), 500

@app.route('/api/upload_audio', methods=['POST'])
def upload_audio():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    client_token = request.form.get('secret_token', "")
    
    if not server_token or client_token != server_token:
        return "Unauthorized", 401

    if 'audio_file' not in request.files:
        return "No file part", 400
    
    file = request.files['audio_file']
    detection_id = request.form.get('detection_id')
    
    if not detection_id or not file:
        return "Missing data", 400

    filename = f"detection_{detection_id}.mp3"
    save_path = CLIPS_DIR / filename
    
    try:
        file.save(str(save_path))
        logging.info(f"Successfully received and saved: {filename}")
        return "Upload successful", 200
    except Exception as e:
        logging.error(f"Failed to save uploaded audio: {e}")
        return str(e), 500

@app.route('/api/upload_image', methods=['POST'])
def upload_image():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    client_token = request.form.get('secret_token', "")
    
    if not server_token or client_token != server_token:
        return "Unauthorized", 401

    if 'image_file' not in request.files:
        return "No file part", 400
    
    file = request.files['image_file']
    species_name = request.form.get('species_name')
    source_url = request.form.get('source_url', '')
    status = request.form.get('status', 'OK')
    
    if not species_name or not file:
        return "Missing data", 400

    image_data = file.read()
    last_updated = time.strftime('%Y-%m-%d %H:%M:%S')

    try:
        query = """
            INSERT INTO species_images (species_name, image_data, source_url, last_updated, status) 
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (species_name) DO UPDATE SET
            image_data = EXCLUDED.image_data,
            source_url = EXCLUDED.source_url,
            last_updated = EXCLUDED.last_updated,
            status = EXCLUDED.status
        """
        query_db(query, (species_name, image_data, source_url, last_updated, status), fetch=False)
        logging.info(f"Successfully received and saved image for: {species_name}")
        return "Upload successful", 200
    except Exception as e:
        logging.error(f"Failed to save uploaded image: {e}")
        return str(e), 500

@app.route('/api/delete_image', methods=['POST'])
def delete_image():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    client_token = request.form.get('secret_token', "")
    
    if not server_token or client_token != server_token:
        return "Unauthorized", 401

    species_name = request.form.get('species_name')
    if not species_name:
        return "Missing species_name", 400

    try:
        query_db("DELETE FROM species_images WHERE species_name = ?", (species_name,), fetch=False)
        logging.info(f"Successfully deleted image for: {species_name}")
        return "Delete successful", 200
    except Exception as e:
        logging.error(f"Failed to delete image: {e}")
        return str(e), 500

@app.route('/api/upload_config', methods=['POST'])
def upload_config():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    client_token = request.form.get('secret_token', "")
    
    if not server_token or client_token != server_token:
        return "Unauthorized", 401

    if 'config_file' not in request.files:
        return "No file part", 400

    file = request.files['config_file']
    if not file or not file.filename:
        return "Invalid file", 400

    try:
        file_content = file.read().decode('utf-8')
        json.loads(file_content) 

        with config_lock:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                f.write(file_content)

        logging.info("Successfully received and updated birdnet_config.json from Edge node.")
        return "Config updated successfully", 200
    except json.JSONDecodeError:
        return "Invalid JSON format", 400
    except Exception as e:
        logging.error(f"Failed to update config: {e}")
        return str(e), 500

@app.route('/api/login', methods=['POST'])
def login_user():
    data = request.get_json()
    try:
        rows = query_db("SELECT id, password_hash FROM users WHERE email = ?", (data.get('email'),))
        if rows and bcrypt.check_password_hash(rows[0]['password_hash'], data.get('password')):
            return jsonify(access_token=create_access_token(identity=str(rows[0]['id'])))
    except: pass
    return jsonify({"error": "Invalid"}), 401

@app.route('/api/streams', methods=['GET'])
def get_streams():
    cfg = read_config()
    return _no_cache_json(cfg.get('streams',[])) if cfg else _no_cache_json([])

@app.route('/api/audio/<path:filename>')
def serve_audio(filename):
    if (CLIPS_DIR / filename).exists():
        mime = 'audio/wav' if filename.endswith('.wav') else 'audio/mpeg'
        return send_from_directory(CLIPS_DIR, filename, mimetype=mime)
    return jsonify({"error": "File not found"}), 404

@app.route('/audio/<path:filename>')
def serve_audio_web(filename):
    return serve_audio(filename)

@app.route('/api/image/<path:species_name>')
def serve_image(species_name):
    try:
        species_name = unquote(species_name)
        rows = query_db("SELECT image_data FROM species_images WHERE species_name = ?", (species_name,))
        if rows and rows[0].get('image_data'):
            img_data = rows[0]['image_data']
            if isinstance(img_data, memoryview):
                img_data = bytes(img_data)
            
            if img_data and img_data != b"failed" and len(img_data) > 50:
                return Response(img_data, mimetype='image/jpeg')
            
        return Response(FALLBACK_SVG, mimetype='image/svg+xml')
        
    except Exception as e:
        logging.error(f"Image serving error: {e}")
        return Response(FALLBACK_SVG, mimetype='image/svg+xml')

# ==============================================================================
# THE MASTER GATE BROADCAST QUEUE
# ==============================================================================
class MasterGateThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True

    def run(self):
        # --- THE BOOT COLLISION PATCH ---
        # Sleep for 15 seconds to let Gunicorn fully spin up the HTTP workers 
        # BEFORE we execute any structural DB queries that could trigger timeouts.
        time.sleep(15)
        logging.info("🚪 Master Gate Thread started. Managing feed broadcast interleaved ratio...")
        
        # Defer table creation until the web workers are safely running
        init_broadcast_table()
        
        last_processed_id = 0
        try:
            # --- THE STARTUP FAST-FORWARD PATCH ---
            # By fetching the absolute max ID directly from the detections table, 
            # the thread instantly skips any un-broadcasted historical backlog
            # that built up while the server was down/restarting, preventing massive map floods.
            max_res = query_db("SELECT MAX(id) as m FROM detections")
            if max_res and max_res[0]['m']:
                last_processed_id = int(max_res[0]['m'])
        except Exception as e:
            logging.error(f"Failed to fetch fast-forward detection ID on startup: {e}")

        while True:
            try:
                config = read_config()
                if not config:
                    time.sleep(2)
                    continue
                    
                ratio = config.get("map_settings", {}).get("audio_vision_ratio", 70)
                is_cloud = config.get('database_cloud', {}).get('enabled', False)
                alert_cond_sql = "alert_sent = TRUE" if is_cloud else "alert_sent = 1"
                
                # --- THE POINTER PATCH ---
                # Blazing fast primary key lookup. No full table scans!
                query = f"""
                    SELECT id, detection_method, timestamp 
                    FROM detections 
                    WHERE id > {last_processed_id} AND {alert_cond_sql}
                    ORDER BY id ASC 
                    LIMIT 500
                """
                pending_rows = query_db(query, fetch=True)
                
                if pending_rows:
                    current_ts = time.time()
                    
                    # Safely move the pointer forward to the max ID found in this batch
                    last_processed_id = max(r['id'] for r in pending_rows)
                    
                    audio_ids = []
                    vision_ids =[]
                    
                    for r in pending_rows:
                        # --- CLOCK DRIFT IMMUNITY PATCH (V5.1) ---
                        # Use a generous 2-hour (7200s) TTL instead of 60s. 
                        # This easily absorbs normal Windows hardware clock drift,
                        # but still acts as a hard failsafe against extreme ancient backlogs.
                        if current_ts - r['timestamp'] > 7200:
                            continue 
                            
                        if r['detection_method'] in ('audio', 'multimodal'):
                            audio_ids.append(r['id'])
                        elif r['detection_method'] == 'vision':
                            vision_ids.append(r['id'])
                    
                    batch_data =[]
                    
                    while audio_ids or vision_ids:
                        if audio_ids and vision_ids:
                            if random.randint(1, 100) <= ratio:
                                chosen_id = audio_ids.pop(0)
                            else:
                                chosen_id = vision_ids.pop(0)
                        elif audio_ids:
                            chosen_id = audio_ids.pop(0)
                        else:
                            chosen_id = vision_ids.pop(0)
                            
                        batch_data.append((chosen_id, current_ts))
                        current_ts += 0.01 
                        
                    if batch_data:
                        insert_q = "INSERT INTO map_feed_broadcast (detection_id, release_timestamp) VALUES (?, ?) ON CONFLICT (detection_id) DO NOTHING"
                        query_db(insert_q, batch_data, fetch=False, many=True)
                        
            except Exception as e:
                pass
            
            time.sleep(3)

# ==============================================================================
# THE CLOUD JANITOR THREAD (48-Hour Time-Based)
# ==============================================================================
class CloudJanitorThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True
        
    def run(self):
        time.sleep(random.uniform(15, 30))
        logging.info("🧹 Cloud Janitor Thread started (Time-Based Mode).")
        
        while True:
            try:
                config = read_config()
                retention_hours = 48.0
                if config and "housekeeping" in config:
                    retention_hours = float(config["housekeeping"].get("legacy_log_retention_hours", 48.0))
                    
                safety_cutoff = time.time() - (retention_hours * 3600)
                
                # 1. Fetch Golden Anchors (Audio)
                try:
                    anchor_rows = query_db("SELECT baseline_detection_id FROM species_stream_profiles WHERE baseline_detection_id IS NOT NULL")
                    protected_ids = {str(r['baseline_detection_id']) for r in anchor_rows if r.get('baseline_detection_id')}
                except Exception as e:
                    logging.error(f"Janitor Anchor Query Error: {e}")
                    protected_ids = set()
                    
                # 2. Clean Audio Clips
                if CLIPS_DIR.exists():
                    deleted_audio = 0
                    audio_freed = 0
                    for f in CLIPS_DIR.glob("detection_*.*"):
                        if f.suffix in ['.mp3', '.wav']:
                            try:
                                parts = f.stem.split('_')
                                if len(parts) >= 2:
                                    clip_id = parts[1].split('.')[0]
                                    if clip_id not in protected_ids:
                                        if f.stat().st_mtime < safety_cutoff:
                                            audio_freed += f.stat().st_size
                                            f.unlink()
                                            deleted_audio += 1
                            except Exception:
                                pass
                    if deleted_audio > 0:
                        logging.info(f"🧹 Cloud Janitor: Wiped {deleted_audio} audio clips older than {retention_hours}h ({(audio_freed/1024/1024):.1f} MB freed). Protected {len(protected_ids)} Golden Anchors.")

                # 3. Clean Vision Snapshots
                if VAULT_DIR.exists():
                    deleted_vision = 0
                    vision_freed = 0
                    for f in VAULT_DIR.glob("*.jpg"):
                        if f.stat().st_mtime < safety_cutoff:
                            try:
                                vision_freed += f.stat().st_size
                                f.unlink()
                                deleted_vision += 1
                            except Exception:
                                pass
                    if deleted_vision > 0:
                        logging.info(f"🧹 Cloud Janitor: Wiped {deleted_vision} vision snapshots older than {retention_hours}h ({(vision_freed/1024/1024):.1f} MB freed).")
                        
            except Exception as e:
                logging.error(f"Cloud Janitor Master Loop Error: {e}")
                
            time.sleep(3600)

# --- THE MULTI-WORKER MUTEX PATCH ---
_thread_lock_socket = None

def start_background_tasks():
    global _thread_lock_socket
    
    try:
        # Use a socket bind as a cross-process Mutex lock. 
        # Only the first Gunicorn worker will succeed; others will silently fail.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('127.0.0.1', 55555)) 
        _thread_lock_socket = s # Keep reference to prevent GC from closing it
        
        janitor = CloudJanitorThread()
        janitor.start()

        master_gate = MasterGateThread()
        master_gate.start()
        
        logging.info("✅ Mutex Acquired: Background threads (Master Gate & Janitor) successfully started in this worker.")
    except socket.error:
        logging.info("ℹ️ Mutex Locked: Background threads are already running in another worker. Skipping.")

start_background_tasks()

if __name__ == '__main__':
    from waitress import serve
    logging.info("Starting GBR Cloud API Server v5.1 (The Floodgate Patch)...")
    serve(app, host="0.0.0.0", port=5000, threads=8)