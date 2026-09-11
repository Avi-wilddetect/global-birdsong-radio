# FILE: vision_scheduler.py
# VERSION: 10.20 - "The Dynamic Blindspot Patch"
# RESPONSIBILITY: Exclusively handles AI Inference (Gemini), Multi-Modal Bridge Logic, Telegram Reporting, and Dormancy Backoff constraints.
# CHANGELOG:
# [2026-09-10 21:01] - v10.20: Implemented Dynamic Conditional Blindspot. Vision engine now injects active cooldowns into Gemini's prompt, instructing it to ignore recently detected animals and search for secondary subjects to allow Visual Biodiversity Bursts without hallucinations.
# [2026-09-06 14:34] - v10.19: Fixed the Apostrophe Capitalization Bug where Python's .title() function created invalid names like "Grauer'S Gorilla", breaking the Wikipedia Image Curator.
# [2026-09-04 01:25] - v10.18: Decoupled Vision Engine from Audio Engine network failures. Vision now only skips explicitly DEAD streams (FATAL/SUSPENDED) and ignores UNRESPONSIVE/FAILURE flags.
# [2026-09-03 02:55] - v10.17: Fixed Drip-Feed Turnstile math to properly pace streams without starving the worker pool.
# [2026-09-03 02:07] - v10.16: Implemented Drip-Feed Turnstile pacing to cure the Burst & Starve coma.

import sys
import traceback
import subprocess
import os
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent
VISION_LOG_FILE = ROOT / "vision_debug.txt"

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

# ==============================================================================
# CRITICAL STARTUP TRAP
# ==============================================================================
try:
    import json
    import logging
    import time
    import re
    import socket
    import random
    import io
    import html
    from urllib.parse import quote_plus
    import threading
    import concurrent.futures
    from datetime import timedelta, timezone
    import sqlite3
    import requests
    import numpy as np

    from google import genai
    from google.genai import types
    from PIL import Image, ImageChops, ImageStat
    
    # --- SUPPRESS UNDETECTED CHROMEDRIVER LOG SPAM ---
    logging.getLogger('undetected_chromedriver').setLevel(logging.ERROR)
    
    # --- INTERNAL MODULES ---
    sys.path.append(str(ROOT))
    import db_connector
    import trait_inference
    import network_manager
    from vision_capture import extract_stream_url, grab_video_frame
    
    try:
        import stream_resolver
    except ImportError:
        stream_resolver = None

except Exception as e:
    with open(VISION_LOG_FILE, "a", encoding='utf-8') as f:
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        f.write(f"\n[{ts}][CRITICAL STARTUP ERROR] Vision Engine failed to import libraries:\n{traceback.format_exc()}\n")
    sys.exit(1)

# --- CONFIGURATION ---
CONFIG_FILE = ROOT / "birdnet_config.json"
DATABASE_PATH = ROOT / "detections.db"
VAULT_DIR = ROOT / "vision_snapshots"
COOLDOWN_STATE_PATH = ROOT / "cooldown_state.json"
VISION_TARGETS_FILE = ROOT / "vision_targets.json"
BASELINE_CLIPS_DIR = ROOT / "baseline_clips"
HYDRA_STATE_FILE = ROOT / "hydra_heat_state.json"
PROXY_MAP_FILE = ROOT / "proxy_map.json"

VAULT_DIR.mkdir(exist_ok=True)

# --- THREAD LOCKS ---
config_lock = threading.Lock()
cooldown_lock = threading.Lock()
targets_lock = threading.Lock()
gemini_api_lock = threading.Lock() # Kept for legacy compatibility if needed elsewhere

# ==============================================================================
# ISOLATED CUSTOM LOGGER
# ==============================================================================
v_logger = logging.getLogger("VisionEngine")
v_logger.setLevel(logging.INFO)
v_logger.propagate = False 

if v_logger.hasHandlers():
    v_logger.handlers.clear()

formatter = logging.Formatter('%(asctime)s - [VISION] - %(message)s')

file_handler = logging.FileHandler(VISION_LOG_FILE, mode='a', encoding='utf-8')
file_handler.setFormatter(formatter)
v_logger.addHandler(file_handler)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
v_logger.addHandler(stream_handler)


