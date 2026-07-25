# FILE: world_map_app.py
# VERSION: 39.1 - "The Multimodal Hub (Patch 1)"
# UPDATED: Fixed sqlite3.Row dict casting to prevent .get() crashes.

import sys
import json
import logging
import webbrowser
import sqlite3
import time
import base64
import io
import os
import threading
import http.server
import socketserver
import socket
import hashlib
import shutil
import traceback
import tempfile
import re
from pathlib import Path
from datetime import datetime
from urllib.parse import quote_plus, urlparse, urljoin, parse_qs, unquote
from collections import Counter

try:
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
except ImportError:
    print("FATAL: requests library missing.")
    sys.exit(1)

# --- NUCLEAR GPU DISABLE ---
sys.argv.append("--disable-gpu")
sys.argv.append("--disable-software-rasterizer")
sys.argv.append("--disable-gpu-compositing")
sys.argv.append("--disable-accelerated-2d-canvas")
sys.argv.append("--disable-d3d11") 

try:
    from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QPushButton, QLabel, QMessageBox)
    from PyQt6.QtCore import Qt
    from PIL import Image
except ImportError as e:
    print(f"FATAL: A required library is missing: {e}")
    sys.exit(1)

# --- Configuration ---
ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "birdnet_config.json"
DATABASE_FILE = ROOT / "detections.db"
IMAGE_DB_PATH = ROOT / "image_database.db"
TEMPLATE_FILE = ROOT / "map_template.html"
CLIPS_DIR = ROOT / "baseline_clips"
PROXY_LOG_FILE = ROOT / "proxy_debug.txt"
VISION_TARGETS_FILE = ROOT / "vision_targets.json"

# --- Logging ---
log_file = ROOT / "monitor_debug.txt"
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[logging.FileHandler(log_file, mode='a'), logging.StreamHandler()])

# --- GLOBAL STATE ---
STREAM_REFERER_LOOKUP = {}
GLOBAL_DEBUG_ENABLED = False

def log_proxy(msg):
    if not GLOBAL_DEBUG_ENABLED:
        return
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

# --- Database & Data Logic ---
def get_db_connection_robust():
    if not DATABASE_FILE.exists(): return None, None
    try:
        temp_dir = Path(tempfile.gettempdir())
        temp_path = temp_dir / f"gbr_map_snap_{int(time.time())}.db"
        shutil.copy2(DATABASE_FILE, temp_path)
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

def generate_stable_id(url):
    return "id_" + hashlib.md5(url.encode('utf-8')).hexdigest()

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

