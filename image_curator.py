# FILE: image_curator.py
# VERSION: 15.10 - "The False Ban Patch"
# RESPONSIBILITY: Instant Local UI load, background scraping, Cloud sync, and AI Content/License Moderation.
# UPDATED: Fixed a massive flaw where Google's generic 15 RPM Speed Limit error was triggering the 24-hour Daily Quota ban. The script now ONLY bans a key if the error explicitly says "per day" or "daily".

import sys
import json
import logging
import requests
import webbrowser
import random
import threading
from pathlib import Path
from urllib.parse import quote_plus
from io import BytesIO
import time
from datetime import datetime, timezone, timedelta
import re
import sqlite3
import unicodedata
import os

try:
    from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                                 QLabel, QTableWidget, QTableWidgetItem, QHeaderView,
                                 QPushButton, QMessageBox, QLineEdit, QFormLayout,
                                 QMenu, QDialog, QDialogButtonBox, QComboBox, QCheckBox)
    from PyQt6.QtCore import QTimer, Qt, QThread, pyqtSignal
    from PyQt6.QtGui import QPixmap, QAction, QColor, QBrush, QFont
    from PIL import Image
    
    # --- NEW AI MODERATION IMPORTS ---
    from google import genai
    from google.genai import types
except ImportError as e:
    print(f"FATAL: A required library is missing: {e}")
    sys.exit(1)

# --- INTERNAL MODULES ---
ROOT = Path(__file__).resolve().parent
sys.path.append(str(ROOT))
import db_connector

# --- CONFIGURATION ---
CONFIG_FILE = ROOT / "birdnet_config.json"
DASHBOARD_SETTINGS_FILE = ROOT / "dashboard_settings.json"
IMAGE_DB_PATH = ROOT / "image_database.db"
TARGETS_FILE = ROOT / "bioacoustic_targets.json"
THUMBNAIL_SIZE = (400, 400) 

log_file = ROOT / "monitor_debug.txt"
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s -[%(filename)s:%(lineno)d] - %(message)s',
                    handlers=[logging.FileHandler(log_file, mode='a'), logging.StreamHandler()])

# --- CATEGORY LISTS ---

# STRICT EXACT-MATCH NOISE LIST (Synced with Dashboard)
BIRDNET_NOISE_CLASSES = {
    "Siren", "Dog", "Motor vehicle (road)", "Car alarm", "Human voice", 
    "Human narrator", "Human whistling", "Human vocal", "Human footstep", 
    "Engine", "Wind", "Rain", "Gunshot, gunfire", "Fireworks", "Noise", "Car"
}

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

GENERAL_ANIMALS = {
    "animal", "bird", "mammal", "reptile", "amphibian", "insect", "fish", "unknown", "unknown animal",
    "elephant", "lion", "tiger", "bear", "wolf", "coyote", "fox", "hyena", "leopard", "jaguar", "panther",
    "monkey", "ape", "gorilla", "baboon", "macaque", "lemur", "primate",
    "zebra", "horse", "cow", "cattle", "sheep", "goat", "pig", "boar", "deer", "elk", "moose", "antelope",
    "seal", "sea lion", "walrus", "otter", "whale", "dolphin",
    "frog", "toad", "snake", "crocodile", "alligator",
    "cricket", "cicada", "grasshopper", "katydid",
    "penguin", "badger", "wildebeest", "giraffe", "rhino", "rhinoceros", "mongoose", "jackal", "hyrax", "stork", "ostrich"
}

# --- THREAD LOCKS ---
config_lock = threading.Lock()

def is_general_animal(name):
    clean = name.strip().lower()
    if "(" in clean: return False 
    return clean in GENERAL_ANIMALS

def get_species_category(name, visual_species_set):
    name_clean = name.strip()
    if visual_species_set and name_clean in visual_species_set: return 'VISUAL'
    name_lower = name_clean.lower()
    bird_exceptions =["heron", "frogmouth", "cowbird", "egret", "finch", "dove", "tyrant", "woodpecker", "duck", "goose", "swan", "sparrow", "weaver", "bunting", "warbler", "thrush", "hawk", "eagle", "falcon", "owl", "gull", "tern", "wren", "bird"]
    for exc in bird_exceptions:
        if re.search(r'\b' + re.escape(exc) + r'\b', name_lower): return 'BIRD'
    for kw in BIOACOUSTIC_KEYWORDS:
        if re.search(r'\b' + re.escape(kw) + r'\b', name_lower): return 'DSP'
    if TARGETS_FILE.exists():
        try:
            raw = json.loads(TARGETS_FILE.read_text(encoding='utf-8'))
            if raw.get("_version") == 2:
                for url, targets in raw.get("assignments", {}).items():
                    for t in targets:
                        if isinstance(t, dict) and 'display' in t:
                            if t['display'].strip().lower() == name_lower: return 'DSP'
            else:
                for val in raw.values():
                    if isinstance(val, list):
                        for v in val:
                            t_name = v['display'].strip().lower() if isinstance(v, dict) else v.strip().lower()
                            if t_name == name_lower: return 'DSP'
                    elif isinstance(val, str):
                        if val.strip().lower() == name_lower: return 'DSP'
        except: pass
    return 'BIRD'

