# FILE: stream_to_alert_birdnet.py
# VERSION: 38.5 - "The Mojibake Prevention Patch"
# RESPONSIBILITY: Analyzes audio, Reports to DB, Manages Queue Escalations & Cooldowns.
# CHANGELOG:
# [2026-09-07 03:02] - v38.5: Added inline Mojibake sanitizer (_fix_mojibake) to instantly decode broken UTF-8 characters (like Ã¼ -> ü) returning from BirdNET before they hit the database.
# [2026-09-04 01:32] - v38.4: Added "auto_resolver_failed" to the HICCUP list and retry-break list to prevent Auto-Healer failures from permanently banning streams.
# [2026-09-02 02:16] - v38.3: Fixed severe race condition causing cooldown state amnesia between Audio and Vision engines.
# [2026-08-10 12:00] - v38.2: Added CREATE_NO_WINDOW to the Node.js subprocess check to prevent black console boxes from popping up.

import sys
import os
import traceback
import warnings
from pathlib import Path

# --- SUPPRESS WARNING SPAM ---
warnings.filterwarnings("ignore", category=UserWarning, message=".*tf.lite.Interpreter is deprecated.*")
warnings.filterwarnings("ignore", category=UserWarning, module="tensorflow")

# --- CRITICAL STARTUP TRAP ---
ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "monitor_debug.txt"

try:
    import json
    import logging
    import time
    import hashlib
    import random
    import io
    import re
    import html
    import subprocess
    import tempfile
    from urllib.parse import quote_plus
    from typing import Optional, List
    from datetime import datetime

    # External Libraries
    import numpy as np
    import requests

    from birdnetlib.analyzer import Analyzer
    from birdnetlib import Recording
    from pydub import AudioSegment

    # --- INTERNAL MODULES ---
    sys.path.append(str(ROOT))
    import db_connector
    import trait_inference
    import migration_manager
    import network_manager
    from audio_capture import AudioCaptureEngine

    # --- BIOACOUSTIC MODULE ---
    try:
        import bioacoustic_profiles
        BIOACOUSTIC_AVAILABLE = True
    except ImportError:
        BIOACOUSTIC_AVAILABLE = False

except Exception as e:
    with open(LOG_FILE, "a") as f:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        f.write(f"\n[{timestamp}][CRITICAL STARTUP ERROR] stream_to_alert_birdnet.py failed to import libraries:\n{traceback.format_exc()}\n")
    sys.exit(1)

# --- GLOBAL NODE.JS PATH INJECTION ---
def ensure_node_in_path():
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        subprocess.run(["node", "-v"], check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, creationflags=flags)
        return
    except: pass
    search_paths =[r"C:\Program Files\nodejs", r"C:\Program Files (x86)\nodejs", os.path.expandvars(r"%APPDATA%\npm")]
    current_path = os.environ.get("PATH", "")
    for p in search_paths:
        if os.path.exists(os.path.join(p, "node.exe")):
            if p not in current_path:
                os.environ["PATH"] = f"{p};{current_path}"
                break

ensure_node_in_path()

# --- Configuration ---
CFG_PATH = ROOT / "birdnet_config.json"
TARGETS_FILE = ROOT / "bioacoustic_targets.json"
DATABASE_PATH = ROOT / "detections.db"
BASELINE_CLIPS_DIR = ROOT / "baseline_clips"
COOLDOWN_STATE_PATH = ROOT / "cooldown_state.json"
PROXY_MAP_FILE = ROOT / "proxy_map.json"

AudioSegment.converter = str(ROOT / "ffmpeg" / "bin" / "ffmpeg.exe")
AudioSegment.ffprobe   = str(ROOT / "ffmpeg" / "bin" / "ffprobe.exe")

# --- LOGGING ---
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s -[DB] - %(message)s',
                    handlers=[logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8'), logging.StreamHandler()],
                    force=True) 

def init_status_database():
    try:
        with db_connector.get_db_connection(force_local=True) as con:
            con.execute('''CREATE TABLE IF NOT EXISTS scheduler_status (listener_id TEXT PRIMARY KEY, cycle_start_time REAL, total_cycle_seconds REAL, managed_streams_json TEXT, last_updated REAL, cycle_count INTEGER DEFAULT 0)''')
    except: pass