def get_live_payload():
    reload_config_state()
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            
        streams_config = {s['page_url']: s for s in config.get('streams',[]) if s.get('enabled', True)}
        map_settings = config.get("map_settings", {})
        live_window = int(map_settings.get("live_window_seconds", 120))
        
        url_to_name = {} 
        for s in config.get('streams',[]):
            name = s.get('name', 'Unknown Stream')
            p_url = s.get('page_url', '')
            if p_url:
                url_to_name[p_url] = name
                url_to_name[sanitize_url_for_matching(p_url)] = name
            o_url = s.get('original_url', '')
            if o_url:
                url_to_name[o_url] = name
                url_to_name[sanitize_url_for_matching(o_url)] = name

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

        con, temp_db_path = get_db_connection_robust()
        latest_detections = {}
        feed_rows =[]
        
        if con:
            try:
                cur = con.cursor()
                
                priv_list = list(private_species)
                if priv_list:
                    placeholders = ','.join('?' for _ in priv_list)
                    where_clause = f"WHERE species NOT IN ({placeholders})"
                    
                    query_latest = f"""
                        SELECT t1.id, t1.channel_url, t1.species, t1.timestamp, t1.distance_category, t1.detection_method 
                        FROM detections t1 
                        JOIN (SELECT channel_url, MAX(timestamp) as max_ts FROM detections {where_clause} GROUP BY channel_url) t2 
                        ON t1.channel_url = t2.channel_url AND t1.timestamp = t2.max_ts
                        {where_clause.replace("WHERE ", "WHERE t1.")}
                    """
                    params_latest = tuple(priv_list) + tuple(priv_list)
                    
                    query_feed = f"SELECT id, species, channel_url, timestamp, distance_category, detection_method FROM detections {where_clause} ORDER BY timestamp DESC LIMIT 50"
                    params_feed = tuple(priv_list)
                    
                else:
                    query_latest = """
                        SELECT t1.id, t1.channel_url, t1.species, t1.timestamp, t1.distance_category, t1.detection_method 
                        FROM detections t1 
                        JOIN (SELECT channel_url, MAX(timestamp) as max_ts FROM detections GROUP BY channel_url) t2 
                        ON t1.channel_url = t2.channel_url AND t1.timestamp = t2.max_ts
                    """
                    params_latest = ()
                    query_feed = "SELECT id, species, channel_url, timestamp, distance_category, detection_method FROM detections ORDER BY timestamp DESC LIMIT 50"
                    params_feed = ()

                cur.execute(query_latest, params_latest)
                # CRITICAL FIX: Forcefully cast sqlite3.Row to standard Python Dictionary
                latest_detections = {row['channel_url']: {k: row[k] for k in row.keys()} for row in cur.fetchall()}
                
                cur.execute(query_feed, params_feed)
                # CRITICAL FIX: Same here for the feed list
                feed_rows = [{k: row[k] for k in row.keys()} for row in cur.fetchall()]
                
            except Exception as e:
                logging.error(f"SQL Error in map payload: {e}")
                cleanup_temp_db(con, temp_db_path); return {"nodes": [], "feed":[], "error": f"SQL Error: {e}"}
        else: 
            return {"nodes":[], "feed":[], "error": "DB Error"}

        species_counts = Counter([r['species'] for r in feed_rows])
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

            stable_id = generate_stable_id(clean_url)
            
            name_to_canonical[conf['name']] = {
                'id': stable_id,
                'type': stream_type
            }

            node = {
                'id': stable_id,
                'name': conf['name'],
                'lat': lat, 'lon': lon,
                'yt_id': yt_id, 
                'audio_url': audio_url,
                'page_url': clean_url,
                'external_url': human_url,
                'stream_type': stream_type,
                'species': 'Online', 'status': 'listening', 'time_ago_str': 'Scanning...',
                'detection_method': 'audio', 'dist': 'Unknown',
                'image': None, 'links': {'wiki': '#', 'aab': '#'}, 
                'mig_target': None, 'mig_origin': None, 'mig_volume': 0, 'audio_id': None
            }

            det = latest_detections.get(url) or latest_detections.get(clean_url)
            
            if det:
                diff = now - det['timestamp']
                node['species'] = det['species']
                node['audio_id'] = det['id']
                node['detection_method'] = det.get('detection_method') or 'audio'
                node['dist'] = det.get('distance_category') or 'Unknown'
                
                safe_name_wiki = det['species'].replace(" ", "_")
                node['links']['wiki'] = f"https://en.wikipedia.org/wiki/{safe_name_wiki}"
                node['links']['aab'] = f"https://www.allaboutbirds.org/guide/{quote_plus(det['species'])}"
                
                if con:
                    vectors = get_migration_target(det['species'], con)
                    if vectors: node['mig_target'] = vectors['target']; node['mig_origin'] = vectors['origin']
                node['mig_volume'] = species_counts.get(det['species'], 1)
                
                if diff < live_window: 
                    node['status'] = 'live'
                    node['time_ago_str'] = 'LIVE NOW'
                elif diff < 3600: 
                    node['status'] = 'recent'
                    node['time_ago_str'] = f"{int(diff/60)}m ago"
                else: 
                    node['status'] = 'dormant'
                    node['time_ago_str'] = f"{int(diff/3600)}h ago"
                
                node['image'] = get_image_base64(det['species'])

            nodes.append(node)

        feed =[]

        for f in feed_rows:
            diff = now - f['timestamp']
            urgency = 'grey'
            if diff < live_window: urgency = 'red'
            elif diff < 3600: urgency = 'orange'
            
            dist_tag = f.get('distance_category') or "?"
            method_tag = f.get('detection_method') or "audio"
            
            has_mig = False
            if con: has_mig = get_migration_target(f['species'], con) is not None
            
            raw_feed_url = f['channel_url']
            clean_feed_url = sanitize_url_for_matching(raw_feed_url)
            
            stream_name = url_to_name.get(raw_feed_url) or url_to_name.get(clean_feed_url) or "Unknown Stream"
            
            if stream_name in name_to_canonical:
                target_id = name_to_canonical[stream_name]['id']
                st_type = name_to_canonical[stream_name]['type']
            else:
                target_id = generate_stable_id(raw_feed_url)
                st_type = 'unknown'
            
            feed.append({
                'species': f['species'], 'time': datetime.fromtimestamp(f['timestamp']).strftime('%H:%M'),
                'stream': stream_name, 'stream_type': st_type,
                'dist': dist_tag, 'detection_method': method_tag,
                'target_id': target_id, 'urgency': urgency, 
                'is_close': "Near" in dist_tag or "LARGE" in dist_tag.upper(), 'has_migration': has_mig,
                'audio_id': f['id'], 'timestamp': f['timestamp']
            })

        cleanup_temp_db(con, temp_db_path)
        return {"nodes": nodes, "feed": feed}
        
    except Exception as e:
        traceback.print_exc()
        return {"nodes": [], "feed":[], "error": f"Critical: {e}"}

