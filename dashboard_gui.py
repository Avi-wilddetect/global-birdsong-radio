# FILE: dashboard_gui.py
# VERSION: 4.22 - "The Unshackled Patch"
# RESPONSIBILITY: Real-time Dashboard UI. Heavy SQL logic is outsourced to dashboard_worker.py.
# UPDATED: Added support for the master throttling switch. Mirrors the [⚠️ THROTTLING DISABLED] warning string into the Main Pipeline Status tooltip when active.

import sys
import json
import logging
import re
import math
import html
import time
import os
import subprocess
import unicodedata
import webbrowser
from datetime import datetime
from collections import Counter, defaultdict
from urllib.parse import quote_plus

# --- PyQt6 Imports ---
try:
    from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                                 QLabel, QTableWidget, QTableWidgetItem, QHeaderView,
                                 QGroupBox, QTabWidget, QPushButton, QMessageBox,
                                 QMenu, QLineEdit, QComboBox, QStatusBar)
    from PyQt6.QtCore import QTimer, Qt, pyqtSignal
    from PyQt6.QtGui import QColor, QPalette, QBrush, QAction
except ImportError as e:
    print(f"FATAL: PyQt6 is not installed. Error: {e}")
    sys.exit(1)

# --- IMPORT MODULES ---
from dashboard_utils import (
    NumericTableWidgetItem, StatusTableWidgetItem, DateTimeTableWidgetItem, 
    get_db_connection, init_database, ROOT, DATABASE_FILE,
    CONFIG_FILE, BASELINE_CLIPS_DIR, DASHBOARD_SETTINGS_FILE, DASHBOARD_STATE_FILE,
    REFRESH_INTERVAL_MS
)
from dashboard_dialogs import (
    DetailsDialog, RateAnalysisDialog, HealthSettingsDialog,
    MATPLOTLIB_AVAILABLE
)

# IMPORT THE NEW WORKER
from dashboard_worker import DataFetchWorker

# --- Define Missing Path ---
HYDRA_STATE_FILE = ROOT / "hydra_heat_state.json"

# --- Logging ---
log_file = ROOT / "monitor_debug.txt"
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s -[%(filename)s:%(lineno)d] - %(message)s',
                    handlers=[logging.FileHandler(log_file, mode='a', encoding='utf-8'), logging.StreamHandler(sys.stdout)])


# --- SAFECALL HELPERS FOR LEGACY DATABASES ---
def safe_float_format(val, decimals=4):
    """Safely formats a float, protecting against legacy string DB entries."""
    if val is None or val == "": return "N/A"
    try: return f"{float(val):.{decimals}f}"
    except: return str(val)

def safe_timestamp_format(ts, fmt='%Y-%m-%d %H:%M:%S'):
    """Safely formats a timestamp, protecting against legacy string DB entries."""
    if not ts: return "N/A"
    try: return datetime.fromtimestamp(float(ts)).strftime(fmt)
    except: return str(ts)

# --- CUSTOM DISTANCE SORTING ---
class CustomDistanceDistributionItem(QTableWidgetItem):
    def __init__(self, text, dist_counts):
        super().__init__(text)
        self.dist_order =["Point Blank", "Very Near", "Near", "Mid-ground", "Mid-range", "Background", "Far", "Deep Background", "Very Far", "Horizon"]
        self.sort_key = tuple(dist_counts.get(cat, 0) for cat in self.dist_order)

    def __lt__(self, other):
        return self.sort_key > other.sort_key

def strip_accents(s):
    if not s: return ""
    return ''.join(c for c in unicodedata.normalize('NFD', str(s)) if unicodedata.category(c) != 'Mn')


