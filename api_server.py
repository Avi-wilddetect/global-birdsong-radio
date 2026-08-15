# FILE: api_server.py
# VERSION: 6.1 - "The True Headless Patch"
# PURPOSE: Serves Web Map, Mobile API, HLS Proxy, securely accepts telemetry, and autonomously cleans Cloud Storage bloat.
# UPDATED: Completely removed residual http.server and MapLauncher classes that were crashing Gunicorn.

import sqlite3
import json
import logging
import os
import threading
import time
import re
import hashlib
import shutil
import traceback
import tempfile
import random
import socket
import base64
import io
from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlparse, quote, unquote, urljoin, quote_plus

from flask import Flask, jsonify, request, send_from_directory, Response, stream_with_context, make_response
from flask_bcrypt import Bcrypt
from flask_jwt_extended import create_access_token, get_jwt_identity, jwt_required, JWTManager

try:
    from PIL import Image
except ImportError as e:
    print(f"FATAL: A required library is missing (Pillow): {e}")
    import sys
    sys.exit(1)

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
PROXY_LOG_FILE = ROOT / "proxy_debug.txt"

CLIPS_DIR.mkdir(exist_ok=True)
VAULT_DIR.mkdir(exist_ok=True)

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
    if any(exc in name_lower for exc in BIRD_EXCEPTIONS): return True
    if any(kw in name_lower for kw in BIOACOUSTIC_KEYWORDS): return False
    return True

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

STREAM_REFERER_LOOKUP = {}
GLOBAL_DEBUG_ENABLED = False