def patch_database_encoding():
    """Ensures the image database and all required tables/columns exist so we don't get infinite fetch loops."""
    con = None
    try:
        con = sqlite3.connect(str(IMAGE_DB_PATH), timeout=15)
        cur = con.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS species_images (
                species_name TEXT PRIMARY KEY,
                image_data BLOB,
                source_url TEXT,
                last_updated TEXT,
                status TEXT DEFAULT 'PENDING'
            )
        ''')
        
        cur.execute("DELETE FROM species_images WHERE species_name LIKE '%Siren%' OR species_name LIKE '%Engine%'")
        cur.execute("PRAGMA table_info(species_images)")
        cols = [row[1] for row in cur.fetchall()]
        if 'license_info' not in cols: cur.execute("ALTER TABLE species_images ADD COLUMN license_info TEXT DEFAULT 'Unknown'")
        if 'ai_notes' not in cols: cur.execute("ALTER TABLE species_images ADD COLUMN ai_notes TEXT")
        con.commit()
    except Exception as e:
        logging.error(f"Image Database Patch failed: {e}")
    finally:
        if con: con.close()

def strip_accents(s):
    return ''.join(c for c in unicodedata.normalize('NFD', str(s)) if unicodedata.category(c) != 'Mn')

def is_noise(name):
    # STRICT EXACT MATCHING - Prevents "Black-Tailed Prairie Dog" from being hidden by "Dog"
    return name.strip() in BIRDNET_NOISE_CLASSES

# --- GEMINI QUOTA MANAGEMENT ---
def get_available_keys():
    try:
        with config_lock:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            keys = cfg.get("vision_ai", {}).get("api_keys",[])
            now_ts = time.time()
            valid_keys =[]
            for k in keys:
                if k.get("status") == "Exhausted" and now_ts >= k.get("exhausted_until", 0):
                    k["status"] = "Active"
                    k["usage_count"] = 0
                if k.get("status") != "Exhausted" and k.get("key", "").strip() != "":
                    valid_keys.append(k)
                    
        # Separate free and paid, shuffle free to prevent hitting speed limits
        free_keys = [k for k in valid_keys if k.get("tier") == "Free"]
        paid_keys = [k for k in valid_keys if k.get("tier") != "Free"]
        
        random.shuffle(free_keys)
        return free_keys + paid_keys
    except Exception: return[]

def mark_key_exhausted(bad_key_str):
    try:
        with config_lock:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f: cfg = json.load(f)
            now_utc = datetime.now(timezone.utc)
            next_reset = now_utc.replace(hour=8, minute=5, second=0, microsecond=0)
            if now_utc >= next_reset: next_reset += timedelta(days=1)
            for k in cfg.setdefault("vision_ai", {}).get("api_keys",[]):
                if k.get("key") == bad_key_str:
                    k["status"] = "Exhausted"
                    k["exhausted_until"] = next_reset.timestamp()
            tmp = CONFIG_FILE.with_suffix('.tmp')
            tmp.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
            os.replace(tmp, CONFIG_FILE)
    except: pass

def run_ai_moderation(image_bytes, species_name):
    forbidden_words =[]
    try:
        if DASHBOARD_SETTINGS_FILE.exists():
            s = json.loads(DASHBOARD_SETTINGS_FILE.read_text(encoding='utf-8'))
            forbidden_words = s.get("image_moderation_rules",[])
    except: pass

    active_keys = get_available_keys()
    if not active_keys:
        return 'UNCHECKED', "No API key available"

    prompt = f"""
    You are an extremely strict AI Moderator for a scientific wildlife mapping application. 
    Analyze this image which is supposedly a "{species_name}".
    
    Task 1: Content Filtering. Flag the image IF it contains ANY of the following:
    - R-rated, explicit, or inappropriate content.
    - Cities, buildings, vehicles, street signs, or large text.
    - Human singers, music bands, album covers, or concert stages (very common if the animal shares a name with a band).
    - Custom forbidden words: {', '.join(forbidden_words) if forbidden_words else 'None'}.
    
    Task 2: Quality Filtering. Flag the image IF:
    - It is severely blurry or artifacted.
    - It is purely a map, drawing, or diagram instead of a photo.
    - It is a stuffed animal, taxidermy, museum specimen, toy, or statue.
    - It is completely unrecognizable as an animal.
    
    Return ONLY valid JSON with exactly these keys:
    {{
        "contains_forbidden": true or false,
        "forbidden_reason": "Brief explanation if true, else empty",
        "is_low_quality": true or false,
        "quality_reason": "Brief explanation if true, else empty"
    }}
    """

    img = Image.open(BytesIO(image_bytes))
    response = None

    for key_obj in active_keys:
        api_key = key_obj['key']
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model='gemini-2.5-flash', 
                contents=[img, prompt],
                config=types.GenerateContentConfig(temperature=0.1, response_mime_type="application/json")
            )
            break
        except Exception as e:
            err_str = str(e).lower()
            # --- THE FALSE BAN PATCH ---
            if "429" in err_str or "quota" in err_str or "exhausted" in err_str or "too many" in err_str:
                if "per day" in err_str or "daily" in err_str:
                    mark_key_exhausted(api_key)
                else:
                    time.sleep(1.0)
                continue
            else: break

    if not response:
        return 'UNCHECKED', "API Error or Quota Exhausted"

    raw_text = response.text.strip()
    if raw_text.startswith('```'):
        raw_text = re.sub(r'^```[a-zA-Z]*\n', '', raw_text)
        raw_text = re.sub(r'\n```$', '', raw_text).strip()
        
    try:
        data = json.loads(raw_text)
        if data.get("contains_forbidden"):
            return "FLAGGED_CONTENT", f"AI Flag: {data.get('forbidden_reason')}"
        if data.get("is_low_quality"):
            return "FLAGGED_QUALITY", f"AI Flag: {data.get('quality_reason')}"
        return "OK", "Passed AI Verification"
    except Exception as e:
        return 'UNCHECKED', f"Failed to parse AI response: {e}"

# --- UTILS & CUSTOM WIDGETS ---
class NumericTableWidgetItem(QTableWidgetItem):
    def __lt__(self, other):
        try: return int(self.text()) < int(other.text())
        except: return super().__lt__(other)

# --- THE C++ SEGFAULT / SORTING PATCH ---
class DateTimeTableWidgetItem(QTableWidgetItem):
    def __lt__(self, other): 
        try:
            v1 = float(self.data(Qt.ItemDataRole.UserRole) or 0.0)
            v2 = float(other.data(Qt.ItemDataRole.UserRole) or 0.0)
            return v1 < v2
        except (ValueError, TypeError):
            return super().__lt__(other)

class AIModerationConfigDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI Content Moderation Rules")
        self.resize(500, 300)
        self.layout = QVBoxLayout(self)
        
        info = QLabel("Enter custom words/topics that Gemini should strictly reject from the Web Map. (e.g., 'album cover', 'concert', 'cartoon').")
        info.setWordWrap(True)
        self.layout.addWidget(info)
        
        self.edit_rules = QLineEdit()
        self.edit_rules.setPlaceholderText("Comma-separated list...")
        self.layout.addWidget(self.edit_rules)
        
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.layout.addStretch()
        self.layout.addWidget(self.buttons)
        
        try:
            if DASHBOARD_SETTINGS_FILE.exists():
                s = json.loads(DASHBOARD_SETTINGS_FILE.read_text(encoding='utf-8'))
                rules = s.get("image_moderation_rules",[])
                self.edit_rules.setText(", ".join(rules))
        except: pass

    def get_data(self):
        rules =[r.strip() for r in self.edit_rules.text().split(",") if r.strip()]
        return rules, False 

class URLDialog(QDialog):
    def __init__(self, species_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Set Image URL for {species_name}")
        self.layout = QVBoxLayout(self)
        self.label = QLabel(f"Paste the full image URL for '{species_name}':")
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://...")
        
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        
        self.layout.addWidget(self.label)
        self.layout.addWidget(self.url_input)
        self.layout.addWidget(self.buttons)
        
    def get_url(self): 
        return self.url_input.text().strip()

class ClickableImageLabel(QLabel):
    doubleClicked = pyqtSignal()
    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit()
        try:
            super().mouseDoubleClickEvent(event)
        except RuntimeError:
            pass

class ImageViewerDialog(QDialog):
    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Image Viewer")
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background-color: #000;")
        self.original_pixmap = pixmap
        self.layout.addWidget(self.image_label)
        self.btn_close = QPushButton("CLOSE VIEWER")
        self.btn_close.setStyleSheet("background-color: #333; color: white; font-weight: bold; font-size: 16px; padding: 15px; border: none;")
        self.btn_close.clicked.connect(self.accept)
        self.layout.addWidget(self.btn_close)
        try:
            if DASHBOARD_SETTINGS_FILE.exists():
                s = json.loads(DASHBOARD_SETTINGS_FILE.read_text(encoding='utf-8'))
                geom = s.get('curator_viewer_geometry')
                if geom: self.setGeometry(geom['x'], geom['y'], geom['width'], geom['height'])
                else: self.resize(800, 600)
            else: self.resize(800, 600)
        except: self.resize(800, 600)
        self._update_pixmap()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_pixmap()

    def _update_pixmap(self):
        if not self.original_pixmap.isNull():
            scaled = self.original_pixmap.scaled(self.image_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.image_label.setPixmap(scaled)

    def closeEvent(self, event):
        try:
            s = json.loads(DASHBOARD_SETTINGS_FILE.read_text(encoding='utf-8')) if DASHBOARD_SETTINGS_FILE.exists() else {}
            geom = self.geometry()
            s['curator_viewer_geometry'] = {'x': geom.x(), 'y': geom.y(), 'width': geom.width(), 'height': geom.height()}
            DASHBOARD_SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding='utf-8')
        except: pass
        super().closeEvent(event)


# --- DATABASE INTERFACE ---
def get_species_by_status(status_list: list):
    if not IMAGE_DB_PATH.exists(): return set()
    con = None
    try:
        con = sqlite3.connect(str(IMAGE_DB_PATH), timeout=15)
        cur = con.cursor()
        placeholders = ','.join(['?'] * len(status_list))
        cur.execute(f"SELECT species_name FROM species_images WHERE status IN ({placeholders})", tuple(status_list))
        return {row[0] for row in cur.fetchall()}
    except Exception as e:
        logging.error(f"Query execution failed in get_species_by_status: {e}")
        return set()
    finally:
        if con: con.close()

def push_image_to_cloud(species_name, image_data, source_url, status):
    try:
        if not CONFIG_FILE.exists(): return
        config = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        bot_token = config.get("bot_token", "")
        if not bot_token: return
        files = {}; files['image_file'] = (f"{species_name}.jpg", image_data, 'image/jpeg') if image_data else ("dummy.txt", b"failed", 'text/plain')
        data = {'secret_token': bot_token, 'species_name': species_name, 'source_url': source_url, 'status': status}
        requests.post("https://wilddetection.com/api/upload_image", files=files, data=data, timeout=15, verify=True)
    except Exception as e: 
        logging.error(f"Cloud image push failed for {species_name}: {e}")

def store_image_in_db(species_name, image_data, source_url, status, license_info="Unknown", ai_notes=""):
    con = None
    try:
        con = sqlite3.connect(str(IMAGE_DB_PATH), timeout=15)
        cur = con.cursor()
        try:
            cur.execute("INSERT OR REPLACE INTO species_images (species_name, image_data, source_url, last_updated, status, license_info, ai_notes) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                       (species_name, image_data, source_url, time.strftime('%Y-%m-%d %H:%M:%S'), status, license_info, ai_notes))
        except sqlite3.OperationalError:
            # --- THE SCHEMA AGNOSTIC FALLBACK PATCH ---
            cur.execute("INSERT OR REPLACE INTO species_images (species_name, image_data, source_url, last_updated, status) VALUES (?, ?, ?, ?, ?)", 
                       (species_name, image_data, source_url, time.strftime('%Y-%m-%d %H:%M:%S'), status))
        con.commit()
    except Exception as e: 
        logging.error(f"Failed to store image in DB for {species_name}: {e}")
    finally:
        if con: con.close()
        
    push_image_to_cloud(species_name, image_data, source_url, status)

def reset_failed_fetches():
    if not IMAGE_DB_PATH.exists(): return "No local DB."
    con = None
    try:
        con = sqlite3.connect(str(IMAGE_DB_PATH), timeout=15)
        cur = con.cursor()
        cur.execute("UPDATE species_images SET status = 'PENDING' WHERE status = 'FETCH_FAILED' OR status LIKE 'FLAGGED_%'")
        con.commit()
        return "Failed status reset."
    except Exception as e: 
        logging.error(f"Failed to reset fetches: {e}")
        return str(e)
    finally:
        if con: con.close()

# --- WORKER THREADS ---
class SyncThread(QThread):
    progress_update = pyqtSignal(str)
    sync_finished = pyqtSignal()

    def run(self):
        try:
            self.progress_update.emit("Status: Checking targets...")
            all_detected_species = set()
            visual_species = set()
            
            with db_connector.get_db_connection(force_local=True) as con:
                cur = con.cursor()
                try:
                    cur.execute("SELECT DISTINCT species FROM detections")
                    all_detected_species = {row[0].strip() for row in cur.fetchall() if not is_noise(row[0].strip())}
                except: pass
                
                try:
                    cur.execute("SELECT DISTINCT species FROM detections WHERE detection_method IN ('vision', 'multimodal')")
                    visual_species = {row[0].strip() for row in cur.fetchall()}
                except: pass

            processed_species = get_species_by_status(['OK', 'FETCH_FAILED', 'FLAGGED_CONTENT', 'FLAGGED_QUALITY', 'FLAGGED_LEGAL', 'UNCHECKED'])
            new_species_to_fetch = all_detected_species - processed_species

            if new_species_to_fetch:
                logging.info(f"Found {len(new_species_to_fetch)} new species.")
                sorted_new_species = sorted(list(new_species_to_fetch))
                for i, species in enumerate(sorted_new_species):
                    if is_noise(species): continue
                    self.progress_update.emit(f"Status: Fetching image {i+1}/{len(sorted_new_species)} for '{species}'...")
                    self.process_single_species(species, visual_species)
                    time.sleep(1.5) 
            
            self.progress_update.emit("Status: Sync complete.")
        except Exception as e:
            self.progress_update.emit(f"Status: Error: {e}")
        finally:
            self.sync_finished.emit()

    def process_single_species(self, species_name, visual_species):
        image_data, source_url, status, license_info, ai_notes = self.advanced_fetch_strategy(species_name, visual_species)
        store_image_in_db(species_name, image_data, source_url, status, license_info, ai_notes)

    def advanced_fetch_strategy(self, species_name, visual_species):
        cat = get_species_category(species_name, visual_species)
        is_bird = (cat == 'BIRD')

        def fetch(search_term):
            return self.fetch_wikipedia_image(search_term, species_name)

        if is_bird:
            res = fetch(f"{species_name} bird")
            if res[0]: return res
            res = fetch(species_name)
            if res[0]: return res
        else:
            search_name = species_name
            if "(" in species_name and ")" in species_name:
                match = re.search(r'\((.*?)\)', species_name)
                if match: search_name = match.group(1)

            if search_name.lower() == "cricket": search_name = "Cricket insect"
            elif search_name.lower() == "panda": search_name = "Giant panda"

            res = fetch(search_name)
            if res[0]: return res
            
            res = fetch(f"{search_name} animal")
            if res[0]: return res
            
            base_name = species_name.split('(')[0].strip()
            if base_name and base_name != search_name:
                 res = fetch(base_name)
                 if res[0]: return res
                 res = fetch(f"{base_name} animal")
                 if res[0]: return res

        return None, None, 'FETCH_FAILED', 'Unknown', 'Fetch failed'

    def fetch_wikipedia_image(self, search_term, original_species_name):
        try:
            headers = { 'User-Agent': 'BirdNET-Monitor/7.0 (Biological Curator)' }
            search_url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={quote_plus(search_term)}&format=json&srlimit=5"
            r = requests.get(search_url, headers=headers, timeout=10)
            results = r.json().get("query", {}).get("search",[])
            if not results: return None, None, None, None, None

            for result in results:
                title = result["title"]
                if "List of" in title or "disambiguation" in title.lower(): continue
                
                # Extended API call to get Licensing Data
                prop_url = f"https://en.wikipedia.org/w/api.php?action=query&titles={quote_plus(title)}&prop=pageimages|imageinfo&iiprop=extmetadata&format=json&pithumbsize=800"
                r2 = requests.get(prop_url, headers=headers, timeout=10)
                pages = r2.json().get("query", {}).get("pages", {})
                
                for _, page_data in pages.items():
                    if "thumbnail" in page_data:
                        img_url = page_data["thumbnail"]["source"]
                        lower_url = img_url.lower()
                        if any(bad in lower_url for bad in['map', 'range', 'distribution', '.svg', 'icon', 'logo', 'symbol']): continue
                        
                        # Extract License Info
                        license_info = "Unknown License"
                        try:
                            if "imageinfo" in page_data:
                                ext = page_data["imageinfo"][0].get("extmetadata", {})
                                license_info = ext.get("LicenseShortName", {}).get("value", "Unknown License")
                        except: pass
                        
                        img_bytes, final_url = self.download_and_process(img_url)
                        if not img_bytes: continue
                        
                        # --- MODERATION FIREWALL ---
                        ai_status, ai_notes = run_ai_moderation(img_bytes, original_species_name)
                        
                        # Check Legal Status
                        if ai_status == 'OK':
                            bad_licenses =["all rights reserved", "fair use", "non-free"]
                            if any(b in license_info.lower() for b in bad_licenses):
                                ai_status = 'FLAGGED_LEGAL'
                                ai_notes = f"License Warning: {license_info}"
                        
                        if ai_status == 'OK':
                            return img_bytes, final_url, ai_status, license_info, ai_notes
                        
                        # If flagged content, quality, or legal, just continue to the next Wikipedia result!
                        continue
                        
            return None, None, None, None, None
        except: return None, None, None, None, None

    def download_and_process(self, img_url):
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            r = requests.get(img_url, headers=headers, timeout=15); r.raise_for_status()
            img = Image.open(BytesIO(r.content))
            if img.mode != 'RGB': img = img.convert('RGB')
            img.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
            byte_arr = BytesIO(); img.save(byte_arr, format='JPEG', quality=85)
            return byte_arr.getvalue(), img_url
        except: return None, None


# --- GUI ---
class ImageCurator(QWidget):
    def __init__(self):
        super().__init__()
        patch_database_encoding()
        self.is_syncing = False
        self._db_cache = {}
        self.init_ui()
        self.load_settings()
        QTimer.singleShot(500, self.refresh_table)
        QTimer.singleShot(1000, self.start_sync)
        self.timer = QTimer(self); self.timer.timeout.connect(self.start_sync); self.timer.start(60000) 

    def init_ui(self):
        self.setWindowTitle("Species Image Curator (Autonomous Mode)")
        self.setGeometry(300, 300, 1050, 700) 
        layout = QVBoxLayout()
        
        search_layout = QHBoxLayout()
        
        # --- FILTERS ---
        self.view_filter_combo = QComboBox()
        self.view_filter_combo.addItems([
            "View: All Valid Targets", 
            "View: Birds Only (Audio)", 
            "View: DSP Bioacoustics (Audio)", 
            "View: Visual Megafauna (AI)"
        ])
        self.view_filter_combo.setStyleSheet("font-weight: bold; background-color: #007acc; color: white;")
        self.view_filter_combo.currentIndexChanged.connect(self.on_primary_filter_changed)
        search_layout.addWidget(self.view_filter_combo)
        
        self.secondary_filter_combo = QComboBox()
        self.secondary_filter_combo.addItems([
            "All Classifications",
            "Endemic Species Only",
            "General Animals Only"
        ])
        self.secondary_filter_combo.setStyleSheet("font-weight: bold; background-color: #555555; color: #aaaaaa;")
        self.secondary_filter_combo.setEnabled(False)
        self.secondary_filter_combo.currentIndexChanged.connect(self.apply_filters)
        search_layout.addWidget(self.secondary_filter_combo)
        
        search_layout.addSpacing(10)
        search_layout.addWidget(QLabel("<b>Search:</b>"))
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Filter...")
        self.search_box.textChanged.connect(self.apply_filters)
        search_layout.addWidget(self.search_box)
        
        # --- BADGE & TOOLS ---
        self.btn_mod_rules = QPushButton("⚙️ AI Moderation Rules")
        self.btn_mod_rules.clicked.connect(self.open_moderation_rules)
        search_layout.addWidget(self.btn_mod_rules)
        
        self.btn_refresh = QPushButton("🔄 Refresh UI")
        self.btn_refresh.clicked.connect(self.refresh_table)
        search_layout.addWidget(self.btn_refresh)
        
        self.status_label = QLabel("Status: Ready.")
        
        # --- TABLE ---
        self.table = QTableWidget(); self.table.setColumnCount(7) 
        self.table.setHorizontalHeaderLabels(["Thumbnail", "Species Name", "Total Detections", "AI Moderation Status", "Last Alarmed", "Last Detected", "Action"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for i in range(1, self.table.columnCount()): self.table.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        self.table.verticalHeader().setDefaultSectionSize(120)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        self.table.verticalScrollBar().valueChanged.connect(self.check_scroll_bottom)
        
        bottom_layout = QHBoxLayout()
        
        self.chk_failed_only = QCheckBox("Show Failed & Flagged Only")
        self.chk_failed_only.setStyleSheet("font-weight: bold;")
        self.chk_failed_only.toggled.connect(self.apply_filters)
        
        self.failed_count_label = QLabel("Failed/Flagged: 0")
        self.failed_count_label.setStyleSheet("font-weight: bold; color: green; margin-right: 10px;")
        
        self.retry_button = QPushButton("Retry Failed & Flagged Fetches")
        self.retry_button.clicked.connect(self.retry_failed)
        
        bottom_layout.addWidget(self.status_label)
        bottom_layout.addStretch()
        bottom_layout.addWidget(self.chk_failed_only)
        bottom_layout.addWidget(self.failed_count_label)
        bottom_layout.addWidget(self.retry_button)

        layout.addLayout(search_layout)
        layout.addWidget(self.table)
        layout.addLayout(bottom_layout)
        self.setLayout(layout)

    def open_moderation_rules(self):
        d = AIModerationConfigDialog(self)
        if d.exec():
            rules, auto_scan = d.get_data()
            try:
                s = json.loads(DASHBOARD_SETTINGS_FILE.read_text(encoding='utf-8')) if DASHBOARD_SETTINGS_FILE.exists() else {}
                s["image_moderation_rules"] = rules
                s["image_moderation_autoscan"] = auto_scan
                DASHBOARD_SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding='utf-8')
                QMessageBox.information(self, "Saved", "Moderation rules saved. The AI Security Guard will now enforce them.")
            except: pass

    def on_primary_filter_changed(self):
        curr = self.view_filter_combo.currentText()
        if "DSP" in curr or "Visual" in curr:
            self.secondary_filter_combo.setEnabled(True)
            self.secondary_filter_combo.setStyleSheet("font-weight: bold; background-color: #007acc; color: white;")
        else:
            self.secondary_filter_combo.setEnabled(False)
            self.secondary_filter_combo.setStyleSheet("font-weight: bold; background-color: #555555; color: #aaaaaa;")
            self.secondary_filter_combo.setCurrentIndex(0)
        self.apply_filters()

    def _populate_row(self, i, species, count, image_info, last_alarm, last_det):
        """Populates a single row in the species image table."""
        image_data, status, license_info, ai_notes = image_info.get(species, (None, 'PENDING', 'Unknown', ''))
        thumb_label = ClickableImageLabel()
        thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if image_data and image_data != b"failed":
            pixmap = QPixmap()
            pixmap.loadFromData(image_data)
            thumb_label.setPixmap(pixmap.scaled(100, 100, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            thumb_label.setToolTip("Double-click to enlarge")
            thumb_label.doubleClicked.connect(lambda p=pixmap: self.open_image_viewer(p))
        elif status == 'FETCH_FAILED':
            thumb_label.setText("Failed"); thumb_label.setStyleSheet("color: red; font-weight: bold;")
        else:
            thumb_label.setText("Pending...")
        item_status = QTableWidgetItem(status)
        item_status.setFlags(item_status.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if status.startswith('FLAGGED_'):
            item_status.setForeground(QBrush(QColor("#D32F2F")))
            font = item_status.font(); font.setBold(True); item_status.setFont(font)
            item_status.setToolTip(f"<b>License:</b> {license_info}<br><b>AI Reason:</b> {ai_notes}")
        elif status == 'OK':
            item_status.setForeground(QBrush(QColor("#4CAF50")))
            item_status.setToolTip(f"<b>License:</b> {license_info}<br><b>AI Notes:</b> {ai_notes}")
        else:
            item_status.setForeground(QBrush(QColor("#888888")))
        alarm_ts = last_alarm.get(species, 0)
        det_ts = last_det.get(species, 0)
        self.table.setCellWidget(i, 0, thumb_label)
        self.table.setItem(i, 1, QTableWidgetItem(species))
        self.table.setItem(i, 2, NumericTableWidgetItem(str(count)))
        self.table.setItem(i, 3, item_status)
        
        item_alarm = DateTimeTableWidgetItem(datetime.fromtimestamp(alarm_ts).strftime('%Y-%m-%d %H:%M') if alarm_ts else "N/A")
        item_alarm.setData(Qt.ItemDataRole.UserRole, float(alarm_ts) if alarm_ts else 0.0)
        self.table.setItem(i, 4, item_alarm)
        item_det = DateTimeTableWidgetItem(datetime.fromtimestamp(det_ts).strftime('%Y-%m-%d %H:%M') if det_ts else "N/A")
        item_det.setData(Qt.ItemDataRole.UserRole, float(det_ts) if det_ts else 0.0)
        self.table.setItem(i, 5, item_det)
        btn_lay = QHBoxLayout()
        btn_lay.setContentsMargins(2, 2, 2, 2)
        btn_url = QPushButton("Manual Override...")
        btn_url.clicked.connect(lambda chk, s=species: self.set_image_from_url(s))
        btn_lay.addWidget(btn_url)
        if status.startswith('FLAGGED_'):
            btn_approve = QPushButton("Force Approve")
            btn_approve.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
            btn_approve.clicked.connect(lambda chk, s=species: self.force_approve_image(s))
            btn_lay.addWidget(btn_approve)
        cell_widget = QWidget()
        cell_widget.setLayout(btn_lay)
        self.table.setCellWidget(i, 6, cell_widget)

    def check_scroll_bottom(self, value):
        # If user scrolls near the bottom, silently load the next batch
        if value >= self.table.verticalScrollBar().maximum() - 2:
            self._load_more_rows()

    def _load_more_rows(self):
        """Appends the next batch of rows silently when scrolling."""
        if not hasattr(self, '_all_sorted_species') or not self._all_sorted_species:
            return
        PAGE_SIZE = 200
        start = self._loaded_rows
        total_species = len(self._all_sorted_species)
        
        if start >= total_species: 
            return # Nothing left to load
            
        end = min(start + PAGE_SIZE, total_species)
        self._loaded_rows = end
        self.table.setRowCount(end)
        
        img_info = self._db_cache.get('image_info', {})
        last_alarm = self._db_cache.get('last_alarm', {})
        last_det = self._db_cache.get('last_det', {})
        
        self.table.setSortingEnabled(False)
        for i, (species, count) in enumerate(self._all_sorted_species[start:end]):
            self._populate_row(start + i, species, count, img_info, last_alarm, last_det)
        self.table.setSortingEnabled(True)
            
        remaining = total_species - end
        if remaining > 0:
            self.status_label.setText(f"Showing {end} of {total_species} species. (Scroll down to load more)")
        else:
            self.status_label.setText(f"Status: Showing all {total_species} species.")

    def apply_filters(self):
        if not hasattr(self, '_db_cache'): return
        
        search_text = strip_accents(self.search_box.text().strip().lower())
        show_failed = self.chk_failed_only.isChecked()
        filter_mode = self.view_filter_combo.currentText()
        secondary_mode = self.secondary_filter_combo.currentText()
        
        filtered_data = {}
        counts = self._db_cache.get('detection_counts', {})
        vis_set = self._db_cache.get('visual_species', set())
        img_info = self._db_cache.get('image_info', {})
        
        for sp, count in counts.items():
            sp_clean = sp.strip()
            if is_noise(sp_clean): continue
            
            # 1. Text Search
            if search_text and search_text not in strip_accents(sp_clean.lower()):
                continue
                
            # 2. Failed/Flagged Checkbox
            status = img_info.get(sp_clean, (None, 'PENDING', '', ''))[1]
            if show_failed and status != 'FETCH_FAILED' and not status.startswith('FLAGGED_'):
                continue
                
            # 3. Primary Combo
            cat = get_species_category(sp_clean, vis_set)
            if "Birds Only" in filter_mode and cat != 'BIRD': continue
            if "DSP Bioacoustics" in filter_mode and cat != 'DSP': continue
            if "Visual Megafauna" in filter_mode and cat != 'VISUAL': continue
            
            # 4. Secondary Combo
            if self.secondary_filter_combo.isEnabled():
                is_general = is_general_animal(sp_clean)
                if secondary_mode == "Endemic Species Only" and is_general: continue
                if secondary_mode == "General Animals Only" and not is_general: continue
                
            filtered_data[sp] = count

        # Sort and Paginate
        PAGE_SIZE = 200
        sorted_species = sorted(filtered_data.items(), key=lambda item: item[1], reverse=True)
        total_species = len(sorted_species)

        self._all_sorted_species = sorted_species
        self._loaded_rows = min(PAGE_SIZE, total_species)
        page = sorted_species[:self._loaded_rows]

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(page))

        last_alarm = self._db_cache.get('last_alarm', {})
        last_det = self._db_cache.get('last_det', {})

        for i, (species, count) in enumerate(page):
            self._populate_row(i, species, count, img_info, last_alarm, last_det)

        self.table.setSortingEnabled(True)

        if total_species > PAGE_SIZE:
            self.status_label.setText(f"Showing {self._loaded_rows} of {total_species} species. (Scroll down to load more)")
        else:
            self.status_label.setText(f"Status: Showing all {total_species} species.")

    def retry_failed(self):
        if QMessageBox.question(self, "Confirm Retry", "Reset 'Failed' and 'Flagged' statuses to allow the AI to search Wikipedia again?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            reset_failed_fetches(); self.start_sync()

    def start_sync(self):
        if self.is_syncing: return
        self.is_syncing = True
        self.sync_thread = SyncThread()
        self.sync_thread.progress_update.connect(self.status_label.setText)
        self.sync_thread.sync_finished.connect(self.on_sync_finished)
        self.sync_thread.start()

    def on_sync_finished(self):
        self.is_syncing = False; self.refresh_table(); self.status_label.setText("Status: Sync complete.")

    def open_image_viewer(self, pixmap):
        if pixmap and not pixmap.isNull():
            viewer = ImageViewerDialog(pixmap, parent=self)
            viewer.exec()

    def refresh_table(self):
        self.table.setSortingEnabled(False)
        try:
            visual_species = set()
            
            with db_connector.get_db_connection(force_local=True) as con: 
                cur = con.cursor()
                cur.execute("SELECT species, COUNT(*) FROM detections GROUP BY species")
                detection_counts = dict(cur.fetchall())
                
                try:
                    cur.execute("SELECT DISTINCT species FROM detections WHERE detection_method IN ('vision', 'multimodal')")
                    visual_species = {row[0].strip() for row in cur.fetchall()}
                except: pass
                
                try:
                    cur.execute("SELECT species, MAX(timestamp) FROM detections GROUP BY species")
                    last_det = dict(cur.fetchall())
                except: last_det = {}
                
                try:
                    try:
                        cur.execute("SELECT species, MAX(timestamp) FROM detections WHERE alert_sent = TRUE GROUP BY species")
                        last_alarm = dict(cur.fetchall())
                    except:
                        cur.execute("SELECT species, MAX(timestamp) FROM detections WHERE alert_sent = 1 GROUP BY species")
                        last_alarm = dict(cur.fetchall())
                except: last_alarm = {}

            image_info = {}
            if IMAGE_DB_PATH.exists():
                img_con = None
                try:
                    img_con = sqlite3.connect(str(IMAGE_DB_PATH), timeout=15)
                    img_cur = img_con.cursor()
                    try:
                        # --- THE SCHEMA AGNOSTIC FALLBACK PATCH ---
                        img_cur.execute("SELECT species_name, image_data, status, license_info, ai_notes FROM species_images")
                        image_info = {row[0]: (row[1], row[2], row[3], row[4]) for row in img_cur.fetchall()}
                    except sqlite3.OperationalError:
                        img_cur.execute("SELECT species_name, image_data, status FROM species_images")
                        image_info = {row[0]: (row[1], row[2], 'Unknown', '') for row in img_cur.fetchall()}
                except Exception as e:
                    logging.error(f"Failed to connect to image DB for UI refresh: {e}")
                finally:
                    if img_con: img_con.close()
            
            # Store everything in memory cache
            self._db_cache = {
                'detection_counts': detection_counts,
                'visual_species': visual_species,
                'last_det': last_det,
                'last_alarm': last_alarm,
                'image_info': image_info
            }
            
            failed_species =[sp for sp, info in image_info.items() if info[1] == 'FETCH_FAILED' or info[1].startswith('FLAGGED_')]
            failed_count = len(failed_species)
            
            self.failed_count_label.setText(f"Failed/Flagged: {failed_count}")
            self.failed_count_label.setStyleSheet(f"font-weight: bold; color: {'red' if failed_count > 0 else 'green'}; margin-right: 10px;")
            
            if failed_count > 0:
                tooltip_text = "<b>Failed/Flagged Species:</b><br>" + "<br>".join([f"• {s}" for s in sorted(failed_species)])
                self.failed_count_label.setToolTip(tooltip_text)
            else:
                self.failed_count_label.setToolTip("")

            self.apply_filters()
            
        except Exception as e: 
            logging.error(f"Table Refresh Failed: {e}")
        
    def show_context_menu(self, pos):
        item = self.table.itemAt(pos)
        if not item: return
        species_name = self.table.item(item.row(), 1).text()
        
        search_name = species_name
        if "(" in species_name and ")" in species_name:
            match = re.search(r'\((.*?)\)', species_name)
            if match:
                search_name = match.group(1)
                
        menu = QMenu()
        menu.addAction(QAction(f"Verify '{search_name}' on Wikipedia", self, triggered=lambda: webbrowser.open(f"https://en.wikipedia.org/wiki/{quote_plus(search_name)}")))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def force_approve_image(self, species_name):
        con = None
        try:
            con = sqlite3.connect(str(IMAGE_DB_PATH), timeout=15)
            cur = con.cursor()
            try:
                cur.execute("UPDATE species_images SET status = 'OK', ai_notes = 'Manual Override' WHERE species_name = ?", (species_name,))
            except sqlite3.OperationalError:
                cur.execute("UPDATE species_images SET status = 'OK' WHERE species_name = ?", (species_name,))
            con.commit()
            self.refresh_table()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to approve: {e}")
        finally:
            if con: con.close()

    def set_image_from_url(self, species_name):
        d = URLDialog(species_name, self)
        if d.exec():
            url = d.get_url()
            if url:
                try:
                    img = Image.open(BytesIO(requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15).content))
                    if img.mode != 'RGB': img = img.convert('RGB')
                    img.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
                    byte_arr = BytesIO(); img.save(byte_arr, format='JPEG', quality=85)
                    
                    store_image_in_db(species_name, byte_arr.getvalue(), url, 'OK', 'Manual Override', 'Bypassed AI')
                        
                    self.refresh_table()
                except Exception as e: QMessageBox.critical(self, "Error", f"Failed: {e}")

    def save_settings(self):
        try:
            settings = json.loads(DASHBOARD_SETTINGS_FILE.read_text(encoding="utf-8")) if DASHBOARD_SETTINGS_FILE.exists() else {}
            settings['image_curator_table_widths'] = [self.table.columnWidth(i) for i in range(self.table.columnCount())]
            
            geom = self.normalGeometry()
            settings['image_curator_geometry'] = {'x': geom.x(), 'y': geom.y(), 'w': geom.width(), 'h': geom.height()}
            settings['image_curator_maximized'] = self.isMaximized()
            
            DASHBOARD_SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding='utf-8')
        except: pass
            
    def load_settings(self):
        try:
            if DASHBOARD_SETTINGS_FILE.exists():
                s = json.loads(DASHBOARD_SETTINGS_FILE.read_text(encoding="utf-8"))
                widths = s.get('image_curator_table_widths')
                if widths: 
                    for i, w in enumerate(widths): self.table.setColumnWidth(i, w)
                
                geom = s.get('image_curator_geometry')
                if geom:
                    x, y, w, h = geom['x'], geom['y'], geom['w'], geom['h']
                    
                    screen = QApplication.primaryScreen().availableGeometry()
                    
                    # --- BULLETPROOF SIZE & POSITION CLAMP ---
                    # If it's off-screen OR taking up the entire screen, shrink it.
                    if (x < screen.x() or x > screen.x() + screen.width() - 50 or 
                        y < screen.y() or y > screen.y() + screen.height() - 50 or
                        w >= screen.width() - 50 or h >= screen.height() - 50):
                        
                        x, y = 150, 150
                        w, h = 1050, 700  # Safe default size
                        
                    self.setGeometry(x, y, w, h)
                
                if s.get('image_curator_maximized', False):
                    QTimer.singleShot(100, self.showMaximized)
        except Exception as e:
            logging.error(f"Failed to load UI settings: {e}")

    def closeEvent(self, event): 
        self.save_settings()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ImageCurator()
    window.show()
    sys.exit(app.exec())