# ==============================================================================
# THE SEMANTIC BRIDGE
# ==============================================================================
def check_taxonomy_bridge(stream_url, visual_species):
    try:
        cutoff = time.time() - 1800  # 30 minutes
        with db_connector.get_db_connection(force_local=True) as con:
            cur = con.cursor()
            cur.execute(
                "SELECT id, timestamp, species FROM detections "
                "WHERE channel_url = ? AND timestamp > ? AND detection_method = 'audio' "
                "ORDER BY timestamp DESC LIMIT 20",
                (stream_url, cutoff)
            )
            recent_audio_detections = cur.fetchall()

        if not recent_audio_detections:
            return False, None, None, None

        vis_clean = visual_species.lower().replace('-', ' ').replace('(', '').replace(')', '').strip()
        vis_words = set(vis_clean.split())

        THESAURUS = [
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

        for det_id, det_ts, aud_sp in recent_audio_detections:
            if not aud_sp:
                continue
            audio_clean = aud_sp.lower().replace('-', ' ').replace('(', '').replace(')', '').strip()
            audio_words = set(audio_clean.split())

            if vis_clean == audio_clean or vis_clean in audio_clean or audio_clean in vis_clean:
                return True, det_id, det_ts, aud_sp

            for group in THESAURUS:
                aud_match = any(syn in audio_words for syn in group) or any(syn == audio_clean for syn in group)
                vis_match = any(syn in vis_words for syn in group) or any(syn == vis_clean for syn in group)
                if aud_match and vis_match:
                    return True, det_id, det_ts, aud_sp

            if "mammal" in vis_words and any(w in audio_words for w in [
                "lion", "tiger", "bear", "elephant", "wolf", "coyote", "hyena",
                "monkey", "baboon", "warthog", "hippo", "badger", "seal", "sea", "otter"
            ]):
                return True, det_id, det_ts, aud_sp
            if "rumble" in audio_words and ("elephant" in vis_words or "hippo" in vis_words):
                return True, det_id, det_ts, aud_sp

        return False, None, None, None
    except Exception as e:
        logging.error(f"Vision bridge error: {e}")
        return False, None, None, None


# ==============================================================================
# HEALTH SYNERGY & "PROOF OF SIGHT" LOGGING
# ==============================================================================
def check_stream_health(url):
    try:
        with db_connector.get_db_connection(force_local=True) as con:
            cur = con.cursor()
            cur.execute("SELECT status_note FROM stream_queue WHERE url = ?", (url,))
            row = cur.fetchone()
            if row and row[0]:
                status = row[0].upper()
                # THE DECOUPLING PATCH: Only skip if the stream is truly dead. Ignore UNRESPONSIVE.
                if any(bad in status for bad in ["TERMINAL", "SUSPENDED", "FATAL"]):
                    return status
    except Exception:
        pass 
    return None

def log_vision_health(url, status, message):
    try:
        with db_connector.get_db_connection(force_local=True) as con:
            con.execute("INSERT INTO stream_health_events (stream_url, timestamp, status, message) VALUES (?, ?, ?, ?)",
                        (url, time.time(), status, message))
    except Exception:
        pass

# ==============================================================================
# THE STEALTH HYDRA GUEST & QUOTA ENFORCER (WEIGHTED)
# ==============================================================================
def get_active_hydra_proxies(cfg):
    active_proxies =[]
    network_map = cfg.get("network_map", {})
    expected_interfaces = list(set(network_map.values()) - {"Default / OS"})
    
    # Read the smoothed EMA state to prevent jitter
    hydra_state = {}
    if HYDRA_STATE_FILE.exists():
        try: hydra_state = json.loads(HYDRA_STATE_FILE.read_text(encoding='utf-8'))
        except: pass

    # Read the master proxy map created by scheduler.py
    proxy_map = {}
    if PROXY_MAP_FILE.exists():
        try: proxy_map = json.loads(PROXY_MAP_FILE.read_text(encoding='utf-8'))
        except: pass
        
    pid_settings = cfg.get("hydra_pid_settings", {})
    soft_lockout_thresh = float(pid_settings.get("soft_lockout_pct", 90)) / 100.0
    throttling_enabled = pid_settings.get("throttling_enabled", True)
        
    for interface_name in expected_interfaces:
        proxy_url = proxy_map.get(interface_name)
        if not proxy_url:
            continue
            
        heat = 0.0
        limit_bytes = 0
        
        try:
            used_bytes, limit_bytes, is_over = network_manager.get_interface_quota_status(interface_name)
            
            # Fetch smoothed EMA heat from state file, fallback to raw calculation if missing
            i_data = hydra_state.get(interface_name)
            if i_data and isinstance(i_data, dict) and 'heat' in i_data:
                heat = i_data['heat']
            else:
                heat = network_manager.get_interface_heat(interface_name)
            
            if throttling_enabled:
                # THE DYNAMIC SOFT-LOCKOUT: Pre-emptively drop the SIM before it hits the hard ceiling.
                if is_over or heat >= soft_lockout_thresh:
                    v_logger.warning(f"[HYDRA-PID] {interface_name} is EXHAUSTED (Heat: {heat*100:.1f}%). Excluding from Vision pool.")
                    continue 
                weight = max(0.01, 1.0 - heat)
            else:
                # Throttling Disabled: Proportional workload sharing based on Monthly GB Plan
                if limit_bytes <= 0:
                    weight = 1000.0 * (1024**3) # 1000 GB fallback for unlimited plans
                else:
                    weight = limit_bytes
            
        except Exception:
            weight = 1.0 if not throttling_enabled else 0.5
            
        if interface_name != "Default / OS":
            weight_display = weight / (1024**3) if not throttling_enabled else weight
            weight_suffix = "GB" if not throttling_enabled else ""
            v_logger.info(f"[HYDRA-PID] Proxy Pool - {interface_name} | Heat: {heat*100:.1f}% | Final Weight: {weight_display:.2f}{weight_suffix}")
        
        try:
            port = int(proxy_url.split(':')[-1])
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                if s.connect_ex(('127.0.0.1', port)) == 0:
                    active_proxies.append({
                        "url": proxy_url,
                        "interface": interface_name,
                        "weight": weight
                    })
        except:
            pass
            
    return active_proxies

def update_config_with_new_url(stream_name, new_url):
    for attempt in range(5):
        try:
            with config_lock:
                if not CONFIG_FILE.exists():
                    return
                data = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                updated = False
                
                # Update the main streams list
                old_url = None
                for s in data.get('streams',[]):
                    if s.get('name') == stream_name and s.get('page_url') != new_url:
                        old_url = s.get('page_url')
                        s['page_url'] = new_url
                        s['updated_at'] = time.time()
                        updated = True
                        break
                        
                # --- THE URL TAG-ALONG PATCH ---
                # Also update the vision_ai enabled_streams list so we don't go blind
                if old_url and "vision_ai" in data and "enabled_streams" in data["vision_ai"]:
                    vision_enabled = data["vision_ai"]["enabled_streams"]
                    for i, u in enumerate(vision_enabled):
                        if u == old_url:
                            vision_enabled[i] = new_url
                            updated = True
                            v_logger.info(f"[{stream_name}] Auto-Healer successfully transferred Vision Checkbox state to new URL.")
                            break
                            
                if updated:
                    tmp_path = CONFIG_FILE.with_suffix('.tmp')
                    tmp_path.write_text(json.dumps(data, indent=2), encoding='utf-8')
                    os.replace(tmp_path, CONFIG_FILE)
            return data
        except Exception:
            time.sleep(0.5 + random.random())
    return None

# ==============================================================================
# PREDATOR REFLEX: THE ACOUSTIC BOUNTY HUNTER
# ==============================================================================
def get_acoustic_bounty(active_enabled_urls, processed_bounty_ids, reflex_threshold):
    try:
        cutoff = time.time() - 120.0
        with db_connector.get_db_connection(force_local=True) as con:
            cur = con.cursor()
            query = """
                SELECT d.id, d.channel_url, d.species, d.ai_notes, st.size_class, d.distance_category
                FROM detections d
                LEFT JOIN species_traits st ON d.species = st.species_name
                WHERE d.timestamp > ?
                  AND d.detection_method = 'audio'
                ORDER BY d.timestamp DESC
            """
            cur.execute(query, (cutoff,))
            rows = cur.fetchall()

            for row in rows:
                det_id = row[0]
                url = row[1]
                species = row[2]
                ai_notes = row[3] or ""
                size_class = row[4]
                dist_cat = row[5] or ""

                if url not in active_enabled_urls or det_id in processed_bounty_ids:
                    continue
                if size_class == 1:
                    continue
                if dist_cat not in["Point Blank", "Very Near", "Near"]:
                    continue

                conf_match = re.search(r"Confidence:\s*([0-9.]+)", ai_notes)
                if conf_match:
                    try:
                        conf_val = float(conf_match.group(1))
                        if conf_val >= reflex_threshold:
                            return url, species, det_id
                    except:
                        pass
    except Exception as e:
        v_logger.error(f"Error checking acoustic bounty: {e}")
    return None

# ==============================================================================
# IMAGE ANALYSIS (MOTION & ANTI-SMEAR)
# ==============================================================================
def is_image_smeared(img_path):
    """
    Checks for macroblock smearing. 
    Updated to sample the center of the screen to avoid triggering on 
    black letterboxing bars from 16:9 Chrome Viewports.
    """
    try:
        with Image.open(img_path) as img:
            gray_img = img.convert('L')
            w, h = gray_img.size
            
            # Sample the center 50% of the image (avoids top/bottom black bars)
            center_slice = gray_img.crop((int(w * 0.25), int(h * 0.25), int(w * 0.75), int(h * 0.75)))
            arr = np.array(center_slice)
            
            std_vertical = np.std(arr, axis=0)
            mean_vertical_std = np.mean(std_vertical)
            
            # Lowered threshold to 0.5 to prevent triggering on smooth gradients (like empty skies)
            if mean_vertical_std < 0.5:
                return True, mean_vertical_std
    except Exception:
        pass
    return False, 999.0

def check_motion(url, current_img, threshold_percent, previous_frames_dict):
    curr_small = current_img.convert("L").resize((128, 128))
    if url not in previous_frames_dict:
        previous_frames_dict[url] = curr_small
        return True, 100.0  
        
    prev_small = previous_frames_dict[url]
    diff = ImageChops.difference(prev_small, curr_small)
    stat = ImageStat.Stat(diff)
    
    mean_diff = stat.mean[0]
    diff_percent = (mean_diff / 255.0) * 100.0
    previous_frames_dict[url] = curr_small
    return diff_percent >= threshold_percent, diff_percent

def get_local_time_and_night_status(lon):
    if lon is None or lon == 0.0:
        return False, "Unknown"
    
    try:
        lon = float(lon)
        offset_hours = lon / 15.0
        local_time = datetime.utcnow() + timedelta(hours=offset_hours)
        
        hour = local_time.hour
        minute = local_time.minute
        time_val = hour + (minute / 60.0)
        
        is_solar_night = time_val >= 18.5 or time_val <= 6.0
        return is_solar_night, local_time.strftime("%H:%M")
    except:
        return False, "Unknown"

# ==============================================================================
# API VAULT & QUOTA MANAGEMENT
# ==============================================================================
def update_vision_config(updater_func):
    for attempt in range(5):
        try:
            with config_lock:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                updater_func(cfg)
                tmp = CONFIG_FILE.with_suffix('.tmp')
                tmp.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
                os.replace(tmp, CONFIG_FILE)
            return cfg
        except Exception:
            time.sleep(0.5 + random.random())
    return None

def sync_legacy_keys():
    def _updater(cfg):
        vcfg = cfg.setdefault("vision_ai", {})
        if "api_keys" not in vcfg:
            legacy = vcfg.get("api_key", "")
            if legacy:
                vcfg["api_keys"] =[{
                    "key": legacy, 
                    "tier": "Free", 
                    "status": "Active", 
                    "exhausted_until": 0, 
                    "usage_count": 0
                }]
    update_vision_config(_updater)

def get_next_reset_timestamp():
    now_utc = datetime.now(timezone.utc)
    next_reset = now_utc.replace(hour=8, minute=5, second=0, microsecond=0)
    if now_utc >= next_reset:
        next_reset += timedelta(days=1)
    return next_reset.timestamp()

def get_available_keys():
    try:
        with config_lock:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            keys = cfg.get("vision_ai", {}).get("api_keys",[])
            now_ts = time.time()
            needs_save = False
            
            free_keys = []
            paid_keys = []
            
            for k in keys:
                if k.get("status") == "Exhausted" and now_ts >= k.get("exhausted_until", 0):
                    k["status"] = "Active"
                    k["usage_count"] = 0
                    needs_save = True
                if k.get("status") != "Exhausted" and k.get("key", "").strip() != "":
                    if k.get("tier") == "Free":
                        free_keys.append(k)
                    else:
                        paid_keys.append(k)
                    
            if needs_save:
                cfg.setdefault("vision_ai", {})["api_keys"] = keys
                tmp = CONFIG_FILE.with_suffix('.tmp')
                tmp.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
                os.replace(tmp, CONFIG_FILE)
                
        # Shuffle the free keys so parallel workers don't all hit Key #1 simultaneously
        random.shuffle(free_keys)
        
        # Always append paid keys to the absolute bottom of the randomized free shield
        return free_keys + paid_keys
    except Exception:
        return[]

def mark_key_exhausted(bad_key_str):
    def _updater(cfg):
        keys = cfg.setdefault("vision_ai", {}).get("api_keys",[])
        for k in keys:
            if k.get("key") == bad_key_str:
                k["status"] = "Exhausted"
                k["exhausted_until"] = get_next_reset_timestamp()
    update_vision_config(_updater)

def increment_key_usage(used_key_str):
    def _updater(cfg):
        keys = cfg.setdefault("vision_ai", {}).get("api_keys",[])
        for k in keys:
            if k.get("key") == used_key_str:
                k["usage_count"] = k.get("usage_count", 0) + 1
    update_vision_config(_updater)

# ==============================================================================
# HIERARCHICAL TAXONOMIC RESOLUTION & TRANSLATION MAPS
# ==============================================================================
LEGACY_TRANSLATION = {
    "POINT BLANK": ("MASSIVE", "POINT BLANK"),
    "VERY NEAR": ("HUGE", "VERY NEAR"),
    "NEAR": ("LARGE", "NEAR"),
    "MID-RANGE": ("MEDIUM", "MID-GROUND"),
    "FAR": ("SMALL", "BACKGROUND"),
    "VERY FAR": ("TINY", "DEEP BACKGROUND"),
    "SPECK": ("SPECK", "HORIZON")
}

FRAME_MAP = {"MASSIVE": 7, "HUGE": 6, "LARGE": 5, "MEDIUM": 4, "SMALL": 3, "TINY": 2, "SPECK": 1, "NONE": 0}
DEPTH_MAP = {"POINT BLANK": 7, "VERY NEAR": 6, "NEAR": 5, "MID-GROUND": 4, "BACKGROUND": 3, "DEEP BACKGROUND": 2, "HORIZON": 1, "NONE": 0}
REVERSE_FRAME_MAP = {v: k for k, v in FRAME_MAP.items()}
REVERSE_DEPTH_MAP = {v: k for k, v in DEPTH_MAP.items()}

def get_dual_reqs(source_dict, prefix="min"):
    if not source_dict:
        return None, None, None, None
        
    def _clean_val(val):
        if not val: 
            return None
        v_up = str(val).upper()
        if v_up in["DEFAULT (AUTO)", "DON'T CHANGE (USE GLOBAL DEFAULTS)", "NONE", ""]:
            return None
        return val

    fs = _clean_val(source_dict.get(f"{prefix}_frame_single"))
    ds = _clean_val(source_dict.get(f"{prefix}_depth_single"))
    ff = _clean_val(source_dict.get(f"{prefix}_frame_flock"))
    df = _clean_val(source_dict.get(f"{prefix}_depth_flock"))
    
    legacy_s = _clean_val(source_dict.get("min_size_single") or source_dict.get("min_size"))
    if not fs and not ds and legacy_s:
        fs, ds = LEGACY_TRANSLATION.get(legacy_s.upper(), (None, None))
        
    legacy_f = _clean_val(source_dict.get("min_size_flock") or source_dict.get("min_size"))
    if not ff and not df and legacy_f:
        ff, df = LEGACY_TRANSLATION.get(legacy_f.upper(), (None, None))
        
    return fs, ds, ff, df

def load_vision_targets():
    with targets_lock:
        if VISION_TARGETS_FILE.exists():
            try: 
                return json.loads(VISION_TARGETS_FILE.read_text(encoding='utf-8'))
            except Exception: 
                pass
    return {"global_defaults": {"ignore_general_animals": False, "default_taxonomy_mute": 60, "master_system_prompt": "", "forbidden_words":[]}, "registry": {}, "assignments": {}, "stream_overrides": {}}

def save_vision_targets(data):
    with targets_lock:
        try: 
            VISION_TARGETS_FILE.write_text(json.dumps(data, indent=2), encoding='utf-8')
        except Exception as e: 
            v_logger.error(f"Failed to save vision targets: {e}")

def resolve_and_route(general, common, scientific, is_endemic, targets_data, enforce_targets=None):
    registry = targets_data.get("registry", {})
    default_mute = targets_data.get("global_defaults", {}).get("default_taxonomy_mute", 60)
    
    is_specific_guess = False
    
    # --- THE APOSTROPHE CAPITALIZATION PATCH ---
    if common and common.upper() != "NONE":
        final_name = common.title().replace("'S", "'s").strip()
        is_specific_guess = True
    elif scientific and scientific.upper() != "NONE":
        final_name = scientific.capitalize().strip()
        is_specific_guess = True
    elif general and general.upper() != "NONE":
        final_name = general.title().replace("'S", "'s").strip()
    else:
        final_name = "Unknown Animal"

    final_name = final_name.replace("  ", " ").strip()

    is_truly_specific = is_specific_guess or is_endemic

    for c_name, c_data in registry.items():
        if c_name.lower() == final_name.lower():
            return c_name, c_data, False, c_name
        synonyms =[s.lower().strip() for s in c_data.get("synonyms", [])]
        if final_name.lower() in synonyms:
            return c_name, c_data, False, c_name

    parent_category = general.title().replace("'S", "'s").strip() if general and general.upper() != "NONE" else None
    matched_parent = None
    
    if parent_category:
        if enforce_targets:
            for target in enforce_targets:
                if target.lower() in parent_category.lower() or parent_category.lower() in target.lower():
                    matched_parent = target
                    break
        if not matched_parent:
            for c_name, c_data in registry.items():
                if c_name.lower() == parent_category.lower():
                    matched_parent = c_name
                    break
                synonyms =[s.lower().strip() for s in c_data.get("synonyms", [])]
                if parent_category.lower() in synonyms:
                    matched_parent = c_name
                    break

    inherited_data = {
        "type": "Specific" if is_truly_specific else "General", 
        "synonyms":[], 
        "behavior": "Alert", 
        "cooldown_minutes": default_mute, 
        "show_on_map": True
    }

    if matched_parent and matched_parent in registry:
        parent_data = registry[matched_parent]
        parent_type = parent_data.get("type", "General")
        
        if is_truly_specific and parent_type == "General" and final_name.lower() != matched_parent.lower():
            v_logger.info(f"🚀 SMART BREAKOUT: '{final_name}' is Specific, escaping General parent '{matched_parent}' mute constraints.")
            inherited_data["behavior"] = "Alert"
            inherited_data["show_on_map"] = True
            inherited_data["cooldown_minutes"] = parent_data.get("cooldown_minutes", default_mute)
        else:
            v_logger.info(f"🧬 HIERARCHICAL INHERITANCE: '{final_name}' inheriting rules from parent '{matched_parent}'.")
            inherited_data["behavior"] = parent_data.get("behavior", "Alert")
            inherited_data["cooldown_minutes"] = parent_data.get("cooldown_minutes", default_mute)
            inherited_data["show_on_map"] = parent_data.get("show_on_map", True)
        
        if "min_size_single" in parent_data: inherited_data["min_size_single"] = parent_data["min_size_single"]
        if "min_size_flock" in parent_data: inherited_data["min_size_flock"] = parent_data["min_size_flock"]
        if "min_frame_single" in parent_data: inherited_data["min_frame_single"] = parent_data["min_frame_single"]
        if "min_depth_single" in parent_data: inherited_data["min_depth_single"] = parent_data["min_depth_single"]
        if "min_frame_flock" in parent_data: inherited_data["min_frame_flock"] = parent_data["min_frame_flock"]
        if "min_depth_flock" in parent_data: inherited_data["min_depth_flock"] = parent_data["min_depth_flock"]

    return final_name, inherited_data, True, matched_parent

# ==============================================================================
# JANITOR & MEDIA LOGIC
# ==============================================================================
def clean_vision_log(retention_hours=48):
    if not VISION_LOG_FILE.exists(): 
        return
    try:
        cutoff_time = datetime.now() - timedelta(hours=retention_hours)
        with open(VISION_LOG_FILE, 'r', encoding='utf-8', errors='ignore') as f: 
            lines = f.readlines()
        if not lines: 
            return
            
        kept_lines =[]
        date_pattern = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
        keep_current_block = True 
        
        for line in lines:
            match = date_pattern.match(line)
            if match:
                try:
                    log_time = datetime.strptime(match.group(1), '%Y-%m-%d %H:%M:%S')
                    keep_current_block = log_time >= cutoff_time
                except ValueError: 
                    pass 
            if keep_current_block: 
                kept_lines.append(line)
                
        if len(kept_lines) < len(lines):
            tmp_log = VISION_LOG_FILE.with_suffix('.tmp')
            with open(tmp_log, 'w', encoding='utf-8') as f: 
                f.writelines(kept_lines)
            try: 
                os.replace(tmp_log, VISION_LOG_FILE)
            except Exception: 
                if tmp_log.exists(): 
                    tmp_log.unlink()
    except Exception: 
        pass

def enforce_retention_limit(limit):
    try:
        from collections import defaultdict
        files_by_group = defaultdict(list)
        for file in VAULT_DIR.glob("*.jpg"):
            parts = file.stem.split('__')
            if len(parts) >= 2: 
                files_by_group[f"{parts[0]}__{parts[1]}"].append(file)
                
        for group, files in files_by_group.items():
            if len(files) > limit:
                files.sort(key=lambda x: x.stat().st_mtime)
                for file_to_delete in files[:-limit]: 
                    file_to_delete.unlink()
    except Exception: 
        pass

def sweep_orphaned_snapshots_auto(max_age_hours):
    if not VAULT_DIR.exists(): 
        return
    try:
        cutoff_time = time.time() - (max_age_hours * 3600)
        with db_connector.get_db_connection(force_local=True) as con:
            cur = con.cursor()
            cur.execute("SELECT vision_path FROM detections WHERE vision_path IS NOT NULL")
            valid_paths = {str(Path(r[0]).resolve()).lower() for r in cur.fetchall() if r[0]}
            
        for f in VAULT_DIR.glob("*.jpg"):
            if f.stat().st_mtime < cutoff_time:
                if str(f.resolve()).lower() not in valid_paths:
                    try: 
                        f.unlink()
                    except Exception: 
                        pass
    except Exception: 
        pass

# ==============================================================================
# THE CONCURRENT VISION WORKER
# ==============================================================================
def process_vision_stream(url, bounty_species, bounty_det_id, active_proxies, strict_proxy, res_val, cookies_path, 
                          use_motion_detector, motion_sensitivity, global_temp, g_fs, g_ds, g_ff, g_df, 
                          cfg, streams_data, previous_frames_dict):
    """
    Isolated worker function to process a single vision stream.
    All variables passed to this function must be thread-safe.
    """
    tid = threading.get_ident()
    temp_img = VAULT_DIR / f"temp_vision_grab_{tid}_{int(time.time()*1000)}.jpg"
    
    eco_cfg = cfg.get("economic_control", {})
    ai_model = eco_cfg.get("ai_model", "gemini-3.7-flash")
    if ai_model == "custom":
        ai_model = eco_cfg.get("custom_ai_model", "gemini-3.7-flash")
    if not ai_model.strip():
        ai_model = "gemini-3.7-flash"
        
    try:
        targets_data = load_vision_targets()
        stream_overrides = targets_data.get("stream_overrides", {})
        
        default_master_prompt = "ANTI-PAREIDOLIA GUARDRAIL: You are looking at a wildlife camera. These cameras (especially nests and feeders) are frequently EMPTY. Do NOT mistake sticks, twigs, grass, mud, or shadows for animals. If the scene is empty of clear, living biological creatures, you MUST output 'NONE'."
        master_system_prompt = targets_data.get("global_defaults", {}).get("master_system_prompt", "").strip()
        if not master_system_prompt:
            master_system_prompt = default_master_prompt
        
        stream = streams_data.get(url)
        if not stream: 
            return
        
        audio_health_status = check_stream_health(url)
        if audio_health_status:
            v_logger.info(f"[{stream['name']}] Skipping: Audio Engine flagged as {audio_health_status}")
            return
        
        if active_proxies:
            weights =[p.get('weight', 1.0) for p in active_proxies]
            chosen_proxy_dict = random.choices(active_proxies, weights=weights, k=1)[0]
        else:
            chosen_proxy_dict = None
        
        if chosen_proxy_dict:
            chosen_proxy = chosen_proxy_dict['url']
            chosen_interface = chosen_proxy_dict['interface']
            proxy_label = f"[Proxy: {chosen_proxy.split(':')[-1]} | {chosen_interface}]"
        else:
            chosen_proxy = None
            chosen_interface = "Default / OS"
            proxy_label = "[Direct IP / OS Fallback]"
            
            if strict_proxy:
                v_logger.warning(f"[{stream['name']}] Skipping: Strict Proxy is ON but no SIM proxies are available.")
                return
        
        if not bounty_species:
            v_logger.info(f"[{stream['name']}] Scanning... {proxy_label}[Res: {res_val}p]")
        
        raw_url, used_proxy, dynamic_headers = extract_stream_url(
            stream['page_url'], 
            cookies_path=cookies_path, 
            proxy_url=chosen_proxy, 
            resolution=res_val, 
            strict_proxy=strict_proxy
        )
        
        if not raw_url: 
            return
        
        proxy_for_ffmpeg = chosen_proxy if used_proxy else None
        
        combined_headers = dynamic_headers.copy() if dynamic_headers else []
        original_url = stream.get('original_url', '')
        if original_url and not any("Referer:" in h for h in combined_headers):
            combined_headers.append(f"Referer: {original_url}")
        
        grab_success = grab_video_frame(raw_url, temp_img, headers_list=combined_headers, proxy_url=proxy_for_ffmpeg)
        
        if grab_success and used_proxy and chosen_interface != "Default / OS":
            try:
                network_manager.log_app_usage(chosen_interface, 'vision', int(8.0 * 1024 * 1024))
            except Exception: 
                pass
        
        if not grab_success and stream_resolver:
            original_url = stream.get('original_url', stream.get('page_url'))
            is_youtube = "youtube.com" in original_url.lower() or "youtu.be" in original_url.lower()
            
            if not is_youtube:
                v_logger.info(f"[{stream['name']}] Attempting Magic Wand Auto-Healer...")
                try:
                    fast_mode = cfg.get("fast_mode_enabled", True)
                    result = stream_resolver.resolve_stream_url(original_url, proxy_url=proxy_for_ffmpeg, fast_mode=fast_mode)
                    
                    if result and len(result) == 3:
                        links, stype, msg = result
                        if links:
                            new_url = links[0].replace(r"\u0026", "&").replace(r"\/", "/")
                            update_config_with_new_url(stream['name'], new_url)
                            grab_success = grab_video_frame(new_url, temp_img, headers_list=[f"Referer: {original_url}"], proxy_url=proxy_for_ffmpeg)
                except Exception as ex:
                    v_logger.error(f"[{stream['name']}] Auto-Healer exception: {ex}")
        
        if grab_success:
            smeared, smear_val = is_image_smeared(temp_img)
            if smeared:
                v_logger.warning(f"[{stream['name']}] 🟪 Frame Corruption (Macroblock Smear) detected (Vertical StdDev: {smear_val:.2f}). Dropping frame to protect AI.")
                log_vision_health(url, "VISION_GLITCH", "Corrupted frame dropped (Packet Loss)")
                return

            try:
                s_overrides = stream_overrides.get(url, {})
                env_type = s_overrides.get("environment_type", "Auto-Detect")
                is_aquatic = env_type in["Underwater", "Hybrid/Variable"]

                img_to_analyze = Image.open(temp_img)
                stat = ImageStat.Stat(img_to_analyze)
                avg_pixel_val = sum(stat.mean) / len(stat.mean)
                brightness_pct = (avg_pixel_val / 255.0) * 100.0
                
                if brightness_pct < 2.0:
                    v_logger.info(f"[{stream['name']}] ⬛ Black frame detected ({brightness_pct:.1f}% brightness). Skipping AI.")
                    log_vision_health(url, "VISION_DARK", "Black frame (<2% brightness)")
                    return
                    
                color_variance = 0.0
                if len(stat.mean) >= 3:
                    r, g, b = stat.mean[:3]
                    color_variance = max(abs(r-g), abs(g-b), abs(b-r))
                    
                contrast = sum(stat.stddev) / len(stat.stddev)
                
                is_monochrome = color_variance < 5.0
                is_dark = brightness_pct < 30.0
                is_low_contrast = contrast < 40.0
                
                if is_aquatic:
                    apply_visibility_penalty = is_dark
                    adjusted_sensitivity = max(0.5, motion_sensitivity * 0.6)
                else:
                    apply_visibility_penalty = (is_dark or is_monochrome) and is_low_contrast
                    adjusted_sensitivity = motion_sensitivity
                
                if use_motion_detector and not bounty_species:
                    has_motion, diff_pct = check_motion(url, img_to_analyze, adjusted_sensitivity, previous_frames_dict)
                    if not has_motion:
                        v_logger.info(f"[{stream['name']}] 💨 No Motion ({diff_pct:.1f}%). Skipping Gemini.")
                        log_vision_health(url, "VISION_NO_MOTION", f"Skipped (Motion {diff_pct:.1f}%)")
                        return
                    else:
                        v_logger.info(f"[{stream['name']}] 🌊 Motion Triggered! ({diff_pct:.1f}% changed). Sending to AI...")
                
                lon_val = stream.get('lon', 0.0)
                is_solar_night, local_time_str = get_local_time_and_night_status(lon_val)

                stream_assignments = targets_data.get("assignments", {}).get(url,[])
                enforce_targets =[t["name"] for t in stream_assignments if t["rule"] == "Enforce Target"]
                local_ignores = [t["name"] for t in stream_assignments if t["rule"] == "Local Ignore"]
                watchlist_str = ", ".join(enforce_targets) if enforce_targets else "None"
                
                global_custom_prompt = s_overrides.get("custom_prompt", "").strip()
                
                registry = targets_data.get("registry", {})
                injected_prompts =[]
                
                for tgt in enforce_targets:
                    t_data = registry.get(tgt, {})
                    g_prompt = t_data.get("global_prompt", "").strip()
                    if g_prompt:
                        injected_prompts.append(f"- For {tgt} (Global Rule): {g_prompt}")
                
                is_night_mode = apply_visibility_penalty or is_solar_night
                
                day_species_rules = s_overrides.get("species_rules", {})
                night_species_rules = s_overrides.get("night_species_rules", {})
                
                def get_active_rules(animal_name):
                    if is_night_mode and animal_name in night_species_rules:
                        return night_species_rules[animal_name]
                    return day_species_rules.get(animal_name, {})
                
                all_tuned_animals = set(day_species_rules.keys()).union(set(night_species_rules.keys()))
                for sp in all_tuned_animals:
                    rules = get_active_rules(sp)
                    if "prompt" in rules and rules["prompt"]:
                        injected_prompts.append(f"- For {sp} (Stream Rule): {rules['prompt']}")
                        
                custom_instruction_block = ""
                
                forbidden_words = targets_data.get("global_defaults", {}).get("forbidden_words",[])
                if forbidden_words:
                    f_words_str = ", ".join(f"'{w}'" for w in forbidden_words)
                    custom_instruction_block += f"\nCRITICAL TAXONOMY GUARDRAIL: You are STRICTLY FORBIDDEN from using any of these words in your 'common_endemic_name' or 'general_animal' output:[{f_words_str}]. If you are unsure, make your best specific guess, or output 'NONE'.\n"

                # --- THE DYNAMIC BLINDSPOT PATCH (OPTION C) ---
                cooldown_animals = []
                now_ts = time.time()
                try:
                    if COOLDOWN_STATE_PATH.exists():
                        with cooldown_lock:
                            state_dict = json.loads(COOLDOWN_STATE_PATH.read_text(encoding='utf-8'))
                        
                        default_mute = targets_data.get("global_defaults", {}).get("default_taxonomy_mute", 60)
                        prefix = f"vision||{url}||".lower()
                        
                        reg_lower_map = {k.lower(): k for k in targets_data.get("registry", {}).keys()}
                        
                        for k, last_seen in state_dict.items():
                            if k.startswith(prefix):
                                sp_lower = k.split("||")[2]
                                proper_sp = reg_lower_map.get(sp_lower, sp_lower.title())
                                
                                cd_mins = targets_data.get("registry", {}).get(proper_sp, {}).get("cooldown_minutes", default_mute)
                                if (now_ts - last_seen) < (cd_mins * 60):
                                    cooldown_animals.append(proper_sp)
                                    
                        if cooldown_animals:
                            cd_animals_str = ", ".join([f"'{a}'" for a in cooldown_animals])
                            blindspot_prompt = f"\nCRITICAL RULE: IF the most prominent animal you see in this image is one of these: [{cd_animals_str}], you must ignore it. Instead, report the next most prominent DIFFERENT animal. IF there are no other animals besides those on this list, or if the frame is empty, you MUST output 'NONE'."
                            custom_instruction_block += blindspot_prompt + "\n"
                except Exception as e:
                    v_logger.error(f"Failed to read cooldowns for blindspot logic: {e}")
                # -----------------------------------------------
                
                if bounty_species:
                    bounty_prompt = f"🚨 ACOUSTIC CUE ACTIVE: A high-confidence audio detection of a '{bounty_species}' was recorded at this location just seconds ago. Scan the environment carefully for this specific animal. HOWEVER, you MUST NOT hallucinate it out of rocks, shadows, or branches. If the animal is off-camera, hidden, or you are not 100% certain, you MUST output 'NONE'."
                    custom_instruction_block += f"\n{bounty_prompt}\n"
                
                if is_night_mode:
                    night_prompt = f"LOW LIGHT / IR MODE DETECTED (Local Time: {local_time_str}, Brightness: {brightness_pct:.1f}%, Contrast: {contrast:.1f}): This is likely a night-vision or low-light camera. Do NOT mistake IR glare, floating dust, or dark shadows for animals. Be extremely conservative. If uncertain, output 'NONE'."
                    custom_instruction_block += f"\n{night_prompt}\n"
                    
                if global_custom_prompt:
                    custom_instruction_block += f"\nGLOBAL CAMERA INSTRUCTION: {global_custom_prompt}\n"
                if injected_prompts:
                    custom_instruction_block += "\nSPECIFIC ANIMAL INSTRUCTIONS:\n" + "\n".join(injected_prompts) + "\n"
                
                prompt = f"""
                Analyze this nature/wildlife camera image for animals or marine life. 
                Camera Context (Name): "{stream['name']}"
                Location: Latitude {stream.get('lat', 0)}, Longitude {stream.get('lon', 0)}.
                
                {master_system_prompt}
                
                Task: Identify medium-to-large animals (mammals, birds, reptiles, amphibians, fish, marine life) if present.
                Do NOT identify small insects, vague shadows, rocks, or trees.
                {custom_instruction_block}
                YOUR CAMERA WATCHLIST:[{watchlist_str}]
                
                CRITICAL GUARDRAIL: You may use the Camera Context for clues, but you MUST NOT hallucinate animals just because they are mentioned in the name. You must rely purely on clear visual evidence.
                
                Instructions:
                1. Scan the image for animals.
                2. "general_animal": The broad category of the animal. IF the animal belongs to a category on your WATCHLIST, you MUST output that exact watchlist category here (e.g. if watchlist has 'Monkey', output 'Monkey').
                3. "common_endemic_name": Provide the MOST SPECIFIC common name or sub-species possible (e.g., 'Rhesus Macaque', 'Bald Eagle', 'Great White Shark'). Do NOT just repeat the general category if you know the specific species.
                4. "visible_features": Describe the specific biological features you can clearly see. If empty or it looks like a rock/tree, state "None".
                5. "location_in_frame": Briefly describe exactly where the animal is located in the image (e.g., 'bottom-left corner near the water', 'center background on a branch').
                6. "frame_size": Choose exactly one from this 7-Level Scale based purely on how much of the 2D pixel space the animal occupies:
                    - "MASSIVE": Animal takes up the majority of the entire image.
                    - "HUGE": Animal dominates a large quadrant of the image.
                    - "LARGE": Animal is a major subject, clearly taking up significant screen space.
                    - "MEDIUM": Animal is distinct but occupies a modest footprint.
                    - "SMALL": Animal takes up a minor portion of the frame.
                    - "TINY": Animal is very small in terms of pixels.
                    - "SPECK": Animal is barely a few pixels wide, essentially a dot.
                    If no animal, "NONE".
                7. "distance_category": Choose exactly one from this 7-Level Scale based on the 3D physical depth of the animal in the real-world scene:
                    - "POINT BLANK": Animal is right up against the camera lens.
                    - "VERY NEAR": Animal is in the immediate foreground.
                    - "NEAR": Animal is in the middle-ground, close to the camera station.
                    - "MID-GROUND": Animal is in the main viewing area, standard distance.
                    - "BACKGROUND": Animal is far away, behind the main subjects.
                    - "DEEP BACKGROUND": Animal is very far away in the distant background or water.
                    - "HORIZON": Animal is at the absolute edge of visibility on the horizon.
                    If no animal, "NONE".
                8. "is_endemic_confirmed": Boolean. Can you confirm the exact regional species without guessing?
                9. "scientific_name": The Latin species name. If false, "NONE".
                10. "is_group": Boolean. Are there multiple individuals of this type clearly visible?

                Return ONLY JSON:
                {{
                    "visible_features": "...",
                    "location_in_frame": "...",
                    "frame_size": "...",
                    "distance_category": "...",
                    "general_animal": "...",
                    "is_endemic_confirmed": true,
                    "common_endemic_name": "...",
                    "scientific_name": "...",
                    "is_group": false
                }}
                """
                
                response = None
                used_key = None
                active_keys = get_available_keys()
                
                if not active_keys:
                    v_logger.error(f"[{stream['name']}] 🔴 CRITICAL: All API keys Exhausted. Paused.")
                    return
                    
                for attempt in range(3):
                    last_err_str = ""
                    try:
                        # Iterate through available keys (no global lock needed!)
                        for key_obj in active_keys:
                            target_api_key = key_obj['key']
                            try:
                                # REMOVED HTTP OPTIONS TO FIX PYDANTIC CRASH
                                client = genai.Client(api_key=target_api_key)
                                response = client.models.generate_content(
                                    model=ai_model, 
                                    contents=[img_to_analyze, prompt],
                                    config=types.GenerateContentConfig(
                                        temperature=global_temp,
                                        response_mime_type="application/json"
                                    )
                                )
                                used_key = target_api_key
                                break # Success! Break out of key loop
                            except Exception as api_e:
                                err_str = str(api_e).lower()
                                last_err_str = err_str
                                if "429" in err_str or "quota" in err_str or "exhausted" in err_str or "too many" in err_str:
                                    if "per day" in err_str or "daily" in err_str:
                                        # Actual Daily Limit Hit - Permanently lock the key for 24 hours
                                        mark_key_exhausted(target_api_key)
                                        v_logger.warning(f"Key ending in ...{target_api_key[-4:]} hit DAILY limit. Banned for 24h.")
                                    # We do NOT sleep here anymore. Instantly try the next key.
                                    continue
                                else:
                                    raise api_e # Immediate raise for non-429 errors (e.g. 503, bad request)
                        
                        if response:
                            break # Success! Break out of attempt loop
                        else:
                            # If we get here, ALL keys were looped through and hit 429s (RPM limits)
                            raise Exception(f"All available keys hit RPM speed limits. Last error: {last_err_str}")
                            
                    except Exception as network_e:
                        if attempt == 2:
                            v_logger.error(f"[{stream['name']}] Gemini API Pivot Failed after 3 attempts: {network_e}")
                        else:
                            time.sleep(2.0) # Sleep on the ATTEMPT loop to let Google cool down, not the KEY loop
                
                if not response:
                    return
                    
                increment_key_usage(used_key)
                
                log_vision_health(url, "VISION_SUCCESS", "Frame analyzed by AI")

                raw_text = response.text.strip()
                if raw_text.startswith('```'):
                    raw_text = re.sub(r'^```[a-zA-Z]*\n', '', raw_text)
                    raw_text = re.sub(r'\n```$', '', raw_text).strip()
                    
                try:
                    data = json.loads(raw_text)
                    if isinstance(data, list): data = data[0] if len(data) > 0 else {}
                    if not isinstance(data, dict): data = {}
                except Exception:
                    data = {}
                
                visible_features = data.get("visible_features", "None")
                location_in_frame = data.get("location_in_frame", "Unknown location")
                ai_notes_str = f"Location: {location_in_frame}\nFeatures: {visible_features}"
                
                if apply_visibility_penalty:
                    ai_notes_str = f"🌙[LOW-VISIBILITY PENALTY APPLIED] " + ai_notes_str
                elif is_solar_night:
                    ai_notes_str = f"🌙[SOLAR NIGHT] " + ai_notes_str
                    
                if bounty_species:
                    ai_notes_str = f"🎯[ACOUSTIC REFLEX CUE: {bounty_species}] " + ai_notes_str
                
                ai_frame = data.get("frame_size", "NONE").upper()
                ai_depth = data.get("distance_category", "NONE").upper()
                
                # --- THE APOSTROPHE CAPITALIZATION PATCH ---
                general_animal = data.get("general_animal", "NONE").title().replace("'S", "'s").strip()
                is_endemic_confirmed = bool(data.get("is_endemic_confirmed", False))
                common_endemic = data.get("common_endemic_name", "NONE").title().replace("'S", "'s").strip()
                scientific = data.get("scientific_name", "NONE").capitalize().strip()
                is_group = bool(data.get("is_group", False))

                if general_animal.upper() == "NONE" and common_endemic.upper() == "NONE":
                    v_logger.info(f"[{stream['name']}] ⚪ Gemini sees nothing. Reason: {visible_features}")
                    return
                    
                final_species, c_data, is_new, parent_category = resolve_and_route(
                    general_animal, common_endemic, scientific, is_endemic_confirmed, targets_data, enforce_targets
                )
                
                if final_species in local_ignores or (parent_category and parent_category in local_ignores):
                    v_logger.info(f"[{stream['name']}] ⚫ {final_species} (Parent: {parent_category}) is LOCALLY IGNORED on this camera. Dropping.")
                    return
                
                if is_new and c_data["type"] == "General" and targets_data.get("global_defaults", {}).get("ignore_general_animals", False):
                    return
                    
                if c_data.get("behavior") == "Ignore":
                    return
                    
                parent_rules = get_active_rules(parent_category) if parent_category else {}
                sp_rules = get_active_rules(final_species)
                
                req_fs, req_ds, req_ff, req_df = get_dual_reqs(sp_rules)
                if not req_fs: req_fs, req_ds, req_ff, req_df = get_dual_reqs(parent_rules)
                if not req_fs: req_fs, req_ds, req_ff, req_df = get_dual_reqs(s_overrides)
                if not req_fs: req_fs, req_ds, req_ff, req_df = get_dual_reqs(c_data)
                
                req_frame = (req_ff if is_group else req_fs) or (g_ff if is_group else g_fs)
                req_depth = (req_df if is_group else req_ds) or (g_df if is_group else g_ds)
                
                ai_f_val = FRAME_MAP.get(ai_frame, 0)
                req_f_val = FRAME_MAP.get(req_frame.upper(), 0) if req_frame else 0
                
                ai_d_val = DEPTH_MAP.get(ai_depth, 0)
                req_d_val = DEPTH_MAP.get(req_depth.upper(), 0) if req_depth else 0
                
                if apply_visibility_penalty:
                    if req_f_val > 0 and req_f_val < 7: req_f_val += 1
                    if req_d_val > 0 and req_d_val < 7: req_d_val += 1
                    
                pass_frame = ai_f_val >= req_f_val
                pass_depth = ai_d_val >= req_d_val

                filter_reasons =[]
                is_public = True

                if c_data.get("behavior", "Alert") == "Silent Log":
                    is_public = False
                    filter_reasons.append("Taxonomy behavior is set to 'Silent Log'")

                if not c_data.get("show_on_map", True):
                    is_public = False
                    filter_reasons.append("Taxonomy is marked as Private / Hidden from Map")
                        
                if not (pass_frame and pass_depth):
                    is_public = False
                    req_frame_text = REVERSE_FRAME_MAP.get(req_f_val, "NONE").upper()
                    req_depth_text = REVERSE_DEPTH_MAP.get(req_d_val, "NONE").title()
                    eval_mode = "Flock/Herd" if is_group else "Single"
                    night_tag = "[Low-Vis Penalty Active]" if apply_visibility_penalty else ""
                    filter_reasons.append(f"Failed Size/Depth constraints (Evaluated as {eval_mode}{night_tag}). (Frame: {ai_frame} vs Req {req_frame_text}, Depth: {ai_depth.title()} vs Req {req_depth_text})")
                    
                filter_reason_str = " | ".join(filter_reasons)
                
                dist_category = f"{ai_depth.title()}"
                if not (pass_frame and pass_depth):
                    dist_category = f"Curate: {ai_depth.title()}"
                if is_group:
                    dist_category += " (Flock/Herd)"

                is_multimodal, audio_det_id, audio_timestamp, audio_species = check_taxonomy_bridge(url, final_species)
                det_method = "multimodal" if is_multimodal else "vision"

                cd_key = f"vision||{url}||{final_species}".lower()
                now = time.time()
                cd_seconds = c_data.get("cooldown_minutes", 60) * 60
                
                with cooldown_lock:
                    fresh_cooldowns = {}
                    if COOLDOWN_STATE_PATH.exists():
                        try: fresh_cooldowns = json.loads(COOLDOWN_STATE_PATH.read_text(encoding='utf-8'))
                        except: pass
                        
                    last_seen = fresh_cooldowns.get(cd_key, 0)
                    on_cooldown = (now - last_seen) < cd_seconds
                    
                    if not on_cooldown:
                        fresh_cooldowns[cd_key] = now
                        COOLDOWN_STATE_PATH.write_text(json.dumps(fresh_cooldowns))
                
                if on_cooldown and not is_multimodal:
                    v_logger.info(f"[{stream['name']}] 🟡 {final_species} is on cooldown. Skipping.")
                    return
                    
                if on_cooldown and is_multimodal:
                    is_public = False
                    if filter_reason_str:
                        filter_reason_str += " | On Vision Cooldown"
                    else:
                        filter_reason_str = "On Vision Cooldown"
                
                if is_new:
                    fresh_targets = load_vision_targets()
                    if "registry" not in fresh_targets: fresh_targets["registry"] = {}
                    fresh_targets["registry"][final_species] = c_data
                    save_vision_targets(fresh_targets)
                    v_logger.info(f"[{stream['name']}] ✨ AUTO-DISCOVERY: Added '{final_species}' to Global Registry safely.")
                
                clean_stream_name = "".join(x for x in stream['name'] if x.isalnum() or x in " _-")[:20]
                clean_species = "".join(x for x in final_species if x.isalnum() or x in " _-")
                ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                perm_filename = f"{clean_stream_name}____{clean_species}__{ts_str}.jpg"
                perm_path = VAULT_DIR / perm_filename
                img_to_analyze.save(perm_path)
                    
                audio_file_path = None
                if is_multimodal and audio_det_id:
                    p_mp3 = BASELINE_CLIPS_DIR / f"detection_{audio_det_id}.mp3"
                    p_wav = BASELINE_CLIPS_DIR / f"detection_{audio_det_id}.wav"
                    audio_file_path = p_mp3 if p_mp3.exists() else (p_wav if p_wav.exists() else None)

                alert_sent_val = 1 if is_public else 0
                det_timestamp = time.time()
                bot_token = cfg.get("bot_token")
                chat_id = cfg.get("chat_id")
                
                cloud_cfg = cfg.get('database_cloud', {})
                if cloud_cfg.get('enabled') and bot_token and (is_public or is_multimodal):
                    proxies_dict = {"http": chosen_proxy, "https": chosen_proxy} if chosen_proxy else None
                    
                    for attempt in range(3):
                        try:
                            current_proxies = proxies_dict if attempt < 2 else None
                            
                            if is_multimodal and audio_timestamp:
                                payload = {
                                    'secret_token': bot_token, 
                                    'original_timestamp': str(audio_timestamp), 
                                    'channel_url': url, 
                                    'distance_category': dist_category, 
                                    'alert_sent': "true" if is_public else "false", 
                                    'frame_size': ai_frame,
                                    'filter_reason': filter_reason_str,
                                    'ai_notes': ai_notes_str
                                }
                                res = requests.post("https://wilddetection.com/api/upgrade_multimodal", data=payload, timeout=30, verify=True, proxies=current_proxies)
                                
                                if res.status_code == 404:
                                    payload = {
                                        'secret_token': bot_token, 'timestamp': str(det_timestamp), 'channel_url': url, 
                                        'species': final_species, 'latitude': str(stream.get('lat', '')), 'longitude': str(stream.get('lon', '')), 
                                        'distance_category': dist_category, 'snr': "99.9", 'alert_sent': "true" if is_public else "false", 
                                        'listener_id': "VISION_ENGINE", 'network_interface': "Local", 'detection_method': det_method,
                                        'frame_size': ai_frame, 'filter_reason': filter_reason_str, 'ai_notes': ai_notes_str
                                    }
                                    requests.post("https://wilddetection.com/api/inject_detection", data=payload, timeout=30, verify=True, proxies=current_proxies)
                            else:
                                payload = {
                                    'secret_token': bot_token, 'timestamp': str(det_timestamp), 'channel_url': url, 
                                    'species': final_species, 'latitude': str(stream.get('lat', '')), 'longitude': str(stream.get('lon', '')), 
                                    'distance_category': dist_category, 'snr': "99.9", 'alert_sent': "true" if is_public else "false", 
                                    'listener_id': "VISION_ENGINE", 'network_interface': "Local", 'detection_method': det_method,
                                    'frame_size': ai_frame, 'filter_reason': filter_reason_str, 'ai_notes': ai_notes_str
                                }
                                requests.post("https://wilddetection.com/api/inject_detection", data=payload, timeout=30, verify=True, proxies=current_proxies)
                            
                            break # Success
                        except requests.exceptions.ReadTimeout:
                            v_logger.warning(f"[{stream['name']}] Cloud API ReadTimeout (Server is processing). Preventing duplicate resend.")
                            break
                        except Exception as cloud_e: 
                            if attempt == 2:
                                v_logger.error(f"[{stream['name']}] Cloud DB Pivot Error after 3 attempts: {cloud_e}")
                            else:
                                time.sleep(1.0)

                try:
                    with db_connector.get_db_connection(force_local=True) as con:
                        if is_multimodal and audio_det_id:
                            con.execute(
                                """UPDATE detections 
                                   SET detection_method = 'multimodal', distance_category = ?, 
                                       vision_path = ?, ai_notes = ?, frame_size = ?, filter_reason = ?, alert_sent = MAX(alert_sent, ?)
                                   WHERE id = ?""", 
                                (dist_category, str(perm_path), ai_notes_str, ai_frame, filter_reason_str, alert_sent_val, audio_det_id)
                            )
                        else:
                            con.execute(
                                "INSERT INTO detections (timestamp, channel_url, species, distance_category, detection_method, vision_path, human_verified, alert_sent, ai_notes, frame_size, filter_reason) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)", 
                                (det_timestamp, url, final_species, dist_category, det_method, str(perm_path), alert_sent_val, ai_notes_str, ai_frame, filter_reason_str)
                            )
                except Exception as e: 
                    v_logger.error(f"[{stream['name']}] Local DB Insert/Update Error: {e}")
                    
                time_icon = "🌙" if is_solar_night else "☀️"
                
                display_species = audio_species if is_multimodal else final_species
                
                if is_public:
                    v_logger.info(f"[{stream['name']}] 🟢 GEMINI PUBLIC ALERT: {time_icon} {display_species} (Frame: {ai_frame} | Depth: {ai_depth})")
                else:
                    v_logger.info(f"[{stream['name']}] 🟠 ADMIN TELEGRAM (Filtered): {time_icon} {display_species}. Reason(s): {filter_reason_str}")
                    
                tg_prefs = cfg.get("vision_ai", {}).get("telegram_alerts", {
                    "enabled": True,
                    "alert_multimodal": True,
                    "alert_public": True,
                    "alert_filtered": False,
                    "attach_image": True,
                    "include_reasoning": True
                })
                
                send_audio_all = cfg.get('send_audio_telegram_all', False)
                send_audio_multi = cfg.get('send_audio_telegram_multi', True)
                should_send_audio = send_audio_all or (is_multimodal and send_audio_multi)

                should_send_tg = tg_prefs.get("enabled", True)
                if should_send_tg:
                    if is_multimodal and not tg_prefs.get("alert_multimodal", True):
                        should_send_tg = False
                    elif not is_multimodal and is_public and not tg_prefs.get("alert_public", True):
                        should_send_tg = False
                    elif not is_multimodal and not is_public and not tg_prefs.get("alert_filtered", False):
                        should_send_tg = False
                        
                if should_send_tg and bot_token and chat_id:
                    safe_species = html.escape(display_species)
                    safe_stream = html.escape(stream['name'])
                    safe_dist = html.escape(dist_category)
                    safe_frame = html.escape(ai_frame)
                    safe_net = html.escape(chosen_interface)
                    
                    if is_public:
                        header = "<b>[ 🌍 PUBLIC: SENT TO WEB MAP ]</b>\n"
                    else:
                        safe_reason = html.escape(filter_reason_str)
                        header = f"<b>[ 🛑 FILTERED: Not sent to Web Map ]</b>\n<b>Reason(s):</b> {safe_reason}\n\n"
                    
                    icon = "🔥 MULTIMODAL UPGRADE" if is_multimodal else "📷 VISION DETECTED"
                    
                    search_url = f"https://www.google.com/search?q={quote_plus(display_species)}"
                    
                    base_caption = f"{header}{time_icon} {icon}: <a href='{search_url}'><b>{safe_species}</b></a>\nStream: {safe_stream}\nDepth: {safe_dist}\nFrame Size: {safe_frame}\n📡 VISION_ENGINE | Net: {safe_net}"
                    
                    if tg_prefs.get("include_reasoning", True):
                        room_left = 1000 - len(base_caption) 
                        if room_left > 50:
                            truncated_notes = ai_notes_str
                            if len(truncated_notes) > room_left:
                                truncated_notes = truncated_notes[:room_left] + "..."
                            safe_notes = html.escape(truncated_notes)
                            caption = base_caption + f"\n\n<b>AI Notes:</b>\n<i>{safe_notes}</i>"
                        else:
                            caption = base_caption
                    else:
                        caption = base_caption
                        
                    try:
                        proxies_dict = {"http": chosen_proxy, "https": chosen_proxy} if chosen_proxy else None
                        
                        for attempt in range(3):
                            try:
                                current_proxies = proxies_dict if attempt < 2 else None
                                
                                audio_sent = False
                                if should_send_audio and audio_file_path and audio_file_path.exists():
                                    with open(audio_file_path, 'rb') as af:
                                        mime_type = "audio/mpeg" if audio_file_path.suffix == '.mp3' else "audio/wav"
                                        file_tuple = (audio_file_path.name, af, mime_type)
                                        res = requests.post(
                                            f"https://api.telegram.org/bot{bot_token}/sendAudio", 
                                            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}, 
                                            files={"audio": file_tuple}, 
                                            timeout=45, proxies=current_proxies
                                        )
                                        if res.status_code == 200:
                                            audio_sent = True
                                        else:
                                            v_logger.error(f"[{stream['name']}] Telegram Audio Error: {res.status_code} - {res.text}")

                                if tg_prefs.get("attach_image", True) and perm_path.exists():
                                    photo_caption = f"📷 Accompanying Vision for {safe_species}" if audio_sent else caption
                                    with open(perm_path, 'rb') as photo:
                                        res = requests.post(f"https://api.telegram.org/bot{bot_token}/sendPhoto", data={"chat_id": chat_id, "caption": photo_caption, "parse_mode": "HTML"}, files={"photo": photo}, timeout=30, proxies=current_proxies)
                                        if res.status_code != 200:
                                            v_logger.error(f"[{stream['name']}] Telegram Photo Error: {res.status_code} - {res.text}")
                                elif not audio_sent:
                                    res = requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": caption, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=30, proxies=current_proxies)
                                    if res.status_code != 200:
                                        v_logger.error(f"[{stream['name']}] Telegram Message Error: {res.status_code} - {res.text}")
                                        
                                break 
                            except requests.exceptions.ReadTimeout:
                                v_logger.warning(f"[{stream['name']}] Telegram API ReadTimeout. Preventing duplicate message.")
                                break
                            except Exception as tel_e: 
                                if attempt == 2:
                                    v_logger.error(f"[{stream['name']}] Telegram Post Error after 3 attempts: {tel_e}")
                                else:
                                    time.sleep(1.5)
                    except Exception as wrap_e:
                        v_logger.error(f"[{stream['name']}] Telegram Broadcast Logic Error: {wrap_e}")

            except Exception as e: 
                v_logger.error(f"[{stream.get('name', url)}] Execution Error: {e}")
                
    finally:
        if temp_img.exists():
            try: temp_img.unlink()
            except: pass