def log_proxy(msg):
    if not GLOBAL_DEBUG_ENABLED: return
    try:
        timestamp = datetime.now().strftime('%H:%M:%S')
        with open(PROXY_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {msg}\n")
    except: pass

def sanitize_url_for_matching(raw_url):
    if not raw_url: return ""
    clean = raw_url.replace(r"\u0026", "&").replace(r"\/", "/")
    clean = re.sub(r'[\?&]variant=\d+', '', clean)
    return clean.strip()

def reload_config_state():
    global STREAM_REFERER_LOOKUP, GLOBAL_DEBUG_ENABLED
    if not CONFIG_FILE.exists(): return
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
        GLOBAL_DEBUG_ENABLED = config.get("debug_logging", False)
        for s in config.get('streams',[]):
            page = s.get('page_url', '').strip()
            orig = s.get('original_url', '').strip()
            if page and orig:
                STREAM_REFERER_LOOKUP[page] = orig
                sanitized = sanitize_url_for_matching(page)
                STREAM_REFERER_LOOKUP[sanitized] = orig
    except: pass

def perform_startup_audit():
    reload_config_state() 

# Call immediately on module load
perform_startup_audit()

def read_config():
    with config_lock:
        if not CONFIG_FILE.exists(): return None
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f: return json.load(f)

def query_db(query, params=(), fetch=True, many=False, force_local=False):
    config = read_config()
    is_cloud = config and config.get('database_cloud', {}).get('enabled', False)
    
    if is_cloud and not force_local:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        c_cfg = config['database_cloud']
        conn = None
        try:
            try:
                conn = psycopg2.connect(host='db', dbname=c_cfg.get('dbname'), user=c_cfg.get('user'), password=c_cfg.get('password'), port=c_cfg.get('port', 5432), connect_timeout=5)
            except psycopg2.OperationalError:
                conn = psycopg2.connect(host=c_cfg.get('host'), dbname=c_cfg.get('dbname'), user=c_cfg.get('user'), password=c_cfg.get('password'), port=c_cfg.get('port', 5432), connect_timeout=5)
            
            pg_query = query.replace('?', '%s')
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            if many: cur.executemany(pg_query, params)
            else:
                if isinstance(params, list): params = tuple(params)
                cur.execute(pg_query, params)
                
            if fetch: res =[dict(r) for r in cur.fetchall()]
            else:
                res = None
                if "RETURNING ID" in pg_query.upper():
                    row = cur.fetchone()
                    if row: res = row['id']
                else: res = cur.rowcount
                conn.commit()
            cur.close()
            return res
        finally:
            if conn:
                try: conn.close()
                except: pass
    else:
        conn = None
        try:
            conn = sqlite3.connect(DATABASE_PATH, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            cur = conn.cursor()
            
            if many: cur.executemany(query, params)
            else: cur.execute(query, params)
                
            if fetch: res =[dict(r) for r in cur.fetchall()]
            else:
                res = cur.lastrowid
                if query.strip().upper().startswith("UPDATE") or query.strip().upper().startswith("DELETE"): res = cur.rowcount
                conn.commit()
            cur.close()
            return res
        finally:
            if conn:
                try: conn.close()
                except: pass

def init_broadcast_table():
    config = read_config()
    is_cloud = config and config.get('database_cloud', {}).get('enabled', False)
    
    if is_cloud: query = "CREATE TABLE IF NOT EXISTS map_feed_broadcast (id SERIAL PRIMARY KEY, detection_id INTEGER UNIQUE, release_timestamp DOUBLE PRECISION)"
    else: query = "CREATE TABLE IF NOT EXISTS map_feed_broadcast (id INTEGER PRIMARY KEY AUTOINCREMENT, detection_id INTEGER UNIQUE, release_timestamp REAL)"
    try:
        query_db(query, fetch=False)
        count_res = query_db("SELECT COUNT(*) as c FROM map_feed_broadcast")
        if count_res and count_res[0]['c'] == 0:
            if is_cloud: query_db("INSERT INTO map_feed_broadcast (detection_id, release_timestamp) SELECT id, timestamp FROM detections ORDER BY timestamp DESC LIMIT 500 ON CONFLICT (detection_id) DO NOTHING", fetch=False)
            else: query_db("INSERT OR IGNORE INTO map_feed_broadcast (detection_id, release_timestamp) SELECT id, timestamp FROM detections ORDER BY timestamp DESC LIMIT 500", fetch=False)
        query_db("CREATE INDEX IF NOT EXISTS idx_broadcast_release ON map_feed_broadcast(release_timestamp)", fetch=False)
        query_db("CREATE INDEX IF NOT EXISTS idx_broadcast_detid ON map_feed_broadcast(detection_id)", fetch=False)
    except Exception as e: logging.error(f"Failed to create map_feed_broadcast table: {e}")

def generate_stable_id(url): return "id_" + hashlib.md5(url.encode('utf-8')).hexdigest()
def get_fuzzy_base(url):
    if not url: return ""
    if "youtube.com" in url or "youtu.be" in url: return url
    if "?" in url: return url.split('?')[0]
    return url

def _no_cache_json(data):
    response = jsonify(data)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

def get_db_connection_robust():
    if not DATABASE_PATH.exists(): return None, None
    try:
        temp_dir = Path(tempfile.gettempdir())
        temp_path = temp_dir / f"gbr_map_snap_{int(time.time())}.db"
        shutil.copy2(DATABASE_PATH, temp_path)
        con = sqlite3.connect(str(temp_path), timeout=5)
        con.row_factory = sqlite3.Row
        return con, temp_path
    except: return None, None

def cleanup_temp_db(con, temp_path):
    if con:
        try: con.close()
        except: pass
    if temp_path and os.path.exists(temp_path):
        try: os.unlink(temp_path)
        except: pass

def get_migration_target(species_name, db_con):
    try:
        cur = db_con.cursor()
        cur.execute("SELECT breeding_lat, breeding_lon, wintering_lat, wintering_lon FROM species_geography WHERE species_name = ?", (species_name,))
        row = cur.fetchone()
        if not row: return None
        month = datetime.now().month
        if 4 <= month <= 8: return {'origin': [row['wintering_lat'], row['wintering_lon']], 'target': [row['breeding_lat'], row['breeding_lon']]} if row['wintering_lat'] and row['breeding_lat'] else None
        else: return {'origin': [row['breeding_lat'], row['breeding_lon']], 'target': [row['wintering_lat'], row['wintering_lon']]} if row['breeding_lat'] and row['wintering_lat'] else None
    except: return None

def get_image_base64(species_name):
    con = None
    try:
        con = sqlite3.connect(str(IMAGE_DB_PATH), timeout=5)
        cur = con.cursor()
        cur.execute("SELECT image_data FROM species_images WHERE species_name = ?", (species_name,))
        row = cur.fetchone()
        con.close()
        if row and row[0] and row[0] != b"failed":
            img = Image.open(io.BytesIO(row[0]))
            img.thumbnail((400, 400)) 
            buffered = io.BytesIO()
            img.save(buffered, format="JPEG", quality=85)
            img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
            return f"data:image/jpeg;base64,{img_str}"
    except:
        if con: con.close()
    return None

def get_live_payload():
    try:
        config = read_config()
        if not config: return {"nodes": [], "feed": [], "error": "Config missing", "maintenance_mode": False, "map_settings": {}}
            
        streams_config = {s['page_url']: s for s in config.get('streams',[]) if s.get('enabled', True)}
        map_settings = config.get("map_settings", {})
        live_window = int(map_settings.get("live_window_seconds", 120))
        
        url_to_name = {} 
        url_to_coords = {}
        for s in config.get('streams',[]):
            name = s.get('name', 'Unknown Stream')
            raw_orig_lat = s.get('original_lat')
            raw_orig_lon = s.get('original_lon')
            if raw_orig_lat is not None and raw_orig_lon is not None: lat = float(raw_orig_lat); lon = float(raw_orig_lon)
            else: lat = float(s.get('lat') or 0.0); lon = float(s.get('lon') or 0.0)
            coords = (lat, lon)
            
            p_url = s.get('page_url', '')
            if p_url:
                san = sanitize_url_for_matching(p_url); fuz = get_fuzzy_base(p_url); fuz_san = get_fuzzy_base(san)
                url_to_name[p_url] = name; url_to_name[san] = name; url_to_name[fuz] = name; url_to_name[fuz_san] = name 
                url_to_coords[p_url] = coords; url_to_coords[san] = coords; url_to_coords[fuz] = coords; url_to_coords[fuz_san] = coords
                
            o_url = s.get('original_url', '')
            if o_url:
                san = sanitize_url_for_matching(o_url); fuz = get_fuzzy_base(o_url); fuz_san = get_fuzzy_base(san)
                url_to_name[o_url] = name; url_to_name[san] = name; url_to_name[fuz] = name; url_to_name[fuz_san] = name 
                url_to_coords[o_url] = coords; url_to_coords[san] = coords; url_to_coords[fuz] = coords; url_to_coords[fuz_san] = coords

        private_species = set()
        if VISION_TARGETS_FILE.exists():
            try:
                v_data = json.loads(VISION_TARGETS_FILE.read_text(encoding='utf-8'))
                for sp, details in v_data.get("registry", {}).items():
                    if not details.get("show_on_map", True): private_species.add(sp.strip())
            except: pass

        con, temp_db_path = get_db_connection_robust()
        is_cloud = config.get('database_cloud', {}).get('enabled', False)
        alert_cond_sql = "d.alert_sent = TRUE" if is_cloud else "d.alert_sent = 1"
        
        priv_list = list(private_species)
        if priv_list:
            placeholders = ','.join('?' for _ in priv_list)
            where_d = f"WHERE {alert_cond_sql} AND d.species NOT IN ({placeholders})"
            params_latest = tuple(priv_list); params_feed = tuple(priv_list)
        else:
            where_d = f"WHERE {alert_cond_sql}"
            params_latest = (); params_feed = ()
            
        query_latest_raw = f"SELECT d.id, d.channel_url, d.species, d.timestamp, b.release_timestamp, d.distance_category, d.detection_method, d.frame_size, d.filter_reason, d.ai_notes FROM map_feed_broadcast b JOIN detections d ON b.detection_id = d.id {where_d} ORDER BY b.release_timestamp DESC LIMIT 2000"
        feed_capacity = int(map_settings.get("target_sidebar_capacity", 50))
        query_feed = f"SELECT d.id, d.species, d.channel_url, d.timestamp, b.release_timestamp, d.distance_category, d.detection_method, d.frame_size, d.filter_reason, d.ai_notes FROM map_feed_broadcast b JOIN detections d ON b.detection_id = d.id {where_d} ORDER BY b.release_timestamp DESC LIMIT {feed_capacity}"

        try:
            raw_latest = query_db(query_latest_raw, params_latest)
            feed_rows = query_db(query_feed, params_feed)
        except Exception as e:
            if "relation \"map_feed_broadcast\" does not exist" in str(e) or "no such table" in str(e):
                init_broadcast_table()
                try:
                    raw_latest = query_db(query_latest_raw, params_latest); feed_rows = query_db(query_feed, params_feed)
                except Exception as retry_e:
                    cleanup_temp_db(con, temp_db_path); return {"nodes": [], "feed":[], "error": f"SQL Error: {retry_e}", "maintenance_mode": False, "map_settings": map_settings}
            else:
                cleanup_temp_db(con, temp_db_path); return {"nodes": [], "feed":[], "error": f"SQL Error: {e}", "maintenance_mode": False, "map_settings": map_settings}

        latest_rows = []
        seen_audio_keys = set(); seen_vision_keys = set()
        
        for r in raw_latest:
            key = (r['channel_url'], r['species'])
            if r['detection_method'] in ('audio', 'multimodal'):
                if key not in seen_audio_keys: seen_audio_keys.add(key); latest_rows.append(r)
            elif r['detection_method'] == 'vision':
                if key not in seen_vision_keys: seen_vision_keys.add(key); latest_rows.append(r)

        target_audio_pct = map_settings.get("audio_vision_ratio", 70)
        target_map_capacity = int(map_settings.get("target_map_capacity", 50))
        
        def apply_dynamic_slots(rows, total_capacity, audio_pct):
            target_audio_slots = int(total_capacity * (audio_pct / 100.0))
            target_vision_slots = total_capacity - target_audio_slots
            audio_rows = [r for r in rows if r['detection_method'] in ('audio', 'multimodal')]
            vision_rows =[r for r in rows if r['detection_method'] == 'vision']
            a_count = len(audio_rows); v_count = len(vision_rows)
            allowed_a = target_audio_slots; allowed_v = target_vision_slots
            
            if a_count < target_audio_slots: allowed_v += target_audio_slots - a_count; allowed_a = a_count
            elif v_count < target_vision_slots: allowed_a += target_vision_slots - v_count; allowed_v = v_count
                
            audio_rows.sort(key=lambda x: x.get('release_timestamp') or x['timestamp'], reverse=True)
            vision_rows.sort(key=lambda x: x.get('release_timestamp') or x['timestamp'], reverse=True)
            kept_rows = audio_rows[:allowed_a] + vision_rows[:allowed_v]
            kept_rows.sort(key=lambda x: x.get('release_timestamp') or x['timestamp'], reverse=True)
            return kept_rows

        latest_rows = apply_dynamic_slots(latest_rows, target_map_capacity, target_audio_pct)
        feed_rows = feed_rows[:feed_capacity]

        latest_detections = {}; latest_detections_base = {}
        for row in latest_rows:
            row_time = row.get('release_timestamp') or row['timestamp']
            key = (row['channel_url'], row['species'])
            key_base = (get_fuzzy_base(row['channel_url']), row['species'])
            
            if key not in latest_detections or row_time > (latest_detections[key].get('release_timestamp') or latest_detections[key]['timestamp']):
                latest_detections[key] = row
            if key_base not in latest_detections_base or row_time > (latest_detections_base[key_base].get('release_timestamp') or latest_detections_base[key_base]['timestamp']):
                latest_detections_base[key_base] = row 

        url_to_active_dets = defaultdict(list)
        for (u, sp), row in latest_detections.items(): url_to_active_dets[u].append(row)
        for (u_base, sp), row in latest_detections_base.items(): url_to_active_dets[u_base].append(row)
        
        anchor_rows = query_db("SELECT stream_url, species_name, baseline_detection_id FROM species_stream_profiles WHERE baseline_detection_id IS NOT NULL")
        anchor_map = {(r['stream_url'], r['species_name']): r['baseline_detection_id'] for r in anchor_rows}
        
        def get_valid_audio_id(stream_url, species, current_det_id=None):
            if current_det_id and ((CLIPS_DIR / f"detection_{current_det_id}.mp3").exists() or (CLIPS_DIR / f"detection_{current_det_id}.wav").exists()): return current_det_id
            anchor_id = anchor_map.get((stream_url, species))
            if not anchor_id: return None
            if (CLIPS_DIR / f"detection_{anchor_id}.mp3").exists() or (CLIPS_DIR / f"detection_{anchor_id}.wav").exists(): return anchor_id
            return None
        
        nodes =[]; now = time.time(); name_to_canonical = {}
        for url, conf in streams_config.items():
            lat, lon = url_to_coords.get(url, (0.0, 0.0))
            clean_url = sanitize_url_for_matching(url)
            explicit_type = conf.get('stream_type'); human_url = conf.get('original_url') or clean_url
            yt_id = None; audio_url = None; stream_type = 'unknown'
            
            if explicit_type:
                stream_type = explicit_type
                if stream_type == 'youtube' or stream_type == 'video':
                    match = re.search(r'(?:v=|\/live\/|\/embed\/|\/v\/|youtu\.be\/)([^&?#\/]+)', clean_url)
                    yt_id = match.group(1) if match else None
                elif stream_type in['hls', 'direct_stream', 'audio']: audio_url = clean_url
            else:
                if "youtu" in clean_url.lower():
                    stream_type = 'video'
                    match = re.search(r'(?:v=|\/live\/|\/embed\/|\/v\/|youtu\.be\/)([^&?#\/]+)', clean_url)
                    yt_id = match.group(1) if match else None
                elif ".m3u8" in clean_url: stream_type = 'hls'; audio_url = clean_url
                else: stream_type = 'audio'; audio_url = clean_url

            if ".m3u8" in clean_url and stream_type == 'audio': stream_type = 'hls'
            name_lower = conf['name'].lower()
            if "(audio" in name_lower or "(mp3" in name_lower: stream_type = 'audio'; audio_url = clean_url
            
            camera_base_id = generate_stable_id(clean_url)
            name_to_canonical[conf['name']] = {'id': camera_base_id, 'type': stream_type}

            dets_for_cam = (url_to_active_dets.get(url) or url_to_active_dets.get(clean_url) or url_to_active_dets.get(get_fuzzy_base(url)) or url_to_active_dets.get(get_fuzzy_base(clean_url)) or [])

            unique_dets = {}
            for d in dets_for_cam:
                if d['species'] not in unique_dets or (d.get('release_timestamp') or d['timestamp']) > (unique_dets[d['species']].get('release_timestamp') or unique_dets[d['species']]['timestamp']):
                    unique_dets[d['species']] = d

            if not unique_dets:
                nodes.append({'id': camera_base_id, 'camera_id': camera_base_id, 'name': conf['name'], 'lat': lat, 'lon': lon, 'yt_id': yt_id, 'audio_url': audio_url, 'page_url': clean_url, 'external_url': human_url, 'stream_type': stream_type, 'species': 'Online', 'status': 'listening', 'time_ago_str': 'Scanning...', 'image': None, 'links': {'wiki': '#', 'secondary': '#', 'secondary_name': 'ALL ABOUT BIRDS'}, 'audio_id': None, 'detection_method': 'audio', 'dist': 'Unknown', 'frame_size': 'N/A', 'filter_reason': '', 'ai_notes': ''})
            else:
                for sp, det in unique_dets.items():
                    norm_ts = det.get('release_timestamp') or det['timestamp']
                    diff = max(0, now - norm_ts)
                    unique_node_id = generate_stable_id(clean_url + "_" + sp)
                    node = {'id': unique_node_id, 'camera_id': camera_base_id, 'name': conf['name'], 'lat': lat, 'lon': lon, 'yt_id': yt_id, 'audio_url': audio_url, 'page_url': clean_url, 'external_url': human_url, 'stream_type': stream_type, 'species': sp, 'audio_id': get_valid_audio_id(det['channel_url'], sp, det['id']), 'detection_method': det.get('detection_method', 'audio'), 'dist': det.get('distance_category', 'Unknown'), 'frame_size': det.get('frame_size', 'N/A'), 'filter_reason': det.get('filter_reason', ''), 'ai_notes': det.get('ai_notes', '')}
                    safe_name = sp.replace(" ", "_")
                    is_b = is_bird(sp)
                    sec_url = f"https://www.allaboutbirds.org/guide/{quote(sp)}" if is_b else f"https://www.inaturalist.org/search?q={quote_plus(sp)}"
                    node['links'] = {'wiki': f"https://en.wikipedia.org/wiki/{safe_name}", 'secondary': sec_url, 'secondary_name': "ALL ABOUT BIRDS" if is_b else "INATURALIST DATABASE"}
                    
                    if diff < live_window: node['status'] = 'live'; node['time_ago_str'] = 'LIVE NOW'
                    elif diff < 3600: node['status'] = 'recent'; node['time_ago_str'] = f"{int(diff/60)}m ago"
                    else: node['status'] = 'dormant'; node['time_ago_str'] = f"{int(diff/3600)}h ago"
                    node['image'] = f"/api/image/{quote(sp)}" 
                    if con:
                        vectors = get_migration_target(sp, con)
                        if vectors: node['mig_target'] = vectors['target']; node['mig_origin'] = vectors['origin']
                    nodes.append(node)

        feed =[]
        for f in feed_rows:
            norm_ts = f.get('release_timestamp') or f['timestamp']
            diff = max(0, now - norm_ts)
            urgency = 'red' if diff < live_window else 'orange' if diff < 3600 else 'grey'
            raw_url = f['channel_url']
            clean_url = sanitize_url_for_matching(raw_url)
            c_lat, c_lon = (url_to_coords.get(raw_url) or url_to_coords.get(clean_url) or url_to_coords.get(get_fuzzy_base(raw_url)) or url_to_coords.get(get_fuzzy_base(clean_url)) or (0.0, 0.0))
            stream_name = (url_to_name.get(raw_url) or url_to_name.get(clean_url) or url_to_name.get(get_fuzzy_base(raw_url)) or url_to_name.get(get_fuzzy_base(clean_url)) or "Unknown Stream")
            
            if stream_name in name_to_canonical:
                cid = name_to_canonical[stream_name]['id']
                stype = name_to_canonical[stream_name]['type']
            else:
                cid = generate_stable_id(raw_url)
                stype = 'unknown' 
                
            unique_tid = generate_stable_id(clean_url + "_" + f['species'])
                
            has_mig = False
            if con: has_mig = get_migration_target(f['species'], con) is not None
            feed.append({'audio_id': get_valid_audio_id(raw_url, f['species'], f['id']), 'timestamp': norm_ts, 'species': f['species'], 'time': datetime.fromtimestamp(norm_ts).strftime('%H:%M'), 'stream': stream_name, 'stream_type': stype, 'dist': f['distance_category'] or "?", 'target_id': unique_tid, 'camera_id': cid, 'lat': c_lat, 'lon': c_lon, 'urgency': urgency, 'is_close': "Near" in (f['distance_category'] or ""), 'has_migration': has_mig, 'detection_method': f.get('detection_method', 'audio'), 'frame_size': f.get('frame_size', 'N/A'), 'filter_reason': f.get('filter_reason', ''), 'ai_notes': f.get('ai_notes', '')})

        cleanup_temp_db(con, temp_db_path)
        return {"nodes": nodes, "feed": feed, "maintenance_mode": config.get('maintenance_mode', False), "map_settings": map_settings}
    except Exception as e:
        traceback.print_exc()
        return {"nodes": [], "feed": [], "error": f"Critical: {e}", "maintenance_mode": False, "map_settings": {}}

# ==============================================================================
# FLASK ROUTES
# ==============================================================================

@app.route('/')
def root(): return send_from_directory(ROOT, 'map_template.html')

@app.route('/data')
def map_data(): return _no_cache_json(get_live_payload())

@app.route('/proxy')
def proxy_stream():
    target_url_raw = request.args.get('url')
    passed_referer_b64 = request.args.get('r')
    if not target_url_raw:
        return "Missing URL", 400

    target_url = sanitize_url_for_matching(target_url_raw)
    human_referer = ""
    if passed_referer_b64:
        try: human_referer = base64.b64decode(passed_referer_b64).decode('utf-8')
        except: pass
    else:
        if target_url in STREAM_REFERER_LOOKUP:
            human_referer = STREAM_REFERER_LOOKUP[target_url]
        elif target_url_raw in STREAM_REFERER_LOOKUP:
            human_referer = STREAM_REFERER_LOOKUP[target_url_raw]
        else:
            for k, v in STREAM_REFERER_LOOKUP.items():
                if k in target_url: 
                    human_referer = v
                    break

    root_referer = ""
    if human_referer:
        p_ref = urlparse(human_referer)
        root_referer = f"{p_ref.scheme}://{p_ref.netloc}/"
    elif target_url:
        p_t = urlparse(target_url)
        root_referer = f"{p_t.scheme}://{p_t.netloc}/"

    strategies =[]
    if human_referer: strategies.append(("STRICT", human_referer))
    if root_referer and root_referer != human_referer: strategies.append(("ROOT", root_referer))
    strategies.append(("EMPTY", ""))

    final_resp = None
    for label, ref_val in strategies:
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Connection': 'keep-alive'
            }
            if ref_val: headers['Referer'] = ref_val
            
            r = requests.get(target_url, headers=headers, stream=True, timeout=8, verify=False, allow_redirects=True)
            if 200 <= r.status_code < 400:
                final_resp = r
                break
        except Exception as e:
            continue

    if not final_resp:
        return "All proxy strategies failed", 502
        
    ct = final_resp.headers.get('Content-Type', '')
    is_playlist = 'mpegurl' in ct or target_url.endswith('.m3u8')
    
    if is_playlist:
        content_bytes = final_resp.content 
        content_str = content_bytes.decode('utf-8', errors='ignore')
        if not is_playlist and '#EXTM3U' in content_str: is_playlist = True
        
        final_url = final_resp.url
        base_url = final_url.rsplit('/', 1)[0] + '/'
        
        ref_to_pass = human_referer if human_referer else root_referer
        ref_param = ""
        if ref_to_pass:
            b64_ref = base64.b64encode(ref_to_pass.encode('utf-8')).decode('utf-8')
            ref_param = f"&r={b64_ref}"
        
        new_lines =[]
        for line in content_str.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                new_lines.append(line)
            else:
                full_link = urljoin(base_url, line)
                proxied_link = f"/proxy?url={quote_plus(full_link)}{ref_param}"
                new_lines.append(proxied_link)
        
        resp = Response('\n'.join(new_lines), content_type=ct)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    else:
        def generate():
            for chunk in final_resp.iter_content(chunk_size=8192):
                if chunk: yield chunk
        resp = Response(stream_with_context(generate()), content_type=ct)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp

@app.route('/api/inject_detection', methods=['POST'])
def route_inject_detection():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    if not server_token or request.form.get('secret_token', "") != server_token: return "Unauthorized", 401
    try:
        timestamp = float(request.form.get('timestamp', time.time()))
        url = request.form.get('channel_url'); species = request.form.get('species')
        dist = request.form.get('distance_category'); snr = float(request.form.get('snr', 0.0))
        alert_sent = request.form.get('alert_sent', 'false').lower() == 'true'
        listener_id = request.form.get('listener_id'); network_interface = request.form.get('network_interface')
        detection_method = request.form.get('detection_method') or ("multimodal" if dist and "Multimodal" in dist else "vision" if listener_id == "VISION_ENGINE" else "audio")
        if not url or not species: return "Missing fields", 400
        lat = float(request.form.get('latitude')) if request.form.get('latitude') else None
        lon = float(request.form.get('longitude')) if request.form.get('longitude') else None
        det_id = query_db("INSERT INTO detections (timestamp, channel_url, species, latitude, longitude, distance_category, snr, alert_sent, listener_id, network_interface, detection_method, frame_size, filter_reason, ai_notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id", (timestamp, url, species, lat, lon, dist, snr, alert_sent, listener_id, network_interface, detection_method, request.form.get('frame_size', 'N/A'), request.form.get('filter_reason', ''), request.form.get('ai_notes', '')), fetch=False)
        if 'audio_file' in request.files and det_id: request.files['audio_file'].save(str(CLIPS_DIR / f"detection_{det_id}.mp3"))
        return jsonify({"status": "success", "detection_id": det_id}), 200
    except Exception as e: return str(e), 500

@app.route('/api/upgrade_multimodal', methods=['POST'])
def route_upgrade_multimodal():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    if not server_token or request.form.get('secret_token', "") != server_token: return "Unauthorized", 401
    try:
        if not request.form.get('original_timestamp') or not request.form.get('channel_url'): return "Missing fields", 400
        orig_ts = float(request.form.get('original_timestamp')); t_min = orig_ts - 15.0; t_max = orig_ts + 15.0
        rowcount = query_db("UPDATE detections SET detection_method = 'multimodal', distance_category = ?, frame_size = ?, filter_reason = ?, ai_notes = ?, alert_sent = alert_sent OR ? WHERE id IN (SELECT id FROM detections WHERE channel_url = ? AND timestamp >= ? AND timestamp <= ? ORDER BY ABS(timestamp - ?) ASC LIMIT 1)", (request.form.get('distance_category'), request.form.get('frame_size', 'N/A'), request.form.get('filter_reason', ''), request.form.get('ai_notes', ''), request.form.get('alert_sent', 'false').lower() == 'true', request.form.get('channel_url'), t_min, t_max, orig_ts), fetch=False)
        if rowcount == 0: return jsonify({"status": "failed", "message": "Record not found."}), 404
        return jsonify({"status": "success"}), 200
    except Exception as e: return str(e), 500

@app.route('/api/migrate_detection_urls', methods=['POST'])
def route_migrate_detection_urls():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    if not server_token or request.form.get('secret_token', "") != server_token: return "Unauthorized", 401
    try:
        rowcount = query_db("UPDATE detections SET channel_url = ? WHERE channel_url = ?", (request.form.get('new_url'), request.form.get('old_url')), fetch=False)
        return jsonify({"status": "success", "message": f"Migrated {rowcount} records."}), 200
    except Exception as e: return str(e), 500

@app.route('/api/curate_detection', methods=['POST'])
def route_curate_detection():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    if not server_token or request.form.get('secret_token', "") != server_token: return "Unauthorized", 401
    try:
        ts = float(request.form.get('timestamp')); t_min = ts - 15.0; t_max = ts + 15.0
        rowcount = query_db("UPDATE detections SET species = ? WHERE id IN (SELECT id FROM detections WHERE channel_url = ? AND timestamp >= ? AND timestamp <= ? ORDER BY ABS(timestamp - ?) ASC LIMIT 1)", (request.form.get('new_species'), request.form.get('channel_url'), t_min, t_max, ts), fetch=False)
        if rowcount == 0: return jsonify({"status": "failed"}), 404
        return jsonify({"status": "success"}), 200
    except Exception as e: return str(e), 500

@app.route('/api/delete_detection', methods=['POST'])
def route_delete_detection():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    if not server_token or request.form.get('secret_token', "") != server_token: return "Unauthorized", 401
    try:
        ts = float(request.form.get('timestamp')); t_min = ts - 15.0; t_max = ts + 15.0
        rowcount = query_db("DELETE FROM detections WHERE id IN (SELECT id FROM detections WHERE channel_url = ? AND timestamp >= ? AND timestamp <= ? ORDER BY ABS(timestamp - ?) ASC LIMIT 1)", (request.form.get('channel_url'), t_min, t_max, ts), fetch=False)
        if rowcount == 0: return jsonify({"status": "failed"}), 404
        return jsonify({"status": "success"}), 200
    except Exception as e: return str(e), 500

@app.route('/api/upload_audio', methods=['POST'])
def route_upload_audio():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    if not server_token or request.form.get('secret_token', "") != server_token: return "Unauthorized", 401
    if 'audio_file' not in request.files: return "No file part", 400
    try:
        request.files['audio_file'].save(str(CLIPS_DIR / f"detection_{request.form.get('detection_id')}.mp3"))
        return "Upload successful", 200
    except Exception as e: return str(e), 500

@app.route('/api/upload_image', methods=['POST'])
def route_upload_image():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    if not server_token or request.form.get('secret_token', "") != server_token: return "Unauthorized", 401
    if 'image_file' not in request.files: return "No file", 400
    try:
        query_db("INSERT INTO species_images (species_name, image_data, source_url, last_updated, status) VALUES (?, ?, ?, ?, ?) ON CONFLICT (species_name) DO UPDATE SET image_data = EXCLUDED.image_data, source_url = EXCLUDED.source_url, last_updated = EXCLUDED.last_updated, status = EXCLUDED.status", (request.form.get('species_name'), request.files['image_file'].read(), request.form.get('source_url', ''), time.strftime('%Y-%m-%d %H:%M:%S'), request.form.get('status', 'OK')), fetch=False)
        return "Upload successful", 200
    except Exception as e: return str(e), 500

@app.route('/api/delete_image', methods=['POST'])
def route_delete_image():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    if not server_token or request.form.get('secret_token', "") != server_token: return "Unauthorized", 401
    try:
        query_db("DELETE FROM species_images WHERE species_name = ?", (request.form.get('species_name'),), fetch=False)
        return "Delete successful", 200
    except Exception as e: return str(e), 500

@app.route('/api/upload_config', methods=['POST'])
def route_upload_config():
    config = read_config()
    server_token = config.get("bot_token", "") if config else ""
    if not server_token or request.form.get('secret_token', "") != server_token: return "Unauthorized", 401
    if 'config_file' not in request.files: return "No file", 400
    try:
        file_content = request.files['config_file'].read().decode('utf-8')
        json.loads(file_content) 
        with config_lock:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f: f.write(file_content)
        return "Config updated successfully", 200
    except Exception as e: return str(e), 500

@app.route('/api/login', methods=['POST'])
def route_login_user():
    data = request.get_json()
    try:
        rows = query_db("SELECT id, password_hash FROM users WHERE email = ?", (data.get('email'),))
        if rows and bcrypt.check_password_hash(rows[0]['password_hash'], data.get('password')): return jsonify(access_token=create_access_token(identity=str(rows[0]['id'])))
    except: pass
    return jsonify({"error": "Invalid"}), 401

@app.route('/api/streams', methods=['GET'])
def route_get_streams():
    cfg = read_config()
    return _no_cache_json(cfg.get('streams',[])) if cfg else _no_cache_json([])

@app.route('/api/audio/<path:filename>')
def serve_audio_route(filename):
    if (CLIPS_DIR / filename).exists(): return send_from_directory(CLIPS_DIR, filename, mimetype='audio/wav' if filename.endswith('.wav') else 'audio/mpeg')
    return jsonify({"error": "File not found"}), 404

@app.route('/audio/<path:filename>')
def serve_audio_web(filename): return serve_audio_route(filename)

@app.route('/api/image/<path:species_name>')
def serve_image_route(species_name):
    try:
        rows = query_db("SELECT image_data FROM species_images WHERE species_name = ?", (unquote(species_name),))
        if rows and rows[0].get('image_data'):
            img_data = rows[0]['image_data']
            if isinstance(img_data, memoryview): img_data = bytes(img_data)
            if img_data and img_data != b"failed" and len(img_data) > 50: return Response(img_data, mimetype='image/jpeg')
        return Response(FALLBACK_SVG, mimetype='image/svg+xml')
    except: return Response(FALLBACK_SVG, mimetype='image/svg+xml')

# ==============================================================================
# BACKGROUND THREADS (CLOUD NODE)
# ==============================================================================

class MasterGateThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True

    def run(self):
        time.sleep(15)
        init_broadcast_table()
        last_processed_id = 0
        try:
            max_res = query_db("SELECT MAX(id) as m FROM detections")
            if max_res and max_res[0]['m']: last_processed_id = int(max_res[0]['m'])
        except: pass

        while True:
            try:
                config = read_config()
                if not config: time.sleep(2); continue
                ratio = config.get("map_settings", {}).get("audio_vision_ratio", 70)
                is_cloud = config.get('database_cloud', {}).get('enabled', False)
                alert_cond_sql = "alert_sent = TRUE" if is_cloud else "alert_sent = 1"
                pending_rows = query_db(f"SELECT id, detection_method, timestamp FROM detections WHERE id > {last_processed_id} AND {alert_cond_sql} ORDER BY id ASC LIMIT 500", fetch=True)
                if pending_rows:
                    current_ts = time.time()
                    last_processed_id = max(r['id'] for r in pending_rows)
                    audio_ids = []; vision_ids =[]
                    for r in pending_rows:
                        if current_ts - r['timestamp'] > 7200: continue 
                        if r['detection_method'] in ('audio', 'multimodal'): audio_ids.append(r['id'])
                        elif r['detection_method'] == 'vision': vision_ids.append(r['id'])
                    batch_data =[]
                    while audio_ids or vision_ids:
                        if audio_ids and vision_ids:
                            chosen_id = audio_ids.pop(0) if random.randint(1, 100) <= ratio else vision_ids.pop(0)
                        elif audio_ids: chosen_id = audio_ids.pop(0)
                        else: chosen_id = vision_ids.pop(0)
                        batch_data.append((chosen_id, current_ts)); current_ts += 0.01 
                    if batch_data: query_db("INSERT INTO map_feed_broadcast (detection_id, release_timestamp) VALUES (?, ?) ON CONFLICT (detection_id) DO NOTHING", batch_data, fetch=False, many=True)
            except: pass
            time.sleep(3)

class CloudJanitorThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True
        
    def run(self):
        time.sleep(random.uniform(15, 30))
        while True:
            try:
                config = read_config()
                retention_hours = float(config["housekeeping"].get("legacy_log_retention_hours", 48.0)) if config and "housekeeping" in config else 48.0
                safety_cutoff = time.time() - (retention_hours * 3600)
                try: protected_ids = {str(r['baseline_detection_id']) for r in query_db("SELECT baseline_detection_id FROM species_stream_profiles WHERE baseline_detection_id IS NOT NULL") if r.get('baseline_detection_id')}
                except: protected_ids = set()
                if CLIPS_DIR.exists():
                    for f in CLIPS_DIR.glob("detection_*.*"):
                        if f.suffix in ['.mp3', '.wav']:
                            try:
                                parts = f.stem.split('_')
                                if len(parts) >= 2 and parts[1].split('.')[0] not in protected_ids and f.stat().st_mtime < safety_cutoff: f.unlink()
                            except: pass
                if VAULT_DIR.exists():
                    for f in VAULT_DIR.glob("*.jpg"):
                        if f.stat().st_mtime < safety_cutoff:
                            try: f.unlink()
                            except: pass
            except: pass
            time.sleep(3600)

_thread_lock_socket = None

def start_background_tasks():
    global _thread_lock_socket
    try:
        # Lock port 55555. Only one Gunicorn worker will succeed and spawn the background threads.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('127.0.0.1', 55555)) 
        _thread_lock_socket = s 
        CloudJanitorThread().start()
        MasterGateThread().start()
    except socket.error:
        # Port already in use by another worker, silently abort.
        pass

# Spawn the background threads dynamically upon module import by Gunicorn
start_background_tasks()

if __name__ == '__main__':
    # Local dev mode via Waitress if executed directly (e.g. python api_server.py)
    from waitress import serve
    serve(app, host="0.0.0.0", port=5000, threads=8)