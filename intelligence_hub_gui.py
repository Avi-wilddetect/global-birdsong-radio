# FILE: intelligence_hub_gui.py
# VERSION: 16.10 - "The QGroupBox Padding & Sync Patch"
# CHANGES: 
# 1. Injected padding-top: 15px into all QGroupBox stylesheets to prevent the invisible title box from blocking spinbox Up-Arrows.
# 2. Fixed open_global_vision_settings() to fully sync all keys from birdnet_config.json before opening, eliminating stale memory overwrites.

import sys
import json
import logging
import copy
import subprocess
import psutil
import os
import sqlite3
import re
import time
import webbrowser
import signal
from pathlib import Path
from functools import partial
from urllib.parse import quote_plus

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QListWidget, QListWidgetItem, 
                             QLabel, QPushButton, QMessageBox, QGroupBox, 
                             QSplitter, QFrame, QLineEdit, QCheckBox, QAbstractItemView,
                             QTableWidget, QTableWidgetItem, QHeaderView, QTabWidget,
                             QMenu, QToolButton, QSizePolicy, QTextEdit, QDialog, QDialogButtonBox,QComboBox, QSpinBox,QFormLayout)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QBrush, QFont, QAction

# --- IMPORT DIALOGS FROM NEW FILE ---
from intelligence_hub_dialogs import (
    StreamOverrideDialog, AnimalTuningDialog, GlobalAudioSettingsDialog,
    GlobalVisionSettingsDialog, EditVisionRegistryDialog, BatchEditVisionRegistryDialog,
    AssignVisionTargetDialog, AddAnimalDialog, TaxonomyAuditorDialog, ForbiddenPurgeDialog,
    FRAME_LEVELS, DEPTH_LEVELS
)

# --- IMPORT PROFILES TO GET ANIMAL LIST ---
try:
    import bioacoustic_profiles
    AVAILABLE_ANIMALS = sorted(list(bioacoustic_profiles.PROFILES.keys()))
except ImportError:
    AVAILABLE_ANIMALS =["ERROR: bioacoustic_profiles.py missing"]

# --- IMPORT DB CONNECTOR & TRAITS ---
try:
    import db_connector
    import trait_inference
except ImportError:
    print("FATAL: core modules missing.")
    sys.exit(1)

# --- CONFIGURATION ---
ROOT = Path(__file__).resolve().parent
GBR_CONFIG = ROOT / "birdnet_config.json"
TARGETS_FILE = ROOT / "bioacoustic_targets.json"
VISION_TARGETS_FILE = ROOT / "vision_targets.json"
VISION_SCHEDULER_SCRIPT = ROOT / "vision_scheduler.py"
VAULT_DIR = ROOT / "vision_snapshots"
VISION_LOG_FILE = ROOT / "vision_debug.txt"
HUB_SETTINGS_FILE = ROOT / "intelligence_hub_settings.json"

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

class NumericTableWidgetItem(QTableWidgetItem):
    def __lt__(self, other):
        try: return float(self.text() or 0) < float(other.text() or 0)
        except: return super().__lt__(other)

def get_species_category(name):
    name_clean = name.strip()
    name_lower = name_clean.lower()
    if any(exc in name_lower for exc in BIRD_EXCEPTIONS): return 'BIRD'
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

# ==============================================================================
# ASYNCHRONOUS WORKER THREADS
# ==============================================================================
class TaxonomyAuditorWorker(QThread):
    progress_update = pyqtSignal(str)
    result_ready = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)

    def __init__(self, api_key, animals):
        super().__init__()
        self.api_key = api_key
        self.animals = animals

    def run(self):
        try:
            import google.genai as genai
            from google.genai import types
            
            client = genai.Client(api_key=self.api_key)
            chunk_size = 50
            results = {}
            total = len(self.animals)
            
            for i in range(0, total, chunk_size):
                chunk = self.animals[i:i+chunk_size]
                self.progress_update.emit(f"⏳ Querying Gemini... ({min(i+chunk_size, total)}/{total})")
                
                prompt = (
                    "You are an expert wildlife taxonomist. "
                    "I will give you a JSON list of animal names. For each, determine if it is a 'Specific' species (e.g., Bald Eagle, Rhesus Macaque, Great White Shark) "
                    "or a 'General' category (e.g., Bird, Monkey, Animal, Fish). "
                    "Return ONLY a valid JSON dictionary where keys are the exact animal names provided, and values are either 'Specific' or 'General'. "
                    f"Animals to evaluate: {json.dumps(chunk)}"
                )
                
                response = client.models.generate_content(
                    model='gemini-2.5-flash', 
                    contents=[prompt],
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        response_mime_type="application/json"
                    )
                )
                
                raw_text = response.text.strip()
                if raw_text.startswith('```'):
                    raw_text = re.sub(r'^```[a-zA-Z]*\n', '', raw_text)
                    raw_text = re.sub(r'\n```$', '', raw_text).strip()
                    
                eval_data = json.loads(raw_text)
                results.update(eval_data)
                time.sleep(1) # Quick throttle to avoid spamming the API
                
            self.result_ready.emit(results)
        except Exception as e:
            self.error_occurred.emit(str(e))