# --- MAIN CLASS ---
class StatsDashboard(QWidget):
    def __init__(self):
        super().__init__()
        self.url_to_name_map, self.name_to_stream_data_map, self.audited_clips, self.health_data = {}, {}, set(),[]
        self.current_health_timeframe = "Last Full Cycle"
        self.current_target_filter_mode = "View: All Valid Targets"
        self.intermittent_threshold = 2
        self.unresponsive_threshold = 5
        self.cycle_aggregation_mode = 0
        self.details_dialog_open = False
        self.highlighted_clip_id = None
        self.last_rate_period_index = 3
        self.last_rate_per_index = 0
        self.last_rate_profile_checked = False
        
        self.active_streams =[]
        self.worker = None
        
        init_database()
        self.init_ui()
        self.load_settings()
        self.load_state()
        
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.start_refresh)
        self.refresh_timer.start(REFRESH_INTERVAL_MS)
        self.start_refresh()
        
    def init_ui(self):
        self.setWindowTitle("BirdNET Monitor - Real-Time Dashboard & Tools")
        self.setGeometry(200, 200, 1400, 800) 
        main_layout = QVBoxLayout()
        self.tabs = QTabWidget()
        dashboard_widget = QWidget(); dashboard_layout = QVBoxLayout(dashboard_widget)
        
        stats_group = QGroupBox("Overall Summary"); stats_layout = QHBoxLayout()
        self.total_detections_label = QLabel("Total Detections:\n--")
        self.total_alerts_label = QLabel("Total Alerts Sent:\n--")
        self.unique_species_label = QLabel("Unique Species:\n--")
        self.recently_active_channels_label = QLabel("Recently Active (24h):\n--")
        self.enabled_channels_label = QLabel("Enabled Channels:\n--")
        self.total_channels_label = QLabel("Total Channels Configured:\n--")
        
        self.system_pulse_label = QLabel("System Pulse (30m):\nChecking...")
        
        labels =[
            self.total_detections_label, self.total_alerts_label, 
            self.unique_species_label, self.recently_active_channels_label, 
            self.enabled_channels_label, self.total_channels_label
        ]
        
        for label in labels:
            label.setStyleSheet("font-size: 14px; text-align: center; font-weight: bold;")
            stats_layout.addWidget(label)
            
        stats_layout.addSpacing(50) 
        self.system_pulse_label.setStyleSheet("font-size: 14px; text-align: center; font-weight: bold;")
        stats_layout.addWidget(self.system_pulse_label)
            
        stats_group.setLayout(stats_layout)
        
        dashboard_search_layout = QHBoxLayout()
        
        self.master_filter_combo = QComboBox()
        self.master_filter_combo.addItems([
            "View: All Valid Targets", 
            "View: Birds Only", 
            "View: Bioacoustics Only (DSP)", 
            "View: Visual AI Only (Gemini)", 
            "View: Multimodal Only (Audio + Vision)"
        ])
        self.master_filter_combo.setStyleSheet("font-weight: bold; background-color: #007acc; color: white;")
        self.master_filter_combo.currentIndexChanged.connect(self.start_refresh)
        dashboard_search_layout.addWidget(self.master_filter_combo)
        
        dashboard_search_layout.addSpacing(15)
        dashboard_search_layout.addWidget(QLabel("<b>Universal Dashboard Search:</b>"))
        self.dashboard_search_box = QLineEdit()
        self.dashboard_search_box.setPlaceholderText("Filter species and channels below (Accent-insensitive)...")
        self.dashboard_search_box.textChanged.connect(self.filter_dashboard_tables)
        dashboard_search_layout.addWidget(self.dashboard_search_box)
        
        tables_layout = QHBoxLayout()
        
        species_group = QGroupBox("Most Frequently Detected Species (Hover for Locations, Right-click to Verify)")
        species_layout = QVBoxLayout()
        self.species_table = QTableWidget()
        self.species_table.setColumnCount(2)
        self.species_table.setHorizontalHeaderLabels(["Species", "Count"])
        self.species_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.species_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.species_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.species_table.customContextMenuRequested.connect(self.species_table_context_menu)
        self.species_table.setSortingEnabled(True)
        species_layout.addWidget(self.species_table)
        species_group.setLayout(species_layout)
        
        channels_group = QGroupBox("Most Active Channels (Hover for Info, Right-click for Actions)")
        channels_layout = QVBoxLayout()
        self.channels_table = QTableWidget()
        self.channels_table.setColumnCount(4) 
        self.channels_table.setHorizontalHeaderLabels(["Channel", "Count", "Created", "Distance Distribution"])
        self.channels_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.channels_table.setColumnWidth(1, 60)
        self.channels_table.setColumnWidth(2, 95)
        for i in range(1, 4): self.channels_table.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        self.channels_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.channels_table.customContextMenuRequested.connect(self.generic_table_context_menu)
        self.channels_table.setSortingEnabled(True)
        channels_layout.addWidget(self.channels_table)
        channels_group.setLayout(channels_layout)
        
        tables_layout.addWidget(species_group, 3)
        tables_layout.addWidget(channels_group, 7)
        
        self.health_group = QGroupBox("Stream Health Analysis")
        health_layout = QVBoxLayout()
        
        health_controls_layout = QHBoxLayout()
        health_controls_layout.addWidget(QLabel("<b>Health View:</b>"))
        self.health_timeframe_selector = QComboBox()
        self.health_timeframe_selector.addItems([
            "Last Full Cycle", "Last 3 Full Cycles", "Last 30 Minutes", "Last 1 Hour",
            "Last 3 Hours", "Last 6 Hours", "Last 12 Hours", "Last 24 Hours", 
            "Last 7 Days", "Last 30 Days", "All-Time"
        ])
        
        self.health_timeframe_selector.currentIndexChanged.connect(self.start_refresh)
        health_controls_layout.addWidget(self.health_timeframe_selector)
        
        health_controls_layout.addSpacing(20)
        health_controls_layout.addWidget(QLabel("<b>Search:</b>"))
        self.health_search_box = QLineEdit()
        self.health_search_box.setPlaceholderText("Filter by Channel, Status...")
        self.health_search_box.textChanged.connect(self.filter_health_table)
        health_controls_layout.addWidget(self.health_search_box)
        
        health_controls_layout.addSpacing(15)
        self.global_health_label = QLabel("System Health: Init...")
        self.global_health_label.setStyleSheet("padding: 4px 10px; border-radius: 4px; font-weight: bold; background-color: #e0e0e0; color: #333;")
        health_controls_layout.addWidget(self.global_health_label)
        
        health_controls_layout.addStretch()
        
        self.rate_button = QPushButton("Detection & Alert Rate...")
        if MATPLOTLIB_AVAILABLE: self.rate_button.clicked.connect(self.open_rate_dialog)
        else: self.rate_button.setEnabled(False); self.rate_button.setToolTip("Install 'matplotlib' to enable.")
        health_controls_layout.addWidget(self.rate_button)
        
        self.thresholds_button = QPushButton("Live Progress & View...")
        self.thresholds_button.clicked.connect(self.open_thresholds_dialog)
        health_controls_layout.addWidget(self.thresholds_button)
        
        self.clear_health_button = QPushButton("Clear Health History")
        self.clear_health_button.clicked.connect(self.clear_stream_health_history)
        health_controls_layout.addWidget(self.clear_health_button)
        health_layout.addLayout(health_controls_layout)
        
        self.health_table = QTableWidget()
        self.health_table.setColumnCount(8)
        self.health_table.setHorizontalHeaderLabels(["Channel", "Status", "Detections (Cycle)", "Lifetime Detections", "Cycles", "Last Alarmed", "Last Detected", "Connectivity Audit"])
        
        self.health_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.health_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.health_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.health_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.health_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive) 
        self.health_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        self.health_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Interactive)
        self.health_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.Interactive)
        
        self.health_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.health_table.customContextMenuRequested.connect(self.generic_table_context_menu)
        self.health_table.setSortingEnabled(True)
        health_layout.addWidget(self.health_table)
        self.health_group.setLayout(health_layout)
        
        dashboard_layout.addWidget(stats_group)
        dashboard_layout.addLayout(dashboard_search_layout)
        dashboard_layout.addLayout(tables_layout)
        dashboard_layout.addWidget(self.health_group)
        
        calibration_widget = QWidget()
        calibration_layout = QVBoxLayout(calibration_widget)
        self.cal_intro_label = QLabel("This table shows every learned 'Baseline Max SNR' value. Resetting allows the system to re-learn audio levels.")
        self.cal_intro_label.setWordWrap(True)
        
        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("<b>Universal Search:</b>"))
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Start typing to filter by Stream or Species...")
        self.search_box.textChanged.connect(self.filter_profiles_table)
        search_layout.addWidget(self.search_box)
        
        self.profiles_table = QTableWidget()
        self.profiles_table.setStyleSheet("QTableWidget::item:selected { border: 2px dashed #0078d7; }")
        self.profiles_table.setColumnCount(7)
        self.profiles_table.setHorizontalHeaderLabels(["Stream Name", "Species Name", "Baseline Max SNR (dB)", "Avg. Noise Floor (dBFS)", "Sample Count", "Baseline Set Date", "Audit"])
        self.profiles_table.setSortingEnabled(True)
        self.profiles_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.profiles_table.customContextMenuRequested.connect(self.generic_table_context_menu)
        self.profiles_table.cellClicked.connect(self.on_profiles_table_clicked)
        for i in range(self.profiles_table.columnCount()): self.profiles_table.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        self.profiles_table.horizontalHeader().setStretchLastSection(False)
        
        button_layout = QHBoxLayout()
        self.reset_all_button = QPushButton("⚠️ Reset ALL Profiles")
        self.reset_all_button.setStyleSheet("background-color: #d32f2f; color: white; font-weight: bold;")
        self.reset_all_button.clicked.connect(self.reset_all_profiles)
        self.reset_selected_button = QPushButton("Reset Selected Profile(s)")
        self.reset_selected_button.clicked.connect(self.reset_selected_profiles)

        button_layout.addWidget(self.reset_all_button)
        button_layout.addStretch()
        button_layout.addWidget(self.reset_selected_button)
        
        calibration_layout.addWidget(self.cal_intro_label)
        calibration_layout.addLayout(search_layout)
        calibration_layout.addWidget(self.profiles_table)
        calibration_layout.addLayout(button_layout)
        
        system_log_widget = QWidget()
        system_log_layout = QVBoxLayout(system_log_widget)
        log_controls_layout = QHBoxLayout()
        log_controls_layout.addWidget(QLabel("<b>Filter by Type:</b>"))
        self.log_type_filter = QComboBox()
        log_controls_layout.addWidget(self.log_type_filter)
        log_controls_layout.addSpacing(20)
        log_controls_layout.addWidget(QLabel("<b>Search Log:</b>"))
        self.log_search_box = QLineEdit()
        self.log_search_box.textChanged.connect(self.update_system_log_tab)
        self.log_type_filter.currentIndexChanged.connect(self.update_system_log_tab)
        log_controls_layout.addWidget(self.log_search_box)
        log_controls_layout.addStretch()
        
        self.clear_log_button = QPushButton("Clear System Log")
        self.clear_log_button.clicked.connect(self.clear_system_log)
        log_controls_layout.addWidget(self.clear_log_button)
        
        self.system_log_table = QTableWidget()
        self.system_log_table.setColumnCount(4)
        self.system_log_table.setHorizontalHeaderLabels(["Timestamp", "Event Type", "Status", "Message"])
        self.system_log_table.setSortingEnabled(True)
        for i in range(self.system_log_table.columnCount()): self.system_log_table.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        system_log_layout.addLayout(log_controls_layout)
        system_log_layout.addWidget(self.system_log_table)
        
        self.tabs.addTab(dashboard_widget, "Dashboard")
        self.tabs.addTab(calibration_widget, "Calibration Manager")
        self.tabs.addTab(system_log_widget, "System Log")
        
        self.statusBar = QStatusBar()
        main_layout.addWidget(self.tabs)
        main_layout.addWidget(self.statusBar)
        self.setLayout(main_layout)

    def _calculate_estimated_cycle_time(self):
        try:
            if not CONFIG_FILE.exists(): return 1800
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return int(cfg.get("interval_seconds", 300))
        except Exception as e: return 1800

    def start_refresh(self):
        if self.details_dialog_open: return
        
        self.current_target_filter_mode = self.master_filter_combo.currentText()
        
        try:
            config_data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            self.intermittent_threshold = config_data.get('intermittent_threshold', 2)
            self.unresponsive_threshold = config_data.get('unresponsive_threshold', 5)
            
            estimated_cycle_sec = int(config_data.get("interval_seconds", 300))
            self.health_timeframe_selector.setItemText(0, f"Last Full Cycle (Est. {math.ceil(estimated_cycle_sec / 60)} min)")
            self.health_timeframe_selector.setItemText(1, f"Last 3 Full Cycles (Est. {math.ceil((estimated_cycle_sec * 3) / 60)} min)")
            
            all_configured_streams = config_data.get('streams',[])
            self.active_streams =[s for s in all_configured_streams if s.get('enabled', True)]
            
            self.url_to_name_map = {s['page_url']: s['name'] for s in all_configured_streams}
            self.name_to_stream_data_map = {s['name']: s for s in all_configured_streams}
            
            self.enabled_channels_label.setText(f"Enabled Channels:\n{len(self.active_streams)}")
            self.total_channels_label.setText(f"Total Channels Configured:\n{len(all_configured_streams)}")
            
        except Exception as e:
            logging.error(f"Config Load Error: {e}")
            return

        if self.worker is not None and self.worker.isRunning():
             return 
             
        self.worker = DataFetchWorker(self.cycle_aggregation_mode, self.health_timeframe_selector.currentText(), self.url_to_name_map, self.current_target_filter_mode)
        self.worker.data_ready.connect(self.update_ui_from_payload)
        self.worker.start()

    def update_ui_from_payload(self, p):
        if 'error' in p:
            self.global_health_label.setText(f"System Health: DB ERROR - {p['error']}")
            self.global_health_label.setStyleSheet("padding: 4px 10px; border-radius: 4px; font-weight: bold; background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb;")
            self.system_pulse_label.setText("Pulse:\nError")
            return

        def format_time_ago(ts, now_time):
            if not ts: return "Never"
            try:
                diff = now_time - float(ts)
                if diff < 60: return f"{int(diff)}s"
                if diff < 3600: return f"{int(diff/60)}m"
                if diff < 86400: return f"{int(diff/3600)}h"
                return f"{int(diff/86400)}d"
            except:
                return "Unknown"

        try:
            species_scroll = self.species_table.verticalScrollBar().value()
            channels_scroll = self.channels_table.verticalScrollBar().value()
            health_scroll = self.health_table.verticalScrollBar().value()
            profiles_scroll = self.profiles_table.verticalScrollBar().value()
            
            for table in[self.species_table, self.channels_table, self.health_table, self.profiles_table]:
                table.setSortingEnabled(False)
                
            self.refresh_system_log_tab(p.get("system_log_events", []))
            
            self.total_detections_label.setText(f"Total Detections:\n{p['total_detections']}")
            self.total_alerts_label.setText(f"Total Alerts Sent:\n{p['total_alerts']}")
            self.unique_species_label.setText(f"Unique Species:\n{p['unique_species']}")
            self.recently_active_channels_label.setText(f"Recently Active (24h):\n{p['active_24h']}")
            
            if p['alerts_30m'] > 0:
                seconds_per_alert = int(1800 / p['alerts_30m'])
                pulse_text = f"Pulse (30m):\n1 Alert / {seconds_per_alert}s"
                if seconds_per_alert < 60: self.system_pulse_label.setStyleSheet("font-size: 14px; text-align: center; font-weight: bold; color: green;")
                elif seconds_per_alert > 300: self.system_pulse_label.setStyleSheet("font-size: 14px; text-align: center; font-weight: bold; color: orange;")
                else: self.system_pulse_label.setStyleSheet("font-size: 14px; text-align: center; font-weight: bold; color: white;")
            else:
                pulse_text = "Pulse (30m):\nNo Alerts"
                self.system_pulse_label.setStyleSheet("font-size: 14px; text-align: center; font-weight: bold; color: gray;")
            self.system_pulse_label.setText(pulse_text)
            
            species_channel_breakdown = defaultdict(Counter)
            channel_species_breakdown = defaultdict(Counter)
            for sp, url, cnt in p['breakdown_rows']:
                c_name = self.url_to_name_map.get(url, url)
                species_channel_breakdown[sp][c_name] += cnt
                channel_species_breakdown[c_name][sp] += cnt
                
            channel_dist_data = defaultdict(lambda: {'dist_counts': Counter(), 'dist_species': defaultdict(Counter)})
            for url, dist, cnt in p['dist_rows']:
                c_name = self.url_to_name_map.get(url, url)
                channel_dist_data[c_name]['dist_counts'][dist] += cnt
                
            for url, dist, sp, cnt in p['dist_spec_rows']:
                c_name = self.url_to_name_map.get(url, url)
                channel_dist_data[c_name]['dist_species'][dist][sp] += cnt
            
            recent_25_map = p.get('recent_25_map', defaultdict(list))
            
            self.populate_sortable_species_table(p['species_counts'], species_channel_breakdown)
            
            lifetime_channel_counts = {row[0]: row[1] for row in p['channel_counts_raw']}
            sorted_channels = sorted(lifetime_channel_counts.items(), key=lambda x: x[1], reverse=True)
            named_channel_counts =[]
            for url, cnt in sorted_channels:
                name = self.url_to_name_map.get(url, url)
                named_channel_counts.append((name, cnt))
            self.populate_sortable_channels_table(named_channel_counts, channel_species_breakdown, channel_dist_data, recent_25_map)
            
            self.health_data =[]
            now = time.time()
            
            status_order = { "Good": 1, "Medium": 2, "Quiet": 3, "Recovered from Loop": 4, "Visually Active (Mic Muted)": 5, "No Audio Detected": 6, "Intermittent": 7, "Looping (Under Observation)": 8, "Persistent Loop (Quarantined)": 9, "Unresponsive": 10, "Suspended / Offline": 11, "Fatal Error": 12 }
            status_details = { "Good": ("green", "High-performing stream."), "Medium": ("#CCCC00", "Stream is working, few detections."), "Quiet": ("#87CEEB", "Online, no recent detections."), "Recovered from Loop": ("#20B2AA", "Was a loop, now providing new data."), "Visually Active (Mic Muted)": ("#00897B", "Audio is dead, but AI detects visual life."), "No Audio Detected": ("gray", "Connects, but is digitally silent."), "Intermittent": ("orange", "Has recent connection issue(s)."), "Looping (Under Observation)": ("#FF6347", "In observation period for a detected loop."), "Persistent Loop (Quarantined)": ("purple", "Confirmed persistent loop; checks are infrequent."), "Unresponsive": ("red", "Repeatedly failing to connect."), "Suspended / Offline": ("#AB47BC", "Stream is temporarily offline or private."), "Fatal Error": ("#B71C1C", "Dead Stream (404/Removed)") }

            for stream in self.active_streams:
                url, name = stream['page_url'], stream['name']
                lifetime_detections = lifetime_channel_counts.get(url, 0)
                detections_in_view = p['detections_in_timeframe_map'].get(url, 0)
                failures = p['recent_failures_count'].get(url, 0)
                status = ""
                
                a_cycles = p.get('audio_cycles_map', {}).get(url, 0)
                v_cycles = p.get('vision_cycles_map', {}).get(url, 0)
                
                if "Visual" in self.current_target_filter_mode:
                    cycles_val = v_cycles
                elif "Multimodal" in self.current_target_filter_mode:
                    cycles_val = min(a_cycles, v_cycles)
                elif "All Valid Targets" in self.current_target_filter_mode:
                    cycles_val = max(a_cycles, v_cycles)
                else:
                    cycles_val = a_cycles
                
                last_a_ts = p.get('last_audio_verify_map', {}).get(url, 0)
                last_v_ts = p.get('last_vision_verify_map', {}).get(url, 0)
                
                a_str_full = f"Audio Verified ({format_time_ago(last_a_ts, now)} ago)" if last_a_ts else "Audio: Unverified"
                v_str_full = f"Vision Verified ({format_time_ago(last_v_ts, now)} ago)" if last_v_ts else "Vision: Unverified"
                rec_summary_full = f"{a_str_full}  |  {v_str_full}"
                
                a_cond = f"A: {format_time_ago(last_a_ts, now)}" if last_a_ts else "A: --"
                v_cond = f"V: {format_time_ago(last_v_ts, now)}" if last_v_ts else "V: --"
                rec_summary_condensed = f"{a_cond} | {v_cond}"
                
                try: audit_sort_val = float(min(last_a_ts or 0, last_v_ts or 0))
                except: audit_sort_val = 0.0
                
                engine_status = p['queue_status_map'].get(url, "")
                
                noise_val = p['stream_noise_profiles_map'].get(url)
                try: is_silent = float(noise_val) == float('-inf')
                except: is_silent = False
                
                if engine_status in['FATAL', 'TERMINAL']: status = "Fatal Error"
                elif engine_status == 'SUSPENDED': status = "Suspended / Offline"
                elif engine_status == 'UNRESPONSIVE': status = "Unresponsive"
                elif engine_status == 'INTERMITTENT': status = "Intermittent"
                elif engine_status == 'SILENT_VISUAL': status = "Visually Active (Mic Muted)"
                elif engine_status == 'SILENT' or is_silent: status = "No Audio Detected"
                elif engine_status == 'LOOP': status = "Persistent Loop (Quarantined)"
                else:
                    if failures >= self.unresponsive_threshold: status = "Unresponsive"
                    elif failures >= self.intermittent_threshold: status = "Intermittent"
                    else:
                        has_recent = detections_in_view > 0 or p['view_cutoff'] == 0
                        if has_recent: status = "Good" if lifetime_detections > 5 else "Medium"
                        else: status = "Quiet"
                
                color_str, _ = status_details.get(status, ("black", "Unknown.")); color = QColor(color_str)
                self.health_data.append({
                    "name": name, "status": status, "detections_in_view": detections_in_view,
                    "lifetime_count": lifetime_detections, "cycles": cycles_val, 
                    "rec_condensed": rec_summary_condensed, "rec_full": rec_summary_full, "audit_sort_val": audit_sort_val,
                    "color": color, 
                    "last_alarmed": p['last_alarmed_map'].get(url), "last_detected": p['last_detected_map'].get(url)
                })

            self.health_table.setRowCount(len(self.health_data))
            legend_parts =["<b><u>Status Legend (Sort Order):</u></b>"]
            for status, order_num in sorted(status_order.items(), key=lambda item: item[1]):
                color, desc = status_details[status]; legend_parts.append(f"<font color='{color}'>■</font> <b>({order_num}) {status}:</b> {desc}")
            status_legend_html = "<br>".join(legend_parts)
            
            cycle_tt = "100% Reliably Verified Cycles.<br>The engine successfully connected to the stream, captured the media, and completed the analysis (even if no animal was detected)."
            
            for i, data in enumerate(self.health_data):
                try:
                    if self.health_table.item(i, 0): self.health_table.item(i, 0).setText(data['name'])
                    else: self.health_table.setItem(i, 0, QTableWidgetItem(data['name']))
                    self.add_tooltip_from_breakdown(self.health_table.item(i, 0), channel_species_breakdown.get(data['name']), "Species Detected:", recent_25_map.get(data['name']), data['name'])

                    if not self.health_table.item(i, 1): self.health_table.setItem(i, 1, StatusTableWidgetItem(data['status']))
                    status_item = self.health_table.item(i, 1)
                    status_item.setText(data['status']); status_item.setData(Qt.ItemDataRole.UserRole, status_order.get(data['status'], 99))
                    status_item.setBackground(data['color'])
                    status_item.setToolTip(status_legend_html)
                    
                    if data['status'] in["Persistent Loop (Quarantined)", "Good", "Unresponsive", "No Audio Detected", "Looping (Under Observation)", "Recovered from Loop", "Fatal Error", "Visually Active (Mic Muted)", "Suspended / Offline"]: 
                        status_item.setForeground(QColor('white'))
                    else: 
                        status_item.setForeground(QColor('black'))

                    if not self.health_table.item(i, 2): self.health_table.setItem(i, 2, NumericTableWidgetItem(str(data['detections_in_view'])))
                    else: self.health_table.item(i, 2).setText(str(data['detections_in_view']))

                    if not self.health_table.item(i, 3): self.health_table.setItem(i, 3, NumericTableWidgetItem(str(data['lifetime_count'])))
                    else: self.health_table.item(i, 3).setText(str(data['lifetime_count']))

                    if not self.health_table.item(i, 4): self.health_table.setItem(i, 4, NumericTableWidgetItem(str(data['cycles'])))
                    else: self.health_table.item(i, 4).setText(str(data['cycles']))
                    self.health_table.item(i, 4).setToolTip(cycle_tt)

                    if not self.health_table.item(i, 5): self.health_table.setItem(i, 5, DateTimeTableWidgetItem("N/A"))
                    item = self.health_table.item(i, 5)
                    if data['last_alarmed']: 
                        sp, ts = data['last_alarmed']
                        item.setText(safe_timestamp_format(ts, '%Y-%m-%d %H:%M'))
                        try: item.setData(Qt.ItemDataRole.UserRole, float(ts))
                        except: item.setData(Qt.ItemDataRole.UserRole, 0)
                        item.setToolTip(f"<b>Last Alarmed Species:</b><br>{sp}")
                    else: item.setText("N/A"); item.setData(Qt.ItemDataRole.UserRole, 0); item.setToolTip("")

                    if not self.health_table.item(i, 6): self.health_table.setItem(i, 6, DateTimeTableWidgetItem("N/A"))
                    item = self.health_table.item(i, 6)
                    if data['last_detected']: 
                        sp, ts = data['last_detected']
                        item.setText(safe_timestamp_format(ts, '%Y-%m-%d %H:%M'))
                        try: item.setData(Qt.ItemDataRole.UserRole, float(ts))
                        except: item.setData(Qt.ItemDataRole.UserRole, 0)
                        item.setToolTip(f"<b>Last Detected Species:</b><br>{sp}")
                    else: item.setText("N/A"); item.setData(Qt.ItemDataRole.UserRole, 0); item.setToolTip("")

                    if not self.health_table.item(i, 7): self.health_table.setItem(i, 7, DateTimeTableWidgetItem(""))
                    sort_item = self.health_table.item(i, 7)
                    sort_item.setData(Qt.ItemDataRole.UserRole, data['audit_sort_val'])
                    
                    existing_container = self.health_table.cellWidget(i, 7)
                    if existing_container and isinstance(existing_container, QWidget):
                        btn = existing_container.findChild(QPushButton)
                        if btn:
                            btn.setText(data['rec_condensed'])
                            btn.setToolTip(data['rec_full'])
                    else:
                        button = QPushButton(data['rec_condensed'])
                        button.setToolTip(data['rec_full'])
                        button.setFixedWidth(130)
                        button.setCursor(Qt.CursorShape.PointingHandCursor)
                        button.clicked.connect(lambda chk, b=button: self.handle_interactive_cell_click(b))
                        
                        container = QWidget()
                        lay = QHBoxLayout(container)
                        lay.setContentsMargins(0, 2, 0, 2)
                        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
                        lay.addWidget(button)
                        
                        self.health_table.setCellWidget(i, 7, container)
                except Exception as row_e:
                    logging.error(f"Error populating health table row {i}: {row_e}")
            
            stream_max_snrs = defaultdict(lambda: -999)
            for row in p['all_profiles']:
                if row[2] is not None:
                    try: stream_max_snrs[row[0]] = max(float(row[2]), stream_max_snrs.get(row[0], -999.0))
                    except: pass
            
            total_p = p.get('total_profiles_count', len(p['all_profiles']))
            shown_p = len(p['all_profiles'])
            if total_p > shown_p:
                self.cal_intro_label.setText(f"Showing {shown_p} most recent of {total_p} total calibration records. Use the search box to find specific streams or species.")
            else:
                self.cal_intro_label.setText("This table shows every learned 'Baseline Max SNR' value. Resetting allows the system to re-learn audio levels.")
            self.profiles_table.setRowCount(shown_p)
            
            for i, (url, species, max_snr, count, ts, det_id, avg_noise) in enumerate(p['all_profiles']):
                try:
                    stream_name = self.url_to_name_map.get(url, url)
                    
                    try: avg_n_float = float(avg_noise) if avg_noise is not None else None
                    except: avg_n_float = None
                    
                    display_noise = "Silent" if avg_n_float == float('-inf') else safe_float_format(avg_noise, 4)
                    display_snr = safe_float_format(max_snr, 4)
                    display_ts = safe_timestamp_format(ts)
                    
                    row_items =[
                        QTableWidgetItem(stream_name), 
                        QTableWidgetItem(species), 
                        NumericTableWidgetItem(display_snr), 
                        NumericTableWidgetItem(display_noise), 
                        NumericTableWidgetItem(str(count)), 
                        QTableWidgetItem(display_ts)
                    ]
                    
                    self.add_tooltip_from_breakdown(row_items[0], channel_species_breakdown.get(stream_name), "Species Detected on this Stream:", recent_25_map.get(stream_name), stream_name)
                    self.add_tooltip_from_breakdown(row_items[1], species_channel_breakdown.get(species), "Detected on these Streams:")
                    
                    is_highlighted = self.highlighted_clip_id == (stream_name, species)
                    
                    try: is_anchor_row = (max_snr is not None and float(max_snr) == stream_max_snrs.get(url))
                    except: is_anchor_row = False
                    
                    is_audited = str(det_id) in self.audited_clips
                    
                    for col_idx, new_item in enumerate(row_items):
                        if is_highlighted: 
                            new_item.setBackground(QBrush(QColor("#0078d7")))
                        else:
                            if is_anchor_row: new_item.setBackground(QColor("#FFF3CD")); new_item.setForeground(QColor("black"))
                            if is_audited: new_item.setBackground(QColor("#E6F2FF")); new_item.setForeground(QColor("black"))
                        
                        existing = self.profiles_table.item(i, col_idx)
                        if existing:
                            existing.setText(new_item.text())
                            existing.setBackground(new_item.background())
                            existing.setForeground(new_item.foreground())
                            existing.setToolTip(new_item.toolTip())
                        else:
                            self.profiles_table.setItem(i, col_idx, new_item)
                    
                    if det_id:
                        existing_btn = self.profiles_table.cellWidget(i, 6)
                        if not isinstance(existing_btn, QPushButton):
                            listen_button = QPushButton("Listen")
                            listen_button.clicked.connect(lambda chk, d=det_id, anchor=is_anchor_row, button=listen_button: self.play_clip(d, anchor, button))
                            self.profiles_table.setCellWidget(i, 6, listen_button)
                    else:
                        self.profiles_table.removeCellWidget(i, 6)
                except Exception as row_e:
                    logging.error(f"Error populating profiles table row {i}: {row_e}")

            # Retrieve global network telemetry for Main Pipeline Status
            throttle_enabled = True
            try:
                if CONFIG_FILE.exists():
                    cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                    throttle_enabled = cfg.get("hydra_pid_settings", {}).get("throttling_enabled", True)
            except: pass

            g_ind = "<span style='color: #888;'>Waiting for telemetry...</span>"
            if not throttle_enabled:
                g_ind = "<span style='color: #FF3D00; font-weight: bold;'>[⚠️ THROTTLING DISABLED]</span>"
            elif HYDRA_STATE_FILE.exists():
                try:
                    heat_state = json.loads(HYDRA_STATE_FILE.read_text(encoding='utf-8'))
                    g_data = heat_state.get("GLOBAL")
                    if g_data:
                        global_heat = g_data['heat']
                        g_arrow = g_data['arrow']
                        
                        if "🚨" in g_arrow:
                            g_ind = f"<span style='color: #FF1744; font-weight: bold;'>[{g_arrow}]</span>"
                        elif global_heat >= 1.0:
                            g_ind = "<span style='color: #FF3D00; font-weight: bold;'>[🛑 GLOBAL SPEED BLOCKED (100% Heat)]</span>"
                        elif global_heat >= 0.95:
                            g_ind = f"<span style='color: #FF9800; font-weight: bold;'>[🟠 GLOBAL THROTTLED MAX] ({global_heat*100:.1f}%)</span>"
                        elif "🔺" in g_arrow:
                            g_ind = f"<span style='color: #FF5252; font-weight: bold;'>[🔺 Heating Up] ({global_heat*100:.1f}%)</span>"
                        elif "🔽" in g_arrow:
                            g_ind = f"<span style='color: #448AFF; font-weight: bold;'>[🔽 Cooling Down] ({global_heat*100:.1f}%)</span>"
                        else:
                            g_ind = f"<span style='color: #4CAF50; font-weight: bold;'>[➖ Stable] ({global_heat*100:.1f}%)</span>"
                except: pass

            total_active = len(self.active_streams)
            if total_active > 0:
                status_counts = Counter([d['status'] for d in self.health_data]) 
                healthy_statuses =["Good", "Medium", "Quiet", "Recovered from Loop", "Visually Active (Mic Muted)"]
                healthy_count = sum(1 for d in self.health_data if d['status'] in healthy_statuses)
                health_pct = int((healthy_count / total_active) * 100)
                
                cnt = p['alerts_30m']
                freq_str = f"1 Alert every {int(1800/cnt)}s" if cnt > 0 else "Silent"
                
                if health_pct >= 90: desc = "Stable"; style = "padding: 4px 10px; border-radius: 4px; font-weight: bold; background-color: #d4edda; color: #155724; border: 1px solid #c3e6cb;"
                elif health_pct >= 70: desc = "Degraded"; style = "padding: 4px 10px; border-radius: 4px; font-weight: bold; background-color: #fff3cd; color: #856404; border: 1px solid #ffeeba;"
                else: desc = "Critical"; style = "padding: 4px 10px; border-radius: 4px; font-weight: bold; background-color: #f8d7da; color: #721c24; border: 1px solid #f5c6cb;"
                
                self.global_health_label.setText(f"System Health: {health_pct}% ({desc}) | {freq_str}")
                self.global_health_label.setStyleSheet(style)
                
                tooltip = "<b>System Health Breakdown</b><hr>"
                tooltip += f"<b>Total Active:</b> {total_active}<br>"
                for status in sorted(status_counts.keys(), key=lambda k: status_order.get(k, 99)):
                    count = status_counts[status]
                    # THE DICTIONARY FALLBACK PATCH
                    color_str = status_details.get(status, ("#888", "Unknown"))[0]
                    tooltip += f"<font color='{color_str}'>■</font> {status}: <b>{count}</b><br>"
                
                # ADD PIPELINE TO TOOLTIP
                tooltip += "<hr><b>Main Pipeline Status:</b><br>"
                tooltip += g_ind
                
                self.global_health_label.setToolTip(tooltip)

        except Exception as e:
            logging.error(f"UI Update Error: {e}", exc_info=True)
            self.global_health_label.setText(f"System Health: UI CRASH - {str(e)}")
            self.global_health_label.setStyleSheet("padding: 4px 10px; border-radius: 4px; font-weight: bold; background-color: #f8d7da; color: #721c24;")
        finally:
            for table in[self.species_table, self.channels_table, self.health_table, self.profiles_table]:
                table.setSortingEnabled(True)
            
            self.species_table.verticalScrollBar().setValue(species_scroll)
            self.channels_table.verticalScrollBar().setValue(channels_scroll)
            self.health_table.verticalScrollBar().setValue(health_scroll)
            self.profiles_table.verticalScrollBar().setValue(profiles_scroll)

            self.filter_health_table(); self.filter_dashboard_tables(); self.filter_profiles_table()

    # --- DIALOG & QOL UI METHODS ---
    def open_thresholds_dialog(self):
        d = HealthSettingsDialog(self.cycle_aggregation_mode, self)
        if d.exec():
            self.cycle_aggregation_mode = d.get_values()
            self.save_settings_and_state()
            self.start_refresh()

    def open_rate_dialog(self):
        d = RateAnalysisDialog(self._calculate_estimated_cycle_time(), self.last_rate_period_index, self.last_rate_per_index, self.last_rate_profile_checked, self)
        d.target_filter_mode = self.current_target_filter_mode
        d.exec(); self.last_rate_period_index, self.last_rate_per_index, self.last_rate_profile_checked = d.get_selections()

    def filter_profiles_table(self): self._generic_filter(self.profiles_table, self.search_box,[0, 1])
    def filter_dashboard_tables(self): 
        self._generic_filter(self.species_table, self.dashboard_search_box,[0])
        self._generic_filter(self.channels_table, self.dashboard_search_box, [0])
    def filter_health_table(self): self._generic_filter(self.health_table, self.health_search_box,[0, 1, 2, 3, 4, 7])
    
    def _generic_filter(self, table, line_edit, cols):
        txt = strip_accents(line_edit.text().strip().lower())
        for i in range(table.rowCount()):
            match = False
            for col in cols:
                item = table.item(i, col)
                widget = table.cellWidget(i, col)
                content = item.text() if item else (widget.text() if hasattr(widget, 'text') else "")
                
                # Check inside a nested layout if needed
                if not content and isinstance(widget, QWidget) and widget.layout():
                    child_btn = widget.findChild(QPushButton)
                    if child_btn:
                        content = child_btn.text()
                        
                content_clean = strip_accents(content.lower())
                if txt in content_clean: match = True; break
            table.setRowHidden(i, not match)

    def clear_stream_health_history(self):
        if QMessageBox.question(self, 'Confirm', "Delete all stream health history?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            try:
                with get_db_connection() as con: con.execute("DELETE FROM stream_health_events")
                self.start_refresh()
            except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def clear_system_log(self):
        if QMessageBox.question(self, 'Confirm', "Clear System Log?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            try:
                with get_db_connection() as con: con.execute("DELETE FROM system_events")
                self.refresh_system_log_tab()
            except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def get_diagnostic_parts(self, stream_url, stream_name):
        parts =[]
        try:
            with get_db_connection() as con:
                cur = con.cursor()
                cur.execute("SELECT timestamp, message FROM stream_health_events WHERE stream_url = ? AND status='FAILURE' ORDER BY timestamp DESC LIMIT 5", (stream_url,))
                fails = cur.fetchall()
                if fails:
                    fail_html = "<ul>" + "".join([f"<li>{safe_timestamp_format(ts, '%H:%M:%S')}: {msg}</li>" for ts, msg in fails]) + "</ul>"
                    parts.append({'title': 'Recent Failures (Last 5)', 'details': fail_html})
                else:
                    parts.append({'title': 'Recent Failures', 'details': 'No recent failures logged.'})

                cur.execute("SELECT average_noise_dbfs, sample_count, last_updated FROM stream_noise_profiles WHERE stream_url = ?", (stream_url,))
                noise = cur.fetchone()
                if noise:
                    db, count, updated = noise
                    try:
                        status = "Silent (Check Audio)" if float(db) == float('-inf') else ("Loud" if float(db) > -40 else "Quiet/Good")
                    except:
                        status = "Unknown"
                    parts.append({'title': 'Audio Profile', 'details': f"<b>Average Level:</b> {db} dBFS<br><b>Samples:</b> {count}<br><b>Status:</b> {status}"})
                else:
                    parts.append({'title': 'Audio Profile', 'details': 'No audio data collected yet.'})

                cur.execute("SELECT hash_text, first_seen_timestamp FROM audio_hashes WHERE stream_url = ? ORDER BY first_seen_timestamp DESC LIMIT 1", (stream_url,))
                loop = cur.fetchone()
                if loop:
                    parts.append({'title': 'Loop Analysis', 'details': f"Last unique audio hash seen at {safe_timestamp_format(loop[1], '%H:%M:%S')}."})
        except Exception as e:
            parts.append({'title': 'Error', 'details': f"Could not fetch diagnostics: {e}"})
        return parts

    def handle_interactive_cell_click(self, button_widget):
        self.details_dialog_open = True; self.refresh_timer.stop()
        clicked_table = None
        clicked_row = -1
        
        for table in[self.health_table, self.system_log_table]:
            for row in range(table.rowCount()):
                w = table.cellWidget(row, table.columnCount() - 1)
                if w is button_widget or (isinstance(w, QWidget) and w.layout() and w.layout().indexOf(button_widget) != -1): 
                    clicked_table, clicked_row = table, row; break
            if clicked_table: break
            
        if not clicked_table: 
            self.details_dialog_open = False; self.refresh_timer.start(REFRESH_INTERVAL_MS); return
            
        clicked_table.selectRow(clicked_row)
        title, details = "Details", "No details could be loaded."
        
        try:
            if clicked_table is self.health_table:
                stream_name_item = clicked_table.item(clicked_row, 0)
                if stream_name_item:
                    stream_name = stream_name_item.text()
                    data = next((d for d in self.health_data if d['name'] == stream_name), None)
                    if data: 
                        url = self.name_to_stream_data_map.get(data['name'], {}).get('page_url')
                        if url:
                            parts = self.get_diagnostic_parts(url, stream_name)
                            title = f"Health Details for {data['name']}"
                            details = "<hr>".join(f"<h3>{part['title']}</h3><p>{part['details']}</p>" for part in parts)
                        else:
                            details = "Error: Could not resolve stream URL."
            else:
                timestamp_item = clicked_table.item(clicked_row, 0)
                if timestamp_item:
                    exact_timestamp = timestamp_item.data(Qt.ItemDataRole.UserRole)
                    with get_db_connection() as con:
                        cur = con.cursor()
                        cur.execute("SELECT event_type, message, details FROM system_events WHERE timestamp = ?", (exact_timestamp,))
                        res = cur.fetchone()
                        if res:
                            event_type, msg, event_details = res
                            title = f"Log Details: {event_type}"
                            clean_msg = re.sub(r'\[\d+(;\d+)*m', '', msg) if msg else ""
                            try: clean_details = json.dumps(json.loads(event_details), indent=4)
                            except: clean_details = str(event_details)
                            details = f"<b>Message:</b><br><pre>{html.escape(clean_msg)}</pre><hr><b>Details:</b><br><pre>{html.escape(clean_details)}</pre>"
        except Exception as e:
            details = f"Error loading details: {str(e)}"

        dialog = DetailsDialog(title, details, self); dialog.exec()
        clicked_table.clearSelection(); self.details_dialog_open = False; self.refresh_timer.start(REFRESH_INTERVAL_MS)

    def add_tooltip_from_breakdown(self, item, breakdown_counter, title, recent_detections=None, stream_name=None):
        if not breakdown_counter and not recent_detections:
            return
            
        all_time_items = breakdown_counter.most_common() if breakdown_counter else []
        recent_items = recent_detections if recent_detections else[]

        html_str = "<table cellpadding='4' cellspacing='0'><tr>"

        if stream_name:
            html_str = f"<div style='color:#00E5FF; font-size:14px; font-weight:bold; border-bottom:1px solid #333; margin-bottom:6px; padding-bottom:4px;'>{html.escape(stream_name)}</div>" + html_str

        html_str += f"<td valign='top' style='padding-right: 20px;'><b><u>{title}</u></b></td>"
        if len(all_time_items) > 25:
            html_str += "<td valign='top' style='padding-right: 20px;'><b><u>(Cont.)</u></b></td>"
        if recent_items:
            html_str += "<td valign='top'><b><u>Last 25 Detections</u></b></td>"
        html_str += "</tr><tr>"

        html_str += "<td valign='top' style='white-space: nowrap; padding-right: 20px;'>"
        if all_time_items:
            col1_len = math.ceil(len(all_time_items)/2) if len(all_time_items) > 25 else len(all_time_items)
            html_str += "<br>".join([f"• {name} ({count})" for name, count in all_time_items[:col1_len]])
        else:
            html_str += "None"
        html_str += "</td>"

        if len(all_time_items) > 25:
            html_str += "<td valign='top' style='white-space: nowrap; padding-right: 20px;'>"
            html_str += "<br>".join([f"• {name} ({count})" for name, count in all_time_items[col1_len:]])
            html_str += "</td>"

        if recent_items:
            html_str += "<td valign='top' style='white-space: nowrap;'>"
            recent_strs =[]
            for det in recent_items:
                if len(det) == 6:
                    sp, ts, method, dist, frame, reason = det
                else:
                    sp, ts = det[0], det[1]
                    method, dist, frame, reason = "audio", "Unknown", "N/A", ""
                
                time_str = safe_timestamp_format(ts, '%H:%M:%S')
                icon = "🔥" if method == "multimodal" else "📷" if method == "vision" else "🐦"
                meta_str = f"[{dist}]" if method == "audio" else f"[D:{dist} | F:{frame}]"
                
                recent_strs.append(f"{time_str} - {icon} {sp} <span style='color:#aaa; font-size:10px;'>{meta_str}</span>")
            html_str += "<br>".join(recent_strs)
            html_str += "</td>"

        html_str += "</tr></table>"
        item.setToolTip(html_str)

    def on_profiles_table_clicked(self, row, column):
        if column != self.profiles_table.columnCount() - 1:
            if self.highlighted_clip_id: self.highlighted_clip_id = None; self.start_refresh()

    def play_clip(self, detection_id, is_anchor=False, button=None):
        if button:
            row = self.profiles_table.indexAt(button.pos()).row(); self.profiles_table.clearFocus()
            stream_name, species_name = self.profiles_table.item(row, 0).text(), self.profiles_table.item(row, 1).text()
            self.highlighted_clip_id = (stream_name, species_name); self.start_refresh()
        
        mp3_path = BASELINE_CLIPS_DIR / f"detection_{detection_id}.mp3"
        wav_path = BASELINE_CLIPS_DIR / f"detection_{detection_id}.wav"
        
        final_path = None
        if mp3_path.exists(): final_path = mp3_path
        elif wav_path.exists(): final_path = wav_path
            
        if not final_path:
            QMessageBox.warning(self, "File Not Found", f"Audio clip not found (checked .mp3 and .wav)."); self.highlighted_clip_id = None; self.start_refresh(); return
            
        try:
            if sys.platform == "win32": os.startfile(final_path)
            elif sys.platform == "darwin": subprocess.call(["open", final_path])
            else: subprocess.call(["xdg-open", final_path])
            if is_anchor: self.audited_clips.add(str(detection_id)); self.save_state(); QTimer.singleShot(500, self.start_refresh)
        except Exception as e:
            logging.error(f"Failed to open audio clip {final_path}: {e}", exc_info=True); QMessageBox.critical(self, "Error", f"Could not open the audio file: {e}"); self.highlighted_clip_id = None; self.start_refresh()

    def reset_selected_profiles(self):
        selected_items = self.profiles_table.selectedItems()
        if not selected_items: QMessageBox.warning(self, "Selection Error", "Please select one or more rows to reset."); return
        profiles_to_reset = {(self.profiles_table.item(i.row(), 0).text(), self.profiles_table.item(i.row(), 1).text()) for i in selected_items}
        reply = QMessageBox.question(self, 'Confirm Reset', "Are you sure you want to reset the baseline for:\n\n" + "\n".join(f"- {s} on {st}" for st, s in sorted(list(profiles_to_reset))), QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            try:
                name_to_url = {v: k for k, v in self.url_to_name_map.items()}
                with get_db_connection() as con:
                    for stream_name, species_name in profiles_to_reset:
                        if stream_name in name_to_url: con.execute("DELETE FROM species_stream_profiles WHERE stream_url = ? AND species_name = ?", (name_to_url[stream_name], species_name))
                QMessageBox.information(self, "Success", f"{len(profiles_to_reset)} profile(s) reset."); self.start_refresh()
            except Exception as e: logging.error(f"Error resetting profiles: {e}", exc_info=True); QMessageBox.critical(self, "Database Error", f"Could not reset profiles: {e}")

    def reset_all_profiles(self):
        msg = ("<b>⚠️ WARNING: MASSIVE RESET</b><br><br>You are about to delete <b>ALL</b> calibration data.<br>Are you absolutely sure?")
        reply = QMessageBox.warning(self, "Confirm Full Reset", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            try:
                with get_db_connection() as con: con.execute("DELETE FROM species_stream_profiles")
                QMessageBox.information(self, "Reset Complete", "All calibration profiles have been deleted."); self.start_refresh()
            except Exception as e: QMessageBox.critical(self, "Error", f"Failed to reset: {e}")

    def populate_sortable_channels_table(self, data, breakdown_data, dist_data_map, recent_25_map):
        self.channels_table.setRowCount(len(data))
        
        # Updated V40.0 Distance Map
        abbr_map = {"Point Blank": "PB", "Very Near": "VN", "Near": "N", "Mid-range": "MR", "Mid-ground": "MG", "Background": "BG", "Far": "F", "Deep Background": "DBG", "Very Far": "VF", "Horizon": "HZ", "First Sighting (Calibrating...)": "FSC", "Pending...": "PE", "Unknown": "UN", "Multimodal Confirmed": "MULTI"}
        
        for row_idx, (name, count) in enumerate(data):
            name_item = QTableWidgetItem(name)
            self.add_tooltip_from_breakdown(name_item, breakdown_data.get(name), "Species Detected:", recent_25_map.get(name), name)
            
            created = self.name_to_stream_data_map.get(name, {}).get('created_at')
            created_str = safe_timestamp_format(created, '%Y-%m-%d') if created else "N/A"
            created_item = DateTimeTableWidgetItem(created_str)
            try: created_item.setData(Qt.ItemDataRole.UserRole, float(created) if created else 0)
            except: created_item.setData(Qt.ItemDataRole.UserRole, 0)
            
            dist_counts = dist_data_map.get(name, {}).get('dist_counts', Counter()); dist_species_counts = dist_data_map.get(name, {}).get('dist_species', defaultdict(Counter))
            summary_parts =[]
            
            for dist_cat in["Point Blank", "Very Near", "Near", "Mid-range", "Mid-ground", "Far", "Background", "Very Far", "Deep Background", "Horizon", "Multimodal Confirmed", "First Sighting (Calibrating...)", "Pending...", "Unknown"]:
                if dist_cat in dist_counts: summary_parts.append(f"{abbr_map.get(dist_cat, dist_cat[:2].upper())}:{dist_counts[dist_cat]}")
                
            tooltip_parts =["<b>Species per Distance:</b>"]
            has_dist_species = any(dist_species_counts.values())
            for dist_cat in["Point Blank", "Very Near", "Near", "Mid-range", "Mid-ground", "Far", "Background", "Very Far", "Deep Background", "Horizon", "Multimodal Confirmed", "First Sighting (Calibrating...)", "Pending...", "Unknown"]:
                if dist_cat in dist_species_counts:
                    species_counter = dist_species_counts[dist_cat]; total_detections_in_cat = sum(species_counter.values())
                    tooltip_parts.append(f"- {dist_cat} ({len(species_counter)} species, {total_detections_in_cat} total):")
                    for species_name, num in species_counter.most_common(): tooltip_parts.append(f"&nbsp;&nbsp;- {species_name} ({num})")
            if has_dist_species: tooltip_parts.append("<hr>")
            tooltip_parts.append("<b>Abbreviation Legend:</b><br>" + ", ".join([f"<b>{k}:</b> {v}" for k,v in abbr_map.items()]))
            
            dist_item = CustomDistanceDistributionItem(", ".join(summary_parts), dist_counts)
            dist_item.setToolTip("<br>".join(tooltip_parts))
            
            self.channels_table.setItem(row_idx, 0, name_item)
            self.channels_table.setItem(row_idx, 1, NumericTableWidgetItem(str(count)))
            self.channels_table.setItem(row_idx, 2, created_item) 
            self.channels_table.setItem(row_idx, 3, dist_item)

    def populate_sortable_species_table(self, species_data, breakdown_data):
        self.species_table.setRowCount(len(species_data))
        for row_idx, (species_name, count) in enumerate(species_data):
            name_item = QTableWidgetItem(species_name); self.add_tooltip_from_breakdown(name_item, breakdown_data.get(species_name), "Detected on these Streams:", None)
            self.species_table.setItem(row_idx, 0, name_item); self.species_table.setItem(row_idx, 1, NumericTableWidgetItem(str(count)))

    def species_table_context_menu(self, pos): self.show_verify_menu(self.species_table, pos, 0)

    def generic_table_context_menu(self, pos):
        table = self.sender()
        item = table.itemAt(pos)
        if not item or (table is self.health_table and item.column() == 7): return 
        
        menu = QMenu()
        
        if item.column() == 0 and table in[self.channels_table, self.health_table]:
            stream_name = item.text()
            stream_data = self.name_to_stream_data_map.get(stream_name)
            if stream_data:
                if stream_data.get('page_url'):
                    url_action = QAction(f"🌐 Open Stream URL", self)
                    url_action.triggered.connect(lambda checked=False, url=stream_data.get('page_url'): self.open_stream_url(url))
                    menu.addAction(url_action)
                if stream_data.get('lat') is not None:
                    map_action = QAction(f"🗺️ Show '{stream_name}' on Map", self)
                    map_action.triggered.connect(lambda checked=False, d=stream_data: self.open_map_link(d))
                    menu.addAction(map_action)
                    
        if table is self.profiles_table and item.column() == 1:
            verify_action = QAction(f"Verify '{item.text()}'", self)
            verify_action.triggered.connect(lambda checked=False, sp=item.text(): self.open_verify_link(sp))
            menu.addAction(verify_action)
            
        if menu.actions(): 
            menu.exec(table.viewport().mapToGlobal(pos))

    def show_verify_menu(self, table, pos, species_col_idx):
        item = table.itemAt(pos)
        if not item or item.column() != species_col_idx: return
        menu = QMenu()
        verify_action = QAction(f"Verify '{item.text()}'", self)
        verify_action.triggered.connect(lambda checked=False, sp=item.text(): self.open_verify_link(sp))
        menu.addAction(verify_action)
        menu.exec(table.viewport().mapToGlobal(pos))

    def open_verify_link(self, species_name): webbrowser.open(f"https://www.allaboutbirds.org/guide/{quote_plus(species_name)}")
    def open_stream_url(self, url): webbrowser.open(url)
    def open_map_link(self, d): webbrowser.open(f"https://www.google.com/maps/search/?api=1&query={d.get('lat')},{d.get('lon')}")

    def refresh_system_log_tab(self, all_events=None):
        try:
            if all_events is None:
                # Fallback: fetch directly only if called standalone (not from worker payload)
                with get_db_connection() as con:
                    cur = con.cursor()
                    cur.execute("SELECT timestamp, event_type, status, message, details FROM system_events ORDER BY timestamp DESC LIMIT 200")
                    all_events = cur.fetchall()
            
            event_types = sorted(list({e[1] for e in all_events})); current_filter = self.log_type_filter.currentText()
            self.log_type_filter.blockSignals(True); self.log_type_filter.clear(); self.log_type_filter.addItems(["All"] + event_types)
            if current_filter in["All"] + event_types: self.log_type_filter.setCurrentText(current_filter)
            self.log_type_filter.blockSignals(False)
            
            self.system_log_table.clearContents()
            self.system_log_table.setRowCount(len(all_events))
            
            for i, (ts, event_type, status, msg, details) in enumerate(all_events):
                ts_item = DateTimeTableWidgetItem(safe_timestamp_format(ts))
                try: ts_item.setData(Qt.ItemDataRole.UserRole, float(ts))
                except: ts_item.setData(Qt.ItemDataRole.UserRole, 0)
                
                type_item = QTableWidgetItem(event_type); status_item = QTableWidgetItem(status)
                if status == 'FAILURE': status_item.setForeground(QColor('red'))
                elif status in ['SUCCESS', 'INFO']: status_item.setForeground(QColor('green'))
                
                self.system_log_table.setItem(i, 0, ts_item)
                self.system_log_table.setItem(i, 1, type_item)
                self.system_log_table.setItem(i, 2, status_item)
                
                clean_msg = re.sub(r'\[\d+(;\d+)*m', '', msg) if msg else ""
                
                # WIDGET REUSE FIX
                existing_btn = self.system_log_table.cellWidget(i, 3)
                if isinstance(existing_btn, QPushButton):
                    existing_btn.setText(clean_msg.split('\n')[0])
                else:
                    button = QPushButton(clean_msg.split('\n')[0])
                    button.clicked.connect(lambda chk, b=button: self.handle_interactive_cell_click(b))
                    self.system_log_table.setCellWidget(i, 3, button)
                    
            self.update_system_log_tab()
        except Exception as e: logging.error(f"Error refreshing system log tab: {e}", exc_info=True)
            
    def update_system_log_tab(self, *args):
        try:
            search_text = self.log_search_box.text().lower(); type_filter = self.log_type_filter.currentText()
            for i in range(self.system_log_table.rowCount()):
                type_item, message_widget = self.system_log_table.item(i, 1), self.system_log_table.cellWidget(i, 3)
                message_text = message_widget.text() if message_widget else ""
                
                if not message_text and isinstance(message_widget, QWidget) and message_widget.layout():
                    child_btn = message_widget.findChild(QPushButton)
                    if child_btn:
                        message_text = child_btn.text()
                        
                type_match = (type_filter == "All") or (type_item and type_filter == type_item.text())
                search_match = (search_text in message_text.lower())
                self.system_log_table.setRowHidden(i, not (type_match and search_match))
        except Exception as e: logging.error(f"Error filtering system log table: {e}", exc_info=True)

    def save_settings_and_state(self):
        try:
            settings = {}
            if DASHBOARD_SETTINGS_FILE.exists():
                try: settings = json.loads(DASHBOARD_SETTINGS_FILE.read_text(encoding="utf-8"))
                except json.JSONDecodeError: pass
            
            settings['health_timeframe_index'] = self.health_timeframe_selector.currentIndex()
            settings['cycle_aggregation_mode'] = self.cycle_aggregation_mode
            
            settings['profiles_table_widths'] =[self.profiles_table.columnWidth(i) for i in range(self.profiles_table.columnCount())]
            settings['species_table_widths'] =[self.species_table.columnWidth(i) for i in range(self.species_table.columnCount())]
            settings['channels_table_widths'] =[self.channels_table.columnWidth(i) for i in range(self.channels_table.columnCount())]
            settings['health_table_widths'] =[self.health_table.columnWidth(i) for i in range(self.health_table.columnCount())]
            settings['system_log_table_widths'] =[self.system_log_table.columnWidth(i) for i in range(self.system_log_table.columnCount())]
            geom = self.geometry(); settings['window_geometry'] = {'x': geom.x(), 'y': geom.y(), 'width': geom.width(), 'height': geom.height()}
            settings['rate_dialog_period_index'] = self.last_rate_period_index
            settings['rate_dialog_per_index'] = self.last_rate_per_index
            settings['rate_dialog_profile_checked'] = self.last_rate_profile_checked
            DASHBOARD_SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
        except Exception as e: logging.error(f"Could not save settings: {e}", exc_info=True)

    def load_state(self):
        try:
            if DASHBOARD_STATE_FILE.exists():
                self.audited_clips = set(json.loads(DASHBOARD_STATE_FILE.read_text(encoding="utf-8")).get("audited_clip_ids",[]))
        except Exception as e:
            logging.error(f"Could not load dashboard state: {e}", exc_info=True)

    def load_settings(self):
        try:
            if DASHBOARD_SETTINGS_FILE.exists():
                settings = json.loads(DASHBOARD_SETTINGS_FILE.read_text(encoding="utf-8"))
                
                saved_index = settings.get('health_timeframe_index', 0)
                if saved_index < self.health_timeframe_selector.count():
                    self.health_timeframe_selector.setCurrentIndex(saved_index)
                
                self.cycle_aggregation_mode = settings.get('cycle_aggregation_mode', 0)
                self.last_rate_period_index = settings.get('rate_dialog_period_index', 3)
                self.last_rate_per_index = settings.get('rate_dialog_per_index', 0)
                self.last_rate_profile_checked = settings.get('rate_dialog_profile_checked', False)
                if 'window_geometry' in settings: self.setGeometry(settings['window_geometry']['x'], settings['window_geometry']['y'], settings['window_geometry']['width'], settings['window_geometry']['height'])
                table_settings = {'profiles_table_widths': self.profiles_table, 'species_table_widths': self.species_table, 'channels_table_widths': self.channels_table, 'health_table_widths': self.health_table, 'system_log_table_widths': self.system_log_table}
                for key, table in table_settings.items():
                    if key in settings and len(settings[key]) == table.columnCount():
                        for i, w in enumerate(settings[key]): table.setColumnWidth(i, w)
        except Exception as e: logging.error(f"Could not load settings: {e}", exc_info=True)

    def closeEvent(self, event):
        self.save_settings_and_state()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    dashboard = StatsDashboard()
    dashboard.show()
    sys.exit(app.exec())