class DatabaseManager:
    def __init__(self): pass 

    def log_health_event(self, url, status, message):
        try:
            with db_connector.get_db_connection() as con: con.execute("INSERT INTO stream_health_events (stream_url, timestamp, status, message) VALUES (?, ?, ?, ?)", (url, time.time(), status, message))
        except: pass

    def get_historical_count(self, url, species, days=14):
        try:
            cutoff = time.time() - (days * 86400)
            with db_connector.get_db_connection() as con:
                cur = con.cursor()
                cur.execute("SELECT COUNT(*) FROM detections WHERE channel_url = ? AND species = ? AND timestamp > ?", (url, species, cutoff))
                return cur.fetchone()[0]
        except: return 0

    def has_recent_visuals(self, url, days=7):
        try:
            cutoff = time.time() - (days * 86400)
            with db_connector.get_db_connection(force_local=True) as con:
                cur = con.cursor()
                cur.execute("SELECT 1 FROM detections WHERE channel_url = ? AND detection_method IN ('vision', 'multimodal') AND timestamp > ? LIMIT 1", (url, cutoff))
                return cur.fetchone() is not None
        except: 
            return False

    def update_noise_profile(self, url, dbfs):
        try:
            with db_connector.get_db_connection() as con:
                con.execute("INSERT INTO stream_noise_profiles (stream_url, last_updated) VALUES (?, ?) ON CONFLICT (stream_url) DO NOTHING", (url, time.time()))
                cur = con.cursor(); cur.execute("SELECT sample_count, average_noise_dbfs FROM stream_noise_profiles WHERE stream_url = ?", (url,))
                row = cur.fetchone()
                if row:
                    cnt, old = row
                    new_avg = dbfs if old == float('-inf') else ((old * cnt) + dbfs) / (cnt + 1)
                    con.execute("UPDATE stream_noise_profiles SET sample_count = ?, average_noise_dbfs = ?, last_updated = ? WHERE stream_url = ?", (cnt + 1, new_avg, time.time(), url))
        except: pass

    def check_audio_hash(self, url, wav_bytes):
        try:
            h = hashlib.md5(wav_bytes).hexdigest()
            with db_connector.get_db_connection() as con:
                con.execute("INSERT INTO audio_hashes (hash_text, stream_url, first_seen_timestamp) VALUES (?, ?, ?) ON CONFLICT (hash_text, stream_url) DO NOTHING", (h, url, time.time()))
                cur = con.cursor(); cur.execute("SELECT first_seen_timestamp FROM audio_hashes WHERE hash_text = ? AND stream_url = ?", (h, url))
                row = cur.fetchone()
                if row and (time.time() - row[0] > 300): 
                    self.log_health_event(url, "LOOP_DETECTED", f"Audio Hash Repetition: {h[:8]}...")
                    return True
            return False
        except: return False

    def update_species_stream_profile(self, cur, stream_url, species_name, current_snr, detection_id):
        try:
            current_snr = float(current_snr)
            cur.execute("SELECT max_snr_observed, baseline_detection_id FROM species_stream_profiles WHERE stream_url = ? AND species_name = ?", (stream_url, species_name))
            row = cur.fetchone()
            
            is_new_anchor = False
            new_max, new_base_id = current_snr, detection_id
            
            if row:
                if current_snr > row[0]:
                    is_new_anchor = True
                else:
                    new_max, new_base_id = row[0], row[1]
            else:
                is_new_anchor = True
                
            cur.execute("""INSERT INTO species_stream_profiles (stream_url, species_name, max_snr_observed, sample_count, last_updated, baseline_detection_id) VALUES (?, ?, ?, COALESCE((SELECT sample_count FROM species_stream_profiles WHERE stream_url=? AND species_name=?), 0) + 1, ?, ?) ON CONFLICT (stream_url, species_name) DO UPDATE SET max_snr_observed = EXCLUDED.max_snr_observed, sample_count = EXCLUDED.sample_count, last_updated = EXCLUDED.last_updated, baseline_detection_id = EXCLUDED.baseline_detection_id""", (stream_url, species_name, new_max, stream_url, species_name, time.time(), new_base_id))
            return is_new_anchor
        except: return False

    def log_detection(self, url, species, lat, lon, dist, snr, confidence, wav_bytes, threshold, listener_id, interface_name, bot_token=None, is_alert=False, detection_method='audio', dsp_debug='', vision_path=None, proxy_url=None):
        try:
            snr = float(snr)
            
            ai_notes_parts =[]
            if confidence is not None:
                ai_notes_parts.append(f"Confidence: {confidence:.3f}")
            if dsp_debug:
                ai_notes_parts.append(f"DSP: {dsp_debug}")
            ai_notes_str = " | ".join(ai_notes_parts)
            
            is_new_anchor = False
            try:
                with db_connector.get_db_connection(force_local=True) as con:
                    cur = con.cursor()
                    cur.execute("SELECT max_snr_observed FROM species_stream_profiles WHERE stream_url = ? AND species_name = ?", (url, species))
                    row = cur.fetchone()
                    if row is None or snr > row[0]:
                        is_new_anchor = True
            except: pass 
            
            unified_timestamp = time.time()
            alert_str = "true" if is_alert else "false"
            
            cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
            is_cloud = cfg.get('database_cloud', {}).get('enabled', False)
            
            if is_cloud and bot_token:
                try:
                    payload = {
                        'secret_token': bot_token, 'timestamp': str(unified_timestamp), 'channel_url': url,
                        'species': species, 'latitude': str(lat) if lat else "", 'longitude': str(lon) if lon else "",
                        'distance_category': dist, 'snr': str(snr), 'alert_sent': alert_str,
                        'listener_id': listener_id, 'network_interface': interface_name,
                        'detection_method': detection_method,
                        'vision_path': str(vision_path) if vision_path else "",
                        'frame_size': 'N/A',
                        'filter_reason': '',
                        'ai_notes': ai_notes_str
                    }
                    
                    audio_bytes = None
                    if is_new_anchor or is_alert:
                        mp3_buffer = io.BytesIO()
                        s = AudioSegment.from_file(io.BytesIO(wav_bytes), format="wav")
                        s.export(mp3_buffer, format="mp3", bitrate="64k")
                        audio_bytes = mp3_buffer.getvalue()
                        
                    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url and proxy_url != "None" else None
                    api_url = "https://wilddetection.com/api/inject_detection"
                    
                    for attempt in range(3):
                        try:
                            files = {'audio_file': (f"temp.mp3", audio_bytes, 'audio/mpeg')} if audio_bytes else None
                            current_proxies = proxies if attempt < 2 else None
                            
                            res = requests.post(api_url, data=payload, files=files, timeout=10, verify=True, proxies=current_proxies)
                            
                            if res.status_code == 200:
                                if is_new_anchor or is_alert:
                                    logging.info(f"[{listener_id}] Cloud Sync: Uploaded Audio for {species} (Anchor: {is_new_anchor} | Alert: {is_alert})")
                                break
                        except Exception as e:
                            if attempt == 2:
                                logging.error(f"[{listener_id}] HTTPS Pivot Failed after 3 attempts: {e}")
                            else:
                                time.sleep(1.0)
                                
                except Exception as e:
                    logging.error(f"[{listener_id}] Cloud Logic Error: {e}")

            with db_connector.get_db_connection(force_local=True) as con:
                cur = con.cursor()
                cur.execute("""INSERT INTO detections (timestamp, channel_url, species, latitude, longitude, distance_category, snr, alert_sent, listener_id, network_interface, detection_method, vision_path, ai_notes, frame_size, filter_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (unified_timestamp, url, species, lat, lon, dist, snr, is_alert, listener_id, interface_name, detection_method, str(vision_path) if vision_path else None, ai_notes_str, 'N/A', ''))
                det_id = cur.lastrowid
                
                if not det_id: 
                    cur.execute("SELECT id FROM detections WHERE channel_url = ? AND species = ? ORDER BY id DESC LIMIT 1", (url, species))
                    row = cur.fetchone()
                    if row: det_id = row[0]

                if not det_id: return None

                actual_is_new_anchor = self.update_species_stream_profile(cur, url, species, snr, det_id)

                try:
                    cur.execute("SELECT 1 FROM species_traits WHERE species_name = ?", (species,))
                    if not cur.fetchone():
                        fam, size, beak, color, sil, sound = trait_inference.infer_traits(species)
                        cur.execute("INSERT INTO species_traits (species_name, family_group, size_class, beak_type, color_primary, silhouette, call_pattern) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (species_name) DO NOTHING", (species, fam, size, beak, color, sil, sound))
                except: pass
                migration_manager.check_and_update_migration(species)

                BASELINE_CLIPS_DIR.mkdir(exist_ok=True)
                try:
                    s = AudioSegment.from_file(io.BytesIO(wav_bytes), format="wav")
                    s.export(BASELINE_CLIPS_DIR / f"detection_{det_id}.mp3", format="mp3", bitrate="64k")
                except:
                    (BASELINE_CLIPS_DIR / f"detection_{det_id}.wav").write_bytes(wav_bytes)
                        
                return det_id
        except Exception as local_e: 
            logging.error(f"[{listener_id}] Local DB Insert Crash (Alert Aborted): {local_e}")
            return None

class StreamListener:
    def __init__(self, stream_config, global_config, listener_id, available_proxies, analyzer):
        self.cfg = stream_config
        self.g_cfg = global_config
        self.lid = listener_id
        self.available_proxies = available_proxies
        self.db = DatabaseManager()
        self.analyzer = analyzer
        
        # Instantiate the decoupled extraction engine
        self.capture_engine = AudioCaptureEngine(self.lid, self.db, self.g_cfg)
        
        self.interface_name = "Default / OS"
        self.proxy_url = None
        
        self.bio_targets =[]
        self.bio_v2_data = {}
        
        if BIOACOUSTIC_AVAILABLE and TARGETS_FILE.exists():
            try:
                raw = json.loads(TARGETS_FILE.read_text(encoding='utf-8'))
                if raw.get("_version") == 2:
                    self.bio_v2_data = raw
                    self.bio_targets = raw.get("assignments", {}).get(self.cfg['page_url'],[])
                else:
                    raw_target = raw.get(self.cfg['page_url'])
                    if isinstance(raw_target, str):
                        self.bio_targets.append({'profile': raw_target, 'display': raw_target.title().replace("_", " ")})
                    elif isinstance(raw_target, list):
                        self.bio_targets = raw_target
                
                if self.bio_targets:
                    names =[t['display'] for t in self.bio_targets]
                    logging.info(f"[{self.lid}] Bioacoustic Targets Active: {', '.join(names)}")
            except Exception as e:
                logging.error(f"[{self.lid}] Target Load Error: {e}")

    def check_vision_bridge(self, stream_url, audio_species):
        try:
            cutoff = time.time() - 1800 # 30 minutes
            with db_connector.get_db_connection(force_local=True) as con:
                cur = con.cursor()
                cur.execute("SELECT vision_path, species FROM detections WHERE channel_url = ? AND timestamp > ? AND detection_method IN ('vision', 'multimodal') ORDER BY timestamp DESC", (stream_url, cutoff))
                recent_vision_detections = cur.fetchall()
                
            if not recent_vision_detections:
                return False, None
                
            audio_clean = audio_species.lower().replace('-', ' ').replace('(', '').replace(')', '').strip()
            audio_words = set(audio_clean.split())
            
            THESAURUS =[
                {"seal", "sea lion", "walrus", "otter", "pinniped", "sea_lion"},
                {"wolf", "coyote", "dog", "canine", "fox", "jackal", "canid"},
                {"elephant", "rhino", "rhinoceros", "hippopotamus", "hippo"}, 
                {"monkey", "ape", "gorilla", "chimpanzee", "baboon", "macaque", "lemur", "primate"},
                {"bear", "panda"},
                {"tiger", "lion", "leopard", "jaguar", "panther", "cat", "feline", "puma", "cougar", "lynx"},
                {"deer", "elk", "moose", "caribou", "antelope", "gazelle", "springbok", "wildebeest", "impala", "waterbuck", "kudu"},
                {"pig", "boar", "warthog", "swine"},
                {"cow", "cattle", "sheep", "goat", "yak", "camel", "bovid", "bison", "buffalo"},
                {"frog", "toad", "peeper", "amphibian"},
                {"reptile", "snake", "alligator", "crocodile"},
                {"cricket", "cicada", "grasshopper", "katydid", "insect", "mammal"}
            ]
            
            for v_path, vis_sp in recent_vision_detections:
                vis_clean = vis_sp.lower().replace('-', ' ').strip()
                vis_words = set(vis_clean.split())
                
                if audio_clean == vis_clean or audio_clean in vis_clean or vis_clean in audio_clean:
                    return True, v_path
                    
                for group in THESAURUS:
                    aud_match = any(syn in audio_words for syn in group) or any(syn == audio_clean for syn in group)
                    vis_match = any(syn in vis_words for syn in group) or any(syn == vis_clean for syn in group)
                    if aud_match and vis_match:
                        return True, v_path
                        
                if "mammal" in audio_words and any(w in vis_words for w in["lion", "tiger", "bear", "elephant", "wolf", "coyote", "hyena", "monkey", "baboon", "warthog", "hippo", "badger", "seal", "sea", "otter"]):
                    return True, v_path
                if "rumble" in audio_words and ("elephant" in vis_words or "hippo" in vis_words):
                    return True, v_path
                    
            return False, None
        except Exception as e:
            logging.error(f"Vision bridge error: {e}")
            return False, None

    def process(self):
        url = self.cfg['page_url']; name = self.cfg['name']; original_url = self.cfg.get('original_url', '')

        if self.cfg.get('mute_audio', False):
            logging.info(f"[{self.lid}] AUDIO MUTED for {name}. Skipping audio capture and BirdNET analysis (Vision-Only mode).")
            self.db.log_health_event(url, "SUCCESS", "Audio Muted (Vision Only)")
            return 'SUCCESS'

        max_retries = 3; wav_data = None; last_error_str = ""; dynamic_headers = []
        
        strict_proxy = self.g_cfg.get("vision_ai", {}).get("strict_proxy", False)
        
        try:
            if PROXY_MAP_FILE.exists():
                self.available_proxies = json.loads(PROXY_MAP_FILE.read_text(encoding='utf-8'))
        except: pass

        # --- THE PROPORTIONAL WORKLOAD PATCH ---
        pid_settings = self.g_cfg.get("hydra_pid_settings", {})
        throttling_enabled = pid_settings.get("throttling_enabled", True)

        network_map = self.g_cfg.get("network_map", {})
        assigned_iface = network_map.get(self.lid, "Default / OS")
        all_interfaces = list(set(network_map.values()) - {"Default / OS"})
        
        # If throttling is disabled, bypass the strict assignment lock and 
        # let the network manager proportionally distribute the load.
        if not throttling_enabled:
            assigned_interfaces = []
        else:
            assigned_interfaces = [assigned_iface] if assigned_iface not in ("Default / OS", "", None) else []

        for attempt in range(max_retries):
            # Try assigned interface first (if throttling is enabled)
            best_iface, best_ip = None, None
            if assigned_interfaces:
                best_iface, best_ip = network_manager.get_best_available_interface(assigned_interfaces)

            # Fall back to full pool (always used if throttling disabled, or if assigned is dead/banned)
            if not best_iface and all_interfaces:
                best_iface, best_ip = network_manager.get_best_available_interface(all_interfaces)

            if best_iface:
                self.interface_name = best_iface
                self.proxy_url = self.available_proxies.get(best_iface)
            else:
                if all_interfaces and strict_proxy:
                    logging.warning(f"[{self.lid}] 🔴 ALL SIMS EXHAUSTED/BANNED. Strict Proxy is ON. Aborting audio scan to prevent OS data leak.")
                    last_error_str = "strict proxy active - no sims available"
                    break
                
                self.interface_name = "Default / OS"
                self.proxy_url = None

            if self.interface_name != "Default / OS":
                while not network_manager.get_api_token(self.interface_name, cooldown_seconds=5):
                    time.sleep(1.0)
            
            try:
                wav_data, dynamic_headers = self.capture_engine.grab_audio(url, original_url, name, self.cfg, self.interface_name, self.proxy_url)
                if wav_data: break 
            except Exception as e:
                last_error_str = str(e).lower()
                logging.warning(f"[{self.lid}] Grab Audio Attempt {attempt+1} failed for {name}. Error: {str(e)}")
                
                # --- THE SNIFFER AMNESTY PATCH ---
                # We specifically check for auto_resolver_failed to break out of pointless retry loops
                # when the auto-healer fails to find a stream link, preventing useless retries.
                if any(x in last_error_str for x in ["auto_resolver_failed", "auto-resolver failed", "terminated", "removed", "404", "410", "has ended", "strict proxy", "vod_rejected"]): 
                    break 
                    
                if attempt < max_retries - 1: 
                    time.sleep(2)
        
        if not wav_data:
            logging.error(f"[{self.lid}] AUDIO CAPTURE COMPLETELY FAILED for {name}. Reason: {last_error_str[:100]}")
            if any(x in last_error_str for x in["terminated", "removed", "copyright", "has ended", "vod_rejected"]):
                self.db.log_health_event(url, "FATAL", f"Dead Stream: {last_error_str[:100]}")
                return 'FATAL'
            if any(x in last_error_str for x in["video unavailable", "is not available", "recording is not available", "private", "offline", "waiting for"]):
                self.db.log_health_event(url, "SUSPENDED", f"Stream Offline: {last_error_str[:100]}")
                return 'SUSPENDED'
            
            # --- THE SNIFFER AMNESTY PATCH ---
            # Added auto_resolver_failed to the HICCUP list. This prevents temporary Selenium 
            # sniffer failures (e.g. from getting a captcha wall) from permanently banning the stream.
            if any(x in last_error_str for x in["timeout", "connection reset", "network is unreachable", "429", "too many requests", "503", "rate-limited", "strict proxy", "404", "410", "no such host", "malformed url", "-138", "403 forbidden", "invalid data found", "auto-resolver failed", "auto_resolver_failed", "timed out", "10054"]):
                self.db.log_health_event(url, "HICCUP", f"Network/Proxy Block: {last_error_str[:100]}")
                return 'HICCUP'
            self.db.log_health_event(url, "FAILURE", f"Error: {last_error_str[:100]}")
            return 'FAILURE'

        try:
            seg = AudioSegment.from_file(io.BytesIO(wav_data), format="wav")
            db_fs = seg.dBFS
            self.db.update_noise_profile(url, db_fs)
            
            if db_fs == float('-inf'): 
                logging.warning(f"[{self.lid}] AUDIO SILENT for {name}. The stream connected but contains no audio data.")
                if self.db.has_recent_visuals(url, days=7):
                    logging.info(f"[{self.lid}] Stream {name} is audio-silent but visually active. Flagging as SILENT_VISUAL.")
                    return 'SILENT_VISUAL'
                return 'SILENT' 
                
            if self.g_cfg.get('loop_management', {}).get('enabled', True):
                if self.db.check_audio_hash(url, wav_data): return 'LOOP'
        except Exception as e: 
            logging.error(f"[{self.lid}] Error analyzing silence/hashing for {name}: {e}")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tf: 
            tf.write(wav_data); tmp = tf.name
        
        try:
            week = min(datetime.now().isocalendar().week, 48)
            rec = Recording(analyzer=self.analyzer, path=tmp, lat=self.cfg.get('lat'), lon=self.cfg.get('lon'), week_48=week, min_conf=0.05)
            rec.analyze()
            
            # --- THE MOJIBAKE PREVENTION PATCH ---
            def _fix_mojibake(s):
                if not isinstance(s, str): return s
                if 'Ã' in s:
                    try: return s.encode('latin-1').decode('utf-8')
                    except: pass
                return s
                
            detections =[{"species": _fix_mojibake(d['common_name']), **d} for d in rec.detections]
        except Exception as e:
            logging.error(f"[{self.lid}] AI Analysis Error for {name}: {e}")
            os.unlink(tmp); return 'FAILURE'
        
        os.unlink(tmp)

        weather_noise_labels = {"Wind", "Rain", "Noise", "Siren", "Engine", "Motor vehicle (road)", "Human voice", "Human whistling"}
        max_noise_conf = 0.0
        for d in detections:
            if d['species'] in weather_noise_labels:
                if d['confidence'] > max_noise_conf:
                    max_noise_conf = d['confidence']

        if self.bio_targets and BIOACOUSTIC_AVAILABLE:
            if max_noise_conf >= 0.4:
                logging.info(f"[{self.lid}] BIO-VETO: BirdNET detected Noise/Weather (Conf: {max_noise_conf:.2f}). Bioacoustics skipped.")
            else:
                for target_data in self.bio_targets:
                    prof = target_data['profile']
                    disp = target_data['display']
                    try:
                        score, debug_msg = bioacoustic_profiles.analyze_target(wav_data, prof)
                        if score >= 0.2:
                            logging.info(f"[{self.lid}] BIO-HIT: {disp} ({score:.2f}) | DBG: {debug_msg}")
                            fake_det =[{'species': disp, 'confidence': score, 'target_data': target_data, 'dsp_debug': debug_msg}]
                            self._handle_alert(fake_det, wav_data, is_bioacoustic=True)
                    except Exception as e:
                        logging.error(f"[{self.lid}] Bioacoustic Error on {disp}: {e}")

        master_noise_labels = {
            "Siren", "Dog", "Motor vehicle (road)", "Car alarm", "Human voice", 
            "Human narrator", "Human whistling", "Human vocal", "Human footstep", 
            "Engine", "Wind", "Rain", "Gunshot, gunfire", "Fireworks", "Noise", "Car"
        }
        
        thresh = float(self.cfg.get('score_threshold_override', self.g_cfg.get('score_threshold', 0.55)))
        valid =[d for d in detections if d['confidence'] >= thresh and d['species'] not in master_noise_labels]
        
        if valid: 
            self._handle_alert(valid, wav_data)
        
        return 'SUCCESS'

    def _handle_alert(self, detections, wav_data, is_bioacoustic=False):
        cooldowns = {}
        now_ts = time.time()
        if COOLDOWN_STATE_PATH.exists(): 
            try: 
                cooldowns = json.loads(COOLDOWN_STATE_PATH.read_text(encoding='utf-8'))
                keys_to_scrub =[k for k, v in cooldowns.items() if v > now_ts + 60]
                for k in keys_to_scrub:
                    cooldowns[k] = 0
            except: pass

        tiered_config = self.g_cfg.get("tiered_cooldowns", {})
        calc_days = tiered_config.get("calculation_period_days", 14)
        tiers = tiered_config.get("tiers",[])
        first_sight = tiered_config.get("first_sighting_override", {"enabled": True, "count": 2})
        
        detections = sorted(detections, key=lambda x: x.get('confidence', 0), reverse=True)
        processed_species_in_clip = set()
            
        for d in detections:
            sp = d['species']
            
            if sp in processed_species_in_clip:
                continue
            
            processed_species_in_clip.add(sp)
            
            dist_cat, snr = "Unknown", 0.0
            should_alert = False
            
            is_multimodal, v_path = self.check_vision_bridge(self.cfg['page_url'], sp)
            det_method = "multimodal" if is_multimodal else "audio"
            
            if is_bioacoustic:
                td = d.get('target_data', {})
                global_defs = self.bio_v2_data.get("global_defaults", {}) if self.bio_v2_data else {}
                
                use_snr = td.get("enable_adaptive_snr", global_defs.get("enable_adaptive_snr", False))
                
                if use_snr:
                    try:
                        audio = AudioSegment.from_file(io.BytesIO(wav_data), format="wav")
                        chunk_ms = 1000
                        max_rms = 1
                        min_rms = float('inf')
                        for i in range(0, len(audio), chunk_ms):
                            c = audio[i:i+chunk_ms]
                            if c.rms > max_rms: max_rms = c.rms
                            if c.rms > 0 and c.rms < min_rms: min_rms = c.rms
                        if min_rms == float('inf'): min_rms = 1
                        raw_snr = 20 * np.log10(max_rms / min_rms)
                        snr = float(raw_snr + 12.0)
                        
                        with db_connector.get_db_connection(force_local=True) as con:
                            cur = con.cursor()
                            cur.execute("SELECT max_snr_observed FROM species_stream_profiles WHERE stream_url = ? AND species_name = ?", (self.cfg['page_url'], sp))
                            res = cur.fetchone()
                            stream_max = float(res[0]) if res and res[0] else snr
                            
                        ratio = ((snr - -30.0) / (stream_max - -30.0)) * 100 if (stream_max - -30.0) > 0 else 0
                        if ratio > 85: dist_cat = "Very Near"
                        elif ratio > 60: dist_cat = "Near"
                        elif ratio > 35: dist_cat = "Mid-range"
                        elif ratio > 15: dist_cat = "Far"
                        else: dist_cat = "Very Far"
                    except Exception as e:
                        logging.error(f"[{self.lid}] Bio SNR Error: {e}")
                        dist_cat = "Bio-Match"
                        snr = d['confidence'] * 10
                else:
                    dist_cat = "Bio-Match"
                    snr = d['confidence'] * 10

                stream_rules = self.bio_v2_data.get("stream_rules", {}).get(self.cfg['page_url'], {}) if self.bio_v2_data else {}
                species_rules = self.bio_v2_data.get("species_rules", {}).get(sp, {}) if self.bio_v2_data else {}
                
                cd_target = td.get("cooldown_minutes", global_defs.get("cooldown_target_minutes", 60))
                cd_stream = stream_rules.get("cooldown_minutes", global_defs.get("cooldown_stream_minutes", 0))
                cd_species = species_rules.get("cooldown_minutes", global_defs.get("cooldown_species_minutes", 0))
                
                key_target = f"bio_target||{self.cfg['page_url']}||{sp}".lower()
                key_stream = f"bio_stream||{self.cfg['page_url']}".lower()
                key_species = f"bio_species||{sp}".lower()
                
                pass_target = (now_ts - cooldowns.get(key_target, 0)) >= (cd_target * 60)
                pass_stream = (now_ts - cooldowns.get(key_stream, 0)) >= (cd_stream * 60)
                pass_species = (now_ts - cooldowns.get(key_species, 0)) >= (cd_species * 60)
                
                if pass_target and pass_stream and pass_species:
                    should_alert = True
                    
            else:
                key = f"{self.cfg['page_url']}||{sp}".lower()
                
                if self.g_cfg.get("distance_estimation", {}).get("enabled", False):
                     try:
                        audio = AudioSegment.from_file(io.BytesIO(wav_data), format="wav")
                        s_start = int(d.get('start_time', 0)*1000)
                        s_end = int(d.get('end_time', 3)*1000)
                        signal = audio[s_start:s_end]
                        noise = audio[:s_start] + audio[s_end:]
                        if len(noise) > 0:
                            raw_snr = 20 * np.log10((signal.rms or 1) / (noise.rms or 1))
                            snr = float(raw_snr + 12.0)
                            with db_connector.get_db_connection(force_local=True) as con:
                                cur = con.cursor()
                                cur.execute("SELECT max_snr_observed FROM species_stream_profiles WHERE stream_url = ? AND species_name = ?", (self.cfg['page_url'], sp))
                                res = cur.fetchone()
                                stream_max = float(res[0]) if res and res[0] else snr
                            ratio = ((snr - -30.0) / (stream_max - -30.0)) * 100 if (stream_max - -30.0) > 0 else 0
                            if ratio > 85: dist_cat = "Very Near"
                            elif ratio > 60: dist_cat = "Near"
                            elif ratio > 35: dist_cat = "Mid-range"
                            elif ratio > 15: dist_cat = "Far"
                            else: dist_cat = "Very Far"
                     except: pass

                hist_count = self.db.get_historical_count(self.cfg['page_url'], sp, calc_days)
                
                matched_tier = None
                for t in tiers:
                    min_d = t.get("min_detections", 0)
                    max_d = t.get("max_detections")
                    if max_d is None: max_d = float('inf')
                    if min_d <= hist_count <= max_d:
                        matched_tier = t
                        break
                
                cd_mins = matched_tier.get("simple_minutes", 60) if matched_tier else 60
                
                if first_sight.get("enabled", True) and hist_count < first_sight.get("count", 2):
                    if (now_ts - cooldowns.get(key, 0)) >= (cd_mins * 60): 
                        should_alert = True
                else:
                    if (now_ts - cooldowns.get(key, 0)) >= (cd_mins * 60):
                        should_alert = True

            if is_multimodal:
                dist_cat = "Multimodal Confirmed"

            bot_token = self.g_cfg.get('bot_token')
            
            did = self.db.log_detection(
                self.cfg['page_url'], sp, self.cfg.get('lat'), self.cfg.get('lon'),
                dist_cat, snr, d.get('confidence', 0.0), wav_data, 0.5, self.lid, self.interface_name,
                bot_token, is_alert=should_alert, detection_method=det_method,
                dsp_debug=d.get('dsp_debug', ''), vision_path=v_path, proxy_url=self.proxy_url
            )
            
            if did and should_alert:
                logging.info(f"[{self.lid}] ALERT: {sp} ({dist_cat})")
                
                # --- THE COOLDOWN RACE CONDITION PATCH ---
                # Re-read the file immediately before writing to prevent wiping out
                # updates made by the Vision Engine or other Audio workers.
                for _attempt in range(3):
                    try:
                        fresh_cooldowns = {}
                        if COOLDOWN_STATE_PATH.exists(): 
                            fresh_cooldowns = json.loads(COOLDOWN_STATE_PATH.read_text(encoding='utf-8'))
                            
                        if is_bioacoustic:
                            fresh_cooldowns[key_target] = now_ts
                            fresh_cooldowns[key_stream] = now_ts
                            fresh_cooldowns[key_species] = now_ts
                        else:
                            fresh_cooldowns[key] = now_ts
                            
                        tmp_path = COOLDOWN_STATE_PATH.with_suffix('.tmp')
                        tmp_path.write_text(json.dumps(fresh_cooldowns))
                        os.replace(tmp_path, COOLDOWN_STATE_PATH)
                        break
                    except Exception as e:
                        time.sleep(0.2)
                
                chat = self.g_cfg.get('chat_id')
                
                send_audio_all = self.g_cfg.get('send_audio_telegram_all', False)
                send_audio_dsp = self.g_cfg.get('send_audio_telegram_dsp', True)
                send_audio_multi = self.g_cfg.get('send_audio_telegram_multi', True)
                send_audio_birdnet = self.g_cfg.get('send_audio_telegram_birdnet', False)
                
                should_send_audio = send_audio_all or (is_multimodal and send_audio_multi) or (is_bioacoustic and send_audio_dsp) or (not is_bioacoustic and not is_multimodal and send_audio_birdnet)

                if bot_token and chat:
                    send_species_alerts = self.g_cfg.get('periodic_report', {}).get('send_species_alerts', True)
                    if send_species_alerts:
                        icon = "🔥" if is_multimodal else ("🐘" if is_bioacoustic else "🐦")
                        method_str = "MULTIMODAL" if is_multimodal else "AUDIO"
                        
                        search_url = f"https://www.google.com/search?q={quote_plus(sp)}"
                        verify_link = f"https://en.wikipedia.org/wiki/{quote_plus(sp)}" if is_bioacoustic else f"https://www.allaboutbirds.org/guide/{quote_plus(sp)}"
                        
                        safe_sp = html.escape(sp)
                        safe_stream = html.escape(self.cfg['name'])
                        safe_dist = html.escape(dist_cat)
                        safe_lid = html.escape(self.lid)
                        safe_net = html.escape(self.interface_name)
                        
                        caption = f"{icon} {method_str} DETECTED: <a href='{search_url}'><b>{safe_sp}</b></a>\nStream: {safe_stream}\nDist: {safe_dist}\nVerify: <a href='{verify_link}'>Link</a>\n📡 {safe_lid} | Net: {safe_net}"
                        
                        if is_bioacoustic and 'dsp_debug' in d:
                            caption += f"\n\n🛠️ DSP Math:\n{html.escape(d['dsp_debug'])}"
                            
                        try:
                            audio_bytes = None
                            if should_send_audio:
                                mp3_buffer = io.BytesIO()
                                s = AudioSegment.from_file(io.BytesIO(wav_data), format="wav")
                                s.export(mp3_buffer, format="mp3", bitrate="64k")
                                audio_bytes = mp3_buffer.getvalue()
                                
                            photo_bytes = None
                            if is_multimodal and v_path and os.path.exists(v_path):
                                with open(v_path, 'rb') as photo_file:
                                    photo_bytes = photo_file.read()
                                    
                            proxies_dict = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url and self.proxy_url != "None" else None
                            
                            for attempt in range(3):
                                try:
                                    files = {'audio': (f"detection_{did}.mp3", audio_bytes, "audio/mpeg")} if should_send_audio else None
                                    current_proxies = proxies_dict if attempt < 2 else None
                                    
                                    if should_send_audio: 
                                        res = requests.post(f"https://api.telegram.org/bot{bot_token}/sendAudio", 
                                                      data={"chat_id": chat, "caption": caption, "parse_mode": "HTML"}, 
                                                      files={"audio": (f"detection_{did}.mp3", audio_bytes, "audio/mpeg")}, 
                                                      timeout=30, proxies=current_proxies)
                                    else: 
                                        res = requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", 
                                                      json={"chat_id": chat, "text": caption, "parse_mode": "HTML", "disable_web_page_preview": True}, 
                                                      timeout=30, proxies=current_proxies)
                                    res.raise_for_status()
                                        
                                    if photo_bytes:
                                        res2 = requests.post(f"https://api.telegram.org/bot{bot_token}/sendPhoto", 
                                                      data={"chat_id": chat, "caption": f"📷 Accompanying Vision for {sp}"}, 
                                                      files={"photo": ("vision.jpg", photo_bytes, "image/jpeg")}, 
                                                      timeout=20, proxies=current_proxies)
                                        res2.raise_for_status()
                                        
                                    break 
                                except Exception as tel_e: 
                                    if attempt == 2:
                                        logging.error(f"[{self.lid}] Telegram Post Error after 3 attempts: {tel_e}")
                                    else:
                                        time.sleep(1.5)
                        except Exception as wrap_e:
                            logging.error(f"[{self.lid}] Telegram Broadcast Logic Error: {wrap_e}")

def main(launch_json, lid, available_proxies_json):
    init_status_database()
    cycle_count = 0
    
    try:
        worker_analyzer = Analyzer()
    except Exception as e:
        logging.error(f"[{lid}] Failed to initialize BirdNET Analyzer: {e}")
        return
    
    try:
        launch_data = json.loads(launch_json)
        padded_cycle_s = launch_data.get("padded_cycle_s", 300)
    except:
        padded_cycle_s = 300
        
    try:
        if PROXY_MAP_FILE.exists():
            available_proxies = json.loads(PROXY_MAP_FILE.read_text(encoding='utf-8'))
        else:
            available_proxies = json.loads(available_proxies_json)
    except:
        available_proxies = {}

    while True:
        cycle_count += 1
        try: cfg = json.loads(CFG_PATH.read_text(encoding='utf-8'))
        except: time.sleep(5); continue
        
        try:
            if PROXY_MAP_FILE.exists():
                available_proxies = json.loads(PROXY_MAP_FILE.read_text(encoding='utf-8'))
        except: pass

        try:
            with db_connector.get_db_connection(force_local=True) as con:
                con.execute("""INSERT INTO scheduler_status (listener_id, cycle_start_time, total_cycle_seconds, managed_streams_json, last_updated, cycle_count) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (listener_id) DO UPDATE SET cycle_start_time = EXCLUDED.cycle_start_time, last_updated = EXCLUDED.last_updated, cycle_count = EXCLUDED.cycle_count""", (lid, time.time(), padded_cycle_s, json.dumps(["Requesting work..."]), time.time(), cycle_count))
        except: pass

        batch_size = int(cfg.get('batch_size', 20))
        target_urls = db_connector.checkout_streams(batch_size)
        if not target_urls: time.sleep(10); continue

        config_streams = {s['page_url']: s for s in cfg.get('streams',[])}
        valid_streams =[config_streams[u] for u in target_urls if u in config_streams]

        for i, stream_config in enumerate(valid_streams):
            current_name = stream_config['name']
            
            try:
                with db_connector.get_db_connection(force_local=True) as con:
                    con.execute("UPDATE scheduler_status SET managed_streams_json = ?, last_updated = ? WHERE listener_id = ?", (json.dumps([f"Processing ({i+1}/{len(valid_streams)}): {current_name}"]), time.time(), lid))
            except: pass

            listener = StreamListener(stream_config, cfg, lid, available_proxies, worker_analyzer)
            result_status = listener.process()
            
            penalty_min = 0
            if result_status == 'FATAL': penalty_min = 1440 
            elif result_status == 'SUSPENDED': penalty_min = int(cfg.get('suspended_penalty_minutes', 1440))
            elif result_status == 'TERMINAL': penalty_min = int(cfg.get('terminal_penalty_minutes', 10080)) 
            elif result_status == 'SILENT': penalty_min = int(cfg.get('silent_penalty_minutes', 60)) 
            elif result_status == 'SILENT_VISUAL': penalty_min = int(cfg.get('silent_visual_penalty_minutes', 720)) 
            elif result_status == 'LOOP': penalty_min = int(cfg.get('loop_penalty', 360))
            elif result_status in ('FAILURE', 'HICCUP'):
                consecutive_fails = db_connector.get_consecutive_failures(stream_config['page_url'])
                intermittent_thresh = int(cfg.get('intermittent_threshold', 2))
                unresponsive_thresh = int(cfg.get('unresponsive_threshold', 5))
                
                if consecutive_fails >= unresponsive_thresh:
                    result_status = 'UNRESPONSIVE'
                    last_ok = db_connector.get_last_success(stream_config['page_url'])
                    if (time.time() - last_ok) > 604800:
                        result_status = 'SUSPENDED'
                        penalty_min = int(cfg.get('suspended_penalty_minutes', 1440))
                    else: 
                        penalty_min = int(cfg.get('unresponsive_penalty_minutes', 240))
                elif consecutive_fails >= intermittent_thresh:
                    result_status = 'INTERMITTENT'
                    penalty_min = int(cfg.get('intermittent_penalty_minutes', 120))
                else:
                    if result_status == 'HICCUP':
                        penalty_min = int(cfg.get('hiccup_penalty_minutes', 1))
                    else:
                        penalty_min = int(cfg.get('failure_penalty', 30))
            
            db_connector.update_stream_status(stream_config['page_url'], result_status, penalty_min)

        try:
            with db_connector.get_db_connection(force_local=True) as con: con.execute("UPDATE scheduler_status SET managed_streams_json = ?, last_updated = ? WHERE listener_id = ?", (json.dumps(["Batch Complete. Waiting..."]), time.time(), lid))
        except: pass

        time.sleep(db_connector.get_turnstile_wait(int(cfg.get('interval_seconds', 300)), int(cfg.get('parallel_listeners', 4)), int(cfg.get('interval_jitter', 15))))

if __name__ == "__main__":
    if len(sys.argv) == 4:
        main(sys.argv[1], sys.argv[2], sys.argv[3])
    else:
        logging.error("Missing required arguments. Usage: stream_to_alert_birdnet.py <json_config> <listener_id> <available_proxies_json>")
        sys.exit(1)