# ==============================================================================
# MAIN LOOP
# ==============================================================================
def main():
    v_logger.info("=========================================================================")
    v_logger.info("--- MULTIMODAL VISION ENGINE STARTED (The Decoupling Refactor) ---")
    v_logger.info("=========================================================================")
    
    ensure_node_in_path()
    
    previous_frames_dict = {}
    last_vision_scan_times = {}
    dormant_logged_streams = set()
    
    while True:
        try:
            v_logger.info("--- New Vision Cycle Starting ---")
            
            if not CONFIG_FILE.exists():
                time.sleep(10)
                continue
                
            with config_lock:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                
            vision_cfg = cfg.get("vision_ai", {})
            if not vision_cfg.get("enabled", False):
                time.sleep(30)
                continue
                
            enabled_urls = vision_cfg.get("enabled_streams",[])
            if not enabled_urls:
                time.sleep(30)
                continue
                
            streams_data = {s['page_url']: s for s in cfg.get('streams',[]) if s.get('enabled', True)}
            
            active_enabled_urls = list(set([url for url in enabled_urls if url in streams_data]))
            
            if not active_enabled_urls:
                time.sleep(30)
                continue

            random.shuffle(active_enabled_urls)
            
            last_visual_activity_map = {}
            try:
                with db_connector.get_db_connection(force_local=True) as con:
                    cur = con.cursor()
                    placeholders = ','.join(['?'] * len(active_enabled_urls))
                    cur.execute(f"SELECT channel_url, MAX(timestamp) FROM detections WHERE channel_url IN ({placeholders}) AND detection_method IN ('vision', 'multimodal') GROUP BY channel_url", active_enabled_urls)
                    for r in cur.fetchall():
                        last_visual_activity_map[r[0]] = r[1]
            except Exception as e:
                v_logger.error(f"Failed to fetch last visual activity map: {e}")

            vision_workers = vision_cfg.get("vision_workers", 1)
            cycle_interval = vision_cfg.get("cycle_interval_seconds", 60)
            
            dormancy_threshold_mins = vision_cfg.get("vision_dormancy_threshold_mins", 60)
            dormant_interval_mins = vision_cfg.get("vision_dormant_interval_mins", 20)
            
            retention_limit = vision_cfg.get("retention_limit", 5)
            log_retention_hours = vision_cfg.get("log_retention_hours", 48)
            auto_sweep_hours = vision_cfg.get("auto_sweep_vault_hours", 24)
            use_motion_detector = vision_cfg.get("use_motion_detector", True)
            motion_sensitivity = vision_cfg.get("motion_sensitivity_percent", 5.0)
            
            global_temp = vision_cfg.get("ai_temperature", 0.15)
            
            # --- GLOBAL DEFAULTS ---
            default_min_single = vision_cfg.get("min_size_single", "Near").upper()
            default_min_flock = vision_cfg.get("min_size_flock", "Far").upper()
            
            g_fs = vision_cfg.get("min_frame_single")
            g_ds = vision_cfg.get("min_depth_single")
            if not g_fs or not g_ds: 
                g_fs, g_ds = LEGACY_TRANSLATION.get(default_min_single, ("LARGE", "NEAR"))
                
            g_ff = vision_cfg.get("min_frame_flock")
            g_df = vision_cfg.get("min_depth_flock")
            if not g_ff or not g_df: 
                g_ff, g_df = LEGACY_TRANSLATION.get(default_min_flock, ("SMALL", "BACKGROUND"))
            
            predator_threshold = vision_cfg.get("predator_reflex_threshold", 85) / 100.0
            
            # --- THE EXTRACTION STRATEGY TIES ---
            ext_strat = cfg.get("extraction_strategy", {})
            vision_res_str = ext_strat.get("vision_resolution", "720p")
            res_match = re.search(r'\d+', vision_res_str)
            res_val = res_match.group(0) if res_match else "720"
            strict_proxy = ext_strat.get("strict_proxy", False)
            
            # --- THE GLOBAL PID EXPONENTIAL BRAKE ---
            network_map = cfg.get("network_map", {})
            expected_interfaces = list(set(network_map.values()) - {"Default / OS"})
            
            pid_settings = cfg.get("hydra_pid_settings", {})
            global_brake_thresh = float(pid_settings.get("global_brake_pct", 95)) / 100.0
            throttling_enabled = pid_settings.get("throttling_enabled", True)
            
            global_heat = 0.0
            if expected_interfaces:
                hydra_state = {}
                if HYDRA_STATE_FILE.exists():
                    try: hydra_state = json.loads(HYDRA_STATE_FILE.read_text(encoding='utf-8'))
                    except: pass
                
                g_data = hydra_state.get("GLOBAL")
                if g_data and isinstance(g_data, dict) and 'heat' in g_data:
                    global_heat = g_data['heat']
                else:
                    global_heat = network_manager.get_global_network_heat(expected_interfaces)
            
            if throttling_enabled:
                capped_heat = min(global_brake_thresh - 0.01, global_heat) 
                exp_multiplier = 1.0 / (1.0 - capped_heat)
                
                # Cap the exponential multiplier to a maximum of 1.5x to prevent the Vision Engine 
                # from entering an extreme multi-hour coma during network heat events, while still
                # respecting the base Cruise Control budget.
                safe_multiplier = min(1.5, exp_multiplier)
                padded_cycle_interval = cycle_interval * safe_multiplier
            else:
                exp_multiplier = 1.0
                padded_cycle_interval = cycle_interval

            # --- THE DRIP-FEED TURNSTILE PATCH ---
            total_streams = len(active_enabled_urls)
            drip_delay = padded_cycle_interval / total_streams if total_streams > 0 else 60.0
            
            if throttling_enabled:
                v_logger.info(f"[HYDRA-PID] Main Pipe Aggregate Heat: {global_heat*100:.1f}%. Stretching Cycle from {cycle_interval}s to {padded_cycle_interval:.1f}s.")
                v_logger.info(f"[HYDRA-PID] Drip-feeding {total_streams} streams (Delay: {drip_delay:.1f}s per stream).")
            else:
                v_logger.info(f"[HYDRA-PID] ⚠️ THROTTLING DISABLED. Drip-feeding {total_streams} streams (Delay: {drip_delay:.1f}s).")
            
            active_proxies = get_active_hydra_proxies(cfg)
            
            if expected_interfaces and not active_proxies and strict_proxy:
                if throttling_enabled:
                    v_logger.warning("[HYDRA-PID] 🔴 ALL ASSIGNED SIMS ARE EXHAUSTED (Hit Soft-Lockout)! Strict Proxy is ON. Engaging 60s emergency sleep to prevent OS fallback leak...")
                else:
                    v_logger.warning("[HYDRA-PID] 🔴 ALL ASSIGNED SIMS ARE OFFLINE OR BANNED! Strict Proxy is ON. Engaging 60s emergency sleep to prevent OS fallback leak...")
                time.sleep(60)
                continue
            
            scanned_this_cycle = set()
            processed_bounty_ids = set()
            
            v_logger.info(f"Initializing {vision_workers} Concurrent Worker(s) for {len(active_enabled_urls)} Streams...")

            cycle_start_time = time.time()
            next_submit_time = time.time()
            
            # --- THE MULTI-THREADED POOL ---
            with concurrent.futures.ThreadPoolExecutor(max_workers=vision_workers) as executor:
                futures = {}
                
                while len(scanned_this_cycle) < len(active_enabled_urls) or futures:
                    
                    while len(futures) < vision_workers and len(scanned_this_cycle) < len(active_enabled_urls):
                        
                        now = time.time()
                        # Drip-Feed Turnstile: Sleep if we are trying to submit faster than the allotted drip pace.
                        # We skip this for the first few workers to quickly fill the initial pool.
                        if len(scanned_this_cycle) >= vision_workers and now < next_submit_time:
                            sleep_duration = next_submit_time - now
                            time.sleep(sleep_duration)
                            
                        bounty_info = get_acoustic_bounty(active_enabled_urls, processed_bounty_ids, predator_threshold)
                        bounty_species = None
                        bounty_det_id = None
                        url_to_scan = None
                        
                        if bounty_info:
                            bounty_url, bounty_species, bounty_det_id = bounty_info
                            if bounty_url in futures.values():
                                bounty_info = None 
                            else:
                                processed_bounty_ids.add(bounty_det_id)
                                url_to_scan = bounty_url
                                v_logger.info(f"🚨 PREDATOR REFLEX TRIGGERED: High-confidence '{bounty_species}' heard! Forcing visual focus to stream...")
                        
                        if not url_to_scan:
                            for u in active_enabled_urls:
                                if u not in scanned_this_cycle and u not in futures.values():
                                    
                                    # --- PURE VISUAL DORMANCY BACKOFF LOGIC ---
                                    if dormancy_threshold_mins > 0:
                                        now_ts = time.time()
                                        last_visual_ts = last_visual_activity_map.get(u, 0)
                                        idle_time_mins = (now_ts - last_visual_ts) / 60.0
                                        
                                        if idle_time_mins >= dormancy_threshold_mins:
                                            last_scan_ts = last_vision_scan_times.get(u, 0)
                                            time_since_last_scan_mins = (now_ts - last_scan_ts) / 60.0
                                            
                                            if time_since_last_scan_mins < dormant_interval_mins:
                                                if u not in dormant_logged_streams:
                                                    st_name = streams_data.get(u, {}).get('name', u)
                                                    v_logger.info(f"[{st_name}] 💤 No visual detections for {idle_time_mins:.1f}m. Entering Dormancy Backoff (Checking every {dormant_interval_mins}m).")
                                                    dormant_logged_streams.add(u)
                                                scanned_this_cycle.add(u)
                                                continue 
                                                
                                    if u in dormant_logged_streams and dormancy_threshold_mins > 0:
                                        st_name = streams_data.get(u, {}).get('name', u)
                                        v_logger.info(f"[{st_name}] ⚡ Visual activity detected! Waking up from Dormancy Backoff.")
                                        dormant_logged_streams.remove(u)
                                        
                                    url_to_scan = u
                                    break
                                    
                        if url_to_scan:
                            scanned_this_cycle.add(url_to_scan)
                            last_vision_scan_times[url_to_scan] = time.time()
                            
                            future = executor.submit(
                                process_vision_stream,
                                url_to_scan, bounty_species, bounty_det_id,
                                active_proxies, strict_proxy, res_val, None,
                                use_motion_detector, motion_sensitivity, global_temp,
                                g_fs, g_ds, g_ff, g_df, 
                                cfg, streams_data, previous_frames_dict
                            )
                            futures[future] = url_to_scan
                            
                            # Advance the turnstile clock
                            if len(scanned_this_cycle) >= vision_workers:
                                next_submit_time = time.time() + drip_delay
                        else:
                            break 
                            
                    if futures:
                        # Wait for at least one future to complete
                        done, _ = concurrent.futures.wait(futures.keys(), return_when=concurrent.futures.FIRST_COMPLETED)
                        for f in done:
                            finished_url = futures.pop(f)
                            try:
                                f.result()
                            except Exception as e:
                                v_logger.error(f"[{finished_url}] Vision Processing Thread Error: {e}\n{traceback.format_exc()}")
                
            # Maintenance Block
            enforce_retention_limit(retention_limit)
            clean_vision_log(log_retention_hours)
            sweep_orphaned_snapshots_auto(auto_sweep_hours)
            
            elapsed = time.time() - cycle_start_time
            remaining = padded_cycle_interval - elapsed
            
            if remaining > 0:
                v_logger.info(f"Vision Cycle complete early (Crashes/Fast scans). Sleeping {remaining:.1f}s to respect global pacing budget...")
                time.sleep(remaining)
            else:
                v_logger.info("Vision Cycle complete. Resetting for next loop...")
                time.sleep(15)
            
        except Exception as e:
            v_logger.error(f"Vision Scheduler Main Crash: {e}\n{traceback.format_exc()}")
            time.sleep(30)

if __name__ == "__main__":
    main()