# --- HTTP Server with PROXY Support ---
class GBRHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            
            if parsed.path == '/proxy':
                query = parse_qs(parsed.query)
                target_url_raw = query.get('url', [None])[0]
                passed_referer_b64 = query.get('r', [None])[0]
                
                if not target_url_raw:
                    self.send_error(400, "Missing 'url' parameter")
                    return

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
                        
                        log_proxy(f"ATTEMPT {label}: {target_url} | REF: {ref_val}")
                        r = requests.get(target_url, headers=headers, stream=True, timeout=8, verify=False, allow_redirects=True)
                        
                        if r.status_code >= 200 and r.status_code < 400:
                            final_resp = r
                            log_proxy(f"SUCCESS ({label})")
                            break
                        else:
                            log_proxy(f"FAIL ({label}): {r.status_code}")
                            if r.status_code == 403: continue 
                            else: 
                                final_resp = r 
                                break
                    except Exception as e:
                        log_proxy(f"EXCEPT ({label}): {e}")
                        continue

                if final_resp:
                    self.send_response(final_resp.status_code)
                    ct = final_resp.headers.get('Content-Type', '')
                    if ct: self.send_header('Content-Type', ct)
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    
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
                        
                        self.wfile.write('\n'.join(new_lines).encode('utf-8'))
                    else:
                        for chunk in final_resp.iter_content(chunk_size=8192):
                            if chunk: self.wfile.write(chunk)
                else:
                    self.send_error(502, "All proxy strategies failed")
                return
            
            self.send_response(200)
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.send_header('Access-Control-Allow-Origin', '*') 
            
            if parsed.path == '/':
                self.send_header('Content-type', 'text/html'); self.end_headers()
                try: self.wfile.write(TEMPLATE_FILE.read_text(encoding='utf-8').encode('utf-8'))
                except Exception as e: self.wfile.write(f"Template Error: {e}".encode('utf-8'))
            elif parsed.path.startswith('/data'):
                self.send_header('Content-type', 'application/json'); self.end_headers()
                data = get_live_payload(); self.wfile.write(json.dumps(data).encode('utf-8'))
            elif parsed.path.startswith('/audio/'):
                filename = os.path.basename(parsed.path); file_path = CLIPS_DIR / filename
                if file_path.exists():
                    self.send_header('Content-type', 'audio/mpeg'); self.end_headers()
                    with open(file_path, 'rb') as f: self.wfile.write(f.read())
                else: self.send_error(404, "Audio file not found")
            else: self.send_error(404)
        except Exception as e: 
            if "Broken pipe" not in str(e): traceback.print_exc()
            
    def log_message(self, format, *args): pass 

def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s: s.bind(('127.0.0.1', 0)); return s.getsockname()[1]

class BackendThread(threading.Thread):
    def __init__(self, port): super().__init__(); self.port = port; self.server = None; self.daemon = True
    def run(self):
        try: self.server = socketserver.TCPServer(('127.0.0.1', self.port), GBRHandler); self.server.serve_forever()
        except: pass
    def stop(self):
        if self.server: self.server.shutdown(); self.server.server_close()

class MapLauncher(QWidget):
    def __init__(self):
        super().__init__()
        self.port = get_free_port()
        self.backend = BackendThread(self.port)
        self.backend.start()
        self.init_ui()
        perform_startup_audit()

    def init_ui(self):
        self.setWindowTitle("GBR Map Controller v39.1")
        self.setGeometry(100, 100, 400, 200)
        self.setStyleSheet("background: #222; color: #eee; font-family: sans-serif;")
        layout = QVBoxLayout()
        label = QLabel("SYSTEM ACTIVE")
        label.setStyleSheet("color: #00e676; font-weight: bold; font-size: 18px;")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        info = QLabel(f"Server Port: {self.port}\nStatus: MULTIMODAL ENABLED")
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(info)
        btn = QPushButton("LAUNCH MAP INTERFACE")
        btn.setStyleSheet("background: #0078d7; color: white; padding: 15px; font-weight: bold; border-radius: 5px;")
        btn.clicked.connect(lambda: webbrowser.open(f"http://127.0.0.1:{self.port}/"))
        layout.addWidget(btn)
        self.setLayout(layout)

    def closeEvent(self, event):
        self.backend.stop()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MapLauncher()
    window.show()
    sys.exit(app.exec())