# ==============================================================================
# NEW QOL DIALOGS
# ==============================================================================
class PromptExplorerDialog(QDialog):
    def __init__(self, vision_data, streams_map, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Prompt Explorer (Global & Surgical Rules)")
        self.resize(950, 600)
        layout = QVBoxLayout(self)
        
        search_layout = QHBoxLayout()
        
        self.combo_filter = QComboBox()
        self.combo_filter.addItems([
            "All Prompts", 
            "Master System Prompt", 
            "Global Taxonomy Prompts", 
            "Stream Instruction Prompts", 
            "Surgical Override Prompts",
            "Night/Low-Vis Override Prompts"
        ])
        self.combo_filter.setStyleSheet("font-weight: bold; background-color: #333;")
        self.combo_filter.currentIndexChanged.connect(self.filter_table)
        
        search_layout.addWidget(QLabel("📂 Filter by Type:"))
        search_layout.addWidget(self.combo_filter)
        search_layout.addSpacing(20)
        
        search_layout.addWidget(QLabel("🔍 Search Context/Instruction:"))
        self.search_box = QLineEdit()
        self.search_box.textChanged.connect(self.filter_table)
        search_layout.addWidget(self.search_box, 1)
        
        layout.addLayout(search_layout)
        
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Scope / Type", "Target Context", "Prompt Instruction"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table)
        
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        layout.addWidget(self.btn_close)
        
        self.populate_data(vision_data, streams_map)

    def populate_data(self, v_data, streams_map):
        rows =[]
        
        # 0. Master System Prompt
        master_prompt = v_data.get("global_defaults", {}).get("master_system_prompt", "").strip()
        if master_prompt:
            rows.append(("Master System Prompt", "Global AI Guardrail (All Cameras)", master_prompt))
            
        # 1. Global Taxonomy Prompts
        for sp, data in v_data.get("registry", {}).items():
            prompt = data.get("global_prompt", "").strip()
            if prompt: rows.append(("Global Taxonomy", f"Species: {sp}", prompt))
            
        # 2. Stream Global Prompts
        for url, s_data in v_data.get("stream_overrides", {}).items():
            prompt = s_data.get("custom_prompt", "").strip()
            stream_name = streams_map.get(url, {}).get('name', url)
            if prompt: rows.append(("Stream Instruction", f"Stream: {stream_name}", prompt))
            
            # 3. Surgical Prompts (Animal on Stream)
            for sp, rules in s_data.get("species_rules", {}).items():
                sp_prompt = rules.get("prompt", "").strip()
                if sp_prompt: rows.append(("Surgical Override", f"{sp} on {stream_name}", sp_prompt))
                
            # 4. Night Rules Prompts
            for sp, rules in s_data.get("night_species_rules", {}).items():
                sp_prompt = rules.get("prompt", "").strip()
                if sp_prompt: rows.append(("Night/Low-Vis Override", f"🌙 {sp} on {stream_name}", sp_prompt))
                
        self.table.setRowCount(len(rows))
        for i, (scope, ctx, pr) in enumerate(rows):
            i_scope = QTableWidgetItem(scope)
            if scope == "Master System Prompt": i_scope.setForeground(QBrush(QColor("#FF5252")))
            elif scope == "Global Taxonomy": i_scope.setForeground(QBrush(QColor("#FFF59D")))
            elif scope == "Stream Instruction": i_scope.setForeground(QBrush(QColor("#81D4FA")))
            elif scope == "Surgical Override": i_scope.setForeground(QBrush(QColor("#FFAB91")))
            elif scope == "Night/Low-Vis Override": i_scope.setForeground(QBrush(QColor("#b39ddb")))
            i_scope.setToolTip(scope)
            self.table.setItem(i, 0, i_scope)
            
            i_ctx = QTableWidgetItem(ctx)
            i_ctx.setToolTip(ctx)
            self.table.setItem(i, 1, i_ctx)
            
            i_pr = QTableWidgetItem(pr)
            i_pr.setToolTip(pr)
            self.table.setItem(i, 2, i_pr)
            
    def filter_table(self):
        text = self.search_box.text().lower()
        type_filter = self.combo_filter.currentText()
        
        for i in range(self.table.rowCount()):
            match_text = False
            for j in range(3):
                if text in self.table.item(i, j).text().lower(): 
                    match_text = True
                    break
                    
            match_type = True
            if type_filter != "All Prompts":
                scope_text = self.table.item(i, 0).text()
                if type_filter == "Global Taxonomy Prompts" and scope_text != "Global Taxonomy": match_type = False
                elif type_filter == "Stream Instruction Prompts" and scope_text != "Stream Instruction": match_type = False
                elif type_filter == "Surgical Override Prompts" and scope_text != "Surgical Override": match_type = False
                elif type_filter == "Master System Prompt" and scope_text != "Master System Prompt": match_type = False
                elif type_filter == "Night/Low-Vis Override Prompts" and scope_text != "Night/Low-Vis Override": match_type = False

            self.table.setRowHidden(i, not (match_text and match_type))


class BaseDefaultsDialog(QDialog):
    def __init__(self, vision_cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Base Size Defaults Dictionary")
        self.resize(700, 500)
        layout = QVBoxLayout(self)
        
        info = QLabel("<b>How Default Sizes Work:</b><br>"
                      "When an animal is assigned to a camera without specific size rules, the AI infers its physical class and assigns a base minimum size automatically.<br>"
                      "<i>Global Fallbacks</i> apply if the class is unknown.")
        info.setWordWrap(True)
        layout.addWidget(info)
        
        # Display Global Fallbacks
        g_box = QGroupBox("Current System Fallbacks")
        g_form = QFormLayout(g_box)
        g_form.addRow("Fallback Frame (Single / Flock):", QLabel(f"<span style='color:#00E5FF;'>{vision_cfg.get('min_frame_single','Large').upper()} / {vision_cfg.get('min_frame_flock','Small').upper()}</span>"))
        g_form.addRow("Fallback Depth (Single / Flock):", QLabel(f"<span style='color:#FF9800;'>{vision_cfg.get('min_depth_single','Near').upper()} / {vision_cfg.get('min_depth_flock','Background').upper()}</span>"))
        layout.addWidget(g_box)
        
        # Display trait inference map
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Size Class", "Description", "Default Min Frame (2D)", "Default Min Depth (3D)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)
        
        # Hardcoded map from trait_inference/vision_scheduler
        class_data =[
            ("Class 5", "Megafauna (>300kg)", "LARGE", "MID-GROUND"),
            ("Class 4", "Large (30-300kg)", "MEDIUM", "MID-GROUND"),
            ("Class 3", "Medium (5-30kg)", "SMALL", "BACKGROUND"),
            ("Class 2", "Small (1-5kg)", "TINY", "DEEP BACKGROUND"),
            ("Class 1", "Micro (<1kg)", "SPECK", "HORIZON")
        ]
        
        self.table.setRowCount(len(class_data))
        for i, (c, d, f, dp) in enumerate(class_data):
            self.table.setItem(i, 0, QTableWidgetItem(c))
            self.table.setItem(i, 1, QTableWidgetItem(d))
            self.table.setItem(i, 2, QTableWidgetItem(f))
            self.table.setItem(i, 3, QTableWidgetItem(dp))
            
            for j in range(4): self.table.item(i, j).setToolTip(self.table.item(i, j).text())
            
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        layout.addWidget(self.btn_close)


# ==============================================================================
# MAIN GUI CLASS
# ==============================================================================

class BioacousticConfigurator(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Intelligence Hub (Visual AI & DSP)")
        self.resize(1400, 850)
        
        self.setStyleSheet("""
            QMainWindow, QWidget { background-color: #2b2b2b; color: #ffffff; }
            QGroupBox { border: 1px solid #555; margin-top: 15px; padding-top: 15px; font-weight: bold; color: #4CAF50; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QListWidget, QTableWidget { background-color: #1e1e1e; border: 1px solid #444; color: #ddd; font-size: 13px; outline: none; gridline-color: #333; }
            QListWidget::item:selected, QTableWidget::item:selected { background-color: #0078d7; color: white; }
            QLineEdit, QTextEdit { background-color: #333; color: white; border: 1px solid #555; padding: 5px; }
            QLabel { color: #ccc; }
            QHeaderView::section { background-color: #333; color: white; border: 1px solid #444; padding: 4px; }
            QSpinBox, QDoubleSpinBox, QComboBox { background-color: #333; color: white; border: 1px solid #555; }
            QTabWidget::pane { border: 1px solid #444; }
            QTabBar::tab { background: #333; color: #aaa; padding: 10px 20px; border: 1px solid #444; }
            QTabBar::tab:selected { background: #0078d7; color: white; font-weight: bold; }
        """)
        
        # Add Central Auto-Save Debouncer
        self.ui_save_timer = QTimer(self)
        self.ui_save_timer.setSingleShot(True)
        self.ui_save_timer.timeout.connect(self.save_gui_state)
        
        self.is_dirty = False
        self.streams_map = {}
        
        self.saved_v2_data = {}
        self.saved_vision_settings = {}
        self.saved_vision_data = {}
        
        self.v2_data = {
            "_version": 2,
            "global_defaults": { "cooldown_stream_minutes": 0, "cooldown_species_minutes": 0, "cooldown_target_minutes": 60, "enable_adaptive_snr": False },
            "stream_rules": {}, "species_rules": {}, "assignments": {}
        }
        
        self.vision_settings = { 
            "api_keys":[], "api_key": "", "vision_workers": 1, "cycle_interval_seconds": 60, 
            "use_motion_detector": True, "motion_sensitivity_percent": 5.0,
            "predator_reflex_threshold": 85,
            "vision_dormancy_threshold_mins": 60,
            "vision_dormant_interval_mins": 20,
            "retention_limit": 5, "log_retention_hours": 48, "auto_sweep_vault_hours": 24, "enabled_streams":[], "hide_console": False,
            "ai_temperature": 0.15, "speck_size_percent": 3, "flock_minimum_count": 4, 
            "min_frame_single": "Large", "min_depth_single": "Near",
            "min_frame_flock": "Small", "min_depth_flock": "Background",
            "vision_resolution": "720p", "strict_proxy": False,
            "telegram_alerts": {
                "enabled": True,
                "alert_multimodal": True,
                "alert_public": True,
                "alert_filtered": False,
                "attach_image": True,
                "include_reasoning": True
            }
        }
        
        self.vision_data = {
            "global_defaults": { "ignore_general_animals": False, "default_taxonomy_mute": 60, "master_system_prompt": "", "forbidden_words":[] },
            "registry": {},
            "assignments": {},
            "stream_overrides": {}
        }
        
        self.master_animals = {animal: animal for animal in AVAILABLE_ANIMALS}
        
        self.vision_sort_mode = ("name", False)
        self.audio_sort_mode = ("name", False)
        
        # Memory Contention Fix (File Watcher)
        self.last_vision_mtime = 0
        self.file_watch_timer = QTimer(self)
        self.file_watch_timer.timeout.connect(self.check_external_modifications)
        self.file_watch_timer.start(2000)
        
        self.status_check_timer = QTimer(self)
        self.status_check_timer.timeout.connect(self.update_control_state)
        self.status_check_timer.start(2000)
        
        self.auditor_worker = None
        
        self.init_ui()
        self.load_data()
        self.load_gui_state()
        self.update_control_state()

    def check_external_modifications(self):
        if VISION_TARGETS_FILE.exists():
            current_mtime = VISION_TARGETS_FILE.stat().st_mtime
            if self.last_vision_mtime != 0 and current_mtime > self.last_vision_mtime:
                if not self.is_dirty:
                    logging.info("External modification detected on vision_targets.json. Auto-reloading safely.")
                    self.load_data()
                    self.status_label.setText("Auto-reloaded external changes from Curation Studio.")
                    self.status_label.setStyleSheet("color: #00E676; font-weight: bold;")
                else:
                    self.status_label.setText("⚠️ External changes detected, but you have unsaved edits! Save or Refresh.")
                    self.status_label.setStyleSheet("color: #FF3D00; font-weight: bold;")

    def _clean_empty_overrides(self, data_dict):
        """Recursively cleans out empty dictionaries so hashing doesn't fail on ghosts."""
        clean_data = copy.deepcopy(data_dict)
        
        overrides = clean_data.get("stream_overrides", {})
        empty_urls =[]
        for url, stream_rules in overrides.items():
            if "species_rules" in stream_rules:
                empty_sps =[sp for sp, r in stream_rules["species_rules"].items() if not r]
                for sp in empty_sps:
                    del stream_rules["species_rules"][sp]
                if not stream_rules["species_rules"]:
                    del stream_rules["species_rules"]
            
            if "night_species_rules" in stream_rules:
                empty_night_sps =[sp for sp, r in stream_rules["night_species_rules"].items() if not r]
                for sp in empty_night_sps:
                    del stream_rules["night_species_rules"][sp]
                if not stream_rules["night_species_rules"]:
                    del stream_rules["night_species_rules"]
                    
            if not stream_rules:
                empty_urls.append(url)
                
        for url in empty_urls:
            del overrides[url]
            
        clean_data["stream_overrides"] = overrides
        
        assigns = clean_data.get("assignments", {})
        empty_assigns =[u for u, a in assigns.items() if not a]
        for u in empty_assigns:
            del assigns[u]
        clean_data["assignments"] = assigns
        
        return clean_data

    def check_dirty_state(self):
        try:
            v2_clean = json.dumps(self.v2_data, sort_keys=True)
            v2_saved = json.dumps(self.saved_v2_data, sort_keys=True)
            
            vis_set_clean = json.dumps(self.vision_settings, sort_keys=True)
            vis_set_saved = json.dumps(self.saved_vision_settings, sort_keys=True)
            
            vis_data_clean = json.dumps(self._clean_empty_overrides(self.vision_data), sort_keys=True)
            vis_data_saved = json.dumps(self._clean_empty_overrides(self.saved_vision_data), sort_keys=True)
            
            self.is_dirty = (v2_clean != v2_saved) or (vis_set_clean != vis_set_saved) or (vis_data_clean != vis_data_saved)
            
            if self.is_dirty:
                self.btn_save.setText("Save Changes *")
                self.btn_save.setStyleSheet("font-size: 14px; font-weight: bold; padding: 8px; background-color: #f44336; color: white; border-radius: 4px;")
            else:
                self.btn_save.setText("Save Configurations")
                self.btn_save.setStyleSheet("font-size: 14px; font-weight: bold; padding: 8px; background-color: #0078d7; color: white; border-radius: 4px;")
        except Exception as e:
            logging.error(f"Error checking dirty state: {e}")

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)
        
        header = QLabel("Intelligence Hub")
        header.setStyleSheet("font-size: 20px; font-weight: bold; color: #4CAF50;")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(header)
        
        self.tabs = QTabWidget()
        
        # ==========================================
        # TAB 1: VISUAL AI (GEMINI)
        # ==========================================
        self.tab_vision = QWidget()
        vision_layout = QVBoxLayout(self.tab_vision)
        
        vis_top_layout = QHBoxLayout()
        
        self.v_control_group = QGroupBox("Vision Engine Control")
        v_control_layout = QHBoxLayout(self.v_control_group)
        self.btn_start_vision = QPushButton("START Vision Engine"); self.btn_start_vision.setStyleSheet("background-color: #4CAF50; color: white; padding: 6px; font-weight: bold;")
        self.btn_stop_vision = QPushButton("STOP Vision Engine"); self.btn_stop_vision.setStyleSheet("background-color: #f44336; color: white; padding: 6px; font-weight: bold;")
        self.lbl_vision_status = QLabel("Status: STOPPED"); self.lbl_vision_status.setStyleSheet("color: red; font-weight: bold; font-size: 13px;")
        
        self.btn_start_vision.clicked.connect(self.start_vision_engine)
        self.btn_stop_vision.clicked.connect(self.stop_vision_engine)
        
        v_control_layout.addWidget(self.btn_start_vision); v_control_layout.addWidget(self.btn_stop_vision)
        v_control_layout.addStretch(); v_control_layout.addWidget(self.lbl_vision_status)
        vis_top_layout.addWidget(self.v_control_group, 1)

        vis_tools_layout = QVBoxLayout()
        top_btn_row = QHBoxLayout()
        
        self.btn_vision_settings = QPushButton("⚙️ Global Vision Defaults & AI Tuning")
        self.btn_vision_settings.setStyleSheet("font-weight: bold;")
        self.btn_vision_settings.clicked.connect(self.open_global_vision_settings)
        
        self.btn_prompt_explorer = QPushButton("🔍 Prompt Explorer")
        self.btn_prompt_explorer.clicked.connect(self.open_prompt_explorer)
        
        self.btn_view_defaults = QPushButton("📏 Base Size Defaults")
        self.btn_view_defaults.clicked.connect(self.open_base_defaults)

        top_btn_row.addWidget(self.btn_prompt_explorer)
        top_btn_row.addWidget(self.btn_view_defaults)
        
        vis_tools_layout.addWidget(self.btn_vision_settings)
        vis_tools_layout.addLayout(top_btn_row)
        
        vis_top_layout.addLayout(vis_tools_layout)
        
        vision_layout.addLayout(vis_top_layout)

        self.v_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.v_splitter.setOpaqueResize(False)
        self.v_splitter.splitterMoved.connect(lambda: self.ui_save_timer.start(500))

        v_left_group = QGroupBox("1. Vision Streams")
        v_left_group.setMinimumWidth(50)
        v_left_layout = QVBoxLayout(v_left_group)
        v_left_layout.addWidget(QLabel("Check to enable AI scanning. Select to manage rules."))
        
        v_top_row = QHBoxLayout()
        
        self.combo_vision_filter = QComboBox()
        self.combo_vision_filter.addItems(["Show All", "Enabled Only", "Disabled Only", "Assigned Rules Only"])
        self.combo_vision_filter.currentIndexChanged.connect(self.filter_vision_streams)
        
        self.chk_include_ip_cams = QCheckBox("Include IP Cams")
        self.chk_include_ip_cams.setChecked(False)
        self.chk_include_ip_cams.toggled.connect(self.filter_vision_streams)
        
        v_top_row.addWidget(self.combo_vision_filter)
        v_top_row.addSpacing(10)
        v_top_row.addWidget(self.chk_include_ip_cams)
        v_top_row.addStretch()
        
        self.btn_v_select_all = QPushButton("Check All")
        self.btn_v_select_all.clicked.connect(self.select_all_vision)
        self.btn_v_deselect_all = QPushButton("Uncheck All")
        self.btn_v_deselect_all.clicked.connect(self.deselect_all_vision)
        
        self.btn_v_refresh = QPushButton("Revert")
        self.btn_v_refresh.setToolTip("Discard unsaved changes and reload from config file.")
        self.btn_v_refresh.clicked.connect(self.reload_all_data)
        
        v_top_row.addWidget(self.btn_v_select_all)
        v_top_row.addWidget(self.btn_v_deselect_all)
        v_top_row.addWidget(self.btn_v_refresh)
        v_left_layout.addLayout(v_top_row)
        
        v_search_layout = QHBoxLayout()
        self.vision_search_bar = QLineEdit()
        self.vision_search_bar.setPlaceholderText("🔍 Search Streams...")
        self.vision_search_bar.textChanged.connect(self.filter_vision_streams)
        
        self.lbl_v_stream_count = QLabel("(0 visible | 0 enabled)")
        self.lbl_v_stream_count.setStyleSheet("color: #888888; font-weight: bold;")
        
        self.btn_v_sort = QToolButton()
        self.btn_v_sort.setText("Sort: Name (A-Z) ▼")
        self.btn_v_sort.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_v_sort.setMenu(self.create_sort_menu("vision"))
        
        v_search_layout.addWidget(self.vision_search_bar, 1)
        v_search_layout.addWidget(self.lbl_v_stream_count)
        v_search_layout.addWidget(self.btn_v_sort)
        v_left_layout.addLayout(v_search_layout)

        v_div_layout = QHBoxLayout()
        self.btn_v_sort_div = QToolButton()
        self.btn_v_sort_div.setText("🌟 Sort by Historical Biodiversity ▼")
        self.btn_v_sort_div.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_v_sort_div.setStyleSheet("background-color: #3949AB; color: white; font-weight: bold; padding: 4px;")
        self.btn_v_sort_div.setSizePolicy(self.btn_v_sort.sizePolicy().Policy.Expanding, self.btn_v_sort.sizePolicy().Policy.Fixed)
        
        div_menu = QMenu(self)
        act_bio = QAction("Bioacoustics / Megafauna Only", self)
        act_bio.triggered.connect(lambda: self.sort_by_diversity("vision_bio"))
        act_bird = QAction("Birds Only", self)
        act_bird.triggered.connect(lambda: self.sort_by_diversity("vision_bird"))
        act_all = QAction("Combined (All Visual Life)", self)
        act_all.triggered.connect(lambda: self.sort_by_diversity("vision_combined"))
        
        div_menu.addAction(act_bio)
        div_menu.addAction(act_bird)
        div_menu.addAction(act_all)
        self.btn_v_sort_div.setMenu(div_menu)
        
        v_div_layout.addWidget(self.btn_v_sort_div)
        v_left_layout.addLayout(v_div_layout)
        
        self.vision_stream_list = QListWidget()
        self.vision_stream_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.vision_stream_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.vision_stream_list.customContextMenuRequested.connect(self.v_stream_context_menu)
        self.vision_stream_list.itemChanged.connect(self.on_vision_stream_check_changed)
        self.vision_stream_list.itemSelectionChanged.connect(self.on_vision_stream_selection_changed)
        v_left_layout.addWidget(self.vision_stream_list)
        self.v_splitter.addWidget(v_left_group)

        v_center_group = QGroupBox("2. Stream-Specific Rules")
        v_center_group.setMinimumWidth(50)
        v_center_layout = QVBoxLayout(v_center_group)
        self.lbl_selected_vision_stream = QLabel("No Stream Selected")
        self.lbl_selected_vision_stream.setStyleSheet("font-weight: bold; font-size: 13px; color: #fff;")
        self.lbl_selected_vision_stream.setWordWrap(True)
        self.lbl_selected_vision_stream.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        v_center_layout.addWidget(self.lbl_selected_vision_stream)
        
        info_lbl_v = QLabel("Inject specific watchlists, or review blind spots and AI tuning.")
        info_lbl_v.setWordWrap(True)
        v_center_layout.addWidget(info_lbl_v)
        
        self.v_target_table = QTableWidget()
        self.v_target_table.setColumnCount(3)
        self.v_target_table.setHorizontalHeaderLabels(["Scope / Setting", "Rule Definition", "Type"])
        self.v_target_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.v_target_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.v_target_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.v_target_table.horizontalHeader().setStretchLastSection(False)
        self.v_target_table.horizontalHeader().sectionResized.connect(lambda: self.ui_save_timer.start(500))
        
        self.v_target_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.v_target_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.v_target_table.itemDoubleClicked.connect(self.on_v_target_double_clicked)
        v_center_layout.addWidget(self.v_target_table)
        
        v_target_btn_layout = QHBoxLayout()
        self.btn_v_add_target = QPushButton("+ Add Taxonomy Rule")
        self.btn_v_add_target.setStyleSheet("background-color: #00897B; color: white;")
        self.btn_v_add_target.clicked.connect(self.assign_vision_target)
        self.btn_v_remove_target = QPushButton("- Remove Selected Row")
        self.btn_v_remove_target.clicked.connect(self.remove_vision_target)
        self.btn_v_clear_targets = QPushButton("Clear All")
        self.btn_v_clear_targets.clicked.connect(self.clear_vision_targets)
        
        v_target_btn_layout.addWidget(self.btn_v_add_target)
        v_target_btn_layout.addWidget(self.btn_v_remove_target)
        v_target_btn_layout.addWidget(self.btn_v_clear_targets)
        v_center_layout.addLayout(v_target_btn_layout)
        
        # --- TUNING GROUP (TALLER LAYOUT FOR MORE TEXT) ---
        v_tuning_group = QGroupBox("Stream AI Tuning & Custom Prompts")
        v_tuning_group.setStyleSheet("QGroupBox { margin-top: 15px; padding-top: 15px; }") 
        v_tuning_group.setFixedHeight(180) 
        v_tuning_layout = QVBoxLayout(v_tuning_group)
        v_tuning_layout.setContentsMargins(4, 4, 4, 4)
        v_tuning_layout.setSpacing(4)
        
        p_row = QHBoxLayout()
        p_row.addWidget(QLabel("Custom Global Instruction for this Stream:"))
        self.btn_apply_prompt = QPushButton("Save Prompt")
        self.btn_apply_prompt.setStyleSheet("font-weight: bold; background-color: #4CAF50;")
        self.btn_apply_prompt.clicked.connect(self.apply_custom_prompt)
        p_row.addStretch()
        p_row.addWidget(self.btn_apply_prompt)
        v_tuning_layout.addLayout(p_row)
        
        self.edit_custom_prompt = QTextEdit()
        self.edit_custom_prompt.setPlaceholderText("e.g., 'Ignore the raccoon-shaped rock on the left. Pay special attention to the water.'")
        self.edit_custom_prompt.setMinimumHeight(60) 
        v_tuning_layout.addWidget(self.edit_custom_prompt)
        
        v_tuning_row = QHBoxLayout()
        self.btn_size_overrides = QPushButton("Environment & Size Overrides")
        self.btn_size_overrides.clicked.connect(self.open_stream_size_overrides)
        self.btn_size_overrides.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        
        self.btn_tune_animal = QPushButton("Surgical Tune Specific Animal")
        self.btn_tune_animal.setStyleSheet("background-color: #5C6BC0; color: white;")
        self.btn_tune_animal.clicked.connect(self.open_animal_tuning)
        self.btn_tune_animal.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        
        v_tuning_row.addWidget(self.btn_size_overrides)
        v_tuning_row.addWidget(self.btn_tune_animal)
        v_tuning_layout.addLayout(v_tuning_row)
        
        v_center_layout.addWidget(v_tuning_group)
        self.v_splitter.addWidget(v_center_group)

        v_right_group = QGroupBox("3. Master Taxonomy Registry")
        v_right_group.setMinimumWidth(50)
        v_right_layout = QVBoxLayout(v_right_group)
        
        top_r = QHBoxLayout()
        info_lbl_r = QLabel("Dictionary that coerces Gemini's output into strict Canonical Names.")
        info_lbl_r.setWordWrap(True)
        top_r.addWidget(info_lbl_r)
        
        self.lbl_reg_selection = QLabel("")
        self.lbl_reg_selection.setStyleSheet("color: #00E5FF; font-weight: bold;")
        top_r.addWidget(self.lbl_reg_selection)
        v_right_layout.addLayout(top_r)
        
        v_reg_search_layout = QHBoxLayout()
        self.combo_reg_filter = QComboBox()
        self.combo_reg_filter.addItems(["Show All Taxonomy", "Specific Species Only", "General Animals Only", "Tuned/Edited Animals Only"])
        self.combo_reg_filter.currentIndexChanged.connect(self.filter_vision_registry)
        v_reg_search_layout.addWidget(self.combo_reg_filter)
        
        self.vision_reg_search_bar = QLineEdit()
        self.vision_reg_search_bar.setPlaceholderText("🔍 Search Names or Synonyms...")
        self.vision_reg_search_bar.textChanged.connect(self.filter_vision_registry)
        v_reg_search_layout.addWidget(self.vision_reg_search_bar)
        
        self.btn_v_refresh_reg = QPushButton("🔄 Refresh (Sync)")
        self.btn_v_refresh_reg.setToolTip("Reloads the registry from disk to pull in new AI discoveries.")
        self.btn_v_refresh_reg.clicked.connect(self.reload_vision_registry)
        v_reg_search_layout.addWidget(self.btn_v_refresh_reg)
        v_right_layout.addLayout(v_reg_search_layout)
        
        self.v_registry_table = QTableWidget()
        self.v_registry_table.setColumnCount(5)
        self.v_registry_table.setHorizontalHeaderLabels(["Canonical Name", "Synonyms", "Action", "Mute", "Public"])
        self.v_registry_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.v_registry_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.v_registry_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.v_registry_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.v_registry_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self.v_registry_table.horizontalHeader().setStretchLastSection(False)
        self.v_registry_table.horizontalHeader().sectionResized.connect(lambda: self.ui_save_timer.start(500))
        self.v_registry_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.v_registry_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.v_registry_table.itemSelectionChanged.connect(self.on_registry_selection_changed)
        
        # --- THE DOUBLE-CLICK GOOGLE SEARCH PATCH ---
        self.v_registry_table.cellDoubleClicked.connect(self.on_registry_double_clicked)
        
        v_right_layout.addWidget(self.v_registry_table)
        
        v_reg_btn_layout = QHBoxLayout()
        self.btn_v_add_reg = QPushButton("+ New Animal")
        self.btn_v_add_reg.clicked.connect(self.add_vision_registry)
        
        self.btn_v_edit_reg = QPushButton("Edit Details")
        self.btn_v_edit_reg.clicked.connect(self.edit_vision_registry)
        
        self.btn_v_import_db = QPushButton("🚑 Recover Registry")
        self.btn_v_import_db.setToolTip("Disaster Recovery: Scans your historical detections database to recreate the registry if the JSON was lost.")
        self.btn_v_import_db.clicked.connect(self.import_db_history)
        
        self.btn_v_purge_reg = QPushButton("☠️ Purge Data")
        self.btn_v_purge_reg.setStyleSheet("background-color: #B71C1C; color: white; font-weight: bold;")
        self.btn_v_purge_reg.setToolTip("Deletes ALL DB records and Images for this animal globally, then removes from registry.")
        self.btn_v_purge_reg.clicked.connect(self.purge_vision_registry)
        
        v_reg_btn_layout.addWidget(self.btn_v_add_reg)
        v_reg_btn_layout.addWidget(self.btn_v_edit_reg)
        v_reg_btn_layout.addWidget(self.btn_v_import_db)
        v_reg_btn_layout.addWidget(self.btn_v_purge_reg)
        
        v_right_layout.addLayout(v_reg_btn_layout)
        
        # --- THE TAXONOMY AUDITORS MENU ---
        self.btn_v_auditors = QToolButton()
        self.btn_v_auditors.setText("🪄 AI Taxonomy Auditors ▼")
        self.btn_v_auditors.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_v_auditors.setStyleSheet("background-color: #673AB7; color: white; font-weight: bold; padding: 6px;")
        auditor_menu = QMenu(self)
        
        act_eval = QAction("Run General vs. Specific Auditor (Evaluate & Reclassify)", self)
        act_eval.triggered.connect(self.run_taxonomy_auditor)
        
        act_purge = QAction("Run Forbidden Words Purge (Scan & Destroy)", self)
        act_purge.triggered.connect(self.run_forbidden_words_purge)
        
        auditor_menu.addAction(act_eval)
        auditor_menu.addAction(act_purge)
        self.btn_v_auditors.setMenu(auditor_menu)
        
        v_right_layout.addWidget(self.btn_v_auditors)

        self.v_splitter.addWidget(v_right_group)
        
        # Slightly squished middle default
        self.v_splitter.setSizes([350, 300, 600])
        vision_layout.addWidget(self.v_splitter)
        self.tabs.addTab(self.tab_vision, "📷 Visual AI (Google Gemini)")

        # ==========================================
        # TAB 2: ACOUSTIC DSP TARGETS
        # ==========================================
        self.tab_acoustic = QWidget()
        acoustic_layout = QVBoxLayout(self.tab_acoustic)
        acc_header = QHBoxLayout()
        acc_header.addWidget(QLabel("<b>Audio DSP Target Management</b>"))
        acc_header.addStretch()
        self.btn_global_audio_settings = QPushButton("⚙️ Global Audio Defaults")
        self.btn_global_audio_settings.setStyleSheet("font-weight: bold;")
        self.btn_global_audio_settings.clicked.connect(self.open_global_audio_settings)
        acc_header.addWidget(self.btn_global_audio_settings)
        acoustic_layout.addLayout(acc_header)
        
        self.a_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.a_splitter.setOpaqueResize(False)
        self.a_splitter.splitterMoved.connect(lambda: self.ui_save_timer.start(500))
        
        left_group = QGroupBox("1. Streams")
        left_group.setMinimumWidth(50)
        left_layout = QVBoxLayout(left_group)
        
        a_top_row = QHBoxLayout()
        self.chk_assigned_only = QCheckBox("Assigned Targets Only")
        self.chk_assigned_only.toggled.connect(self.filter_audio_streams)
        
        self.btn_a_refresh = QPushButton("🔄 Revert / Reload")
        self.btn_a_refresh.setToolTip("Discard unsaved changes and reload from config file.")
        self.btn_a_refresh.clicked.connect(self.reload_all_data)
        
        a_top_row.addWidget(self.chk_assigned_only)
        a_top_row.addStretch()
        a_top_row.addWidget(self.btn_a_refresh)
        left_layout.addLayout(a_top_row)
        
        a_search_layout = QHBoxLayout()
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("🔍 Search Streams...")
        self.search_bar.textChanged.connect(self.filter_audio_streams)
        
        self.btn_a_sort = QToolButton()
        self.btn_a_sort.setText("Sort: Name (A-Z) ▼")
        self.btn_a_sort.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_a_sort.setMenu(self.create_sort_menu("audio"))

        a_search_layout.addWidget(self.search_bar, 1) 
        a_search_layout.addWidget(self.btn_a_sort)
        left_layout.addLayout(a_search_layout)
        
        self.stream_list = QListWidget()
        self.stream_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.stream_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.stream_list.customContextMenuRequested.connect(self.a_stream_context_menu)
        self.stream_list.itemSelectionChanged.connect(self.on_audio_stream_selection_changed)
        left_layout.addWidget(self.stream_list)
        
        self.stream_rule_frame = QFrame()
        self.stream_rule_frame.setStyleSheet("background-color: #333; border: 1px solid #555; border-radius: 4px;")
        stream_rule_layout = QHBoxLayout(self.stream_rule_frame)
        stream_rule_layout.addWidget(QLabel("<b>Stream Mute Rule</b> (mins):"))
        self.spin_stream_mute = QSpinBox()
        self.spin_stream_mute.setRange(-1, 9999)
        self.spin_stream_mute.setSpecialValueText("Default")
        self.spin_stream_mute.setEnabled(False)
        self.spin_stream_mute.valueChanged.connect(self.on_stream_mute_changed)
        stream_rule_layout.addWidget(self.spin_stream_mute)
        left_layout.addWidget(self.stream_rule_frame)
        self.a_splitter.addWidget(left_group)
        
        center_group = QGroupBox("2. Assigned Targets on Selected Stream")
        center_group.setMinimumWidth(50)
        center_layout = QVBoxLayout(center_group)
        self.lbl_selected_stream = QLabel("No Stream Selected")
        self.lbl_selected_stream.setStyleSheet("font-weight: bold; font-size: 13px; color: #fff; margin-bottom: 5px;")
        self.lbl_selected_stream.setWordWrap(True)
        self.lbl_selected_stream.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        center_layout.addWidget(self.lbl_selected_stream)
        
        self.target_table = QTableWidget()
        self.target_table.setColumnCount(4)
        self.target_table.setHorizontalHeaderLabels(["Target Name", "Profile", "Cooldown (mins)", "Adaptive SNR"])
        self.target_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.target_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.target_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.target_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.target_table.horizontalHeader().sectionResized.connect(lambda: self.ui_save_timer.start(500))
        self.target_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        center_layout.addWidget(self.target_table)
        
        target_btn_layout = QHBoxLayout()
        self.btn_add_target = QPushButton("+ Add Target")
        self.btn_add_target.setStyleSheet("background-color: #00897B; color: white; font-weight: bold;")
        self.btn_add_target.clicked.connect(self.add_target)
        self.btn_remove_target = QPushButton("- Remove Selected")
        self.btn_remove_target.setStyleSheet("background-color: #EF5350; color: white;")
        self.btn_remove_target.clicked.connect(self.remove_target)
        self.btn_clear_targets = QPushButton("Clear All Targets")
        self.btn_clear_targets.clicked.connect(self.clear_audio_targets)
        
        target_btn_layout.addWidget(self.btn_add_target)
        target_btn_layout.addWidget(self.btn_remove_target)
        target_btn_layout.addWidget(self.btn_clear_targets)
        center_layout.addLayout(target_btn_layout)
        
        auto_btn_layout = QHBoxLayout()
        self.btn_suggest_regional = QPushButton("🌍 Suggest Regional Targets")
        self.btn_suggest_regional.clicked.connect(self.auto_discover_regional_targets)
        self.btn_auto_name = QPushButton("🪄 Auto-Endemic Rename")
        self.btn_auto_name.clicked.connect(self.apply_endemic_names)
        auto_btn_layout.addWidget(self.btn_suggest_regional); auto_btn_layout.addWidget(self.btn_auto_name)
        center_layout.addLayout(auto_btn_layout)
        self.a_splitter.addWidget(center_group)
        
        right_group = QGroupBox("3. Global Species Rules")
        right_group.setMinimumWidth(50)
        right_layout = QVBoxLayout(right_group)
        right_layout.addWidget(QLabel("Global Mute: Mutes this animal on ALL streams for X mins.", wordWrap=True))
        
        dsp_search_layout = QHBoxLayout()
        dsp_search_layout.addWidget(QLabel("<b>Search:</b>"))
        self.dsp_species_search = QLineEdit()
        self.dsp_species_search.setPlaceholderText("Filter animals...")
        self.dsp_species_search.textChanged.connect(self.filter_dsp_species)
        dsp_search_layout.addWidget(self.dsp_species_search)
        right_layout.addLayout(dsp_search_layout)
        
        self.species_table = QTableWidget()
        self.species_table.setColumnCount(2)
        self.species_table.setHorizontalHeaderLabels(["Animal Name", "Global Mute (mins)"])
        self.species_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.species_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.species_table.horizontalHeader().sectionResized.connect(lambda: self.ui_save_timer.start(500))
        self.species_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        right_layout.addWidget(self.species_table)
        self.a_splitter.addWidget(right_group)
        self.a_splitter.setSizes([350, 600, 350])
        acoustic_layout.addWidget(self.a_splitter)
        self.tabs.addTab(self.tab_acoustic, "🎙️ Audio DSP Targets")

        layout.addWidget(self.tabs)
        
        # ==========================================
        # FOOTER
        # ==========================================
        footer_layout = QHBoxLayout()
        self.status_label = QLabel("Ready. REMEMBER: Always click Save to commit changes!")
        self.status_label.setStyleSheet("color: orange; font-weight: bold;")
        
        self.btn_save = QPushButton("Save Configurations")
        self.btn_save.setStyleSheet("font-size: 14px; font-weight: bold; padding: 8px; background-color: #0078d7; color: white; border-radius: 4px;")
        self.btn_save.clicked.connect(self.save_targets)
        
        footer_layout.addWidget(self.status_label); footer_layout.addStretch(); footer_layout.addWidget(self.btn_save)
        layout.addLayout(footer_layout)
        
        self.check_buttons()
        self.check_vision_buttons()

    # --- THE AI TAXONOMY AUDITORS & DOUBLE-CLICK ROUTER ---
    
    def on_v_target_double_clicked(self, item):
        row = item.row()
        name_item = self.v_target_table.item(row, 0)
        if not name_item: return
        
        meta_key = name_item.data(Qt.ItemDataRole.UserRole)
        if not meta_key: return
        
        meta_str = str(meta_key)
        
        if meta_str.startswith("species_tuning:"):
            sp_name = meta_str.split(":", 1)[1]
            self.open_animal_tuning(preselect=sp_name, preselect_night=False)
        elif meta_str.startswith("night_species_tuning:"):
            sp_name = meta_str.split(":", 1)[1]
            self.open_animal_tuning(preselect=sp_name, preselect_night=True)
        elif meta_str in["prompt", "environment_type", "min_frame_single", "min_depth_single", "min_frame_flock", "min_depth_flock", "flock"]:
            self.open_stream_size_overrides()

    def on_registry_double_clicked(self, row, column):
        if column == 0:
            item = self.v_registry_table.item(row, 0)
            if item:
                clean_name = item.data(Qt.ItemDataRole.UserRole + 2)
                if clean_name:
                    search_url = f"https://www.google.com/search?q={quote_plus(clean_name)}"
                    webbrowser.open(search_url)

    def run_forbidden_words_purge(self):
        forbidden = self.vision_data.get("global_defaults", {}).get("forbidden_words",[])
        if not forbidden:
            QMessageBox.information(self, "No Forbidden Words", "You have not set any forbidden words in the Global Vision Defaults.")
            return

        hits =[]
        for name, data in self.vision_data.get("registry", {}).items():
            for word in forbidden:
                # STRICT WORD BOUNDARY REGEX
                pattern = r'\b' + re.escape(word.lower()) + r'\b'
                
                if re.search(pattern, name.lower()):
                    hits.append((name, f"Name contains forbidden word '{word}'"))
                    break
                    
                found_syn = False
                for syn in data.get("synonyms",[]):
                    if re.search(pattern, syn.lower()):
                        hits.append((name, f"Synonym '{syn}' contains forbidden word '{word}'"))
                        found_syn = True
                        break
                if found_syn:
                    break

        if not hits:
            QMessageBox.information(self, "Clean Registry", "Great! Your registry does not contain any forbidden words.")
            return

        dialog = ForbiddenPurgeDialog(hits, self)
        if dialog.exec():
            deleted_rows = 0
            names_to_purge = [h[0] for h in hits]
            
            try:
                with db_connector.get_db_connection(force_local=True) as con:
                    cur = con.cursor()
                    for name in names_to_purge:
                        cur.execute("SELECT vision_path FROM detections WHERE species = ?", (name,))
                        paths = [r[0] for r in cur.fetchall() if r[0]]
                        
                        for p in paths:
                            if os.path.exists(p):
                                try: os.remove(p)
                                except: pass
                        
                        cur.execute("DELETE FROM detections WHERE species = ?", (name,))
                        deleted_rows += cur.rowcount
                    con.commit()
                    
                for name in names_to_purge:
                    if name in self.vision_data["registry"]: del self.vision_data["registry"][name]
                    for url in self.vision_data.get("assignments", {}):
                        self.vision_data["assignments"][url] =[t for t in self.vision_data["assignments"][url] if t["name"] != name]
                        
                self.refresh_vision_registry_table()
                self.on_vision_stream_selection_changed()
                self.check_dirty_state()
                QMessageBox.information(self, "Purge Complete", f"Successfully destroyed {len(names_to_purge)} forbidden entries and {deleted_rows} historical records.")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to purge data: {e}")

    def run_taxonomy_auditor(self):
        animals_to_check =[]
        for name, data in self.vision_data.get("registry", {}).items():
            if data.get("behavior") not in ["Ignore", "Silent Log"]:
                animals_to_check.append(name)
                
        if not animals_to_check:
            QMessageBox.information(self, "Empty", "No active (Alert) animals found in the registry to evaluate.")
            return

        api_keys = self.vision_settings.get("api_keys",[])
        active_key = None
        for k in api_keys:
            if k.get("status") == "Active" and k.get("key", "").strip():
                active_key = k["key"]
                break
                
        if not active_key:
            QMessageBox.critical(self, "No API Key", "Cannot run AI Auditor: No active Gemini API key found in Global Defaults.")
            return

        self.btn_v_auditors.setText("⏳ Starting Worker...")
        self.btn_v_auditors.setEnabled(False)
        
        self.auditor_worker = TaxonomyAuditorWorker(active_key, animals_to_check)
        self.auditor_worker.progress_update.connect(self.btn_v_auditors.setText)
        self.auditor_worker.error_occurred.connect(self._on_auditor_error)
        self.auditor_worker.result_ready.connect(self._on_auditor_finished)
        self.auditor_worker.start()

    def _on_auditor_error(self, err_msg):
        self.btn_v_auditors.setText("🪄 AI Taxonomy Auditors ▼")
        self.btn_v_auditors.setEnabled(True)
        QMessageBox.critical(self, "API Error", f"Failed to run AI Auditor:\n{err_msg}")

    def _on_auditor_finished(self, eval_data):
        self.btn_v_auditors.setText("🪄 AI Taxonomy Auditors ▼")
        self.btn_v_auditors.setEnabled(True)
        
        recommendations =[]
        for name, suggested_type in eval_data.items():
            if name in self.vision_data["registry"]:
                current_type = self.vision_data["registry"][name].get("type", "General")
                if current_type != suggested_type:
                    recommendations.append((name, current_type, suggested_type))
                    
        if not recommendations:
            QMessageBox.information(self, "Auditor Complete", "Gemini agrees with all your current General/Specific categorizations!")
        else:
            d = TaxonomyAuditorDialog(recommendations, self)
            if d.exec():
                changes = d.accepted_changes
                for name, new_type in changes.items():
                    self.vision_data["registry"][name]["type"] = new_type
                
                self.refresh_vision_registry_table()
                self.check_dirty_state()
                QMessageBox.information(self, "Success", f"Applied {len(changes)} taxonomy reclassifications.")


    # --- QOL MENUS & EXPLORERS ---
    def open_prompt_explorer(self):
        d = PromptExplorerDialog(self.vision_data, self.streams_map, self)
        d.exec()
        
    def open_base_defaults(self):
        d = BaseDefaultsDialog(self.vision_settings, self)
        d.exec()
        
    def v_stream_context_menu(self, pos):
        item = self.vision_stream_list.itemAt(pos)
        if not item: return
        url = item.data(Qt.ItemDataRole.UserRole)
        m = QMenu()
        a = QAction("🌐 Open Stream URL", self)
        a.triggered.connect(lambda: webbrowser.open(url))
        m.addAction(a)
        m.exec(self.vision_stream_list.mapToGlobal(pos))
        
    def a_stream_context_menu(self, pos):
        item = self.stream_list.itemAt(pos)
        if not item: return
        url = item.data(Qt.ItemDataRole.UserRole)
        m = QMenu()
        a = QAction("🌐 Open Stream URL", self)
        a.triggered.connect(lambda: webbrowser.open(url))
        m.addAction(a)
        m.exec(self.stream_list.mapToGlobal(pos))
        
    def on_registry_selection_changed(self):
        count = len(self.v_registry_table.selectionModel().selectedRows())
        if count > 1: self.lbl_reg_selection.setText(f"[ {count} Selected ]")
        else: self.lbl_reg_selection.setText("")


    def filter_dsp_species(self):
        txt = self.dsp_species_search.text().lower()
        for i in range(self.species_table.rowCount()):
            item = self.species_table.item(i, 0)
            if item:
                self.species_table.setRowHidden(i, txt not in item.text().lower())

    # --- TUNING LOGIC ---
    def apply_custom_prompt(self):
        selected = self.vision_stream_list.selectedItems()
        if len(selected) != 1: return
        url = selected[0].data(Qt.ItemDataRole.UserRole)
        text = self.edit_custom_prompt.toPlainText().strip()
        
        if "stream_overrides" not in self.vision_data:
            self.vision_data["stream_overrides"] = {}
        if url not in self.vision_data["stream_overrides"]:
            self.vision_data["stream_overrides"][url] = {}
            
        current_prompt = self.vision_data["stream_overrides"][url].get("custom_prompt", "")
        
        if text:
            if current_prompt != text:
                self.vision_data["stream_overrides"][url]["custom_prompt"] = text
                self.check_dirty_state()
        else:
            if "custom_prompt" in self.vision_data["stream_overrides"][url]:
                del self.vision_data["stream_overrides"][url]["custom_prompt"]
                self.check_dirty_state()
                
        self.refresh_vision_target_table(url)
        self.refresh_vision_stream_visuals()
        self.status_label.setText("Prompt saved.")
        self.status_label.setStyleSheet("color: orange;")

    def open_stream_size_overrides(self):
        selected = self.vision_stream_list.selectedItems()
        if len(selected) != 1: return
        url = selected[0].data(Qt.ItemDataRole.UserRole)
        name = self.streams_map.get(url, {}).get('name', 'Unknown')
        
        current_overrides = self.vision_data.get("stream_overrides", {}).get(url, {})
        current_sizes = {k: v for k, v in current_overrides.items() if k in["min_frame_single", "min_depth_single", "min_frame_flock", "min_depth_flock", "flock_minimum_count", "environment_type"]}
        
        d = StreamOverrideDialog(name, current_sizes, self)
        if d.exec():
            new_sizes = d.get_overrides()
            
            if new_sizes != current_sizes:
                if "stream_overrides" not in self.vision_data: self.vision_data["stream_overrides"] = {}
                if url not in self.vision_data["stream_overrides"]: self.vision_data["stream_overrides"][url] = {}
                    
                self.vision_data["stream_overrides"][url].pop("min_frame_single", None)
                self.vision_data["stream_overrides"][url].pop("min_depth_single", None)
                self.vision_data["stream_overrides"][url].pop("min_frame_flock", None)
                self.vision_data["stream_overrides"][url].pop("min_depth_flock", None)
                self.vision_data["stream_overrides"][url].pop("flock_minimum_count", None)
                self.vision_data["stream_overrides"][url].pop("environment_type", None)
                
                self.vision_data["stream_overrides"][url].update(new_sizes)
                
                self.check_dirty_state()
                self.refresh_vision_target_table(url)
                self.refresh_vision_stream_visuals()

    def open_animal_tuning(self, preselect=None, preselect_night=False):
        selected = self.vision_stream_list.selectedItems()
        if len(selected) != 1: return
        url = selected[0].data(Qt.ItemDataRole.UserRole)
        name = self.streams_map.get(url, {}).get('name', 'Unknown')
        
        registry_animals = list(self.vision_data.get("registry", {}).keys())
        if not registry_animals:
            QMessageBox.warning(self, "No Animals", "The taxonomy registry is currently empty.")
            return
            
        day_rules = self.vision_data.get("stream_overrides", {}).get(url, {}).get("species_rules", {})
        night_rules = self.vision_data.get("stream_overrides", {}).get(url, {}).get("night_species_rules", {})
        
        d = AnimalTuningDialog(name, registry_animals, day_rules, night_rules, preselect_animal=preselect, preselect_night=preselect_night, parent=self)
        if d.exec():
            animal, fs, ds, ff, df, prompt_rule, is_night_mode = d.get_tuning()
            
            target_dict_name = "night_species_rules" if is_night_mode else "species_rules"
            
            if "stream_overrides" not in self.vision_data: self.vision_data["stream_overrides"] = {}
            if url not in self.vision_data["stream_overrides"]: self.vision_data["stream_overrides"][url] = {}
            if target_dict_name not in self.vision_data["stream_overrides"][url]: self.vision_data["stream_overrides"][url][target_dict_name] = {}
                
            has_rules = any(x != "Don't change (Use Global Defaults)" for x in[fs, ds, ff, df]) or prompt_rule
            
            if not has_rules:
                if animal in self.vision_data["stream_overrides"][url][target_dict_name]:
                    del self.vision_data["stream_overrides"][url][target_dict_name][animal]
            else:
                if animal not in self.vision_data["stream_overrides"][url][target_dict_name]:
                    self.vision_data["stream_overrides"][url][target_dict_name][animal] = {}
                
                r_dict = self.vision_data["stream_overrides"][url][target_dict_name][animal]
                
                if fs != "Don't change (Use Global Defaults)": r_dict["min_frame_single"] = fs
                else: r_dict.pop("min_frame_single", None)
                
                if ds != "Don't change (Use Global Defaults)": r_dict["min_depth_single"] = ds
                else: r_dict.pop("min_depth_single", None)
                
                if ff != "Don't change (Use Global Defaults)": r_dict["min_frame_flock"] = ff
                else: r_dict.pop("min_frame_flock", None)
                
                if df != "Don't change (Use Global Defaults)": r_dict["min_depth_flock"] = df
                else: r_dict.pop("min_depth_flock", None)
                    
                if prompt_rule: r_dict["prompt"] = prompt_rule
                else: r_dict.pop("prompt", None)
                    
            self.check_dirty_state()
            self.refresh_vision_target_table(url)
            self.refresh_vision_stream_visuals()

    def sweep_orphaned_snapshots(self):
        msg = "This will scan the 'vision_snapshots' folder and delete any .jpg files that are no longer linked to an active database record.\n\nProceed?"
        if QMessageBox.question(self, "Confirm Sweep", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes: return
        
        if not VAULT_DIR.exists():
            QMessageBox.information(self, "Done", "Vault directory does not exist yet.")
            return
            
        try:
            self.status_label.setText("Sweeping vault...")
            QApplication.processEvents()
            
            with db_connector.get_db_connection(force_local=True) as con:
                cur = con.cursor()
                cur.execute("SELECT vision_path FROM detections WHERE vision_path IS NOT NULL")
                valid_paths = {str(Path(r[0]).resolve()).lower() for r in cur.fetchall() if r[0]}
                
            deleted = 0
            scanned = 0
            for f in VAULT_DIR.glob("*.jpg"):
                scanned += 1
                if str(f.resolve()).lower() not in valid_paths:
                    try:
                        f.unlink()
                        deleted += 1
                    except Exception as e: logging.error(f"Failed to delete {f}: {e}")
                    
            QMessageBox.information(self, "Sweep Complete", f"Scanned {scanned} images.\nDeleted {deleted} orphaned images.")
            self.status_label.setText(f"Sweep complete. Removed {deleted} images.")
            self.status_label.setStyleSheet("color: #00E676;")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to sweep vault: {e}")

    def clear_vision_log(self):
        if not VISION_LOG_FILE.exists():
            QMessageBox.information(self, "Info", "Vision log does not exist yet.")
            return
            
        if QMessageBox.question(self, "Confirm", "Erase all contents of vision_debug.txt?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            try:
                with open(VISION_LOG_FILE, 'w', encoding='utf-8') as f:
                    f.truncate(0)
                self.status_label.setText("Vision log cleared.")
                self.status_label.setStyleSheet("color: #00E676;")
                QMessageBox.information(self, "Success", "Vision log has been cleared.")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to clear log: {e}")

    def open_global_vision_settings(self):
        try:
            if GBR_CONFIG.exists():
                fresh_cfg = json.loads(GBR_CONFIG.read_text(encoding='utf-8'))
                vision_cfg = fresh_cfg.get("vision_ai", {})
                
                # --- THE STALE MEMORY FIX (1b) ---
                # Completely refresh our local state from disk before opening the dialog
                for k, v in vision_cfg.items():
                    self.vision_settings[k] = v
        except: pass
        
        d = GlobalVisionSettingsDialog(self.vision_settings, self.vision_data["global_defaults"], self)
        if d.exec():
            v_sets, v_defs = d.get_values()
            self.vision_settings.update(v_sets)
            self.vision_data["global_defaults"].update(v_defs)
            self.check_dirty_state()

    def open_global_audio_settings(self):
        d = GlobalAudioSettingsDialog(self.v2_data["global_defaults"], self)
        if d.exec():
            new_vals = d.get_values()
            self.v2_data["global_defaults"] = new_vals
            self.check_dirty_state()

    def _migrate_to_v2(self, raw_targets):
        if raw_targets.get("_version") == 2:
            self.v2_data = copy.deepcopy(raw_targets)
            for url, tgts in self.v2_data.get("assignments", {}).items():
                for t in tgts:
                    if t['display'] not in self.master_animals:
                        self.master_animals[t['display']] = t['profile']
            return

        self.status_label.setText("Migrating legacy acoustic targets...")
        self.status_label.setStyleSheet("color: orange;")
        
        for u, val in raw_targets.items():
            if u == "_version": continue
            clean_list =[]
            if isinstance(val, str):
                clean_list.append({'profile': val, 'display': val})
                self.master_animals[val] = val
            elif isinstance(val, list):
                for v in val:
                    if isinstance(v, str): 
                        clean_list.append({'profile': v, 'display': v})
                        self.master_animals[v] = v
                    else: 
                        clean_list.append(v)
                        self.master_animals[v['display']] = v['profile']
            self.v2_data["assignments"][u] = clean_list
        self.check_dirty_state()


    # --- STATE SAVING & LOADING ---
    def save_gui_state(self):
        try:
            s = json.loads(HUB_SETTINGS_FILE.read_text()) if HUB_SETTINGS_FILE.exists() else {}
            geom = self.geometry()
            s['geometry'] = {'x': geom.x(), 'y': geom.y(), 'w': geom.width(), 'h': geom.height()}
            s['v_splitter'] = self.v_splitter.sizes()
            s['a_splitter'] = self.a_splitter.sizes()
            s['v_target_widths'] =[self.v_target_table.columnWidth(i) for i in range(self.v_target_table.columnCount())]
            s['v_registry_widths'] =[self.v_registry_table.columnWidth(i) for i in range(self.v_registry_table.columnCount())]
            s['target_widths'] =[self.target_table.columnWidth(i) for i in range(self.target_table.columnCount())]
            s['species_widths'] =[self.species_table.columnWidth(i) for i in range(self.species_table.columnCount())]
            s['current_tab'] = self.tabs.currentIndex()
            HUB_SETTINGS_FILE.write_text(json.dumps(s, indent=2))
        except: pass

    def load_gui_state(self):
        try:
            if HUB_SETTINGS_FILE.exists():
                s = json.loads(HUB_SETTINGS_FILE.read_text())
                if 'geometry' in s:
                    g = s['geometry']
                    self.setGeometry(g['x'], g['y'], g['w'], g['h'])
                if 'v_splitter' in s: self.v_splitter.setSizes(s['v_splitter'])
                if 'a_splitter' in s: self.a_splitter.setSizes(s['a_splitter'])
                
                if 'v_target_widths' in s and len(s['v_target_widths']) == self.v_target_table.columnCount():
                    for i, w in enumerate(s['v_target_widths']): self.v_target_table.setColumnWidth(i, w)
                if 'v_registry_widths' in s and len(s['v_registry_widths']) == self.v_registry_table.columnCount():
                    for i, w in enumerate(s['v_registry_widths']): self.v_registry_table.setColumnWidth(i, w)
                if 'target_widths' in s and len(s['target_widths']) == self.target_table.columnCount():
                    for i, w in enumerate(s['target_widths']): self.target_table.setColumnWidth(i, w)
                if 'species_widths' in s and len(s['species_widths']) == self.species_table.columnCount():
                    for i, w in enumerate(s['species_widths']): self.species_table.setColumnWidth(i, w)
                
                if 'current_tab' in s: self.tabs.setCurrentIndex(s['current_tab'])
        except: pass
        
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.ui_save_timer.start(500)
        
    def moveEvent(self, event):
        super().moveEvent(event)
        self.ui_save_timer.start(500)

    # --- DYNAMIC SORTING UI ---
    
    def update_vision_stream_counts_only(self):
        """Lightweight update for the stream counter, bypassing full list rebuild."""
        visible_count = 0
        enabled_count = 0
        for i in range(self.vision_stream_list.count()):
            item = self.vision_stream_list.item(i)
            if not item.isHidden():
                visible_count += 1
            if item.checkState() == Qt.CheckState.Checked:
                enabled_count += 1
        self.lbl_v_stream_count.setText(f"({visible_count} visible | {enabled_count} enabled)")
    
    def select_all_vision(self):
        self.vision_stream_list.blockSignals(True)
        for i in range(self.vision_stream_list.count()):
            item = self.vision_stream_list.item(i)
            if not item.isHidden():
                item.setCheckState(Qt.CheckState.Checked)
                url = item.data(Qt.ItemDataRole.UserRole)
                if "enabled_streams" not in self.vision_settings: self.vision_settings["enabled_streams"] =[]
                if url not in self.vision_settings["enabled_streams"]:
                    self.vision_settings["enabled_streams"].append(url)
        self.vision_stream_list.blockSignals(False)
        self.update_vision_stream_counts_only()
        self.check_dirty_state()

    def deselect_all_vision(self):
        self.vision_stream_list.blockSignals(True)
        for i in range(self.vision_stream_list.count()):
            item = self.vision_stream_list.item(i)
            if not item.isHidden():
                item.setCheckState(Qt.CheckState.Unchecked)
                url = item.data(Qt.ItemDataRole.UserRole)
                if "enabled_streams" in self.vision_settings and url in self.vision_settings["enabled_streams"]:
                    self.vision_settings["enabled_streams"].remove(url)
        self.vision_stream_list.blockSignals(False)
        self.update_vision_stream_counts_only()
        self.check_dirty_state()

    def create_sort_menu(self, tab_type):
        menu = QMenu(self)
        actions =[
            ("Name (A-Z)", "name", False),
            ("Name (Z-A)", "name", True),
            ("Created (Newest)", "created_at", True),
            ("Created (Oldest)", "created_at", False),
            ("Modified (Newest)", "updated_at", True),
            ("Modified (Oldest)", "updated_at", False),
            ("Status (Assigned/Enabled First)", "status", True),
            ("Status (Unassigned/Disabled First)", "status", False)
        ]
        
        for label, key, reverse in actions:
            action = QAction(label, self)
            action.triggered.connect(partial(self.sort_streams, key, reverse, label, tab_type))
            menu.addAction(action)
        return menu

    def sort_streams(self, key, reverse, label, tab_type):
        if tab_type == "vision":
            self.vision_sort_mode = (key, reverse)
            arrow = "▼" if reverse else "▲"
            self.btn_v_sort.setText(f"Sort: {label} {arrow}")
            self._sort_vision_streams()
        else:
            self.audio_sort_mode = (key, reverse)
            arrow = "▼" if reverse else "▲"
            self.btn_a_sort.setText(f"Sort: {label} {arrow}")
            self._sort_audio_streams()

    def sort_by_diversity(self, mode):
        ranking = {}
        try:
            with db_connector.get_db_connection(force_local=True) as con:
                cur = con.cursor()
                cur.execute("SELECT channel_url, species FROM detections WHERE detection_method IN ('vision', 'multimodal')")
                rows = cur.fetchall()
                
                stream_species = {}
                for url, sp in rows:
                    if url not in stream_species:
                        stream_species[url] =[]
                    stream_species[url].append(sp)
                    
                for url, sp_list in stream_species.items():
                    unique_bio = set()
                    unique_bird = set()
                    total_bio = 0
                    total_bird = 0
                    
                    for sp in sp_list:
                        cat = get_species_category(sp)
                        if cat == 'DSP':
                            unique_bio.add(sp)
                            total_bio += 1
                        else:
                            unique_bird.add(sp)
                            total_bird += 1
                    
                    if mode == "vision_bio":
                        ranking[url] = (len(unique_bio), total_bio)
                    elif mode == "vision_bird":
                        ranking[url] = (len(unique_bird), total_bird)
                    elif mode == "vision_combined":
                        ranking[url] = (len(unique_bio) + len(unique_bird), total_bio + total_bird)
                        
        except Exception as e:
            logging.error(f"Diversity query failed: {e}")

        self.vision_sort_mode = ("diversity", True) 
        self._sort_vision_streams(custom_ranking=ranking)
        
        if mode == "vision_bio":
            self.btn_v_sort_div.setText("🌟 Sort by Biodiversity (Bio Only) ▼")
        elif mode == "vision_bird":
            self.btn_v_sort_div.setText("🌟 Sort by Biodiversity (Birds Only) ▼")
        else:
            self.btn_v_sort_div.setText("🌟 Sort by Biodiversity (Combined) ▼")

    def _sort_vision_streams(self, custom_ranking=None):
        selected_urls =[item.data(Qt.ItemDataRole.UserRole) for item in self.vision_stream_list.selectedItems()]
        
        items_data =[]
        for i in range(self.vision_stream_list.count()):
            item = self.vision_stream_list.item(i)
            url = item.data(Qt.ItemDataRole.UserRole)
            chk = item.checkState()
            items_data.append((url, chk))
            
        key, reverse = self.vision_sort_mode
        def sort_func(x):
            url, chk = x
            if custom_ranking is not None:
                div, vol = custom_ranking.get(url, (0, 0))
                return (div, vol)
                
            meta = self.streams_map.get(url, {})
            rules = len(self.vision_data.get("assignments", {}).get(url,[]))
            
            if key == 'name': return meta.get('name', '').lower()
            if key == 'created_at': return float(meta.get('created_at') or 0.0)
            if key == 'updated_at': return float(meta.get('updated_at') or 0.0)
            if key == 'status': return (chk.value, rules)
            return 0
            
        items_data.sort(key=sort_func, reverse=reverse)
        
        self.vision_stream_list.blockSignals(True)
        self.vision_stream_list.clear()
        
        for url, chk in items_data:
            meta = self.streams_map.get(url, {})
            name = meta.get('name', 'Unknown')
            new_item = QListWidgetItem(name)
            new_item.setFlags(new_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            new_item.setCheckState(chk)
            new_item.setData(Qt.ItemDataRole.UserRole, url)
            new_item.setToolTip(name) 
            self.vision_stream_list.addItem(new_item)
            if url in selected_urls:
                new_item.setSelected(True)
                
        self.vision_stream_list.blockSignals(False)
        self.refresh_vision_stream_visuals() 

    def _sort_audio_streams(self, custom_ranking=None):
        selected_urls =[item.data(Qt.ItemDataRole.UserRole) for item in self.stream_list.selectedItems()]
        
        items_data =[]
        for i in range(self.stream_list.count()):
            item = self.stream_list.item(i)
            url = item.data(Qt.ItemDataRole.UserRole)
            items_data.append(url)
            
        key, reverse = self.audio_sort_mode
        def sort_func(url):
            if custom_ranking is not None:
                div, vol = custom_ranking.get(url, (0, 0))
                return (div, vol)
                
            meta = self.streams_map.get(url, {})
            targets = len(self.v2_data.get("assignments", {}).get(url,[]))
            
            if key == 'name': return meta.get('name', '').lower()
            if key == 'created_at': return float(meta.get('created_at') or 0.0)
            if key == 'updated_at': return float(meta.get('updated_at') or 0.0)
            if key == 'status': return targets
            return 0
            
        items_data.sort(key=sort_func, reverse=reverse)
        
        self.stream_list.blockSignals(True)
        self.stream_list.clear()
        
        for url in items_data:
            meta = self.streams_map.get(url, {})
            name = meta.get('name', 'Unknown')
            new_item = QListWidgetItem(name)
            new_item.setData(Qt.ItemDataRole.UserRole, url)
            new_item.setToolTip(name)
            self.stream_list.addItem(new_item)
            if url in selected_urls:
                new_item.setSelected(True)
                
        self.stream_list.blockSignals(False)
        self.refresh_audio_stream_visuals()


    # --- PROCESS MANAGEMENT ---

    def check_vision_process(self):
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if 'python' in p.info['name'].lower() and p.info['cmdline'] and " ".join(p.info['cmdline']).lower().find("vision_scheduler.py") != -1:
                    return p.pid
            except: pass
        return None

    def update_control_state(self):
        pid = self.check_vision_process()
        if pid:
            self.lbl_vision_status.setText(f"Status: RUNNING (PID: {pid})")
            self.lbl_vision_status.setStyleSheet("color: #00E676; font-weight: bold;")
            self.btn_start_vision.setEnabled(False)
            self.btn_stop_vision.setEnabled(True)
        else:
            self.lbl_vision_status.setText("Status: STOPPED")
            self.lbl_vision_status.setStyleSheet("color: #EF5350; font-weight: bold;")
            self.btn_start_vision.setEnabled(True)
            self.btn_stop_vision.setEnabled(False)

    def start_vision_engine(self):
        if self.is_dirty:
            QMessageBox.warning(self, "Unsaved Changes", "Please save your configurations first.")
            return
            
        if self.check_vision_process():
            QMessageBox.warning(self, "Warning", "Vision Engine is already running."); self.update_control_state(); return
            
        valid_keys =[k for k in self.vision_settings.get("api_keys", []) if k.get("key", "").strip()]
        if not valid_keys:
            QMessageBox.critical(self, "Missing API Key", "Please add at least one Gemini API Key in the 'Global Vision Defaults'."); return

        try:
            ex = sys.executable.replace("pythonw.exe", "python.exe") if "pythonw.exe" in sys.executable.lower() else sys.executable
            flags = {}
            if sys.platform == "win32" and self.vision_settings.get("hide_console", False):
                flags['creationflags'] = subprocess.CREATE_NO_WINDOW
            elif sys.platform == "win32":
                flags['creationflags'] = subprocess.CREATE_NEW_CONSOLE
                
            subprocess.Popen([ex, str(VISION_SCHEDULER_SCRIPT)], **flags)
            self.lbl_vision_status.setText("Status: STARTING..."); self.lbl_vision_status.setStyleSheet("color: orange;")
            self.btn_start_vision.setEnabled(False); QTimer.singleShot(1500, self.update_control_state)
            self.status_label.setText("Vision Engine launched successfully.")
        except Exception as e:
            logging.error(f"Failed to start vision engine: {e}", exc_info=True)
            QMessageBox.critical(self, "Error", f"Failed to start Vision Engine:\n{e}")

    def stop_vision_engine(self):
        pid = self.check_vision_process()
        if pid:
            try:
                if sys.platform == "win32": subprocess.run(f"TASKKILL /F /T /PID {pid}", check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                else: p = psutil.Process(pid);[c.kill() for c in p.children(recursive=True)]; p.kill()
                self.status_label.setText("Vision Engine killed.")
            except Exception as e:
                logging.error(f"Error killing process {pid}: {e}"); self.status_label.setText("Error killing Vision Engine.")
        self.lbl_vision_status.setText("Status: STOPPING..."); self.lbl_vision_status.setStyleSheet("color: orange;")
        self.btn_stop_vision.setEnabled(False); QTimer.singleShot(1500, self.update_control_state)

    def closeEvent(self, event):
        self.save_gui_state()
        self.stop_vision_engine(); event.accept()

    # --- HOT RELOADING ---
    def reload_all_data(self):
        if self.is_dirty:
            reply = QMessageBox.question(self, "Discard Changes?", 
                                         "Are you sure you want to discard your unsaved changes and reload from disk?",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            
        self.load_data()
        self.status_label.setText("Reverted to saved configuration.")
        self.status_label.setStyleSheet("color: #00E676; font-weight: bold;")

    # --- LOADING DATA ---
    def load_data(self):
        if not GBR_CONFIG.exists(): return
        try:
            if VISION_TARGETS_FILE.exists():
                self.last_vision_mtime = VISION_TARGETS_FILE.stat().st_mtime
                
            # 1. MAIN CONFIG
            gbr_data = json.loads(GBR_CONFIG.read_text(encoding='utf-8'))
            vision_cfg = gbr_data.get("vision_ai", {})
            self.vision_settings["api_key"] = vision_cfg.get("api_key", "")
            self.vision_settings["api_keys"] = vision_cfg.get("api_keys",[])
            
            if not self.vision_settings["api_keys"] and self.vision_settings["api_key"]:
                self.vision_settings["api_keys"] =[{"key": self.vision_settings["api_key"], "tier": "Free", "status": "Active", "exhausted_until": 0, "usage_count": 0}]
                
            self.vision_settings["vision_workers"] = vision_cfg.get("vision_workers", 1)
            
            # --- PREDATOR REFLEX LOAD ---
            self.vision_settings["predator_reflex_threshold"] = vision_cfg.get("predator_reflex_threshold", 85)
            
            self.vision_settings["vision_dormancy_threshold_mins"] = vision_cfg.get("vision_dormancy_threshold_mins", 60)
            self.vision_settings["vision_dormant_interval_mins"] = vision_cfg.get("vision_dormant_interval_mins", 20)
            
            self.vision_settings["cycle_interval_seconds"] = vision_cfg.get("cycle_interval_seconds", 60)
            self.vision_settings["use_motion_detector"] = vision_cfg.get("use_motion_detector", True)
            self.vision_settings["motion_sensitivity_percent"] = vision_cfg.get("motion_sensitivity_percent", 5.0)
            self.vision_settings["retention_limit"] = vision_cfg.get("retention_limit", 5)
            self.vision_settings["log_retention_hours"] = vision_cfg.get("log_retention_hours", 48)
            self.vision_settings["auto_sweep_vault_hours"] = vision_cfg.get("auto_sweep_vault_hours", 24)
            self.vision_settings["hide_console"] = vision_cfg.get("hide_console", False)
            
            self.vision_settings["ai_temperature"] = vision_cfg.get("ai_temperature", 0.15)
            self.vision_settings["speck_size_percent"] = vision_cfg.get("speck_size_percent", 3)
            self.vision_settings["flock_minimum_count"] = vision_cfg.get("flock_minimum_count", 4)
            
            self.vision_settings["min_frame_single"] = vision_cfg.get("min_frame_single", "Large")
            self.vision_settings["min_depth_single"] = vision_cfg.get("min_depth_single", "Near")
            self.vision_settings["min_frame_flock"] = vision_cfg.get("min_frame_flock", "Small")
            self.vision_settings["min_depth_flock"] = vision_cfg.get("min_depth_flock", "Background")

            # --- THE DATA THROTTLE INTEGRATION ---
            self.vision_settings["vision_resolution"] = vision_cfg.get("vision_resolution", "720p")
            self.vision_settings["strict_proxy"] = vision_cfg.get("strict_proxy", False)
            
            # --- TELEGRAM ALERTS PREFS ---
            self.vision_settings["telegram_alerts"] = vision_cfg.get("telegram_alerts", {
                "enabled": True,
                "alert_multimodal": True,
                "alert_public": True,
                "alert_filtered": False,
                "attach_image": True,
                "include_reasoning": True
            })
            
            self.vision_settings["enabled_streams"] = vision_cfg.get("enabled_streams",[])
            enabled_vision_streams = set(self.vision_settings["enabled_streams"])

            self.stream_list.clear()
            self.vision_stream_list.clear()
            self.vision_stream_list.blockSignals(True)
            self.streams_map = {}
            
            for s in gbr_data.get('streams',[]):
                if not s.get('enabled', True): continue
                url = s.get('page_url', '')
                if not url: continue
                name = s.get('name', 'Unknown')
                
                stype = s.get('stream_type', '')
                if not stype:
                    if 'youtu' in url: stype = 'youtube'
                    elif '.m3u8' in url: stype = 'hls'
                    elif any(url.endswith(ext) for ext in['.mp3', '.wav', '.ogg']): stype = 'audio'
                    else: stype = 'unknown'
                
                self.streams_map[url] = { 
                    'name': name, 
                    'lat': float(s.get('lat', 0)), 
                    'lon': float(s.get('lon', 0)),
                    'created_at': s.get('created_at', 0),
                    'updated_at': s.get('updated_at', 0),
                    'stream_type': stype
                }
                
                a_item = QListWidgetItem(name); a_item.setData(Qt.ItemDataRole.UserRole, url); a_item.setToolTip(name)
                self.stream_list.addItem(a_item)
                
                # EXCLUDE PURE AUDIO FROM VISION TAB
                if stype != 'audio':
                    v_item = QListWidgetItem(name); v_item.setData(Qt.ItemDataRole.UserRole, url)
                    v_item.setFlags(v_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    v_item.setCheckState(Qt.CheckState.Checked if url in enabled_vision_streams else Qt.CheckState.Unchecked)
                    v_item.setToolTip(name)
                    self.vision_stream_list.addItem(v_item)

            self.vision_stream_list.blockSignals(False)

            # 2. VISION TARGETS
            if VISION_TARGETS_FILE.exists():
                try: self.vision_data = json.loads(VISION_TARGETS_FILE.read_text(encoding='utf-8'))
                except: pass
            
            if "registry" not in self.vision_data: self.vision_data["registry"] = {}
            if "assignments" not in self.vision_data: self.vision_data["assignments"] = {}
            if "stream_overrides" not in self.vision_data: self.vision_data["stream_overrides"] = {}
            if "global_defaults" not in self.vision_data: self.vision_data["global_defaults"] = {"ignore_general_animals": False, "default_taxonomy_mute": 60, "master_system_prompt": "", "forbidden_words":[]}
            
            # Failsafe if forbidden words isn't there
            if "forbidden_words" not in self.vision_data["global_defaults"]:
                self.vision_data["global_defaults"]["forbidden_words"] =[]
            
            for url, tgts in self.vision_data.get("assignments", {}).items():
                migrated =[]
                for t in tgts:
                    if isinstance(t, str): migrated.append({"name": t, "rule": "Enforce Target"})
                    else: migrated.append(t)
                self.vision_data["assignments"][url] = migrated
            
            for k, v in self.vision_data.get("registry", {}).items():
                if "show_on_map" not in v: v["show_on_map"] = True

            # 3. AUDIO TARGETS
            if TARGETS_FILE.exists():
                try:
                    raw = json.loads(TARGETS_FILE.read_text(encoding='utf-8'))
                    self._migrate_to_v2(raw)
                except: pass
                
            self.refresh_species_table() 
            self.refresh_vision_registry_table()
            
            # Tables Sorting Setup
            self.v_target_table.setSortingEnabled(True)
            self.v_registry_table.setSortingEnabled(True)
            self.target_table.setSortingEnabled(True)
            self.species_table.setSortingEnabled(True)
                
            self._sort_vision_streams()
            self._sort_audio_streams()
            
            # Setup Deep State Snapshots
            self.saved_v2_data = copy.deepcopy(self.v2_data)
            self.saved_vision_settings = copy.deepcopy(self.vision_settings)
            self.saved_vision_data = copy.deepcopy(self.vision_data)
            
            self.check_dirty_state()
            
        except Exception as e: 
            logging.error(f"Error loading config: {e}")

    # --- PANE 1: VISION STREAMS ---
    def refresh_vision_stream_visuals(self):
        self.vision_stream_list.blockSignals(True)
        for i in range(self.vision_stream_list.count()):
            item = self.vision_stream_list.item(i)
            url = item.data(Qt.ItemDataRole.UserRole)
            name = self.streams_map.get(url, {}).get('name', 'Unknown')
            
            assignments = self.vision_data["assignments"].get(url,[])
            s_overrides = self.vision_data.get("stream_overrides", {}).get(url, {})
            
            global_override_count = len([k for k in s_overrides if k in["custom_prompt", "min_frame_single", "min_depth_single", "min_frame_flock", "min_depth_flock", "flock_minimum_count", "environment_type"]])
            tuning_count = len(s_overrides.get("species_rules", {}))
            night_tuning_count = len(s_overrides.get("night_species_rules", {}))
            
            total_rules = len(assignments) + global_override_count + tuning_count + night_tuning_count
            
            if total_rules > 0:
                item.setText(f"[ {total_rules} Rules ] {name}")
                item.setForeground(QBrush(QColor("#00E676"))) 
            else:
                item.setText(name)
                item.setForeground(QBrush(QColor("#FFFFFF")))
                
        self.filter_vision_streams()
        self.vision_stream_list.blockSignals(False)

    def filter_vision_streams(self):
        search_text = self.vision_search_bar.text().lower()
        mode = self.combo_vision_filter.currentText()
        show_ip = self.chk_include_ip_cams.isChecked()
        
        visible_count = 0
        enabled_count = 0
        
        for i in range(self.vision_stream_list.count()):
            item = self.vision_stream_list.item(i)
            url = item.data(Qt.ItemDataRole.UserRole)
            meta = self.streams_map.get(url, {})
            
            stype = meta.get('stream_type', 'youtube')
            is_assigned = (item.checkState() == Qt.CheckState.Checked)
            
            if is_assigned:
                enabled_count += 1
                
            if not show_ip and stype in['hls', 'direct_stream']:
                item.setHidden(True)
                continue
                
            has_assignments = len(self.vision_data.get("assignments", {}).get(url,[])) > 0
            has_overrides = len(self.vision_data.get("stream_overrides", {}).get(url, {})) > 0
            has_rules = has_assignments or has_overrides
            
            match_text = search_text in item.text().lower()
            
            if mode == "Enabled Only" and not is_assigned:
                item.setHidden(True)
                continue
            elif mode == "Disabled Only" and is_assigned:
                item.setHidden(True)
                continue
            elif mode == "Assigned Rules Only" and not has_rules:
                item.setHidden(True)
                continue
                
            item.setHidden(not match_text)
            if not item.isHidden():
                visible_count += 1
                
        self.lbl_v_stream_count.setText(f"({visible_count} visible | {enabled_count} enabled)")

    def on_vision_stream_check_changed(self, item):
        url = item.data(Qt.ItemDataRole.UserRole)
        is_checked = (item.checkState() == Qt.CheckState.Checked)
        
        if "enabled_streams" not in self.vision_settings:
            self.vision_settings["enabled_streams"] =[]
            
        if is_checked and url not in self.vision_settings["enabled_streams"]:
            self.vision_settings["enabled_streams"].append(url)
        elif not is_checked and url in self.vision_settings["enabled_streams"]:
            self.vision_settings["enabled_streams"].remove(url)
            
        self.update_vision_stream_counts_only()
        self.check_dirty_state()

    def on_vision_stream_selection_changed(self):
        selected = self.vision_stream_list.selectedItems()
        if not selected:
            self.lbl_selected_vision_stream.setText("No Stream Selected")
            self.v_target_table.setRowCount(0)
            self.btn_v_add_target.setEnabled(False)
            self.btn_v_remove_target.setEnabled(False)
            self.btn_v_clear_targets.setEnabled(False)
            self.edit_custom_prompt.setEnabled(False)
            self.btn_size_overrides.setEnabled(False)
            self.btn_tune_animal.setEnabled(False)
            self.btn_apply_prompt.setEnabled(False)
            self.edit_custom_prompt.clear()
            return

        self.btn_v_add_target.setEnabled(True)

        if len(selected) > 1:
            self.lbl_selected_vision_stream.setText(f"[ {len(selected)} Streams Selected ]")
            self.v_target_table.setRowCount(0)
            self.btn_v_remove_target.setEnabled(False)
            self.btn_v_clear_targets.setEnabled(True)
            
            self.edit_custom_prompt.setEnabled(False)
            self.btn_size_overrides.setEnabled(False)
            self.btn_tune_animal.setEnabled(False)
            self.btn_apply_prompt.setEnabled(False)
            self.edit_custom_prompt.blockSignals(True)
            self.edit_custom_prompt.clear()
            self.edit_custom_prompt.blockSignals(False)
        else:
            url = selected[0].data(Qt.ItemDataRole.UserRole)
            name = self.streams_map.get(url, {}).get('name', 'Unknown')
            self.lbl_selected_vision_stream.setText(name)
            self.refresh_vision_target_table(url)
            
            self.btn_v_remove_target.setEnabled(self.v_target_table.rowCount() > 0)
            self.btn_v_clear_targets.setEnabled(self.v_target_table.rowCount() > 0)
            
            self.edit_custom_prompt.setEnabled(True)
            self.btn_apply_prompt.setEnabled(True)
            self.btn_size_overrides.setEnabled(True)
            self.btn_tune_animal.setEnabled(True)
            
            self.edit_custom_prompt.blockSignals(True)
            overrides = self.vision_data.get("stream_overrides", {}).get(url, {})
            self.edit_custom_prompt.setText(overrides.get("custom_prompt", ""))
            self.edit_custom_prompt.blockSignals(False)

    # --- PANE 2: VISION ASSIGNMENTS ---
    def refresh_vision_target_table(self, url):
        self.v_target_table.setSortingEnabled(False)
        self.v_target_table.setRowCount(0)
        
        assignments = self.vision_data["assignments"].get(url,[])
        overrides = self.vision_data.get("stream_overrides", {}).get(url, {})
        
        override_rows =[]
        
        # 1. Global Stream Prompts
        if overrides.get("custom_prompt"):
            override_rows.append(("Global Prompt", f'"{overrides["custom_prompt"]}"', "System Override", "prompt"))
            
        if "environment_type" in overrides: override_rows.append(("Environment", f'{overrides["environment_type"]}', "System Override", "environment_type"))
        if "min_frame_single" in overrides: override_rows.append(("Min Frame (Single)", f'{overrides["min_frame_single"]}', "System Override", "min_frame_single"))
        if "min_depth_single" in overrides: override_rows.append(("Min Depth (Single)", f'{overrides["min_depth_single"]}', "System Override", "min_depth_single"))
        if "min_frame_flock" in overrides: override_rows.append(("Min Frame (Flock)", f'{overrides["min_frame_flock"]}', "System Override", "min_frame_flock"))
        if "min_depth_flock" in overrides: override_rows.append(("Min Depth (Flock)", f'{overrides["min_depth_flock"]}', "System Override", "min_depth_flock"))
            
        if "flock_minimum_count" in overrides:
            override_rows.append(("Flock Min Count", f'{overrides["flock_minimum_count"]} animals', "System Override", "flock"))
            
        # 2. Nested Animal Tuning Rules
        species_rules = overrides.get("species_rules", {})
        tuning_rows =[]
        for sp, rules in species_rules.items():
            rule_texts =[]
            if "min_frame_single" in rules: rule_texts.append(f"Fr(S): {rules['min_frame_single']}")
            if "min_depth_single" in rules: rule_texts.append(f"Dp(S): {rules['min_depth_single']}")
            if "min_frame_flock" in rules: rule_texts.append(f"Fr(F): {rules['min_frame_flock']}")
            if "min_depth_flock" in rules: rule_texts.append(f"Dp(F): {rules['min_depth_flock']}")
            if "prompt" in rules: rule_texts.append(f"Prompt: '{rules['prompt']}'")
            rule_str = " | ".join(rule_texts)
            tuning_rows.append((sp, rule_str, "Local Override", f"species_tuning:{sp}"))
            
        # 3. Night/Low-Vis Tuning Rules
        night_species_rules = overrides.get("night_species_rules", {})
        night_tuning_rows =[]
        for sp, rules in night_species_rules.items():
            rule_texts =[]
            if "min_frame_single" in rules: rule_texts.append(f"Fr(S): {rules['min_frame_single']}")
            if "min_depth_single" in rules: rule_texts.append(f"Dp(S): {rules['min_depth_single']}")
            if "min_frame_flock" in rules: rule_texts.append(f"Fr(F): {rules['min_frame_flock']}")
            if "min_depth_flock" in rules: rule_texts.append(f"Dp(F): {rules['min_depth_flock']}")
            if "prompt" in rules: rule_texts.append(f"Prompt: '{rules['prompt']}'")
            rule_str = " | ".join(rule_texts)
            night_tuning_rows.append((f"🌙 {sp}", rule_str, "Night/Low-Vis Override", f"night_species_tuning:{sp}"))
            
        self.v_target_table.setRowCount(len(assignments) + len(override_rows) + len(tuning_rows) + len(night_tuning_rows))
        
        row_idx = 0
        
        for name, rule, type_str, meta_key in override_rows:
            item_name = QTableWidgetItem(name)
            item_name.setFlags(item_name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_name.setData(Qt.ItemDataRole.UserRole, meta_key) 
            item_rule = QTableWidgetItem(rule); item_rule.setFlags(item_rule.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_type = QTableWidgetItem(type_str); item_type.setFlags(item_type.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_name.setForeground(QBrush(QColor("#29B6F6"))); item_rule.setForeground(QBrush(QColor("#29B6F6"))); item_type.setForeground(QBrush(QColor("#29B6F6")))
            font = item_name.font(); font.setItalic(True); item_name.setFont(font)
            
            item_name.setToolTip(name)
            item_rule.setToolTip(rule)
            item_type.setToolTip(type_str)
            
            self.v_target_table.setItem(row_idx, 0, item_name); self.v_target_table.setItem(row_idx, 1, item_rule); self.v_target_table.setItem(row_idx, 2, item_type)
            row_idx += 1
            
        for name, rule, type_str, meta_key in tuning_rows:
            item_name = QTableWidgetItem(name)
            item_name.setFlags(item_name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_name.setData(Qt.ItemDataRole.UserRole, meta_key) 
            item_rule = QTableWidgetItem(rule); item_rule.setFlags(item_rule.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_type = QTableWidgetItem(type_str); item_type.setFlags(item_type.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_name.setForeground(QBrush(QColor("#FFA726"))); item_rule.setForeground(QBrush(QColor("#FFA726"))); item_type.setForeground(QBrush(QColor("#FFA726")))
            font = item_name.font(); font.setBold(True); item_name.setFont(font)
            
            item_name.setToolTip(name)
            item_rule.setToolTip(rule)
            item_type.setToolTip(type_str)
            
            self.v_target_table.setItem(row_idx, 0, item_name); self.v_target_table.setItem(row_idx, 1, item_rule); self.v_target_table.setItem(row_idx, 2, item_type)
            row_idx += 1
            
        for name, rule, type_str, meta_key in night_tuning_rows:
            item_name = QTableWidgetItem(name)
            item_name.setFlags(item_name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_name.setData(Qt.ItemDataRole.UserRole, meta_key) 
            item_rule = QTableWidgetItem(rule); item_rule.setFlags(item_rule.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_type = QTableWidgetItem(type_str); item_type.setFlags(item_type.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_name.setForeground(QBrush(QColor("#b39ddb"))); item_rule.setForeground(QBrush(QColor("#b39ddb"))); item_type.setForeground(QBrush(QColor("#b39ddb")))
            font = item_name.font(); font.setBold(True); item_name.setFont(font)
            
            item_name.setToolTip(name)
            item_rule.setToolTip(rule)
            item_type.setToolTip(type_str)
            
            self.v_target_table.setItem(row_idx, 0, item_name); self.v_target_table.setItem(row_idx, 1, item_rule); self.v_target_table.setItem(row_idx, 2, item_type)
            row_idx += 1
        
        for t_data in assignments:
            canonical_name = t_data["name"]
            rule = t_data["rule"]
            
            item_name = QTableWidgetItem(canonical_name)
            item_name.setFlags(item_name.flags() & ~Qt.ItemFlag.ItemIsEditable) 
            item_name.setData(Qt.ItemDataRole.UserRole, "animal")
            item_rule = QTableWidgetItem(rule); item_rule.setFlags(item_rule.flags() & ~Qt.ItemFlag.ItemIsEditable) 
            reg_data = self.vision_data["registry"].get(canonical_name, {})
            v_type = reg_data.get("type", "General")
            item_type = QTableWidgetItem(v_type); item_type.setFlags(item_type.flags() & ~Qt.ItemFlag.ItemIsEditable) 
            
            if rule == "Local Ignore":
                item_rule.setForeground(QBrush(QColor("#EF5350")))
                item_name.setForeground(QBrush(QColor("#888888")))
            elif v_type == 'Specific':
                item_name.setForeground(QBrush(QColor("#00E676")))
                item_type.setForeground(QBrush(QColor("#00E676")))
                item_rule.setForeground(QBrush(QColor("#00E676")))
            else:
                item_name.setForeground(QBrush(QColor("#888888")))
                item_type.setForeground(QBrush(QColor("#888888")))
                
            item_name.setToolTip(canonical_name)
            item_rule.setToolTip(rule)
            item_type.setToolTip(v_type)
                
            self.v_target_table.setItem(row_idx, 0, item_name); self.v_target_table.setItem(row_idx, 1, item_rule); self.v_target_table.setItem(row_idx, 2, item_type)
            row_idx += 1
            
        self.v_target_table.setSortingEnabled(True)

    def assign_vision_target(self):
        try:
            selected_items = self.vision_stream_list.selectedItems()
            if not selected_items: return
            
            dialog = AssignVisionTargetDialog(self.vision_data["registry"],[], self)
            
            if dialog.exec():
                selected_dicts = dialog.get_selected()
                if selected_dicts:
                    for item in selected_items:
                        url = item.data(Qt.ItemDataRole.UserRole)
                        if "assignments" not in self.vision_data: self.vision_data["assignments"] = {}
                        if url not in self.vision_data["assignments"]: self.vision_data["assignments"][url] =[]
                        
                        for new_rule in selected_dicts:
                            self.vision_data["assignments"][url] =[t for t in self.vision_data["assignments"][url] if t["name"] != new_rule["name"]]
                            self.vision_data["assignments"][url].append(new_rule)
                            
                    self.refresh_vision_stream_visuals()
                    self.on_vision_stream_selection_changed()
                    self.check_dirty_state()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to assign vision target: {e}")

    def remove_vision_target(self):
        try:
            selected_streams = self.vision_stream_list.selectedItems()
            if len(selected_streams) != 1: 
                QMessageBox.warning(self, "Selection Error", "Please select exactly ONE stream from the list to remove individual rules.")
                return
                
            url = selected_streams[0].data(Qt.ItemDataRole.UserRole)
            
            selected_rows =[idx.row() for idx in self.v_target_table.selectionModel().selectedRows()]
            if not selected_rows: 
                QMessageBox.warning(self, "Selection Error", "Please highlight a rule from the right table to remove.")
                return
                
            selected_rows = sorted(list(set(selected_rows)), reverse=True)
            
            for r in selected_rows:
                item_name = self.v_target_table.item(r, 0)
                if not item_name: continue
                meta_key = item_name.data(Qt.ItemDataRole.UserRole)
                target_to_remove = item_name.text()
                
                if meta_key == "animal":
                    if url in self.vision_data.get("assignments", {}):
                        self.vision_data["assignments"][url] =[t for t in self.vision_data["assignments"][url] if t["name"] != target_to_remove]
                elif meta_key == "prompt":
                    if url in self.vision_data.get("stream_overrides", {}): self.vision_data["stream_overrides"][url].pop("custom_prompt", None)
                    self.edit_custom_prompt.clear()
                elif meta_key == "min_frame_single":
                    if url in self.vision_data.get("stream_overrides", {}): self.vision_data["stream_overrides"][url].pop("min_frame_single", None)
                elif meta_key == "min_depth_single":
                    if url in self.vision_data.get("stream_overrides", {}): self.vision_data["stream_overrides"][url].pop("min_depth_single", None)
                elif meta_key == "min_frame_flock":
                    if url in self.vision_data.get("stream_overrides", {}): self.vision_data["stream_overrides"][url].pop("min_frame_flock", None)
                elif meta_key == "min_depth_flock":
                    if url in self.vision_data.get("stream_overrides", {}): self.vision_data["stream_overrides"][url].pop("min_depth_flock", None)
                elif meta_key == "flock":
                    if url in self.vision_data.get("stream_overrides", {}): self.vision_data["stream_overrides"][url].pop("flock_minimum_count", None)
                elif meta_key == "environment_type":
                    if url in self.vision_data.get("stream_overrides", {}): self.vision_data["stream_overrides"][url].pop("environment_type", None)
                elif str(meta_key).startswith("species_tuning:"):
                    sp_name = str(meta_key).split(":", 1)[1]
                    if url in self.vision_data.get("stream_overrides", {}) and "species_rules" in self.vision_data["stream_overrides"][url]:
                        self.vision_data["stream_overrides"][url]["species_rules"].pop(sp_name, None)
                elif str(meta_key).startswith("night_species_tuning:"):
                    sp_name = str(meta_key).split(":", 1)[1]
                    if url in self.vision_data.get("stream_overrides", {}) and "night_species_rules" in self.vision_data["stream_overrides"][url]:
                        self.vision_data["stream_overrides"][url]["night_species_rules"].pop(sp_name, None)
                
            self.refresh_vision_target_table(url)
            self.refresh_vision_stream_visuals()
            self.on_vision_stream_selection_changed()
            self.check_dirty_state()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Vision rule remove failed: {e}")

    def clear_vision_targets(self):
        try:
            selected_items = self.vision_stream_list.selectedItems()
            if not selected_items: return
            
            reply = QMessageBox.question(self, "Clear Rules", f"Clear ALL rules and overrides from {len(selected_items)} stream(s)?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            
            if reply == QMessageBox.StandardButton.Yes:
                for item in selected_items:
                    url = item.data(Qt.ItemDataRole.UserRole)
                    if "assignments" not in self.vision_data: self.vision_data["assignments"] = {}
                    self.vision_data["assignments"][url] =[]
                    
                    if url in self.vision_data.get("stream_overrides", {}):
                        self.vision_data["stream_overrides"][url] = {}
                    
                self.refresh_vision_stream_visuals()
                self.on_vision_stream_selection_changed()
                self.check_dirty_state()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Clear vision targets failed: {e}")

    def check_vision_buttons(self):
        selected = self.vision_stream_list.selectedItems()
        has_single_stream = len(selected) == 1
        has_any_stream = len(selected) > 0
        self.btn_v_add_target.setEnabled(has_any_stream)
        has_targets = self.v_target_table.rowCount() > 0
        self.btn_v_remove_target.setEnabled(has_targets)

    # --- PANE 3: VISION REGISTRY ---
    def reload_vision_registry(self):
        if VISION_TARGETS_FILE.exists():
            try:
                disk_data = json.loads(VISION_TARGETS_FILE.read_text(encoding='utf-8'))
                self.vision_data["registry"] = disk_data.get("registry", {})
                
                for k, v in self.vision_data.get("registry", {}).items():
                    if "show_on_map" not in v: v["show_on_map"] = True
                    
                self.refresh_vision_registry_table()
                self.status_label.setText("Registry refreshed from disk.")
                self.status_label.setStyleSheet("color: #00E676;")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to reload registry: {e}")

    def refresh_vision_registry_table(self):
        self.v_registry_table.setSortingEnabled(False)
        self.v_registry_table.setRowCount(0)
        self.v_registry_table.setRowCount(len(self.vision_data["registry"]))
        
        default_mute = self.vision_data.get("global_defaults", {}).get("default_taxonomy_mute", 60)
        
        for i, (name, data) in enumerate(sorted(self.vision_data["registry"].items())):
            reg_type = data.get("type", "General")
            
            # Identify Tuning
            is_tuned = (
                data.get("behavior", "Alert") != "Alert" or 
                data.get("cooldown_minutes", default_mute) != default_mute or 
                not data.get("show_on_map", True) or 
                data.get("global_prompt", "") or
                data.get("min_frame_single", "Default (Auto)") != "Default (Auto)" or 
                data.get("min_depth_single", "Default (Auto)") != "Default (Auto)"
            )
            
            display_name = f"✎ {name}" if is_tuned else name
            
            item_name = QTableWidgetItem(display_name)
            item_name.setFlags(item_name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            
            # Store data for filtering
            item_name.setData(Qt.ItemDataRole.UserRole, reg_type)
            item_name.setData(Qt.ItemDataRole.UserRole + 1, is_tuned)
            item_name.setData(Qt.ItemDataRole.UserRole + 2, name) # Clean name for search
            
            if reg_type == "Specific":
                font = item_name.font(); font.setBold(True); item_name.setFont(font)
                item_name.setForeground(QBrush(QColor("#00E676")))
            else:
                font = item_name.font(); font.setItalic(True); item_name.setFont(font)
                item_name.setForeground(QBrush(QColor("#888888")))

            item_name.setToolTip(display_name)
            self.v_registry_table.setItem(i, 0, item_name)
            
            syns = ", ".join(data.get("synonyms",[]))
            item_syns = QTableWidgetItem(syns)
            item_syns.setFlags(item_syns.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_syns.setToolTip(syns)
            self.v_registry_table.setItem(i, 1, item_syns)
            
            beh_text = data.get("behavior", "Alert")
            item_beh = QTableWidgetItem(beh_text)
            item_beh.setFlags(item_beh.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if beh_text == "Ignore": item_beh.setForeground(QBrush(QColor("#EF5350")))
            elif beh_text == "Silent Log": item_beh.setForeground(QBrush(QColor("#FFA726")))
            item_beh.setToolTip(beh_text)
            self.v_registry_table.setItem(i, 2, item_beh)
            
            cd_val = str(data.get("cooldown_minutes", default_mute))
            item_cd = NumericTableWidgetItem(cd_val)
            item_cd.setFlags(item_cd.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_cd.setToolTip(cd_val)
            self.v_registry_table.setItem(i, 3, item_cd)
            
            is_pub = data.get("show_on_map", True)
            pub_text = "Yes" if is_pub else "No (Private)"
            item_pub = QTableWidgetItem(pub_text)
            item_pub.setFlags(item_pub.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if not is_pub: item_pub.setForeground(QBrush(QColor("#FFA726")))
            item_pub.setToolTip(pub_text)
            self.v_registry_table.setItem(i, 4, item_pub)
            
        self.v_registry_table.setSortingEnabled(True)
        self.filter_vision_registry()

    def filter_vision_registry(self):
        search_text = self.vision_reg_search_bar.text().lower()
        mode = self.combo_reg_filter.currentText()
        
        for i in range(self.v_registry_table.rowCount()):
            item_name = self.v_registry_table.item(i, 0)
            
            reg_type = item_name.data(Qt.ItemDataRole.UserRole)
            is_tuned = item_name.data(Qt.ItemDataRole.UserRole + 1)
            orig_name = item_name.data(Qt.ItemDataRole.UserRole + 2).lower()
            syns = self.v_registry_table.item(i, 1).text().lower()
            
            match_text = search_text in orig_name or search_text in syns
            match_mode = True
            
            if mode == "Specific Species Only" and reg_type != "Specific":
                match_mode = False
            elif mode == "General Animals Only" and reg_type != "General":
                match_mode = False
            elif mode == "Tuned/Edited Animals Only" and not is_tuned:
                match_mode = False
                
            self.v_registry_table.setRowHidden(i, not (match_text and match_mode))

    def add_vision_registry(self):
        d = EditVisionRegistryDialog("", None, self)
        if d.exec():
            name, data = d.get_data()
            if not name: return
            if name in self.vision_data["registry"]:
                QMessageBox.warning(self, "Exists", "This Canonical Name already exists.")
                return
            self.vision_data["registry"][name] = data
            self.refresh_vision_registry_table()
            self.check_dirty_state()

    def edit_vision_registry(self):
        sel = self.v_registry_table.selectedItems()
        if not sel: return
        
        unique_rows = list(set(item.row() for item in sel))
        
        if len(unique_rows) > 1:
            d = BatchEditVisionRegistryDialog(len(unique_rows), self)
            if d.exec():
                res = d.get_data()
                for r in unique_rows:
                    item_name = self.v_registry_table.item(r, 0)
                    name = item_name.data(Qt.ItemDataRole.UserRole + 2)
                    if res['behavior'] != "Leave Unchanged":
                        self.vision_data["registry"][name]["behavior"] = res['behavior']
                    if res['cooldown_minutes'] != -1:
                        self.vision_data["registry"][name]["cooldown_minutes"] = res['cooldown_minutes']
                    if res['show_on_map'] != "Leave Unchanged":
                        self.vision_data["registry"][name]["show_on_map"] = (res['show_on_map'] == "Public (Show on Map)")
                    
                    if res['min_frame_single'] != "Leave Unchanged":
                        self.vision_data["registry"][name]["min_frame_single"] = res['min_frame_single']
                    if res['min_depth_single'] != "Leave Unchanged":
                        self.vision_data["registry"][name]["min_depth_single"] = res['min_depth_single']
                    if res['min_frame_flock'] != "Leave Unchanged":
                        self.vision_data["registry"][name]["min_frame_flock"] = res['min_frame_flock']
                    if res['min_depth_flock'] != "Leave Unchanged":
                        self.vision_data["registry"][name]["min_depth_flock"] = res['min_depth_flock']
                
                self.refresh_vision_registry_table()
                self.check_dirty_state()
        else:
            item_name = self.v_registry_table.item(unique_rows[0], 0)
            name = item_name.data(Qt.ItemDataRole.UserRole + 2)
            data = self.vision_data["registry"].get(name)
            
            d = EditVisionRegistryDialog(name, data, self)
            if d.exec():
                _, new_data = d.get_data()
                self.vision_data["registry"][name] = new_data
                self.refresh_vision_registry_table()
                self.check_dirty_state()

    def import_db_history(self):
        msg = "🚑 DISASTER RECOVERY TOOL\n\nOnly use this if you lost your configuration file. This will scan your entire database history and re-add EVERY animal ever detected by the AI, including ones you may have intentionally deleted because they were hallucinations.\n\nProceed with Rescue?"
        if QMessageBox.critical(self, "Confirm Recovery", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
            
        try:
            with db_connector.get_db_connection(force_local=True) as con:
                cur = con.cursor()
                cur.execute("SELECT DISTINCT species FROM detections WHERE detection_method IN ('vision', 'multimodal')")
                rows = cur.fetchall()
            
            imported_count = 0
            default_mute = self.vision_data.get("global_defaults", {}).get("default_taxonomy_mute", 60)
            
            for r in rows:
                sp = str(r[0]).strip()
                if sp and sp not in self.vision_data["registry"]:
                    self.vision_data["registry"][sp] = {
                        "type": "General", 
                        "synonyms":[],
                        "behavior": "Alert",
                        "cooldown_minutes": default_mute,
                        "show_on_map": True
                    }
                    imported_count += 1
            
            if imported_count > 0:
                self.refresh_vision_registry_table()
                self.check_dirty_state()
                QMessageBox.information(self, "Recovery Complete", f"Successfully rescued {imported_count} previously discovered animals from the database.\n\nYou can now edit or purge them from the list.")
            else:
                QMessageBox.information(self, "Recovery Complete", "No new unique animals found in the database. Your registry is intact.")
                
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to recover from DB: {e}")

    def purge_vision_registry(self):
        try:
            selected_rows =[idx.row() for idx in self.v_registry_table.selectionModel().selectedRows()]
            if not selected_rows: return
            
            unique_rows = sorted(list(set(selected_rows)), reverse=True)
            names_to_purge =[self.v_registry_table.item(r, 0).data(Qt.ItemDataRole.UserRole + 2) for r in unique_rows]
            
            msg = f"<b>DANGER:</b> You are about to permanently delete <b>{len(names_to_purge)}</b> taxonomies.<br><br>This will:<br>1. Delete all history from the SQLite Database.<br>2. Delete all attached .jpg images from the Vault.<br>3. Remove them from the Registry and all Watchlists.<br><br>Are you sure?"
            if QMessageBox.critical(self, "Confirm Purge", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
                
            deleted_rows = 0
            deleted_imgs = 0
            
            with db_connector.get_db_connection(force_local=True) as con:
                cur = con.cursor()
                for name in names_to_purge:
                    cur.execute("SELECT vision_path FROM detections WHERE species = ?", (name,))
                    paths = [r[0] for r in cur.fetchall() if r[0]]
                    
                    for p in paths:
                        if os.path.exists(p):
                            try: os.remove(p); deleted_imgs += 1
                            except: pass
                    
                    cur.execute("DELETE FROM detections WHERE species = ?", (name,))
                    deleted_rows += cur.rowcount
                con.commit()
                
            for name in names_to_purge:
                if name in self.vision_data["registry"]: del self.vision_data["registry"][name]
                for url in self.vision_data.get("assignments", {}):
                    self.vision_data["assignments"][url] =[t for t in self.vision_data["assignments"][url] if t["name"] != name]
                    
            self.refresh_vision_registry_table()
            self.on_vision_stream_selection_changed()
            self.check_dirty_state()
            QMessageBox.information(self, "Purge Complete", f"Deleted {deleted_rows} DB records and {deleted_imgs} Vault images.")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to purge data: {e}")

    def save_targets(self):
        try:
            # 1. Clean and Save Target Files
            clean_assignments = {k: v for k, v in self.v2_data["assignments"].items() if len(v) > 0}
            self.v2_data["assignments"] = clean_assignments
            TARGETS_FILE.write_text(json.dumps(self.v2_data, indent=2), encoding='utf-8')
            
            clean_v_assignments = {k: v for k, v in self.vision_data["assignments"].items() if len(v) > 0}
            self.vision_data["assignments"] = clean_v_assignments
            
            # Clean overrides
            clean_v_overrides = self._clean_empty_overrides(self.vision_data).get("stream_overrides", {})
            self.vision_data["stream_overrides"] = clean_v_overrides
            
            VISION_TARGETS_FILE.write_text(json.dumps(self.vision_data, indent=2), encoding='utf-8')
            
            # 2. Main Config Preservation & Merge
            if GBR_CONFIG.exists():
                gbr_data = json.loads(GBR_CONFIG.read_text(encoding='utf-8'))
                
                live_vision_cfg = gbr_data.get("vision_ai", {})
                live_keys = live_vision_cfg.get("api_keys",[])
                live_map = {k.get('key'): k for k in live_keys if k.get('key')}
                
                for mk in self.vision_settings.get("api_keys",[]):
                    if mk.get('key') in live_map:
                        mk['usage_count'] = live_map[mk['key']].get('usage_count', mk.get('usage_count', 0))
                        mk['status'] = live_map[mk['key']].get('status', mk.get('status', 'Active'))
                        mk['exhausted_until'] = live_map[mk['key']].get('exhausted_until', mk.get('exhausted_until', 0))

                if "vision_ai" not in gbr_data: gbr_data["vision_ai"] = {}
                gbr_data["vision_ai"]["enabled"] = True
                gbr_data["vision_ai"]["api_key"] = self.vision_settings.get("api_key", "").strip()
                gbr_data["vision_ai"]["api_keys"] = self.vision_settings.get("api_keys",[])
                gbr_data["vision_ai"]["vision_workers"] = self.vision_settings.get("vision_workers", 1)
                
                gbr_data["vision_ai"]["predator_reflex_threshold"] = self.vision_settings.get("predator_reflex_threshold", 85)
                
                # --- SAVE DORMANCY SETTINGS ---
                gbr_data["vision_ai"]["vision_dormancy_threshold_mins"] = self.vision_settings.get("vision_dormancy_threshold_mins", 60)
                gbr_data["vision_ai"]["vision_dormant_interval_mins"] = self.vision_settings.get("vision_dormant_interval_mins", 20)
                
                gbr_data["vision_ai"]["cycle_interval_seconds"] = self.vision_settings.get("cycle_interval_seconds", 60)
                gbr_data["vision_ai"]["use_motion_detector"] = self.vision_settings.get("use_motion_detector", True)
                gbr_data["vision_ai"]["motion_sensitivity_percent"] = self.vision_settings.get("motion_sensitivity_percent", 5.0)
                gbr_data["vision_ai"]["retention_limit"] = self.vision_settings.get("retention_limit", 5)
                gbr_data["vision_ai"]["log_retention_hours"] = self.vision_settings.get("log_retention_hours", 48)
                gbr_data["vision_ai"]["auto_sweep_vault_hours"] = self.vision_settings.get("auto_sweep_vault_hours", 24)
                gbr_data["vision_ai"]["hide_console"] = self.vision_settings.get("hide_console", False)
                
                gbr_data["vision_ai"]["ai_temperature"] = self.vision_settings.get("ai_temperature", 0.15)
                gbr_data["vision_ai"]["speck_size_percent"] = self.vision_settings.get("speck_size_percent", 3)
                gbr_data["vision_ai"]["flock_minimum_count"] = self.vision_settings.get("flock_minimum_count", 4)
                
                gbr_data["vision_ai"]["min_frame_single"] = self.vision_settings.get("min_frame_single", "Large")
                gbr_data["vision_ai"]["min_depth_single"] = self.vision_settings.get("min_depth_single", "Near")
                gbr_data["vision_ai"]["min_frame_flock"] = self.vision_settings.get("min_frame_flock", "Small")
                gbr_data["vision_ai"]["min_depth_flock"] = self.vision_settings.get("min_depth_flock", "Background")
                
                gbr_data["vision_ai"]["vision_resolution"] = self.vision_settings.get("vision_resolution", "720p")
                gbr_data["vision_ai"]["strict_proxy"] = self.vision_settings.get("strict_proxy", False)
                
                # --- NEW TELEGRAM ALERTS PREFS ---
                gbr_data["vision_ai"]["telegram_alerts"] = self.vision_settings.get("telegram_alerts", {
                    "enabled": True,
                    "alert_multimodal": True,
                    "alert_public": True,
                    "alert_filtered": False,
                    "attach_image": True,
                    "include_reasoning": True
                })
                
                gbr_data["vision_ai"]["enabled_streams"] = self.vision_settings.get("enabled_streams",[])
                
                GBR_CONFIG.write_text(json.dumps(gbr_data, indent=2), encoding='utf-8')

            self.status_label.setText("Configuration Saved Successfully (Acoustic & Vision).")
            self.status_label.setStyleSheet("color: #00E676; font-weight: bold;")
            
            # Set fresh snapshot for state hashing
            self.saved_v2_data = copy.deepcopy(self.v2_data)
            self.saved_vision_settings = copy.deepcopy(self.vision_settings)
            self.saved_vision_data = copy.deepcopy(self.vision_data)
            self.check_dirty_state()
            
            # Reset memory contention file watcher baseline
            if VISION_TARGETS_FILE.exists():
                self.last_vision_mtime = VISION_TARGETS_FILE.stat().st_mtime
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save: {e}")

    def refresh_audio_stream_visuals(self):
        self.stream_list.blockSignals(True)
        for i in range(self.stream_list.count()):
            item = self.stream_list.item(i)
            url = item.data(Qt.ItemDataRole.UserRole)
            name = self.streams_map.get(url, {}).get('name', 'Unknown')
            assignments = self.v2_data["assignments"].get(url,[])
            if assignments:
                item.setText(f"[ {len(assignments)} Targets ] {name}")
                item.setForeground(QBrush(QColor("#00E676"))) 
                font = item.font(); font.setBold(True); item.setFont(font)
            else:
                item.setText(name)
                item.setForeground(QBrush(QColor("#FFFFFF")))
                font = item.font(); font.setBold(False); item.setFont(font)
        self.filter_audio_streams(); self.stream_list.blockSignals(False)

    def filter_audio_streams(self):
        search_text = self.search_bar.text().lower()
        show_assigned = self.chk_assigned_only.isChecked()
        for i in range(self.stream_list.count()):
            item = self.stream_list.item(i)
            url = item.data(Qt.ItemDataRole.UserRole)
            is_assigned = url in self.v2_data["assignments"] and len(self.v2_data["assignments"][url]) > 0
            match_text = search_text in item.text().lower()
            match_mode = (not show_assigned) or is_assigned
            item.setHidden(not (match_text and match_mode))

    def on_stream_clicked(self, item):
        pass # Handled by selection changed

    def on_audio_stream_selection_changed(self):
        selected = self.stream_list.selectedItems()
        if not selected:
            self.lbl_selected_stream.setText("No Stream Selected")
            self.target_table.setRowCount(0)
            self.btn_add_target.setEnabled(False)
            self.btn_remove_target.setEnabled(False)
            self.btn_clear_targets.setEnabled(False)
            self.spin_stream_mute.setEnabled(False)
            
            self.btn_suggest_regional.setEnabled(False) 
            self.btn_auto_name.setEnabled(False)        
            return
            
        self.btn_add_target.setEnabled(True)
        self.spin_stream_mute.setEnabled(True)
        
        if len(selected) > 1:
            self.lbl_selected_stream.setText(f"[ {len(selected)} Streams Selected ]")
            self.target_table.setRowCount(0)
            self.btn_remove_target.setEnabled(False)
            self.btn_clear_targets.setEnabled(True)
            self.spin_stream_mute.blockSignals(True)
            self.spin_stream_mute.setValue(-1)
            self.spin_stream_mute.blockSignals(False)
            
            self.btn_suggest_regional.setEnabled(False) 
            self.btn_auto_name.setEnabled(False)        
        else:
            url = selected[0].data(Qt.ItemDataRole.UserRole)
            name = self.streams_map.get(url, {}).get('name', 'Unknown')
            self.lbl_selected_stream.setText(name)
            self.refresh_target_table(url)
            self.btn_remove_target.setEnabled(self.target_table.rowCount() > 0)
            self.btn_clear_targets.setEnabled(self.target_table.rowCount() > 0)
            
            self.btn_suggest_regional.setEnabled(True) 
            self.btn_auto_name.setEnabled(True)        
            
            self.spin_stream_mute.blockSignals(True)
            rule = self.v2_data["stream_rules"].get(url, {})
            cd = rule.get("cooldown_minutes", -1)
            self.spin_stream_mute.setValue(cd)
            self.spin_stream_mute.blockSignals(False)

    def on_stream_mute_changed(self, value):
        selected_items = self.stream_list.selectedItems()
        if not selected_items: return
        
        for item in selected_items:
            url = item.data(Qt.ItemDataRole.UserRole)
            if value == -1: self.v2_data["stream_rules"].pop(url, None)
            else:
                if url not in self.v2_data["stream_rules"]: self.v2_data["stream_rules"][url] = {}
                self.v2_data["stream_rules"][url]["cooldown_minutes"] = value
                
        self.check_dirty_state()

    def check_buttons(self):
        selected = self.stream_list.selectedItems()
        has_single_stream = len(selected) == 1
        has_any_stream = len(selected) > 0
        
        self.btn_add_target.setEnabled(has_any_stream)
        self.btn_suggest_regional.setEnabled(has_single_stream)
        self.btn_auto_name.setEnabled(has_single_stream)
        
        has_targets = self.target_table.rowCount() > 0
        self.btn_remove_target.setEnabled(has_targets)

    def refresh_target_table(self, url):
        self.target_table.setSortingEnabled(False)
        self.target_table.setRowCount(0)
        assignments = self.v2_data["assignments"].get(url,[])
        self.target_table.setRowCount(len(assignments))
        for i, target in enumerate(assignments):
            disp = target['display']; prof = target['profile']
            cd = target.get('cooldown_minutes', -1); snr = target.get('enable_adaptive_snr', None) 
            
            item_disp = QTableWidgetItem(disp)
            item_disp.setFlags(item_disp.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_disp.setToolTip(disp)
            self.target_table.setItem(i, 0, item_disp)
            
            item_prof = QTableWidgetItem(prof)
            item_prof.setFlags(item_prof.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_prof.setForeground(QBrush(QColor("#888888")))
            item_prof.setToolTip(prof)
            self.target_table.setItem(i, 1, item_prof)
            
            spin = QSpinBox(); spin.setRange(-1, 9999); spin.setSpecialValueText("Default"); spin.setValue(cd)
            spin.valueChanged.connect(lambda val, u=url, d=disp: self.update_target_cd(u, d, val))
            self.target_table.setCellWidget(i, 2, spin)
            
            combo = QComboBox(); combo.addItems(["Default", "Disabled", "Enabled"])
            if snr is None: combo.setCurrentIndex(0)
            elif snr is False: combo.setCurrentIndex(1)
            else: combo.setCurrentIndex(2)
            combo.currentIndexChanged.connect(lambda idx, u=url, d=disp: self.update_target_snr(u, d, idx))
            self.target_table.setCellWidget(i, 3, combo)
            
        self.target_table.setSortingEnabled(True)

    def update_target_cd(self, url, display_name, val):
        for t in self.v2_data["assignments"].get(url,[]):
            if t['display'] == display_name:
                if val == -1: t.pop('cooldown_minutes', None)
                else: t['cooldown_minutes'] = val
                self.check_dirty_state()
                break

    def update_target_snr(self, url, display_name, idx):
        for t in self.v2_data["assignments"].get(url,[]):
            if t['display'] == display_name:
                if idx == 0: t.pop('enable_adaptive_snr', None)
                elif idx == 1: t['enable_adaptive_snr'] = False
                elif idx == 2: t['enable_adaptive_snr'] = True
                self.check_dirty_state()
                break

    def add_target(self):
        try:
            selected_items = self.stream_list.selectedItems()
            if not selected_items: return
            
            dialog = AddAnimalDialog(self.master_animals,[], self)
            if dialog.exec():
                selected = dialog.get_selected()
                if selected:
                    for item in selected_items:
                        url = item.data(Qt.ItemDataRole.UserRole)
                        if "assignments" not in self.v2_data: self.v2_data["assignments"] = {}
                        if url not in self.v2_data["assignments"]: self.v2_data["assignments"][url] =[]
                        
                        for new_rule in selected:
                            self.v2_data["assignments"][url] =[t for t in self.v2_data["assignments"][url] if t["display"] != new_rule["display"]]
                            self.v2_data["assignments"][url].append(new_rule)
                            
                    self.refresh_audio_stream_visuals()
                    self.on_audio_stream_selection_changed()
                    self.check_dirty_state()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to add target: {e}")

    def remove_target(self):
        try:
            selected_streams = self.stream_list.selectedItems()
            if len(selected_streams) != 1: 
                QMessageBox.warning(self, "Selection Error", "Please select exactly ONE stream from the list to remove individual targets.")
                return
            
            url = selected_streams[0].data(Qt.ItemDataRole.UserRole)
            
            selected_rows =[idx.row() for idx in self.target_table.selectionModel().selectedRows()]
            
            if not selected_rows: 
                QMessageBox.warning(self, "Selection Error", "Please highlight a target row from the right table to remove.")
                return
                
            selected_rows = sorted(list(set(selected_rows)), reverse=True)
            assignments = self.v2_data.get("assignments", {}).get(url,[])
            
            for r in selected_rows:
                item = self.target_table.item(r, 0)
                if item:
                    display_to_remove = item.text()
                    assignments =[t for t in assignments if t.get('display') != display_to_remove]
                    
            if "assignments" not in self.v2_data:
                self.v2_data["assignments"] = {}
                
            self.v2_data["assignments"][url] = assignments
            
            self.refresh_target_table(url)
            self.refresh_audio_stream_visuals()
            self.on_audio_stream_selection_changed()
            
            self.check_dirty_state()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to remove target: {e}")
        
    def clear_audio_targets(self):
        try:
            selected_items = self.stream_list.selectedItems()
            if not selected_items: return
            
            reply = QMessageBox.question(self, "Clear Targets", f"Clear ALL targets from {len(selected_items)} stream(s)?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            
            if reply == QMessageBox.StandardButton.Yes:
                for item in selected_items:
                    url = item.data(Qt.ItemDataRole.UserRole)
                    if "assignments" not in self.v2_data: self.v2_data["assignments"] = {}
                    self.v2_data["assignments"][url] =[]
                    
                self.refresh_audio_stream_visuals()
                self.on_audio_stream_selection_changed()
                self.check_dirty_state()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to clear targets: {e}")

    def refresh_species_table(self):
        self.species_table.setSortingEnabled(False)
        self.species_table.setRowCount(0); self.species_table.setRowCount(len(self.master_animals))
        for i, (display_name, profile) in enumerate(sorted(self.master_animals.items())):
            item_disp = QTableWidgetItem(display_name)
            item_disp.setFlags(item_disp.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_disp.setToolTip(display_name)
            
            if display_name == profile:
                font = item_disp.font(); font.setBold(True); item_disp.setFont(font); item_disp.setForeground(QBrush(QColor("#BBDEFB")))
            self.species_table.setItem(i, 0, item_disp)
            
            rule = self.v2_data.get("species_rules", {}).get(display_name, {}); cd = rule.get("cooldown_minutes", -1)
            spin_val = NumericTableWidgetItem(str(cd)) 
            spin_val.setData(Qt.ItemDataRole.UserRole, cd) 
            self.species_table.setItem(i, 1, spin_val)
            
            spin_widget = QSpinBox(); spin_widget.setRange(-1, 9999); spin_widget.setSpecialValueText("Default"); spin_widget.setValue(cd)
            spin_widget.valueChanged.connect(lambda val, d=display_name: self.update_global_species_cd(d, val))
            spin_widget.setToolTip(f"Global Mute for {display_name}")
            self.species_table.setCellWidget(i, 1, spin_widget)
            
        self.species_table.setSortingEnabled(True)

    def update_global_species_cd(self, display_name, val):
        if "species_rules" not in self.v2_data: self.v2_data["species_rules"] = {}
        
        if val == -1: self.v2_data["species_rules"].pop(display_name, None)
        else:
            if display_name not in self.v2_data["species_rules"]: self.v2_data["species_rules"][display_name] = {}
            self.v2_data["species_rules"][display_name]["cooldown_minutes"] = val
        self.check_dirty_state()

    def _get_region(self, lat, lon):
        if (7 <= lat <= 25) and (-105 <= lon <= -60): return "central_america"
        if (-55 <= lat <= 15) and (-85 <= lon <= -35): return "south_america"
        if (25 < lat <= 75) and (-170 <= lon <= -50): return "north_america"
        if (-35 <= lat <= 37) and (-20 <= lon <= 55): return "africa"
        if (5 <= lat <= 75) and (45 < lon <= 150): return "asia"
        if (35 <= lat <= 75) and (-15 <= lon <= 45): return "europe"
        if (-50 <= lat <= -10) and (110 <= lon <= 180): return "australia"
        return "unknown"

    def _get_regional_suggestions(self, lat, lon):
        suggestions =[]
        region = self._get_region(lat, lon)
        
        if (lat > 30 and lon < -110) or (lat > 50 and lon > 140): suggestions.append(("SEA_LION", "Steller Sea Lion"))
        elif lat < -20 and -80 < lon < -60: suggestions.append(("SEA_LION", "South American Sea Lion"))
        elif lat < -20 and 10 < lon < 40: suggestions.append(("SEA_LION", "Cape Fur Seal"))
        elif region == "australia": suggestions.append(("SEA_LION", "Australian Fur Seal"))
        
        if region == "central_america": suggestions.extend([("CRICKET", "Rainforest Cicada"), ("FROG", "Tink Frog"), ("MONKEY", "Howler Monkey"), ("TIGER", "Jaguar")])
        elif region == "south_america": suggestions.extend([("CRICKET", "Amazonian Cicada"), ("FROG", "Amazonian Tree Frog"), ("MONKEY", "Howler Monkey"), ("TIGER", "Jaguar")])
        elif region == "north_america":
            suggestions.append(("CRICKET", "Field Cricket")); suggestions.append(("FROG", "Spring Peeper" if lon > -100 else "Pacific Treefrog")); suggestions.extend([("COYOTE", "Coyote"), ("WOLF", "Timber Wolf" if lat > 45 else "Gray Wolf"), ("TIGER", "Mountain Lion")])
        elif region == "africa": suggestions.extend([("CRICKET", "African Cicada"), ("FROG", "Painted Reed Frog"), ("ELEPHANT", "African Elephant"), ("LION", "African Lion"), ("HYENA", "Spotted Hyena"), ("MONKEY", "Vervet Monkey"), ("ZEBRA", "Plains Zebra")])
        elif region == "asia": suggestions.extend([("CRICKET", "Asian Cicada"), ("FROG", "Paddy Frog"), ("ELEPHANT", "Asian Elephant"), ("TIGER", "Bengal Tiger"), ("MONKEY", "Macaque"), ("PANDA", "Giant Panda")])
        elif region == "europe": suggestions.extend([("CRICKET", "Field Cricket"), ("FROG", "Common Frog"), ("WOLF", "Iberian Wolf" if (lon < 5 and lat < 45) else "Eurasian Wolf")])
        elif region == "australia": suggestions.extend([("CRICKET", "Australian Greengrocer Cicada"), ("FROG", "Australian Tree Frog")])
        return suggestions

    def auto_discover_regional_targets(self):
        try:
            selected = self.stream_list.selectedItems()
            if not selected:
                QMessageBox.warning(self, "Selection Required", "Please select a stream from the list first.")
                return
                
            url = selected[0].data(Qt.ItemDataRole.UserRole)
            meta = self.streams_map.get(url)
            if not meta: 
                QMessageBox.warning(self, "Error", "Stream metadata not found.")
                return
                
            lat, lon = meta.get('lat', 0.0), meta.get('lon', 0.0)
            if lat == 0.0 and lon == 0.0: 
                QMessageBox.warning(self, "No Coordinates", "This stream has no coordinates configured. Please set them in the 'Add / Edit Stream' panel.")
                return
                
            suggestions = self._get_regional_suggestions(lat, lon)
            if not suggestions: 
                QMessageBox.information(self, "No Matches", "No prominent regional targets found for these coordinates.")
                return
                
            new_discoveries = False
            added_count = 0
            for base_prof, display_name in suggestions:
                if display_name not in self.master_animals:
                    self.master_animals[display_name] = base_prof
                    new_discoveries = True
                    
            if "assignments" not in self.v2_data: 
                self.v2_data["assignments"] = {}
            if url not in self.v2_data["assignments"]: 
                self.v2_data["assignments"][url] =[]
                
            existing_targets = self.v2_data["assignments"][url]
            existing_displays =[t.get('display') for t in existing_targets]
            
            for base_prof, display_name in suggestions:
                if display_name not in existing_displays: 
                    existing_targets.append({'profile': base_prof, 'display': display_name})
                    added_count += 1
                    
            if added_count > 0:
                QMessageBox.information(self, "Success", f"Added {added_count} regional targets to this stream.")
            else:
                QMessageBox.information(self, "No Matches", "Regional targets for this location are already assigned to this stream.")
                
            if new_discoveries: 
                self.refresh_species_table()
                
            self.refresh_target_table(url)
            self.refresh_audio_stream_visuals()
            self.on_audio_stream_selection_changed()
            self.check_dirty_state()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to suggest targets: {e}")

    def apply_endemic_names(self):
        try:
            selected = self.stream_list.selectedItems()
            if not selected:
                QMessageBox.warning(self, "Selection Required", "Please select a stream from the list first.")
                return
                
            url = selected[0].data(Qt.ItemDataRole.UserRole)
            meta = self.streams_map.get(url)
            if not meta: 
                QMessageBox.warning(self, "Error", "Stream metadata not found.")
                return
                
            if url not in self.v2_data.get("assignments", {}) or not self.v2_data["assignments"][url]:
                QMessageBox.information(self, "No Targets", "This stream has no generic targets assigned yet.\n\nPlease add a generic target (like 'FROG') first, or use 'Suggest Regional Targets'.")
                return
                
            lat, lon = meta.get('lat', 0.0), meta.get('lon', 0.0)
            region = self._get_region(lat, lon)
            
            new_discoveries = False
            renamed_count = 0
            
            for t in self.v2_data["assignments"][url]:
                prof = t.get('profile', '')
                old_display = t.get('display', '')
                
                if prof == "CRICKET" or prof == "CICADA":
                    if region == "central_america": t['display'] = "Rainforest Cicada"
                    elif region == "south_america": t['display'] = "Amazonian Cicada"
                    elif region == "africa": t['display'] = "African Cicada"
                    elif region == "asia": t['display'] = "Asian Cicada"
                    elif region == "australia": t['display'] = "Australian Greengrocer Cicada"
                    else: t['display'] = "Field Cricket"
                elif prof == "FROG":
                    if region == "central_america": t['display'] = "Tink Frog"
                    elif region == "south_america": t['display'] = "Amazonian Tree Frog"
                    elif region == "africa": t['display'] = "Painted Reed Frog"
                    elif region == "asia": t['display'] = "Paddy Frog"
                    elif region == "australia": t['display'] = "Australian Tree Frog"
                    elif region == "north_america": t['display'] = "Spring Peeper" if lon > -100 else "Pacific Treefrog"
                    else: t['display'] = "Common Frog"
                elif prof == "TIGER" or prof == "LION":
                    if region in["central_america", "south_america"]: t['display'] = "Jaguar"
                    elif region == "north_america": t['display'] = "Mountain Lion"
                    elif region == "africa": t['display'] = "African Lion" if prof == "LION" else "Leopard"
                    elif region == "asia": t['display'] = "Bengal Tiger"
                    else: t['display'] = prof.title()
                elif prof == "MONKEY":
                    if region in["central_america", "south_america"]: t['display'] = "Howler Monkey"
                    elif region == "africa": t['display'] = "Vervet Monkey"
                    elif region == "asia": t['display'] = "Macaque"
                    else: t['display'] = "Monkey"
                elif prof == "WOLF":
                    if region == "north_america": t['display'] = "Timber Wolf" if lat > 45 else "Gray Wolf"
                    elif region == "europe": t['display'] = "Iberian Wolf" if (lon < 5 and lat < 45) else "Eurasian Wolf"
                    else: t['display'] = "Wolf"
                elif prof == "ELEPHANT":
                    if region == "africa": t['display'] = "African Elephant"
                    elif region == "asia": t['display'] = "Asian Elephant"
                    else: t['display'] = "Elephant"
                elif prof == "SEA_LION" or prof == "SEAL":
                    if (lat > 30 and lon < -110) or (lat > 50 and lon > 140): t['display'] = "Steller Sea Lion"
                    elif lat < -20 and -80 < lon < -60: t['display'] = "South American Sea Lion"
                    elif lat < -20 and 10 < lon < 40: t['display'] = "Cape Fur Seal"
                    elif region == "australia": t['display'] = "Australian Fur Seal"
                    else: t['display'] = "Seal" if prof == "SEAL" else "Sea Lion"
                else: 
                    t['display'] = prof.title().replace("_", " ")
                    
                if t['display'] != old_display:
                    renamed_count += 1
                    
                if t['display'] not in self.master_animals: 
                    self.master_animals[t['display']] = t.get('profile')
                    new_discoveries = True
                    
            if renamed_count > 0:
                QMessageBox.information(self, "Success", f"Successfully renamed {renamed_count} generic targets to their regional endemic names.")
            else:
                QMessageBox.information(self, "No Changes", "No generic targets found that needed renaming for this location.")
                
            if new_discoveries: 
                self.refresh_species_table()
                
            self.refresh_target_table(url)
            self.refresh_audio_stream_visuals()
            self.on_audio_stream_selection_changed()
            self.check_dirty_state()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to rename targets: {e}")

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(sys.argv)
    window = BioacousticConfigurator()
    window.show()
    sys.exit(app.exec())