# FILE: config_editor_gui.py
# VERSION: 13.2 - "The Black Screen Fix"
# RESPONSIBILITY: Configuration GUI.
# UPDATED: Injected Nuclear GPU Disable flags to prevent PyQt6 hardware acceleration black screens on Windows 11.

import sys

# --- NUCLEAR GPU DISABLE (Fix for Windows 11 Black Screen) ---
sys.argv.append("--disable-gpu")
sys.argv.append("--disable-software-rasterizer")
sys.argv.append("--disable-gpu-compositing")
sys.argv.append("--disable-accelerated-2d-canvas")
sys.argv.append("--disable-d3d11")

import json
import requests
import subprocess
import logging
import sqlite3
import time
import random
import os
import webbrowser
import signal
import copy
import re
import io
from functools import partial
from pathlib import Path
from collections import Counter
from datetime import datetime

from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QLineEdit, QPushButton, QListWidget,
                             QListWidgetItem, QCheckBox, QGroupBox, QFormLayout,
                             QMessageBox, QSpinBox, QTableWidget,
                             QTableWidgetItem, QHeaderView, QGridLayout, QFileDialog,
                             QMenu, QComboBox, QToolButton, QInputDialog, QTextEdit, QDialog,
                             QAbstractItemView, QProgressDialog, QSlider, QScrollArea, 
                             QTabWidget, QDialogButtonBox)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread, QObject, QEvent
from PyQt6.QtGui import QColor, QPalette, QBrush, QColorConstants, QAction

# --- SYSTEM MONITORING LIBRARY ---
try:
    import psutil
except ImportError:
    print("FATAL: 'psutil' library is missing. Please run: pip install psutil")
    sys.exit(1)

# --- IMPORT MIGRATOR & ENGINES ---
try:
    import stream_migrator
except ImportError:
    stream_migrator = None

try:
    import yt_sync_engine
except ImportError:
    yt_sync_engine = None

try:
    import stream_resolver
except ImportError:
    stream_resolver = None

# --- IMPORT MODULES ---
from config_editor_utils import ChromeMaintenance, haversine_distance
from config_editor_threads import StreamCheckThread
from config_editor_dialogs import (
    SaveConfirmDialog,
    TieredCooldownDialog,
    ResetConfirmDialog,
    EngineConfigDialog
)

from config_editor_advanced_gui import AdvancedSettingsDialog

from config_editor_sys_dialogs import (
    StreamAuditDialog,
    HousekeepingManagerDialog,
    LogParserWorker,
    NetworkTelemetryDialog,
    ScrollStealFilter
)

import db_connector
import network_manager

# --- Centralized Logging ---
log_file = Path(__file__).resolve().parent / "monitor_debug.txt"
logging.basicConfig(level=logging.DEBUG, 
                    format='%(asctime)s - %(levelname)s -[%(filename)s:%(lineno)d] - %(message)s',
                    handlers=[logging.FileHandler(log_file, mode='a'), logging.StreamHandler()])

# --- Configuration Paths ---
ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "birdnet_config.json"
DATABASE_PATH = ROOT / "detections.db"
SCHEDULER_SCRIPT = ROOT / "scheduler.py"
GUI_SETTINGS_FILE = ROOT / "gui_settings.json"
COOKIE_WARNING_STATE_PATH = ROOT / "cookie_warning_state.json"
SYNC_PROPOSALS_FILE = ROOT / "sync_proposals.json"
HYDRA_STATE_FILE = ROOT / "hydra_heat_state.json"
LOG_ANALYZER_SETTINGS_FILE = ROOT / "log_analyzer_settings.json"

# --- Database Helper ---
import db_connector

def get_db_connection():
    con = sqlite3.connect(DATABASE_PATH, timeout=15)
    con.execute("PRAGMA journal_mode=WAL")
    return con

def log_system_event(event_type: str, status: str, message: str, details: str = ""):
    try:
        with get_db_connection() as con:
            con.execute("INSERT INTO system_events (timestamp, event_type, status, message, details) VALUES (?, ?, ?, ?, ?)", (time.time(), event_type, status, message, details))
    except Exception as e:
        logging.error(f"CRITICAL: Failed to log system event '{event_type}' to database: {e}", exc_info=True)

def send_and_log_system_alert(bot_token, chat_id, message, event_type):
    if not bot_token or not chat_id:
        log_system_event(event_type, 'FAILURE', message, "Bot Token or Chat ID is missing.")
        return False, "Bot Token or Chat ID is missing."
    
    for i in range(1, 4):
        try:
            response = requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": message}, timeout=15)
            response.raise_for_status()
            log_system_event(event_type, 'SUCCESS', message, response.text)
            logging.info("Successfully sent and logged Telegram message.")
            return True, "Message sent successfully!"
        except requests.exceptions.RequestException as e:
            logging.warning(f"Failed to send Telegram message (attempt {i}): {e}")
            if i < 3:
                time.sleep(min(2**i, 15) + random.uniform(0.0, 0.5))
            else:
                log_system_event(event_type, 'FAILURE', message, f"Failed after retries: {e}")
                return False, f"Failed after retries: {e}"
    return False, "An unknown error occurred."

# ==============================================================================
# WORKER THREADS
# ==============================================================================

class ResolutionThread(QThread):
    result_ready = pyqtSignal(object, object, str, str, str)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        if not stream_resolver:
            self.result_ready.emit(False,[], "error", "Resolver module missing.", "")
            return
            
        title = ""
        try:
            if "youtube.com" in self.url or "youtu.be" in self.url:
                import yt_dlp
                with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True}) as ydl:
                    info = ydl.extract_info(self.url, download=False)
                    if info and 'title' in info:
                        title = info['title']
        except Exception as e:
            logging.warning(f"Failed to extract title in resolver thread: {e}")
        
        try:
            links, stype, msg = stream_resolver.resolve_stream_url(self.url, fast_mode=True)
            if links:
                self.result_ready.emit(True, links, stype, msg, title)
            else:
                self.result_ready.emit(False,[], "error", msg, title)
        except Exception as e:
            self.result_ready.emit(False,[], "error", str(e), title)

class AutoSyncWorker(QThread):
    finished = pyqtSignal(list)
    
    def run(self):
        try:
            if yt_sync_engine:
                engine = yt_sync_engine.YouTubeSyncEngine()
                engine.run_sync()
                self.finished.emit(engine.proposals)
            else:
                self.finished.emit([])
        except Exception as e:
            logging.error(f"AutoSyncWorker Error: {e}")
            self.finished.emit([])

class ChannelSyncWorker(QThread):
    progress_update = pyqtSignal(int, int, str)
    finished = pyqtSignal(list)
    
    def __init__(self, target_url):
        super().__init__()
        self.target_url = target_url
        
    def run(self):
        try:
            if yt_sync_engine:
                engine = yt_sync_engine.YouTubeSyncEngine()
                def cb(c, t, m): self.progress_update.emit(c, t, m)
                engine.run_sync(progress_callback=cb, target_channels=[self.target_url])
                self.finished.emit(engine.proposals)
            else:
                self.finished.emit([])
        except Exception as e:
            logging.error(f"ChannelSyncWorker Error: {e}")
            self.finished.emit([])

# ==============================================================================
# DIALOGS
# ==============================================================================

class ChannelManagerDialog(QDialog):
    def __init__(self, editor_ref, parent=None):
        super().__init__(parent)
        self.editor_ref = editor_ref
        self.setWindowTitle("Global Channel Manager & Surgical Scanner")
        self.resize(1100, 500)
        self.layout = QVBoxLayout(self)
        
        info = QLabel("<b>Click a Channel Name</b> to instantly filter the main dashboard's stream list behind this window.<br>"
                      "<b>Auto-Ignored</b> channels have exceeded the max live stream threshold. Use <b>Force Scan</b> to manually parse them anyway.")
        self.layout.addWidget(info)
        
        self.lbl_totals = QLabel("<b>Total Channels:</b> 0 &nbsp;|&nbsp; <b>Total Streams in Channels:</b> 0")
        self.lbl_totals.setStyleSheet("font-size: 14px; color: #00E676; padding: 6px; border: 1px solid #444; border-radius: 4px; background-color: #2b2b2b;")
        self.layout.addWidget(self.lbl_totals)
        
        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("<b>Universal Search:</b>"))
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Filter by Channel Name, Count, or URL...")
        self.search_box.textChanged.connect(self.filter_table)
        search_layout.addWidget(self.search_box)
        self.layout.addLayout(search_layout)
        
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Channel Name (Click to Filter)", "Configured Streams", "Live on YT", "Sync Status", "Main URL (Editable)", "Action"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.layout.addWidget(self.table)
        
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        self.layout.addWidget(btn_close)
        
        self.populate_table()
        
        self.table.cellClicked.connect(self.on_cell_clicked)
        self.table.itemChanged.connect(self.on_item_changed)
        
    def filter_table(self):
        text = self.search_box.text().strip().lower()
        for i in range(self.table.rowCount()):
            match = False
            for j in range(5): 
                item = self.table.item(i, j)
                if item and text in item.text().lower():
                    match = True
                    break
            self.table.setRowHidden(i, not match)
        
    def populate_table(self):
        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        
        channel_counts = Counter()
        total_streams_in_channels = 0
        for i in range(self.editor_ref.stream_list_widget.count()):
            d = self.editor_ref.stream_list_widget.item(i).data(Qt.ItemDataRole.UserRole)
            cn = d.get("channel_name", "").strip()
            if cn:
                channel_counts[cn] += 1
                total_streams_in_channels += 1
                
        for cn in self.editor_ref.unsaved_channels.keys():
            if cn not in channel_counts:
                channel_counts[cn] = 0
                
        self.lbl_totals.setText(f"<b>Total Channels:</b> {len(channel_counts)} &nbsp;|&nbsp; <b>Total Streams in Channels:</b> {total_streams_in_channels}")
        self.table.setRowCount(len(channel_counts))
        
        # Pull live counts and ignored status
        live_counts = {}
        ignored_channels =[]
        try:
            cache_path = ROOT / "youtube_metadata_cache.json"
            if cache_path.exists():
                cache = json.loads(cache_path.read_text(encoding='utf-8'))
                live_counts = cache.get("channel_live_counts", {})
            if CONFIG_FILE.exists():
                cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                ignored_channels = cfg.get("ignored_channels",[])
        except: pass
        
        class NumericItem(QTableWidgetItem):
            def __lt__(self, other):
                try: 
                    # Treat "Unknown" as 0 for sorting
                    sv = int(self.text()) if self.text().isdigit() else 0
                    ov = int(other.text()) if other.text().isdigit() else 0
                    return sv < ov
                except: 
                    return super().__lt__(other)

        row = 0
        for cn, count in sorted(channel_counts.items()):
            item_name = QTableWidgetItem(cn)
            item_name.setFlags(item_name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item_name.setForeground(QBrush(QColor("#00E5FF")))
            font = item_name.font()
            font.setUnderline(True)
            item_name.setFont(font)
            
            item_count = NumericItem(str(count))
            item_count.setFlags(item_count.flags() & ~Qt.ItemFlag.ItemIsEditable)
            
            url = self.editor_ref.unsaved_channels.get(cn, "")
            item_url = QTableWidgetItem(url)
            
            # Status / Live
            live_count = live_counts.get(url, "Unknown")
            status = "Auto-Ignored" if url in ignored_channels else "Allowed"
            
            item_live = NumericItem(str(live_count))
            item_live.setFlags(item_live.flags() & ~Qt.ItemFlag.ItemIsEditable)
            
            item_status = QTableWidgetItem(status)
            item_status.setFlags(item_status.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if status == "Auto-Ignored":
                item_status.setForeground(QBrush(QColor("#FF9800")))
                font = item_status.font(); font.setBold(True); item_status.setFont(font)
            else:
                item_status.setForeground(QBrush(QColor("#00E676")))
            
            # Actions
            action_widget = QWidget()
            action_layout = QHBoxLayout(action_widget)
            action_layout.setContentsMargins(2, 2, 2, 2)
            action_layout.setSpacing(4)
            
            btn_open = QPushButton("🌐 Open")
            btn_open.clicked.connect(lambda chk, u=cn: self.open_url(u))
            
            btn_scan = QPushButton("▶ Force Scan")
            btn_scan.setStyleSheet("background-color: #00897B; color: white;")
            btn_scan.clicked.connect(lambda chk, u=url, n=cn: self.force_scan(n, u))
            if not url: btn_scan.setEnabled(False)
            
            btn_rename = QPushButton("✏️ Rename")
            btn_rename.clicked.connect(lambda chk, old_cn=cn: self.rename_channel(old_cn))
            
            btn_delete = QPushButton("🗑️")
            btn_delete.setStyleSheet("background-color: #B71C1C; color: white;")
            btn_delete.clicked.connect(lambda chk, old_cn=cn: self.delete_channel(old_cn))
            
            action_layout.addWidget(btn_open)
            action_layout.addWidget(btn_scan)
            action_layout.addWidget(btn_rename)
            action_layout.addWidget(btn_delete)
            
            self.table.setItem(row, 0, item_name)
            self.table.setItem(row, 1, item_count)
            self.table.setItem(row, 2, item_live)
            self.table.setItem(row, 3, item_status)
            self.table.setItem(row, 4, item_url)
            self.table.setCellWidget(row, 5, action_widget)
            row += 1
            
        self.table.setSortingEnabled(True)
        self.table.blockSignals(False)
        self.filter_table()

    def force_scan(self, channel_name, url):
        if not url:
            QMessageBox.warning(self, "No URL", "Please save a URL for this channel first.")
            return
            
        self.progress_dialog = QProgressDialog(f"Scanning {channel_name}...", "Cancel", 0, 100, self)
        self.progress_dialog.setWindowTitle("Surgical Force Scan")
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setAutoClose(True)
        
        self.scan_worker = ChannelSyncWorker(url)
        self.scan_worker.progress_update.connect(self._update_scan_progress)
        self.scan_worker.finished.connect(self._on_scan_finished)
        self.scan_worker.start()
        self.progress_dialog.exec()
        
    def _update_scan_progress(self, c, t, msg):
        if self.progress_dialog.wasCanceled():
            self.scan_worker.terminate()
            return
        self.progress_dialog.setMaximum(t)
        self.progress_dialog.setValue(c)
        self.progress_dialog.setLabelText(msg)
        
    def _on_scan_finished(self, proposals):
        self.progress_dialog.accept()
        try:
            existing =[]
            if SYNC_PROPOSALS_FILE.exists():
                existing = json.loads(SYNC_PROPOSALS_FILE.read_text(encoding='utf-8'))
            
            target_url = self.scan_worker.target_url
            filtered =[p for p in existing if p.get('old_channel') != target_url and p.get('new_channel') != target_url]
            filtered.extend(proposals)
            
            SYNC_PROPOSALS_FILE.write_text(json.dumps(filtered, indent=2), encoding='utf-8')
            
            QMessageBox.information(self, "Scan Complete", f"Force Scan completed.\nFound {len(proposals)} updates/streams for this channel.\n\nPlease open the Discovery Hub to review them.")
            self.populate_table() 
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save proposals: {e}")

    def rename_channel(self, old_name):
        new_name, ok = QInputDialog.getText(self, "Rename Channel", f"Enter new name for '{old_name}':", text=old_name)
        if ok and new_name.strip():
            new_name = new_name.strip()
            if new_name == old_name: return
            
            url = self.editor_ref.unsaved_channels.pop(old_name, "")
            self.editor_ref.unsaved_channels[new_name] = url
            
            for i in range(self.editor_ref.stream_list_widget.count()):
                item = self.editor_ref.stream_list_widget.item(i)
                d = item.data(Qt.ItemDataRole.UserRole)
                if d.get("channel_name", "").strip() == old_name:
                    d["channel_name"] = new_name
                    item.setData(Qt.ItemDataRole.UserRole, d)
                    
            self.editor_ref._check_and_update_dirty_state()
            self.editor_ref._populate_channel_dropdown()
            
            curr_item = self.editor_ref.stream_list_widget.currentItem()
            if curr_item:
                curr_d = curr_item.data(Qt.ItemDataRole.UserRole)
                if curr_d.get("channel_name", "").strip() == new_name:
                    self.editor_ref.stream_channel_edit.setCurrentText(new_name)
            
            self.populate_table()

    def delete_channel(self, old_name):
        reply = QMessageBox.question(self, "Confirm Delete", 
            f"Are you sure you want to delete the channel '{old_name}'?\n\nThis will clear the channel name from all associated streams.", 
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            
        if reply == QMessageBox.StandardButton.Yes:
            self.editor_ref.unsaved_channels.pop(old_name, None)
            
            for i in range(self.editor_ref.stream_list_widget.count()):
                item = self.editor_ref.stream_list_widget.item(i)
                d = item.data(Qt.ItemDataRole.UserRole)
                if d.get("channel_name", "").strip() == old_name:
                    d.pop("channel_name", None)
                    item.setData(Qt.ItemDataRole.UserRole, d)
                    
            self.editor_ref._check_and_update_dirty_state()
            self.editor_ref._populate_channel_dropdown()
            
            curr_item = self.editor_ref.stream_list_widget.currentItem()
            if curr_item:
                curr_d = curr_item.data(Qt.ItemDataRole.UserRole)
                if "channel_name" not in curr_d and self.editor_ref.stream_channel_edit.currentText() == old_name:
                    self.editor_ref.stream_channel_edit.setCurrentText("")
                    
            self.populate_table()

    def on_cell_clicked(self, row, col):
        if col == 0:
            item = self.table.item(row, 0)
            if item:
                self.editor_ref.stream_search_box.setText(item.text())
                
    def on_item_changed(self, item):
        if item.column() == 4:
            row = item.row()
            cn_item = self.table.item(row, 0)
            if cn_item:
                cn = cn_item.text()
                new_url = item.text().strip()
                if new_url:
                    self.editor_ref.unsaved_channels[cn] = new_url
                else:
                    self.editor_ref.unsaved_channels.pop(cn, None)
                self.editor_ref._check_and_update_dirty_state()
                
    def open_url(self, cn):
        url = self.editor_ref.unsaved_channels.get(cn, "")
        if url:
            webbrowser.open(url)
        else:
            QMessageBox.warning(self, "No URL", "Please enter and save a Main URL for this channel first.")


# ==============================================================================
# MAIN APPLICATION CLASS
# ==============================================================================

class ConfigEditor(QWidget):
    def __init__(self):
        super().__init__()
        self._loading = True
        self.saved_config_data = {}
        self.saved_gui_settings = {}
        
        # STATE VARIABLES
        self.unsaved_tiered_config = {}
        self.unsaved_loop_config = {}
        self.unsaved_distance_config = {}
        self.unsaved_cookies_path = ""  
        self.unsaved_cookie_alert_config = {}
        self.unsaved_network_map = {}
        self.unsaved_debug_logging = False 
        self.unsaved_report_config = {} 
        self.unsaved_channels = {} 
        
        self.unsaved_send_audio_all = False
        self.unsaved_send_audio_dsp = True
        self.unsaved_send_audio_multi = True
        self.unsaved_send_audio_birdnet = False
        
        self.unsaved_map_config = {}
        self.unsaved_housekeeping_config = {}
        
        self._pending_original_url = ""
        self._pending_stream_type = "youtube"
        self._pending_resolved_url = "" 
        self._pending_yt_title = "" 
        
        self.engine_params = {
            'fast_mode_enabled': True,
            'total_listeners': 4,
            'capture_seconds': 12,
            'interval_seconds': 300, 
            'interval_jitter': 15,
            'score_threshold': 0.55,
            'batch_size': 20,
            
            'hiccup_penalty_minutes': 1, 
            'failure_penalty': 30,
            'intermittent_penalty_minutes': 120,
            'unresponsive_penalty_minutes': 240,
            'loop_penalty': 360,
            'suspended_penalty_minutes': 1440,
            'terminal_penalty_minutes': 10080,
            'silent_penalty_minutes': 60,
            'silent_visual_penalty_minutes': 720,
            
            'intermittent_threshold': 2,
            'unresponsive_threshold': 5,
            
            'distribution_strategy': "Dynamic Dispatch",
            'shuffle_playback': True,
            'quarantine_reserve': 0,
            'quarantine_freq': 0,
            
            'extraction_strategy': {
                'player_client': 'web',
                'ffmpeg_user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'audio_normalization': True,
                'frame_quality': 2
            }
        }
        
        self.monitoring_process = None
        self.is_dirty = None
        self._check_url_is_duplicate = False
        
        self.status_check_timer = QTimer(self)
        self.status_check_timer.timeout.connect(self.update_control_state)
        self.status_check_timer.start(2000)
        
        self.current_sort_mode = ("name", False) 
        self._temp_lat = ""
        self._temp_lon = ""
        self._programmatic_update = False
        
        self.init_ui()
        self.load_config()
        self.setup_auto_sync()

    def setup_auto_sync(self):
        self.auto_sync_timer = QTimer(self)
        self.auto_sync_timer.timeout.connect(self.run_auto_sync)
        self.update_auto_sync_timer()

    def update_auto_sync_timer(self):
        try:
            if CONFIG_FILE.exists():
                cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                settings = cfg.get("sync_engine_settings", {})
                hours = settings.get("scan_interval_hours", 24)
                ms = max(3600000, int(hours * 3600 * 1000))
                self.auto_sync_timer.start(ms)
        except:
            self.auto_sync_timer.start(86400000) 
            
    def run_auto_sync(self):
        if getattr(self, 'auto_sync_worker', None) and self.auto_sync_worker.isRunning():
            return
        logging.info("Starting scheduled background Sync Engine scan...")
        self.auto_sync_worker = AutoSyncWorker()
        self.auto_sync_worker.finished.connect(self.on_auto_sync_complete)
        self.auto_sync_worker.start()
        
    def on_auto_sync_complete(self, proposals):
        logging.info(f"Background Sync Engine complete. Found {len(proposals)} proposals.")
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            settings = cfg.get("sync_engine_settings", {})
            auto_heal = settings.get("auto_heal_enabled", False)
            
            remaining_proposals =[]
            updated = False
            
            for p in proposals:
                if auto_heal and p.get("auto_heal_eligible"):
                    old_url = p.get('old_url')
                    old_name = p.get('friendly_name')
                    
                    target_stream = None
                    for s in cfg.get('streams', []):
                        clean_url = re.sub(r'[\?&]variant=\d+', '', s.get('page_url', ''))
                        if clean_url == old_url or s.get('name') == old_name:
                            target_stream = s
                            break
                            
                    if target_stream:
                        new_url = p['new_url']
                        target_stream['page_url'] = new_url
                        target_stream['updated_at'] = time.time()
                        target_stream.pop('disable_reason', None)
                        target_stream.pop('status_reason', None)
                        if p.get('new_channel_name'):
                            target_stream['channel_name'] = p['new_channel_name']
                            if 'channels' not in cfg: cfg['channels'] = {}
                            if p['new_channel_name'] not in cfg['channels']:
                                cfg['channels'][p['new_channel_name']] = p['new_channel']
                        updated = True
                        
                        if stream_migrator:
                            stream_migrator.migrate_stream_data(old_url, new_url)
                        logging.info(f"Auto-Healed: {old_name} -> {new_url}")
                else:
                    remaining_proposals.append(p)
                    
            if updated:
                self._safe_config_write_internal(cfg)
                if not self.is_dirty:
                    self.load_config() 
                    
            SYNC_PROPOSALS_FILE.write_text(json.dumps(remaining_proposals, indent=2), encoding='utf-8')
            
        except Exception as e:
            logging.error(f"Failed to process background sync results: {e}")

    def _safe_config_write_internal(self, cfg_dict):
        try:
            tmp_file = CONFIG_FILE.with_suffix('.tmp')
            with open(tmp_file, 'w', encoding='utf-8') as f:
                json.dump(cfg_dict, f, indent=2)
            os.replace(tmp_file, CONFIG_FILE)
        except Exception as e:
            logging.error(f"Failed internal atomic config write: {e}")

    def init_ui(self):
        self.setWindowTitle("Global Birdsong Radio - Configuration & Control")
        self.setGeometry(100, 100, 950, 750)
        self.main_layout = QVBoxLayout(self)
        
        self.control_group = QGroupBox("Engine Control")
        control_layout = QHBoxLayout(self.control_group)
        self.start_button = QPushButton("START Monitoring")
        self.start_button.setStyleSheet("background-color: #4CAF50; color: white; font-size: 14px; padding: 8px; font-weight: bold;")
        self.start_button.clicked.connect(self.start_monitoring)
        
        self.stop_button = QPushButton("STOP Monitoring")
        self.stop_button.setStyleSheet("background-color: #f44336; color: white; font-size: 14px; padding: 8px; font-weight: bold;")
        self.stop_button.clicked.connect(self.stop_monitoring)
        
        self.btn_wifi_watchdog = QPushButton("🛡️ Start Wi-Fi Watchdog")
        self.btn_wifi_watchdog.setStyleSheet("background-color: #00BCD4; color: black; font-size: 14px; padding: 8px; font-weight: bold;")
        self.btn_wifi_watchdog.clicked.connect(self.start_wifi_watchdog)
        
        self.btn_maintenance = QPushButton("☁️ Set Map to Maintenance Mode")
        self.btn_maintenance.setCheckable(True)
        self.btn_maintenance.clicked.connect(self.toggle_maintenance_mode)
        self.update_maintenance_button_style(False)
        
        self.status_label = QLabel("Status: STOPPED")
        font = self.status_label.font()
        font.setPointSize(14)
        font.setBold(True)
        self.status_label.setFont(font)
        
        control_layout.addWidget(self.start_button)
        control_layout.addWidget(self.stop_button)
        control_layout.addWidget(self.btn_wifi_watchdog)
        control_layout.addSpacing(15)
        control_layout.addWidget(self.btn_maintenance)
        control_layout.addStretch()
        control_layout.addWidget(self.status_label)
        self.main_layout.addWidget(self.control_group)

        self.main_content_widget = QWidget()
        content_layout = QVBoxLayout(self.main_content_widget)
        self.main_layout.addWidget(self.main_content_widget)

        locations_group = QGroupBox("File Locations")
        locations_layout = QFormLayout(locations_group)
        locations_layout.addRow(QLabel(f"<b>Config File:</b> {CONFIG_FILE.resolve()}"))
        locations_layout.addRow(QLabel(f"<b>Database File:</b> {DATABASE_PATH.resolve()}"))
        content_layout.addWidget(locations_group)

        settings_group = QGroupBox("Global Default Settings")
        settings_layout = QVBoxLayout(settings_group)
        content_layout.addWidget(settings_group)
        
        telegram_grid = QHBoxLayout()
        self.bot_token_edit = QLineEdit()
        self.chat_id_edit = QLineEdit()
        
        self.test_telegram_button = QPushButton("Test")
        self.test_telegram_button.clicked.connect(self.test_telegram)
        
        telegram_grid.addWidget(QLabel("Telegram Bot Token:"))
        telegram_grid.addWidget(self.bot_token_edit)
        telegram_grid.addWidget(QLabel("Chat ID:"))
        telegram_grid.addWidget(self.chat_id_edit)
        telegram_grid.addWidget(self.test_telegram_button)
        settings_layout.addLayout(telegram_grid)
        
        monitoring_params_group = QGroupBox("Core Monitoring Parameters")
        monitoring_params_outer_layout = QHBoxLayout(monitoring_params_group)

        self.lbl_engine_status = QLabel("Loading...")
        self.lbl_engine_status.setStyleSheet("font-size: 13px; font-weight: bold; color: #333; padding: 5px; border: 1px solid #ccc; background: #f0f0f0; border-radius: 4px;")
        monitoring_params_outer_layout.addWidget(self.lbl_engine_status, 2)

        self.show_windows_cb = QCheckBox("Show Listener Windows on Start")
        monitoring_params_outer_layout.addWidget(self.show_windows_cb, 0, Qt.AlignmentFlag.AlignCenter)

        self.btn_configure_engine = QPushButton("⚙ Configure Engine & Network...")
        self.btn_configure_engine.setStyleSheet("font-weight: bold; padding: 6px;")
        self.btn_configure_engine.clicked.connect(self.open_engine_config_dialog)
        monitoring_params_outer_layout.addWidget(self.btn_configure_engine, 1)
        
        settings_layout.addWidget(monitoring_params_group)

        self.browser_group = QGroupBox("YouTube Session Management (Recommended)")
        self.browser_group.setCheckable(True)
        browser_layout = QFormLayout(self.browser_group)
        
        self.browser_controls_widget = QWidget()
        controls_layout = QFormLayout(self.browser_controls_widget)
        controls_layout.setContentsMargins(0, 5, 0, 0)
        profile_layout = QHBoxLayout()
        self.profile_path_edit = QLineEdit()
        self.profile_path_edit.setPlaceholderText("e.g., C:/Users/YourUser/AppData/Local/Google/Chrome/User Data/Profile 2")
        self.profile_browse_button = QPushButton("Browse...")
        self.profile_browse_button.clicked.connect(self.browse_profile)
        profile_layout.addWidget(self.profile_path_edit)
        profile_layout.addWidget(self.profile_browse_button)
        controls_layout.addRow("Path to Chrome User Profile Directory:", profile_layout)
        
        driver_layout = QHBoxLayout()
        self.driver_path_edit = QLineEdit()
        self.driver_path_edit.setPlaceholderText("e.g., C:/.../chromedriver.exe")
        self.driver_browse_button = QPushButton("Browse...")
        self.driver_browse_button.clicked.connect(self.browse_driver)
        driver_layout.addWidget(self.driver_path_edit)
        driver_layout.addWidget(self.driver_browse_button)
        controls_layout.addRow("Path to ChromeDriver Executable:", driver_layout)
        browser_layout.addRow(self.browser_controls_widget)
        self.browser_group.toggled.connect(self.browser_controls_widget.setEnabled)
        settings_layout.addWidget(self.browser_group)

        config_buttons_group = QGroupBox("Configuration Sections")
        config_buttons_layout = QHBoxLayout(config_buttons_group)
        content_layout.addWidget(config_buttons_group)
        
        self.configure_cooldown_button = QPushButton("Configure Tiered Cooldowns...")
        self.configure_cooldown_button.clicked.connect(self.open_cooldown_dialog)
        config_buttons_layout.addWidget(self.configure_cooldown_button)
        
        self.advanced_settings_button = QPushButton("Configure Advanced Settings...")
        self.advanced_settings_button.clicked.connect(self.open_advanced_settings_dialog)
        config_buttons_layout.addWidget(self.advanced_settings_button)
        
        self.audit_button = QPushButton("Stream Audit & Diagnostics")
        self.audit_button.clicked.connect(self.open_audit_dialog)
        config_buttons_layout.addWidget(self.audit_button)

        self.discovery_button = QPushButton("📡 Stream Maintenance & Discovery Hub")
        self.discovery_button.setStyleSheet("background-color: #00897B; color: white; font-weight: bold;")
        self.discovery_button.clicked.connect(self.open_discovery_hub)
        config_buttons_layout.addWidget(self.discovery_button)
        
        self.housekeeping_button = QPushButton("🧹 Log & Storage Housekeeping")
        self.housekeeping_button.setStyleSheet("background-color: #607D8B; color: white; font-weight: bold;")
        self.housekeeping_button.clicked.connect(self.open_housekeeping_dialog)
        config_buttons_layout.addWidget(self.housekeeping_button)

        config_buttons_layout.addStretch()

        bottom_h_layout = QHBoxLayout()
        content_layout.addLayout(bottom_h_layout)
        stream_list_group = QGroupBox("Monitored Streams")
        stream_list_layout = QVBoxLayout(stream_list_group)
        bottom_h_layout.addWidget(stream_list_group, 1)
        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("Search:"))
        self.stream_search_box = QLineEdit()
        self.stream_search_box.textChanged.connect(self._filter_stream_list)
        search_layout.addWidget(self.stream_search_box)
        
        self.lbl_stream_count = QLabel("(0/0)")
        self.lbl_stream_count.setStyleSheet("color: #888888; font-weight: bold;")
        search_layout.addWidget(self.lbl_stream_count)
        
        self.btn_sort = QToolButton()
        self.btn_sort.setText(f"Sort: Name (A-Z) ▼")
        self.btn_sort.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_sort.setMenu(self.create_sort_menu())
        search_layout.addWidget(self.btn_sort)
        
        stream_list_layout.addLayout(search_layout)
        self.stream_list_widget = QListWidget()
        
        self.stream_list_widget.setUniformItemSizes(True) 
        self.stream_list_widget.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        
        self.stream_list_widget.currentItemChanged.connect(self.populate_form_from_selection)
        self.stream_list_widget.itemClicked.connect(lambda item: self.populate_form_from_selection(item, None))
        
        stream_list_layout.addWidget(self.stream_list_widget)
        stream_buttons_layout = QHBoxLayout()
        self.check_all_button = QPushButton("Check All")
        self.uncheck_all_button = QPushButton("Uncheck All")
        self.reset_overrides_button = QPushButton("Reset Selected Overrides")
        self.remove_stream_button = QPushButton("Remove Selected Stream")
        stream_buttons_layout.addWidget(self.check_all_button)
        stream_buttons_layout.addWidget(self.uncheck_all_button)
        stream_buttons_layout.addStretch()
        stream_buttons_layout.addWidget(self.reset_overrides_button)
        stream_buttons_layout.addWidget(self.remove_stream_button)
        self.check_all_button.clicked.connect(lambda: self.set_all_streams_enabled(True))
        self.uncheck_all_button.clicked.connect(lambda: self.set_all_streams_enabled(False))
        self.reset_overrides_button.clicked.connect(self.reset_all_overrides)
        self.remove_stream_button.clicked.connect(self.remove_stream)
        stream_list_layout.addLayout(stream_buttons_layout)
        
        # --- STREAM EDITING ---
        add_stream_group = QGroupBox("Add / Edit Stream")
        add_stream_layout = QVBoxLayout(add_stream_group)
        bottom_h_layout.addWidget(add_stream_group, 1)
        add_form_layout = QGridLayout()
        self.stream_name_edit = QLineEdit()
        
        self.stream_url_edit = QLineEdit()
        self.stream_url_edit.textChanged.connect(self.check_stream_url_input)
        
        self.magic_wand_button = QToolButton()
        self.magic_wand_button.setText("🪄")
        self.magic_wand_button.setToolTip("Resolve Stream (Required for non-YouTube links)")
        self.magic_wand_button.setEnabled(False) 
        self.magic_wand_button.clicked.connect(self.resolve_stream)
        self.magic_wand_button.setStyleSheet("font-size: 16px; border: none;")
        
        url_layout = QHBoxLayout()
        url_layout.addWidget(self.stream_url_edit)
        url_layout.addWidget(self.magic_wand_button)
        
        self.stream_lat_edit = QLineEdit()
        self.stream_lon_edit = QLineEdit()
        self.stream_lat_edit.textChanged.connect(self._handle_smart_lat_paste)
        
        self.capture_override_edit = QLineEdit()
        self.threshold_override_edit = QLineEdit()
        
        self.stream_channel_edit = QComboBox()
        self.stream_channel_edit.setEditable(True)
        self.stream_channel_edit.lineEdit().setPlaceholderText("Optional")
        
        self.capture_override_edit.setFixedWidth(60)
        self.threshold_override_edit.setFixedWidth(60)
        
        self.stream_lat_edit.setFixedWidth(80)
        self.stream_lon_edit.setFixedWidth(80)
        self.global_cb = QCheckBox("Global")
        self.global_cb.setToolTip("Check this to disable regional filtering. Useful for remote islands or extreme latitudes.")
        self.global_cb.toggled.connect(self.toggle_global_mode)
        
        add_form_layout.addWidget(QLabel("Display Name:"), 0, 0)
        add_form_layout.addWidget(self.stream_name_edit, 0, 1)
        add_form_layout.addWidget(QLabel("Page/Stream URL:"), 1, 0)
        add_form_layout.addLayout(url_layout, 1, 1)
        
        coords_layout = QHBoxLayout()
        coords_layout.addWidget(QLabel("Latitude:"))
        coords_layout.addWidget(self.stream_lat_edit)
        coords_layout.addWidget(QLabel("Longitude:"))
        coords_layout.addWidget(self.stream_lon_edit)
        coords_layout.addWidget(self.global_cb)
        
        self.open_map_button = QPushButton("Show Map")
        self.open_map_button.clicked.connect(self.open_map_from_form)
        coords_layout.addWidget(self.open_map_button)
        
        self.chk_mute_audio = QCheckBox("Mute Audio")
        self.chk_mute_audio.setToolTip("Disable audio processing (BirdNET/FFmpeg) for this stream. Useful for underwater or vision-only cameras.")
        self.chk_mute_audio.toggled.connect(self._check_and_update_dirty_state)
        coords_layout.addWidget(self.chk_mute_audio)
        
        add_form_layout.addLayout(coords_layout, 2, 0, 1, 2)
        
        overrides_layout = QHBoxLayout()
        self.btn_channel_manager = QPushButton("Channel Name:")
        self.btn_channel_manager.setStyleSheet("background-color: #5C6BC0; color: white; font-weight: bold; padding: 2px 8px; border-radius: 3px;")
        self.btn_channel_manager.setToolTip("Open Global Channel Manager")
        self.btn_channel_manager.clicked.connect(self.open_channel_manager)
        
        overrides_layout.addWidget(self.btn_channel_manager)
        overrides_layout.addWidget(self.stream_channel_edit, 1)
        overrides_layout.addWidget(QLabel("Capture (s):"))
        overrides_layout.addWidget(self.capture_override_edit)
        overrides_layout.addWidget(QLabel("Threshold (%):"))
        overrides_layout.addWidget(self.threshold_override_edit)
        add_form_layout.addLayout(overrides_layout, 3, 0, 1, 2)
        
        add_stream_layout.addLayout(add_form_layout)
        add_button_layout = QHBoxLayout()
        self.clear_form_button = QPushButton("Clear Form / New")
        self.open_url_button = QPushButton("Open URL")
        self.check_stream_button = QPushButton("Check URL")
        add_button_layout.addWidget(self.clear_form_button)
        add_button_layout.addWidget(self.open_url_button)
        add_button_layout.addWidget(self.check_stream_button)
        self.clear_form_button.clicked.connect(self.clear_form)
        self.open_url_button.clicked.connect(self.open_stream_url)
        self.check_stream_button.clicked.connect(self.check_stream)
        add_stream_layout.addLayout(add_button_layout)
        
        self.check_status_label = QLabel("<i>Status: Ready</i>")
        self.check_status_label.setWordWrap(False) 
        self.check_status_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        add_stream_layout.addWidget(self.check_status_label)
        
        add_stream_layout.addStretch()
        
        update_add_layout = QHBoxLayout()
        self.update_stream_button = QPushButton("Update Selected Stream")
        self.add_stream_button = QPushButton("Add as New Stream")
        update_add_layout.addWidget(self.update_stream_button)
        update_add_layout.addWidget(self.add_stream_button)
        self.update_stream_button.clicked.connect(self.update_stream)
        self.add_stream_button.clicked.connect(self.add_stream)
        add_stream_layout.addLayout(update_add_layout)
        
        bottom_buttons_layout = QHBoxLayout()
        content_layout.addLayout(bottom_buttons_layout)
        self.reset_button = QPushButton("Reset...")
        reset_menu = QMenu()
        revert_action = QAction("Revert Unsaved Changes", self)
        reset_settings_action = QAction("Reset All Settings (Keep My Streams)", self)
        factory_reset_action = QAction("Factory Reset (Deletes All)", self)
        revert_action.triggered.connect(self._revert_ui_to_saved_state)
        reset_settings_action.triggered.connect(self._reset_settings_keep_streams)
        factory_reset_action.triggered.connect(self._factory_reset)
        reset_menu.addAction(revert_action)
        reset_menu.addAction(reset_settings_action)
        reset_menu.addSeparator()
        reset_menu.addAction(factory_reset_action)
        self.reset_button.setMenu(reset_menu)
        
        self.force_sync_button = QPushButton("☁️ Force Sync to Cloud")
        self.force_sync_button.setStyleSheet("background-color: #9C27B0; color: white; font-weight: bold; padding: 8px; font-size: 14px;")
        self.force_sync_button.clicked.connect(self.force_cloud_sync)
        
        self.save_button = QPushButton("Save All Configuration to File")
        self.save_button.clicked.connect(self.show_save_confirmation)
        
        bottom_buttons_layout.addWidget(self.reset_button)
        bottom_buttons_layout.addStretch()
        bottom_buttons_layout.addWidget(self.force_sync_button)
        bottom_buttons_layout.addWidget(self.save_button)
        
        self.update_control_state()
        self._connect_dirty_signals()

    def start_wifi_watchdog(self):
        bat_path = ROOT / "Network" / "wifi_watchdog.bat"
        if not bat_path.exists():
            QMessageBox.warning(self, "Missing File", f"Cannot find the batch file at:\n{bat_path}")
            return
        try:
            if sys.platform == "win32":
                import ctypes
                QApplication.clipboard().setText(f'"{bat_path}"')
                msg = "Right-click to paste the path to wifi_watchdog.bat and press Enter! Start-up takes about 30 seconds."
                cmd_args = f'/k echo ================================================================================================= & echo {msg} & echo ================================================================================================='
                ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", cmd_args, str(ROOT), 1)
            else:
                QMessageBox.information(self, "OS Error", "This script is explicitly designed for Windows.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to start Wi-Fi Watchdog: {e}")

    def _populate_channel_dropdown(self):
        channels = set()
        for i in range(self.stream_list_widget.count()):
            d = self.stream_list_widget.item(i).data(Qt.ItemDataRole.UserRole)
            cn = d.get("channel_name", "").strip()
            if cn: channels.add(cn)
        for cn in self.unsaved_channels.keys():
            if cn.strip(): channels.add(cn.strip())
        
        curr_text = self.stream_channel_edit.currentText()
        self.stream_channel_edit.blockSignals(True)
        self.stream_channel_edit.clear()
        self.stream_channel_edit.addItems(sorted(list(channels)))
        self.stream_channel_edit.setCurrentText(curr_text)
        self.stream_channel_edit.blockSignals(False)

    def _handle_smart_lat_paste(self, text):
        if self._loading: return
        if ',' in text:
            try:
                parts = text.split(',')
                if len(parts) == 2:
                    lat_val = float(parts[0].strip())
                    lon_val = float(parts[1].strip())
                    self.stream_lat_edit.blockSignals(True)
                    self.stream_lat_edit.setText(f"{lat_val:.5f}")
                    self.stream_lon_edit.setText(f"{lon_val:.5f}")
                    self.stream_lat_edit.blockSignals(False)
                    self._check_and_update_dirty_state()
            except ValueError:
                pass 
        else:
            self._check_and_update_dirty_state()

    def open_channel_manager(self):
        if not hasattr(self, 'channel_manager_dialog') or not self.channel_manager_dialog.isVisible():
            self.channel_manager_dialog = ChannelManagerDialog(self)
            self.channel_manager_dialog.show()
        else:
            self.channel_manager_dialog.raise_()
            self.channel_manager_dialog.activateWindow()

    def open_discovery_hub(self):
        if self.is_dirty:
            QMessageBox.warning(self, "Unsaved Changes", "Please save your changes before opening the Maintenance & Discovery Hub.")
            return
        try:
            import importlib
            import stream_discovery_gui
            importlib.reload(stream_discovery_gui)
            
            if not hasattr(self, 'discovery_state_cache'):
                self.discovery_state_cache = {}
                
            self.discovery_window = stream_discovery_gui.StreamDiscoveryHub(editor_ref=self, parent=None)
            self.discovery_window.show()
            self.discovery_window.raise_()
            self.discovery_window.activateWindow()
        except ImportError:
            QMessageBox.critical(self, "Error", "stream_discovery_gui.py not found. Please ensure the file is in the same directory.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to open Discovery Hub: {e}")

    def update_maintenance_button_style(self, checked):
        if checked:
            self.btn_maintenance.setText("⚠️ Map is in Maintenance Mode (Click to clear)")
            self.btn_maintenance.setStyleSheet("background-color: #FF9800; color: black; font-size: 14px; padding: 8px; font-weight: bold;")
        else:
            self.btn_maintenance.setText("☁️ Set Map to Maintenance Mode")
            self.btn_maintenance.setStyleSheet("background-color: #607D8B; color: white; font-size: 14px; padding: 8px; font-weight: bold;")

    def toggle_maintenance_mode(self, checked):
        self.update_maintenance_button_style(checked)
        self.save_config(show_confirmation=False)
        
        bot_token = self.bot_token_edit.text().strip()
        if not bot_token:
            QMessageBox.warning(self, "Missing Token", "Cannot sync: Telegram Bot Token is required for cloud communication.")
            self.btn_maintenance.setChecked(not checked) 
            self.update_maintenance_button_style(not checked)
            self.save_config(show_confirmation=False)
            return
            
        self.btn_maintenance.setEnabled(False)
        QApplication.processEvents()
        
        try:
            with open(CONFIG_FILE, 'rb') as f:
                file_content = f.read()
                
            sync_url = "https://wilddetection.com/api/upload_config"
            files = {'config_file': ('birdnet_config.json', file_content, 'application/json')}
            data = {'secret_token': bot_token}
            
            response = requests.post(sync_url, files=files, data=data, timeout=15, verify=True)
            
            if response.status_code == 200:
                status_text = "ENABLED" if checked else "DISABLED"
                QMessageBox.information(self, "Maintenance Mode", f"Maintenance Mode successfully {status_text} and synced to the Cloud Map!")
            else:
                QMessageBox.critical(self, "Sync Failed", f"Server rejected the upload.\nHTTP {response.status_code}: {response.text}")
                self.btn_maintenance.setChecked(not checked)
                self.update_maintenance_button_style(not checked)
                self.save_config(show_confirmation=False)
        except Exception as e:
            QMessageBox.critical(self, "Network Error", f"Failed to sync to cloud: {e}")
            self.btn_maintenance.setChecked(not checked)
            self.update_maintenance_button_style(not checked)
            self.save_config(show_confirmation=False)
        finally:
            self.btn_maintenance.setEnabled(True)

    def check_stream_url_input(self):
        if self._loading: return
        
        url = self.stream_url_edit.text().strip().lower()
        is_youtube = "youtube.com" in url or "youtu.be" in url
        is_empty = not url
        
        if is_empty:
            self.magic_wand_button.setEnabled(False)
            self.magic_wand_button.setStyleSheet("border: none;")
            return

        has_variant = "?variant=" in url or "&variant=" in url
        is_already_resolved = ((url == self._pending_original_url.lower()) and self._pending_resolved_url) or has_variant

        if is_youtube:
            self.update_stream_button.setEnabled(True)
            self.add_stream_button.setEnabled(True)
            self.magic_wand_button.setEnabled(False)
            self.magic_wand_button.setStyleSheet("border: none; opacity: 0.3;")
            self._pending_stream_type = "youtube"
            self._pending_original_url = "" 
            self._pending_resolved_url = ""
        elif is_already_resolved:
            self.update_stream_button.setEnabled(True)
            self.add_stream_button.setEnabled(True)
            self.magic_wand_button.setEnabled(True)
            self.magic_wand_button.setStyleSheet("border: none; background-color: #66BB6A; border-radius: 4px;") 
        else:
            self.update_stream_button.setEnabled(False)
            self.add_stream_button.setEnabled(False)
            self.magic_wand_button.setEnabled(True)
            self.magic_wand_button.setStyleSheet("border: none; background-color: #FFA726; border-radius: 4px;") 
            self.magic_wand_button.setToolTip("Click to Resolve this Non-YouTube Stream")
            
            if ".m3u8" in url or ".mp4" in url or ".mp3" in url:
                self.magic_wand_button.setStyleSheet("border: none; background-color: #66BB6A; border-radius: 4px;") 
                self.update_stream_button.setEnabled(True)
                self.add_stream_button.setEnabled(True)
                self._pending_resolved_url = url

    def resolve_stream(self):
        url = self.stream_url_edit.text().strip()
        if not url: return
        
        self.magic_wand_button.setEnabled(False)
        self.check_status_label.setText("<i>Resolving stream... please wait...</i>")
        
        self.resolver_thread = ResolutionThread(url)
        self.resolver_thread.result_ready.connect(self.on_resolution_complete)
        self.resolver_thread.start()

    def on_resolution_complete(self, success, links, stype, msg, title):
        self.magic_wand_button.setEnabled(True)
        if success:
            selected_link = links[0]
            
            if len(links) > 1:
                item, ok = QInputDialog.getItem(
                    self, "Select Stream", 
                    f"Found {len(links)} streams. Please select one:", 
                    links, 0, False
                )
                if ok and item:
                    selected_link = item
                else:
                    self.check_status_label.setText("<i>Selection Cancelled</i>")
                    return 

            self._pending_original_url = self.stream_url_edit.text().strip()
            self._pending_stream_type = stype
            self._pending_resolved_url = selected_link
            
            # Step 2.1: Actively cache the fetched title
            if title:
                self._pending_yt_title = title
            
            self.magic_wand_button.setStyleSheet("border: none; background-color: #66BB6A; border-radius: 4px;") 
            self.check_status_label.setText(f"<i>Resolved: {stype}</i>")
            
            self.update_stream_button.setEnabled(True)
            self.add_stream_button.setEnabled(True)
        else:
            self.magic_wand_button.setStyleSheet("border: none; background-color: #EF5350; border-radius: 4px;") 
            self.check_status_label.setText(f"<i>Resolution Failed</i>")
            
            d = QDialog(self)
            d.setWindowTitle("Resolution Error Log")
            d.resize(600, 400)
            lay = QVBoxLayout(d)
            txt = QTextEdit()
            txt.setText(msg)
            txt.setReadOnly(True)
            lay.addWidget(QLabel("The resolver failed. Please copy this log and send it to the developer:"))
            lay.addWidget(txt)
            btn = QPushButton("Close")
            btn.clicked.connect(d.accept)
            lay.addWidget(btn)
            d.exec()

    def open_audit_dialog(self):
        stream_data =[]
        for i in range(self.stream_list_widget.count()):
            stream_data.append(self.stream_list_widget.item(i).data(Qt.ItemDataRole.UserRole))
        
        dialog = StreamAuditDialog(
            stream_data, 
            self.unsaved_report_config, 
            self.unsaved_send_audio_all,
            self.unsaved_send_audio_dsp,
            self.unsaved_send_audio_multi,
            self.unsaved_send_audio_birdnet,
            self
        )
        
        if dialog.exec():
            (self.unsaved_report_config, 
             self.unsaved_send_audio_all,
             self.unsaved_send_audio_dsp,
             self.unsaved_send_audio_multi,
             self.unsaved_send_audio_birdnet) = dialog.get_report_config()
            self.is_dirty = None
            self._check_and_update_dirty_state()

    def open_engine_config_dialog(self):
        d = EngineConfigDialog(self.engine_params, self.unsaved_network_map, self.unsaved_loop_config, self.engine_params['total_listeners'], self)
        if d.exec():
            new_params, new_listeners, new_loop, new_net_map = d.get_results()
            self.engine_params.update(new_params)
            self.engine_params['total_listeners'] = new_listeners
            self.unsaved_loop_config = new_loop
            self.unsaved_network_map = new_net_map
            self.update_engine_status_label()
            
            self.save_config(show_confirmation=False)
            self.update_auto_sync_timer()
            
    def open_housekeeping_dialog(self):
        hk_cfg = self.unsaved_housekeeping_config
        debug_on = self.unsaved_debug_logging
        d = HousekeepingManagerDialog(debug_on, hk_cfg, self)
        if d.exec():
            new_debug, new_hk_conf = d.get_values()
            self.unsaved_debug_logging = new_debug
            self.unsaved_housekeeping_config = new_hk_conf
            self.is_dirty = None
            self._check_and_update_dirty_state()

    def update_engine_status_label(self):
        l = self.engine_params['total_listeners']
        i = self.engine_params['interval_seconds'] 
        j = self.engine_params['interval_jitter']
        nm = len(self.unsaved_network_map)
        cap = self.engine_params['capture_seconds']
        throughput = int((l * 60) / (cap + 3))
        txt = f"Listeners: {l} | Target Cycle: {i}s | Network: {nm} Bindings | Capacity: ~{throughput} Scans/Min"
        self.lbl_engine_status.setText(txt)

    def create_sort_menu(self):
        menu = QMenu(self)
        actions =[
            ("Name (A-Z)", "name", False),
            ("Name (Z-A)", "name", True),
            ("Created (Newest)", "created_at", True),
            ("Created (Oldest)", "created_at", False),
            ("Modified (Newest)", "updated_at", True),
            ("Modified (Oldest)", "updated_at", False),
            ("Status (Enabled First)", "enabled", True),
            ("Status (Disabled First)", "enabled", False),
            ("Mute Audio (Muted First)", "mute_audio", True),
            ("Mute Audio (Unmuted First)", "mute_audio", False),
            ("Global (Global First)", "global", True),
            ("Global (Local First)", "global", False)
        ]
        for label, key, reverse in actions:
            action = QAction(label, self)
            action.triggered.connect(partial(self.sort_streams, key, reverse, label))
            menu.addAction(action)
        return menu

    def sort_streams(self, key, reverse, label):
        self.current_sort_mode = (key, reverse)
        arrow = "▼" if reverse else "▲"
        self.btn_sort.setText(f"Sort: {label} {arrow}")
        self._sort_stream_list()
        self._save_gui_preferences()

    def _sort_stream_list(self):
        v_scroll = self.stream_list_widget.verticalScrollBar().value()
        curr_item = self.stream_list_widget.currentItem()
        curr_url = curr_item.data(Qt.ItemDataRole.UserRole).get('page_url') if curr_item else None

        item_data_list =[]
        for i in range(self.stream_list_widget.count()):
            item = self.stream_list_widget.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            check_state = item.checkState()
            
            item_text = data.get('name', 'Unnamed') 
            item_data_list.append((item_text, check_state, data))
        
        def get_sort_key(tuple_item):
            text, state, data = tuple_item
            key, reverse = self.current_sort_mode
            val = data.get(key)
            if key == 'name': return val.lower() if val else ""
            if key == 'enabled': return state.value 
            if key == 'mute_audio': return bool(data.get('mute_audio', False))
            if key == 'global': 
                try: lat = float(data.get('lat', 0.0))
                except: lat = 0.0
                try: lon = float(data.get('lon', 0.0))
                except: lon = 0.0
                return (lat == 0.0 and lon == 0.0)
            if key == 'created_at' or key == 'updated_at':
                try: return float(val) if val is not None else 0.0
                except (ValueError, TypeError): return 0.0
            if val is None: return 0 if not reverse else float('inf') 
            return val

        item_data_list.sort(key=get_sort_key, reverse=self.current_sort_mode[1])

        self._loading = True
        
        self.stream_list_widget.blockSignals(True)
        self.stream_list_widget.clear()
        
        item_to_select = None
        
        for idx, (text, state, data) in enumerate(item_data_list):
            new_item = QListWidgetItem(text)
            new_item.setFlags(new_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            new_item.setCheckState(state)
            new_item.setData(Qt.ItemDataRole.UserRole, data)
            self.stream_list_widget.addItem(new_item)
            
            if data.get('page_url') == curr_url:
                item_to_select = new_item
                
        if item_to_select:
            self.stream_list_widget.setCurrentItem(item_to_select)
            
        self.stream_list_widget.blockSignals(False)
        self.stream_list_widget.verticalScrollBar().setValue(v_scroll)
        
        self._loading = False
        self._filter_stream_list() 
        self._populate_channel_dropdown()

    def update_stream_list_widget(self):
        curr_item = self.stream_list_widget.currentItem()
        curr_url = curr_item.data(Qt.ItemDataRole.UserRole).get('page_url') if curr_item else None
        
        self._loading = True
        self.stream_list_widget.blockSignals(True)
        self.stream_list_widget.clear()
        
        for stream in self.saved_config_data.get("streams",[]):
            item = QListWidgetItem(stream.get("name", "Unnamed")) 
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if stream.get("enabled", True) else Qt.CheckState.Unchecked)
            if 'created_at' not in stream: stream['created_at'] = None
            if 'updated_at' not in stream: stream['updated_at'] = None
            item.setData(Qt.ItemDataRole.UserRole, copy.deepcopy(stream))
            self.stream_list_widget.addItem(item)
            
            if stream.get('page_url') == curr_url:
                self.stream_list_widget.setCurrentItem(item)
                
        self.stream_list_widget.blockSignals(False)
        self._sort_stream_list()
        self._loading = False

    def apply_audit_updates(self, updated_streams):
        url_map = {s['page_url']: s for s in updated_streams}
        self._loading = True
        for i in range(self.stream_list_widget.count()):
            item = self.stream_list_widget.item(i)
            current_data = item.data(Qt.ItemDataRole.UserRole)
            url = current_data.get('page_url')
            if url in url_map:
                current_data['created_at'] = url_map[url].get('created_at')
                item.setData(Qt.ItemDataRole.UserRole, current_data)
        self._loading = False
        self._check_and_update_dirty_state()
        self._sort_stream_list()

    def check_scheduler_process(self):
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if 'python' in p.info['name'].lower() and p.info['cmdline'] and any("scheduler.py" in cmd.lower() for cmd in p.info['cmdline']): return p.pid
            except: pass
        return None

    def update_control_state(self):
        pid = self.check_scheduler_process()
        if pid:
            self.status_label.setText(f"Status: RUNNING (PID: {pid})")
            self.status_label.setStyleSheet("color: green; font-weight: bold;")
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
        else:
            self.status_label.setText("Status: STOPPED")
            self.status_label.setStyleSheet("color: red; font-weight: bold;")
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)

    def start_monitoring(self):
        if self.is_dirty: QMessageBox.warning(self, "Unsaved Changes", "Please save your changes before starting."); return
        if self.check_scheduler_process(): QMessageBox.warning(self, "Warning", "Monitoring is already running."); self.update_control_state(); return
        if not self._can_start_monitoring(): return
        try:
            if COOKIE_WARNING_STATE_PATH.exists(): COOKIE_WARNING_STATE_PATH.unlink()
        except Exception as e: pass
        try:
            ex = sys.executable.replace("pythonw.exe", "python.exe") if "pythonw.exe" in sys.executable.lower() else sys.executable
            flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
            subprocess.Popen([ex, str(SCHEDULER_SCRIPT), str(self.show_windows_cb.isChecked())], **flags)
            send_and_log_system_alert(self.bot_token_edit.text().strip(), self.chat_id_edit.text().strip(), "[Global Birdsong Radio] Monitoring system started.", 'MONITOR_START')
            self.status_label.setText("Status: STARTING..."); self.status_label.setStyleSheet("color: orange;"); self.start_button.setEnabled(False); QTimer.singleShot(1500, self.update_control_state)
        except Exception as e:
            logging.error(f"Failed to start: {e}", exc_info=True)
            QMessageBox.critical(self, "Error", f"Failed to start:\n{e}")

    def stop_monitoring(self, silent=False):
        if self.monitoring_process:
            try:
                if sys.platform == "win32": subprocess.run(f"TASKKILL /F /T /PID {self.monitoring_process.pid}", check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                else: self.monitoring_process.terminate()
            except Exception: pass
            self.monitoring_process = None
        pid = self.check_scheduler_process()
        if pid:
            try:
                p = psutil.Process(pid)
                for c in p.children(recursive=True): c.kill()
                p.kill()
            except Exception as e: logging.error(f"Error killing process {pid}: {e}")
        if not silent: send_and_log_system_alert(self.bot_token_edit.text().strip(), self.chat_id_edit.text().strip(), "[Global Birdsong Radio] Monitoring stopped by user.", 'MONITOR_STOP')
        self.status_label.setText("Status: STOPPING..."); self.status_label.setStyleSheet("color: orange;"); self.stop_button.setEnabled(False); QTimer.singleShot(1500, self.update_control_state)

    def _can_start_monitoring(self):
        curr = self._get_config_from_ui(); b = curr.get("browser_automation", {}); c = curr.get("youtube_cookies_file", "").strip()
        if b.get("enabled", False) and b.get("chrome_profile_path") and b.get("webdriver_path"): return True 
        if c and os.path.exists(c):
            return QMessageBox.warning(self, "Monitoring Fallback", "Browser Automation not fully configured. Using cookies.txt fallback.\n\nContinue?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
        QMessageBox.critical(self, "Cannot Start", "No YouTube access configured."); return False

    def _save_gui_preferences(self):
        s = self._get_gui_settings_from_ui()
        s['sort_key'] = self.current_sort_mode[0]
        s['sort_reverse'] = self.current_sort_mode[1]
        try: GUI_SETTINGS_FILE.write_text(json.dumps(s, indent=2))
        except: pass
    
    @staticmethod
    def extract_youtube_id(url):
        if not url: return None
        m = re.search(r'(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/(watch\?v=|embed/|v/|live/|shorts/|.+\?v=)?([^&=%\?]{11})', url)
        return m.group(6) if m else None
        
    def _connect_dirty_signals(self):
        for w in[self.bot_token_edit, self.chat_id_edit, self.profile_path_edit, self.driver_path_edit]: w.textChanged.connect(self._check_and_update_dirty_state)
        self.show_windows_cb.toggled.connect(self._check_and_update_dirty_state); self.browser_group.toggled.connect(self._check_and_update_dirty_state)
        self.stream_list_widget.itemChanged.connect(self._on_item_changed); self.profile_path_edit.textChanged.connect(self._check_and_update_dirty_state); self.driver_path_edit.textChanged.connect(self._check_and_update_dirty_state)
        self.stream_channel_edit.currentTextChanged.connect(self._check_and_update_dirty_state)
        
    def _on_item_changed(self, item):
        if self._loading: return
        self.stream_list_widget.blockSignals(True)
        try:
            d = item.data(Qt.ItemDataRole.UserRole)
            if d:
                is_checked = (item.checkState() == Qt.CheckState.Checked)
                if d.get('enabled', True) != is_checked:
                    d['enabled'] = is_checked
                    d['updated_at'] = time.time()
                    item.setData(Qt.ItemDataRole.UserRole, d)
                    if self.current_sort_mode[0] in['enabled', 'updated_at']: 
                        self._sort_stream_list()
                    self._check_and_update_dirty_state()
        finally:
            self.stream_list_widget.blockSignals(False)

    def _clean_for_diff(self, cfg):
        clean_cfg = copy.deepcopy(cfg)
        if 'streams' in clean_cfg:
            for stream in clean_cfg['streams']:
                stream.pop('updated_at', None)
        return clean_cfg

    def _check_and_update_dirty_state(self, *args):
        if self._loading: return
        try:
            curr = self._get_config_from_ui()
        except:
            return

        clean_curr = self._clean_for_diff(curr)
        clean_saved = self._clean_for_diff(self.saved_config_data)
        
        is_diff = json.dumps(clean_curr, sort_keys=True) != json.dumps(clean_saved, sort_keys=True)
        gui_diff = self.show_windows_cb.isChecked() != self.saved_gui_settings.get("show_listener_windows", False)
        self.set_dirty_state(is_diff or gui_diff)

    def set_dirty_state(self, dirty):
        if self.is_dirty == dirty: return
        self.is_dirty = dirty
        self.save_button.setText("Save Changes *" if dirty else "Save All Configuration to File")
        self.save_button.setStyleSheet(f"background-color: {'#f44336' if dirty else '#008CBA'}; color: white; font-size: 16px; padding: 8px;")
        
    def open_cooldown_dialog(self):
        d = TieredCooldownDialog(self.unsaved_tiered_config, self.unsaved_cooldown_widths, self)
        if d.exec(): self.unsaved_tiered_config = d.get_updated_config(); self.is_dirty = None; self._check_and_update_dirty_state()
        
    def open_advanced_settings_dialog(self):
        curr = self._get_config_from_ui()
        cc = { "youtube_cookies_file": self.unsaved_cookies_path if self.unsaved_cookies_path is not None else curr.get("youtube_cookies_file", ""), "cookie_alert": self.unsaved_cookie_alert_config if self.unsaved_cookie_alert_config else curr.get("cookie_alert", {}) }
        
        current_debug = self.unsaved_debug_logging if hasattr(self, 'unsaved_debug_logging') else curr.get("debug_logging", False)
        map_conf = self.unsaved_map_config if self.unsaved_map_config else curr.get("map_settings", {})
        
        d = AdvancedSettingsDialog(
            self.unsaved_loop_config, 
            self.unsaved_distance_config, 
            map_conf, 
            cc, 
            curr.get("browser_automation", {}), 
            current_debug, 
            self
        )
        
        if d.exec():
            updated_map, self.unsaved_distance_config, updated_cookie_config, self.unsaved_debug_logging = d.get_updated_configs()
            self.unsaved_map_config = updated_map 
            
            self.unsaved_cookies_path = updated_cookie_config["youtube_cookies_file"]; self.unsaved_cookie_alert_config = updated_cookie_config["cookie_alert"]
            self.is_dirty = None; self._check_and_update_dirty_state()
            
    def load_config(self):
        self._loading = True
        try:
            default_config = { 
                "bot_token": "", "chat_id": "", "youtube_cookies_file": "", 
                
                "send_audio_telegram_all": False,
                "send_audio_telegram_dsp": True,
                "send_audio_telegram_multi": True,
                "send_audio_telegram_birdnet": False,
                
                "cookie_alert": {"enabled": True, "cooldown_minutes": 60}, 
                "capture_seconds": 12, "interval_seconds": 300, "score_threshold": 0.55, 
                "parallel_listeners": 4, 
                "loop_management": {"enabled": True, "watch_period_hours": 24, "quarantine_check_hours": 12}, 
                "tiered_cooldowns": TieredCooldownDialog.get_default_tiered_config(), 
                "distance_estimation": {"enabled": True, "alert_distances":["Very Near", "Near"]}, 
                
                "map_settings": {
                    "live_window_seconds": 120, "audio_vision_ratio": 70, 
                    "target_map_capacity": 50, "target_sidebar_capacity": 50,
                    "group_feed_history": False, "max_grouping_age_mins": 60, "group_time_gap_mins": 15,
                    "enable_bursts": True, "spiderify_radius": 0.025, "burst_zoom_level": 13,
                    "enable_swarms": True, "swarm_max_distance_km": 500, "swarm_bbox_padding": 1.5, "swarm_max_zoom": 6
                }, 
                
                "browser_automation": {"enabled": True, "chrome_profile_path": "", "webdriver_path": ""},
                
                "batch_size": 20,
                "failure_penalty": 30,
                "loop_penalty": 360,
                "intermittent_threshold": 2,
                "unresponsive_threshold": 5,
                
                "distribution_strategy": "Dynamic Dispatch",
                "shuffle_playback": True,
                "quarantine_reserve": 0,
                "quarantine_freq": 0,
                "debug_logging": False,
                "periodic_report": {"enabled": True, "interval_hours": 12.0, "send_species_alerts": True, "alert_hourly_speed": True, "alert_monthly_quota": True},
                "housekeeping": {"janitor_interval_hours": 1, "legacy_log_retention_hours": 72, "temp_retention_hours": 24, "auto_wipe_python_caches": True, "auto_wipe_orphaned_clips": True, "auto_wipe_db_bloat": False},
                
                "extraction_strategy": {
                    "player_client": "web",
                    "ffmpeg_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "audio_normalization": True,
                    "frame_quality": 2
                },
                
                "streams":[],
                "network_map": {},
                
                "silent_penalty_minutes": 60,
                "silent_visual_penalty_minutes": 720,
                "vision_ai": {},
                "maintenance_mode": False,
                "channels": {}
            }
            
            if not CONFIG_FILE.exists(): CONFIG_FILE.write_text(json.dumps(default_config, indent=2))
            disk_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k, v in default_config.items(): 
                if k not in disk_cfg: disk_cfg[k] = v
            
            self.engine_params['fast_mode_enabled'] = disk_cfg.get("fast_mode_enabled", True)
            self.engine_params['total_listeners'] = disk_cfg.get("parallel_listeners", 4)
            self.engine_params['capture_seconds'] = disk_cfg.get("capture_seconds", 12)
            self.engine_params['interval_seconds'] = disk_cfg.get("interval_seconds", 300)
            self.engine_params['score_threshold'] = disk_cfg.get("score_threshold", 0.55)
            self.engine_params['interval_jitter'] = disk_cfg.get("interval_jitter", 15)
            
            self.engine_params['batch_size'] = disk_cfg.get("batch_size", 20)
            self.engine_params['failure_penalty'] = disk_cfg.get("failure_penalty", 30)
            
            self.engine_params['hiccup_penalty_minutes'] = disk_cfg.get("hiccup_penalty_minutes", 1) 
            self.engine_params['intermittent_penalty_minutes'] = disk_cfg.get("intermittent_penalty_minutes", 120)
            self.engine_params['unresponsive_penalty_minutes'] = disk_cfg.get("unresponsive_penalty_minutes", 240)
            
            self.engine_params['loop_penalty'] = disk_cfg.get("loop_penalty", 360)
            
            self.engine_params['suspended_penalty_minutes'] = disk_cfg.get("suspended_penalty_minutes", 1440)
            self.engine_params['terminal_penalty_minutes'] = disk_cfg.get("terminal_penalty_minutes", 10080)
            
            self.engine_params['silent_penalty_minutes'] = disk_cfg.get("silent_penalty_minutes", 60)
            self.engine_params['silent_visual_penalty_minutes'] = disk_cfg.get("silent_visual_penalty_minutes", 720)
            
            self.engine_params['intermittent_threshold'] = disk_cfg.get("intermittent_threshold", 2)
            self.engine_params['unresponsive_threshold'] = disk_cfg.get("unresponsive_threshold", 5)
            
            self.engine_params['distribution_strategy'] = "Dynamic Dispatch"
            self.engine_params['shuffle_playback'] = True
            self.engine_params['quarantine_reserve'] = 0
            self.engine_params['quarantine_freq'] = 0
            
            # --- THE EXTRACTION STRATEGY LOAD ---
            self.engine_params['extraction_strategy'] = disk_cfg.get("extraction_strategy", {
                "player_client": "web",
                "ffmpeg_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "audio_normalization": True,
                "frame_quality": 2
            })
            
            self.unsaved_network_map = copy.deepcopy(disk_cfg.get("network_map", {}))
            self.unsaved_loop_config = copy.deepcopy(disk_cfg.get("loop_management"))
            self.unsaved_tiered_config = copy.deepcopy(disk_cfg.get("tiered_cooldowns"))
            self.unsaved_distance_config = copy.deepcopy(disk_cfg.get("distance_estimation"))
            self.unsaved_cookies_path = disk_cfg.get("youtube_cookies_file", "")
            self.unsaved_cookie_alert_config = copy.deepcopy(disk_cfg.get("cookie_alert", {}))
            self.unsaved_debug_logging = disk_cfg.get("debug_logging", False)
            self.unsaved_channels = copy.deepcopy(disk_cfg.get("channels", {}))
            
            map_cfg = disk_cfg.get("map_settings", {})
            if "audio_vision_ratio" not in map_cfg: map_cfg["audio_vision_ratio"] = 70
            if "target_map_capacity" not in map_cfg: map_cfg["target_map_capacity"] = 50
            if "target_sidebar_capacity" not in map_cfg: map_cfg["target_sidebar_capacity"] = 50
            
            if "group_feed_history" not in map_cfg: map_cfg["group_feed_history"] = False
            if "max_grouping_age_mins" not in map_cfg: map_cfg["max_grouping_age_mins"] = 60
            if "group_time_gap_mins" not in map_cfg: map_cfg["group_time_gap_mins"] = 15
            if "enable_bursts" not in map_cfg: map_cfg["enable_bursts"] = True
            if "spiderify_radius" not in map_cfg: map_cfg["spiderify_radius"] = 0.025
            if "burst_zoom_level" not in map_cfg: map_cfg["burst_zoom_level"] = 13
            if "enable_swarms" not in map_cfg: map_cfg["enable_swarms"] = True
            if "swarm_max_distance_km" not in map_cfg: map_cfg["swarm_max_distance_km"] = 500
            if "swarm_bbox_padding" not in map_cfg: map_cfg["swarm_bbox_padding"] = 1.5
            if "swarm_max_zoom" not in map_cfg: map_cfg["swarm_max_zoom"] = 6
            
            map_cfg.pop("min_vision_fallback", None)
            self.unsaved_map_config = copy.deepcopy(map_cfg)
            
            old_audio = disk_cfg.get("send_audio_telegram", None)
            if old_audio is not None:
                self.unsaved_send_audio_all = disk_cfg.get("send_audio_telegram_all", old_audio)
                self.unsaved_send_audio_dsp = disk_cfg.get("send_audio_telegram_dsp", old_audio)
                self.unsaved_send_audio_multi = disk_cfg.get("send_audio_telegram_multi", old_audio)
                self.unsaved_send_audio_birdnet = disk_cfg.get("send_audio_telegram_birdnet", old_audio)
            else:
                self.unsaved_send_audio_all = disk_cfg.get("send_audio_telegram_all", False)
                self.unsaved_send_audio_dsp = disk_cfg.get("send_audio_telegram_dsp", True)
                self.unsaved_send_audio_multi = disk_cfg.get("send_audio_telegram_multi", True)
                self.unsaved_send_audio_birdnet = disk_cfg.get("send_audio_telegram_birdnet", False)
            
            self.unsaved_report_config = copy.deepcopy(disk_cfg.get("periodic_report", {"enabled": True, "interval_hours": 12.0, "send_species_alerts": True, "alert_hourly_speed": True, "alert_monthly_quota": True}))
            self.unsaved_housekeeping_config = copy.deepcopy(disk_cfg.get("housekeeping", {"janitor_interval_hours": 1, "legacy_log_retention_hours": 72, "temp_retention_hours": 24, "auto_wipe_python_caches": True, "auto_wipe_orphaned_clips": True, "auto_wipe_db_bloat": False}))
            
            self._load_gui_settings()
            self.bot_token_edit.setText(disk_cfg.get("bot_token", ""))
            self.chat_id_edit.setText(disk_cfg.get("chat_id", ""))
            
            self.update_engine_status_label()
            
            self.btn_maintenance.blockSignals(True)
            is_maint = disk_cfg.get("maintenance_mode", False)
            self.btn_maintenance.setChecked(is_maint)
            self.update_maintenance_button_style(is_maint)
            self.btn_maintenance.blockSignals(False)
            
            b = disk_cfg.get("browser_automation", {})
            self.browser_group.setChecked(b.get("enabled", False))
            self.profile_path_edit.setText(b.get("chrome_profile_path", ""))
            self.driver_path_edit.setText(b.get("webdriver_path", ""))
            self.browser_controls_widget.setEnabled(self.browser_group.isChecked())
            
            self.saved_config_data = copy.deepcopy(disk_cfg)
            
            self.update_stream_list_widget()
            self.clear_form()
            self._populate_channel_dropdown()
            
            self.saved_config_data = self._get_config_from_ui()
        except Exception as e: QMessageBox.critical(self, "Error", f"Load failed:\n{e}"); logging.error("Load config", exc_info=True)
        finally: self._loading = False; self.set_dirty_state(False)
        
    def save_config(self, show_confirmation=True):
        if not self.is_dirty and show_confirmation: QMessageBox.information(self, "No Changes", "No changes."); return
        try:
            cfg = self._get_config_from_ui(); CONFIG_FILE.write_text(json.dumps(cfg, indent=2)); self._save_gui_preferences()
            if show_confirmation: QMessageBox.information(self, "Success", "Saved!")
            self.saved_config_data = copy.deepcopy(cfg)
            
            # Immediately update the auto_sync_timer if the interval changed
            self.update_auto_sync_timer()
            
            self.load_config()
        except Exception as e: QMessageBox.critical(self, "Error", f"Save failed:\n{e}")
        
    def show_save_confirmation(self):
        if not self.is_dirty: QMessageBox.information(self, "No Changes", "No changes."); return
        chg = self._get_config_changes()
        if not chg: QMessageBox.information(self, "No Changes", "No changes."); self.set_dirty_state(False); return
        d = SaveConfirmDialog(chg, self.unsaved_save_dialog_widths, self)
        if d.exec(): self.unsaved_save_dialog_widths = d.get_column_widths(); self.save_config()
        
    def _get_detailed_tiered_changes(self, o, n):
        c =[]; e = "Next Round"
        if not o or not n: return[]
        for k in['active_preset', 'calculation_period_days', 'thresholds_locked']: 
            if o.get(k) != n.get(k): c.append({'setting': f"Cooldown: {k}", 'old': str(o.get(k)), 'new': str(n.get(k)), 'effective': e})
        if json.dumps(o.get('first_sighting_override'), sort_keys=True) != json.dumps(n.get('first_sighting_override'), sort_keys=True):
             c.append({'setting': "Cooldown: First Sighting", 'old': "...", 'new': "Updated", 'effective': e})
        o_tiers = o.get('tiers',[]); n_tiers = n.get('tiers',[])
        if json.dumps(o_tiers, sort_keys=True) != json.dumps(n_tiers, sort_keys=True):
             c.append({'setting': "Cooldown Matrix", 'old': "Old Values", 'new': "New Values", 'effective': e})
        return c
        
    def _get_config_changes(self):
        o = self.saved_config_data; n = self._get_config_from_ui(); c =[]
        if o.get("youtube_cookies_file") != n.get("youtube_cookies_file"): c.append({'setting': "Cookies", 'old': "...", 'new': "Updated", 'effective': "Next Round"})
        
        old_ba = o.get("browser_automation", {}).get("enabled", False)
        new_ba = n.get("browser_automation", {}).get("enabled", False)
        if old_ba != new_ba: c.append({'setting': "Browser Automation", 'old': str(old_ba), 'new': str(new_ba), 'effective': "Restart"})

        keys_map = {
            "bot_token": "Bot Token", "chat_id": "Chat ID", 
            "fast_mode_enabled": "Fast Mode (Auto-Healer)",
            "capture_seconds": "Capture", "interval_seconds": "Target Cycle",
            "interval_jitter": "Jitter", 
            "batch_size": "Batch Size",
            "hiccup_penalty_minutes": "Glitch Penalty", 
            "failure_penalty": "Fail Penalty",
            "intermittent_penalty_minutes": "Intermittent Penalty", 
            "unresponsive_penalty_minutes": "Unresponsive Penalty", 
            "loop_penalty": "Loop Penalty",
            "suspended_penalty_minutes": "Suspended Penalty",
            "terminal_penalty_minutes": "Terminal Penalty",
            "silent_penalty_minutes": "Audio Pause (No Audio)",
            "silent_visual_penalty_minutes": "Audio Pause (Visually Active)",
            
            "send_audio_telegram_all": "Send ALL Audio",
            "send_audio_telegram_dsp": "Send DSP Audio",
            "send_audio_telegram_multi": "Send Multimodal Audio",
            "send_audio_telegram_birdnet": "Send BirdNET Audio",
            
            "intermittent_threshold": "Intermittent Limit",
            "unresponsive_threshold": "Unresponsive Limit",
            "debug_logging": "Debug Logging",
            "maintenance_mode": "Maintenance Mode"
        }
        for k, l in keys_map.items():
            if o.get(k) != n.get(k): c.append({'setting': l, 'old': str(o.get(k)), 'new': str(n.get(k)), 'effective': "Next Round"})
            
        if o.get("parallel_listeners") != n.get("parallel_listeners"): c.append({'setting': "Listeners", 'old': str(o.get("parallel_listeners")), 'new': str(n.get("parallel_listeners")), 'effective': "Restart"})
        
        if json.dumps(o.get('network_map', {}), sort_keys=True) != json.dumps(n.get('network_map', {}), sort_keys=True):
             c.append({'setting': "Network Map", 'old': "...", 'new': "Updated", 'effective': "Restart"})
             
        if json.dumps(o.get('periodic_report', {}), sort_keys=True) != json.dumps(n.get('periodic_report', {}), sort_keys=True):
             c.append({'setting': "Report Config", 'old': "...", 'new': "Updated", 'effective': "Immediate"})
             
        if json.dumps(o.get('map_settings', {}), sort_keys=True) != json.dumps(n.get('map_settings', {}), sort_keys=True):
             c.append({'setting': "Map Settings", 'old': "...", 'new': "Updated", 'effective': "Next Refresh"})

        if json.dumps(o.get('housekeeping', {}), sort_keys=True) != json.dumps(n.get('housekeeping', {}), sort_keys=True):
             c.append({'setting': "Housekeeping", 'old': "...", 'new': "Updated", 'effective': "Next Round"})
             
        if json.dumps(o.get('channels', {}), sort_keys=True) != json.dumps(n.get('channels', {}), sort_keys=True):
             c.append({'setting': "Channel Main URLs", 'old': "...", 'new': "Updated", 'effective': "Immediate"})
             
        # --- THE EXTRACTION STRATEGY DIFF PATCH ---
        if json.dumps(o.get('extraction_strategy', {}), sort_keys=True) != json.dumps(n.get('extraction_strategy', {}), sort_keys=True):
             c.append({'setting': "Extraction Strategy", 'old': "...", 'new': "Updated", 'effective': "Restart"})

        c.extend(self._get_detailed_tiered_changes(o.get("tiered_cooldowns"), n.get("tiered_cooldowns")))

        os_map = {s['name']: s for s in o.get("streams",[])}; ns_map = {s['name']: s for s in n.get("streams",[])}
        for k in set(os_map) - set(ns_map): c.append({'setting': "Removed", 'old': k, 'new': "-", 'effective': "Next Round"})
        for k in set(ns_map) - set(os_map): c.append({'setting': "Added", 'old': "-", 'new': k, 'effective': "Next Round"})
        
        for k in set(os_map) & set(ns_map):
            if json.dumps(os_map[k], sort_keys=True) != json.dumps(ns_map[k], sort_keys=True):
                 c.append({'setting': f"Modified: {k}", 'old': "...", 'new': "Updated", 'effective': "Next Round"})
                 
        return c
        
    def _get_config_from_ui(self):
        streams =[self.stream_list_widget.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.stream_list_widget.count())]
        
        hk_config = copy.deepcopy(self.unsaved_housekeeping_config)
        hk_config['auto_wipe_db_bloat'] = False # FORCE TO FALSE ALWAYS
        
        return {
            "bot_token": self.bot_token_edit.text().strip(), "chat_id": self.chat_id_edit.text().strip(),
            "youtube_cookies_file": self.unsaved_cookies_path if self.unsaved_cookies_path is not None else self.saved_config_data.get("youtube_cookies_file", ""),
            "cookie_alert": self.unsaved_cookie_alert_config if self.unsaved_cookie_alert_config else self.saved_config_data.get("cookie_alert", {}),
            
            "send_audio_telegram_all": self.unsaved_send_audio_all,
            "send_audio_telegram_dsp": self.unsaved_send_audio_dsp,
            "send_audio_telegram_multi": self.unsaved_send_audio_multi,
            "send_audio_telegram_birdnet": self.unsaved_send_audio_birdnet,
            
            "fast_mode_enabled": self.engine_params['fast_mode_enabled'],
            "capture_seconds": self.engine_params['capture_seconds'], 
            "interval_seconds": self.engine_params['interval_seconds'],
            "score_threshold": self.engine_params['score_threshold'], 
            "parallel_listeners": self.engine_params['total_listeners'],
            "interval_jitter": self.engine_params['interval_jitter'],
            
            "batch_size": self.engine_params['batch_size'],
            "hiccup_penalty_minutes": self.engine_params['hiccup_penalty_minutes'], 
            "failure_penalty": self.engine_params['failure_penalty'],
            
            "intermittent_penalty_minutes": self.engine_params['intermittent_penalty_minutes'],
            "unresponsive_penalty_minutes": self.engine_params['unresponsive_penalty_minutes'],
            
            "suspended_penalty_minutes": self.engine_params['suspended_penalty_minutes'],
            "terminal_penalty_minutes": self.engine_params['terminal_penalty_minutes'],
            "silent_penalty_minutes": self.engine_params['silent_penalty_minutes'],
            "silent_visual_penalty_minutes": self.engine_params['silent_visual_penalty_minutes'],
            
            "loop_penalty": self.engine_params['loop_penalty'],
            "intermittent_threshold": self.engine_params['intermittent_threshold'],
            "unresponsive_threshold": self.engine_params['unresponsive_threshold'],
            
            "distribution_strategy": "Dynamic Dispatch",
            "shuffle_playback": True,
            "quarantine_reserve": 0,
            "quarantine_freq": 0,
            
            "extraction_strategy": self.engine_params.get("extraction_strategy", {
                "player_client": "web",
                "ffmpeg_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "audio_normalization": True,
                "frame_quality": 2
            }),
            
            "network_map": self.unsaved_network_map,
            "loop_management": self.unsaved_loop_config, "distance_estimation": self.unsaved_distance_config,
            "streams": streams, "tiered_cooldowns": self.unsaved_tiered_config,
            "browser_automation": {"enabled": self.browser_group.isChecked(), "chrome_profile_path": self.profile_path_edit.text().strip(), "webdriver_path": self.driver_path_edit.text().strip()},
            
            "debug_logging": self.unsaved_debug_logging,
            "periodic_report": self.unsaved_report_config, 
            "housekeeping": hk_config,
            
            "map_settings": self.unsaved_map_config,
            "database_cloud": self.saved_config_data.get("database_cloud", {}),
            
            "vision_ai": self.saved_config_data.get("vision_ai", {}),
            "maintenance_mode": self.btn_maintenance.isChecked(),
            "channels": self.unsaved_channels,
            "sync_engine_settings": self.saved_config_data.get("sync_engine_settings", {}),
            "hydra_pid_settings": json.loads(CONFIG_FILE.read_text(encoding='utf-8')).get("hydra_pid_settings", {}) if CONFIG_FILE.exists() else {},
            "ignored_channels": json.loads(CONFIG_FILE.read_text(encoding='utf-8')).get("ignored_channels", []) if CONFIG_FILE.exists() else []
        }
        
    def _get_gui_settings_from_ui(self):
        s = self.saved_gui_settings.copy(); s["show_listener_windows"] = self.show_windows_cb.isChecked()
        if hasattr(self, 'unsaved_cooldown_widths'): s["cooldown_table_widths"] = self.unsaved_cooldown_widths
        if hasattr(self, 'unsaved_save_dialog_widths'): s["save_dialog_table_widths"] = self.unsaved_save_dialog_widths
        return s
        
    def closeEvent(self, e): self._save_gui_preferences(); self.stop_monitoring(silent=True); e.accept()
    def browse_profile(self):
        d = QFileDialog.getExistingDirectory(self, "Profile"); 
        if d: self.profile_path_edit.setText(d)
    def browse_driver(self):
        f, _ = QFileDialog.getOpenFileName(self, "Driver", "", "*.exe"); 
        if f: self.driver_path_edit.setText(f)
    def test_telegram(self):
        s, d = send_and_log_system_alert(self.bot_token_edit.text(), self.chat_id_edit.text(), "Test", "TEST"); QMessageBox.information(self, "Test", f"{'Success' if s else 'Failed'}\n{d}")
    def _load_gui_settings(self):
        try:
            s = json.loads(GUI_SETTINGS_FILE.read_text(encoding="utf-8")) if GUI_SETTINGS_FILE.exists() else {}
            self.show_windows_cb.setChecked(s.get("show_listener_windows", False))
            self.unsaved_cooldown_widths = s.get("cooldown_table_widths"); self.unsaved_save_dialog_widths = s.get("save_dialog_table_widths")
            
            sort_key = s.get("sort_key", "name")
            sort_reverse = s.get("sort_reverse", False)
            self.current_sort_mode = (sort_key, sort_reverse)
            
            label_map = {
                ('name', False): "Name (A-Z)", ('name', True): "Name (Z-A)",
                ('created_at', True): "Created (Newest)", ('created_at', False): "Created (Oldest)",
                ('updated_at', True): "Modified (Newest)", ('updated_at', False): "Modified (Oldest)",
                ('enabled', True): "Status (Enabled First)", ('enabled', False): "Status (Disabled First)",
                ('mute_audio', True): "Mute Audio (Muted First)", ('mute_audio', False): "Mute Audio (Unmuted First)",
                ('global', True): "Global (Global First)", ('global', False): "Global (Local First)"
            }
            label = label_map.get((sort_key, sort_reverse), "Name (A-Z)")
            arrow = "▼" if sort_reverse else "▲"
            self.btn_sort.setText(f"Sort: {label} {arrow}")
            
        except: pass
        
    def set_all_streams_enabled(self, en):
        self._loading = True
        for i in range(self.stream_list_widget.count()):
            it = self.stream_list_widget.item(i)
            if it.isHidden(): continue 
            it.setCheckState(Qt.CheckState.Checked if en else Qt.CheckState.Unchecked)
            d = it.data(Qt.ItemDataRole.UserRole)
            d['enabled'] = en
            d['updated_at'] = time.time()
            it.setData(Qt.ItemDataRole.UserRole, d)
            
        self._loading = False
        self._sort_stream_list()
        self._check_and_update_dirty_state()
        
    def populate_form_from_selection(self, cur, prev):
        if self._loading or not cur: self.clear_form(); return
        d = cur.data(Qt.ItemDataRole.UserRole); self.stream_name_edit.setText(d.get("name", "")); 
        
        self._pending_original_url = d.get("original_url", "")
        self._pending_stream_type = d.get("stream_type", "youtube")
        
        # Step 2.1 & 2.2: Load pending title into cache state
        self._pending_yt_title = d.get("original_yt_title", "")
        
        if self._pending_original_url:
            self.stream_url_edit.setText(self._pending_original_url)
            self._pending_resolved_url = d.get("page_url", "")
        else:
            self.stream_url_edit.setText(d.get("page_url", ""))
            self._pending_resolved_url = d.get("page_url", "")
            
        lat = float(d.get("lat", 0.0))
        lon = float(d.get("lon", 0.0))
        
        is_global = (lat == 0.0 and lon == 0.0)
        
        self._programmatic_update = True
        self.global_cb.blockSignals(True)
        self.global_cb.setChecked(is_global)
        self.global_cb.blockSignals(False)
        self._programmatic_update = False
        
        self.stream_lat_edit.setText(str(lat)); self.stream_lon_edit.setText(str(lon))
        
        if d.get("original_lat") is not None:
            self._temp_lat = str(d.get("original_lat"))
            self._temp_lon = str(d.get("original_lon"))
        else:
            self._temp_lat = ""
            self._temp_lon = ""
            
        self.update_global_ui_state(is_global)
        
        self.chk_mute_audio.blockSignals(True)
        self.chk_mute_audio.setChecked(d.get("mute_audio", False))
        self.chk_mute_audio.blockSignals(False)
        
        self.stream_channel_edit.setCurrentText(d.get("channel_name", ""))
        self.capture_override_edit.setText(str(d.get("capture_seconds_override", "")))
        t = d.get("score_threshold_override"); self.threshold_override_edit.setText(str(int(t*100)) if t else "")
        
        self.check_stream_url_input()
        
        if self._pending_stream_type == 'hls':
            self.magic_wand_button.setStyleSheet("border: none; background-color: #66BB6A; border-radius: 4px;") 
            self.update_stream_button.setEnabled(True)
        
        status_text = f"Editing: {d.get('name')} ({self._pending_stream_type})"
        if len(status_text) > 80:
            status_text = status_text[:77] + "..."
        self.check_status_label.setText(f"<i>{status_text}</i>")
    
    def update_global_ui_state(self, checked):
        if checked:
            self.stream_lat_edit.setText("0.0"); self.stream_lat_edit.setEnabled(False)
            self.stream_lon_edit.setText("0.0"); self.stream_lon_edit.setEnabled(False)
        else:
            self.stream_lat_edit.setEnabled(True)
            self.stream_lon_edit.setEnabled(True)
            if self._temp_lat: self.stream_lat_edit.setText(self._temp_lat)
            if self._temp_lon: self.stream_lon_edit.setText(self._temp_lon)

    def toggle_global_mode(self, checked):
        if checked and not self._programmatic_update:
            curr_lat = self.stream_lat_edit.text().strip()
            curr_lon = self.stream_lon_edit.text().strip()
            
            if curr_lat and curr_lon and curr_lat != "0.0" and curr_lat != "0":
                self._temp_lat = curr_lat
                self._temp_lon = curr_lon

        self.update_global_ui_state(checked)

        if not self._programmatic_update and checked:
            self.prompt_for_global_coords()

    def prompt_for_global_coords(self):
        default_val = f"{self._temp_lat}, {self._temp_lon}" if (self._temp_lat and self._temp_lon) else ""

        text, ok = QInputDialog.getText(self, "Set Real Map Location", 
            "Global Mode Active.\nEnter the REAL coordinates for the map (Lat, Lon):",
            text=default_val)
        
        if ok:
            if not text.strip():
                QMessageBox.warning(self, "Input Required", "You must enter coordinates for Global Mode.\n(e.g., '28.21, -177.37')")
                self.uncheck_global_safely()
                return

            try:
                parts = text.split(',')
                if len(parts) >= 2:
                    new_lat = parts[0].strip()
                    new_lon = parts[1].strip()
                    float(new_lat); float(new_lon) 
                    
                    self._temp_lat = new_lat
                    self._temp_lon = new_lon
                else:
                    QMessageBox.warning(self, "Invalid Format", "Use format: Lat, Lon")
                    self.uncheck_global_safely()
            except:
                QMessageBox.warning(self, "Invalid Input", "Could not parse coordinates.")
                self.uncheck_global_safely()
        else:
            self.uncheck_global_safely()

    def uncheck_global_safely(self):
        self.global_cb.blockSignals(True)
        self.global_cb.setChecked(False)
        self.global_cb.blockSignals(False)
        self.update_global_ui_state(False)
        
        if not self._temp_lat: 
            self.stream_lat_edit.clear()
            self.stream_lon_edit.clear()

    def clear_form(self):
        self._temp_lat = ""
        self._temp_lon = ""
        self._pending_original_url = ""
        self._pending_stream_type = "youtube"
        self._pending_resolved_url = ""
        self._pending_yt_title = "" # Step 2.1 & 2.2: Reset title tracking
        
        self.stream_list_widget.clearSelection()
        for w in[self.stream_name_edit, self.stream_url_edit, self.stream_lat_edit, self.stream_lon_edit, self.capture_override_edit, self.threshold_override_edit]: w.clear()
        self.stream_channel_edit.setCurrentText("")
        
        self.uncheck_global_safely()
        self.chk_mute_audio.setChecked(False)
        
        self.add_stream_button.setEnabled(True); self.update_stream_button.setEnabled(False); self.check_status_label.setText("<i>Ready</i>")
        self.magic_wand_button.setEnabled(False)
        self.magic_wand_button.setStyleSheet("border: none;")
        
    def reset_all_overrides(self):
        if self.stream_list_widget.currentItem() and QMessageBox.question(self, "Reset", "Reset overrides?") == QMessageBox.StandardButton.Yes:
            self.capture_override_edit.clear(); self.threshold_override_edit.clear(); self.update_stream()
            
    def add_stream(self):
        try:
            d = self.get_stream_data_from_form()
            
            new_url_raw = d.get('original_url', d['page_url']).strip()
            
            is_dup = False
            for i in range(self.stream_list_widget.count()):
                existing_data = self.stream_list_widget.item(i).data(Qt.ItemDataRole.UserRole)
                ex_orig = existing_data.get('original_url', existing_data.get('page_url', '')).strip()
                ex_clean = ex_orig.split('?variant=')[0].split('&variant=')[0]
                new_clean = new_url_raw.split('?variant=')[0].split('&variant=')[0]
                
                if ex_clean == new_clean:
                    is_dup = True
                    break
            
            if is_dup:
                reply = QMessageBox.question(
                    self, "Multi-Cam / Duplicate Detected",
                    f"The URL '{new_clean}' is already in the list.\n\n"
                    "Do you want to add this as a NEW VIEW (Variant)?\n"
                    "(Useful for Nest/Wide/PTZ views of the same stream)",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                
                if reply == QMessageBox.StandardButton.No:
                    return 
                
                sep = "&" if "?" in d['page_url'] else "?"
                d['page_url'] = f"{d['page_url']}{sep}variant={int(time.time())}"

            d['enabled'] = True; d['created_at'] = time.time(); d['updated_at'] = time.time()
            
            it = QListWidgetItem(d['name']); it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable); it.setCheckState(Qt.CheckState.Checked); it.setData(Qt.ItemDataRole.UserRole, d)
            self.stream_list_widget.addItem(it)
            
            self._sort_stream_list() 
            self._focus_on_stream(d['name'])
            
            try:
                if "vision_ai" not in self.saved_config_data:
                    self.saved_config_data["vision_ai"] = {}
                if "enabled_streams" not in self.saved_config_data["vision_ai"]:
                    self.saved_config_data["vision_ai"]["enabled_streams"] =[]
                if d['page_url'] not in self.saved_config_data["vision_ai"]["enabled_streams"]:
                    self.saved_config_data["vision_ai"]["enabled_streams"].append(d['page_url'])
            except Exception as e:
                logging.warning(f"Could not auto-enable Vision AI for new stream: {e}")
                
            self._check_and_update_dirty_state()
        except Exception as e: QMessageBox.warning(self, "Error", str(e))
        
    def update_stream(self):
            it = self.stream_list_widget.currentItem()
            if not it: return
            try:
                d = self.get_stream_data_from_form(True) 
                d['updated_at'] = time.time()
                
                old_data = it.data(Qt.ItemDataRole.UserRole)
                old_url = old_data.get('page_url', '').strip()
                new_url = d.get('page_url', '').strip()
                
                if stream_migrator:
                    if old_url and new_url and old_url != new_url:
                        history_count = stream_migrator.check_history(old_url)
                        if history_count > 0:
                            msg = (f"You are changing the URL for '{d['name']}'.\n\n"
                                   f"The old URL has {history_count} historical detections.\n"
                                   f"Do you want to MIGRATE this history to the new URL?\n\n"
                                   f"YES: Move history to new URL (Continuity).\n"
                                   f"NO: Leave history as orphaned 'ghost' data.")
                            
                            reply = QMessageBox.question(self, "Migrate Stream History?", msg, 
                                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
                            
                            if reply == QMessageBox.StandardButton.Cancel:
                                return 
                            
                            if reply == QMessageBox.StandardButton.Yes:
                                prog_dialog = QDialog(self)
                                prog_dialog.setWindowTitle("Migrating Stream...")
                                prog_dialog.setFixedSize(450, 150)
                                prog_dialog.setModal(True)
                                lay = QVBoxLayout(prog_dialog)
                                lbl = QLabel("Migrating data locally and on the cloud...\nPlease wait.")
                                lbl.setWordWrap(True)
                                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                                lay.addWidget(lbl)
                                btn = QPushButton("OK")
                                btn.setEnabled(False)
                                btn.clicked.connect(prog_dialog.accept)
                                lay.addWidget(btn)
                                
                                prog_dialog.show()
                                QApplication.processEvents()
                                
                                success, log_msg = stream_migrator.migrate_stream_data(old_url, new_url)
                                
                                if success:
                                    lbl.setText(f"Finished successfully!\nData migrated both locally and on the Cloud.\n\nDB Log: {log_msg}")
                                    btn.setEnabled(True)
                                    btn.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
                                else:
                                    lbl.setText(f"Migration Failed.\n\nDB Log: {log_msg}")
                                    btn.setEnabled(True)
                                    btn.setStyleSheet("background-color: #F44336; color: white; font-weight: bold;")
                                    
                                prog_dialog.exec()

                # --- THE URL TAG-ALONG PATCH (GUI) ---
                # If the URL changed, but they didn't migrate history (or history was 0), 
                # we STILL need to update the Vision AI memory so the checkbox stays ticked.
                if old_url and new_url and old_url != new_url:
                    try:
                        if "vision_ai" not in self.saved_config_data:
                            self.saved_config_data["vision_ai"] = {}
                        if "enabled_streams" not in self.saved_config_data["vision_ai"]:
                            self.saved_config_data["vision_ai"]["enabled_streams"] = []
                            
                        vision_enabled = self.saved_config_data["vision_ai"]["enabled_streams"]
                        for i, u in enumerate(vision_enabled):
                            if u == old_url:
                                vision_enabled[i] = new_url
                                break
                    except Exception as e:
                        logging.warning(f"GUI: Failed to swap Vision Checkbox URL: {e}")

                self._loading = True
                it.setData(Qt.ItemDataRole.UserRole, d)
                self._loading = False
                
                self._sort_stream_list()
                self._focus_on_stream(d['name'])
                self._check_and_update_dirty_state()
                
            except ValueError as ve: 
                pass 
            except Exception as e: 
                QMessageBox.warning(self, "Error", str(e))

    def get_stream_data_from_form(self, exist=False):
        d = copy.deepcopy(self.stream_list_widget.currentItem().data(Qt.ItemDataRole.UserRole)) if exist else {}
        d.update({"name": self.stream_name_edit.text().strip()})
        
        human_url = self.stream_url_edit.text().strip()
        
        if self._pending_resolved_url:
            d['page_url'] = self._pending_resolved_url
            d['original_url'] = human_url
        else:
            d['page_url'] = human_url
            if 'original_url' in d: del d['original_url']

        if self._pending_stream_type: d['stream_type'] = self._pending_stream_type
        
        # Step 2.1 & 2.2: Commit the pending title string to the dictionary
        if self._pending_yt_title:
            d['original_yt_title'] = self._pending_yt_title
        
        try: 
            d["lat"] = float(self.stream_lat_edit.text())
            d["lon"] = float(self.stream_lon_edit.text())
        except: raise ValueError("Invalid Coords")
        
        if self.global_cb.isChecked():
            lat_valid = self._temp_lat and self._temp_lat != "0.0" and self._temp_lat != "0"
            lon_valid = self._temp_lon and self._temp_lon != "0.0" and self._temp_lon != "0"
            
            if not lat_valid or not lon_valid:
                QMessageBox.critical(self, "Missing Map Location", 
                    "Map coordinates missing for Global Stream.\n\n"
                    "Please Uncheck and Recheck the 'Global' box to enter them.")
                raise ValueError("Missing Map Coordinates") 

            try:
                d["original_lat"] = float(self._temp_lat)
                d["original_lon"] = float(self._temp_lon)
            except: pass
        else:
            if "original_lat" in d: del d["original_lat"]
            if "original_lon" in d: del d["original_lon"]
            
        cn = self.stream_channel_edit.currentText().strip()
        if cn: d["channel_name"] = cn
        elif "channel_name" in d: del d["channel_name"]
            
        c = self.capture_override_edit.text().strip(); t = self.threshold_override_edit.text().strip()
        if c: d["capture_seconds_override"] = int(c)
        elif "capture_seconds_override" in d: del d["capture_seconds_override"]
        if t: d["score_threshold_override"] = int(t)/100.0
        elif "score_threshold_override" in d: del d["score_threshold_override"]
        
        d["mute_audio"] = self.chk_mute_audio.isChecked()
        
        return d
        
    def remove_stream(self):
        it = self.stream_list_widget.currentItem()
        if it:
            d = it.data(Qt.ItemDataRole.UserRole)
            name = d.get('name', 'Unknown')
            if QMessageBox.question(self, "Remove", f"Remove '{name}'?") == QMessageBox.StandardButton.Yes:
                self._loading = True
                self.stream_list_widget.takeItem(self.stream_list_widget.row(it))
                self._loading = False
                self.clear_form()
                self._sort_stream_list() 
                self._check_and_update_dirty_state()
            
    def find_stream_by_url(self, url):
        nid = self.extract_youtube_id(url)
        for i in range(self.stream_list_widget.count()):
            d = self.stream_list_widget.item(i).data(Qt.ItemDataRole.UserRole)
            eid = self.extract_youtube_id(d.get('page_url', ''))
            if (nid and eid and nid == eid) or d.get('page_url') == url or d.get('original_url') == url: return d['name']
        return None
        
    def check_stream(self):
        u = self.stream_url_edit.text().strip()
        if not u: return
        dup = self.find_stream_by_url(u); self._check_url_is_duplicate = bool(dup)
        self.check_status_label.setText(f"<i>Checking... {'(Duplicate)' if dup else ''}</i>")
        self.ct = StreamCheckThread(u, self.unsaved_cookies_path); self.ct.result_ready.connect(self.on_check_result); self.ct.start()
        
    def on_check_result(self, r, t): 
        self.check_status_label.setText(r)
        # Step 2.1 & 2.2: Extract title and cache it so get_stream_data_from_form captures it
        if "Title: " in r:
            extracted_title = r.split("Title: ")[-1].strip()
            if extracted_title and extracted_title != "N/A":
                self._pending_yt_title = extracted_title
                self._check_and_update_dirty_state()
    
    def open_stream_url(self):
        u = self._pending_original_url if self._pending_original_url else self.stream_url_edit.text().strip()
        if u: webbrowser.open(u)
        
    def open_map_from_form(self):
        lat_to_use = self._temp_lat if self.global_cb.isChecked() and self._temp_lat else self.stream_lat_edit.text()
        lon_to_use = self._temp_lon if self.global_cb.isChecked() and self._temp_lon else self.stream_lon_edit.text()
        try: webbrowser.open(f"https://www.google.com/maps/search/?api=1&query={float(lat_to_use)},{float(lon_to_use)}")
        except: pass
        
    def _revert_ui_to_saved_state(self):
        if QMessageBox.question(self, "Confirm", "Discard changes?") == QMessageBox.StandardButton.Yes: self.load_config()
        
    def _reset_settings_keep_streams(self):
        if QMessageBox.question(self, "Reset", "Reset settings?") == QMessageBox.StandardButton.Yes:
            s = self._get_config_from_ui().get("streams",[]); cfg = TieredCooldownDialog.get_default_tiered_config()
            cfg.update({"streams": s, "loop_management": {"enabled": True, "watch_period_hours": 24, "quarantine_check_hours": 12}})
            cfg["vision_ai"] = self.saved_config_data.get("vision_ai", {})
            CONFIG_FILE.write_text(json.dumps(cfg, indent=2)); self.load_config()
            
    def _factory_reset(self):
        d = ResetConfirmDialog(self)
        if d.exec():
            try: CONFIG_FILE.unlink(); GUI_SETTINGS_FILE.unlink(); self.load_config()
            except: pass
            
    def _filter_stream_list(self):
        search_text = self.stream_search_box.text().strip().lower()
        visible_count = 0
        total_count = self.stream_list_widget.count()
        
        self.stream_list_widget.blockSignals(True)
        
        for i in range(total_count):
            item = self.stream_list_widget.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            
            name = data.get('name', '')
            name_lower = name.lower()
            url = data.get('page_url', '').lower()
            lat = str(data.get('lat', ''))
            lon = str(data.get('lon', ''))
            c_name = data.get('channel_name', '').lower()
            
            is_match = (
                search_text in name_lower or
                search_text in url or
                search_text in lat or
                search_text in lon or
                search_text in c_name
            )
            
            item.setHidden(not is_match)
            
            if is_match:
                visible_count += 1
                item.setText(f"{visible_count}. {name}")
                
        self.stream_list_widget.blockSignals(False)
        self.lbl_stream_count.setText(f"({visible_count}/{total_count})")

    def _focus_on_stream(self, target_name):
        for i in range(self.stream_list_widget.count()):
            it = self.stream_list_widget.item(i)
            d = it.data(Qt.ItemDataRole.UserRole)
            if not it.isHidden() and d.get('name') == target_name: 
                self.stream_list_widget.scrollToItem(it)
                self.stream_list_widget.setCurrentItem(it)
                break

    def force_cloud_sync(self):
        if self.is_dirty:
            QMessageBox.warning(self, "Unsaved Changes", "Please save your configuration locally first (Click 'Save Changes *') before forcing a sync.")
            return
            
        bot_token = self.bot_token_edit.text().strip()
        if not bot_token:
            QMessageBox.warning(self, "Missing Token", "Cannot sync: Telegram Bot Token is required for authentication.")
            return
            
        if not CONFIG_FILE.exists():
            QMessageBox.warning(self, "Missing File", "Configuration file not found.")
            return
            
        self.force_sync_button.setText("☁️ Syncing...")
        self.force_sync_button.setEnabled(False)
        QApplication.processEvents()
        
        try:
            with open(CONFIG_FILE, 'rb') as f:
                file_content = f.read()
                
            sync_url = "https://wilddetection.com/api/upload_config"
            files = {'config_file': ('birdnet_config.json', file_content, 'application/json')}
            data = {'secret_token': bot_token}
            
            response = requests.post(sync_url, files=files, data=data, timeout=15, verify=True)
            
            if response.status_code == 200:
                QMessageBox.information(self, "Sync Successful", "Configuration successfully uploaded to the Cloud Server.\nThe Web Map is now completely up to date!")
            else:
                QMessageBox.critical(self, "Sync Failed", f"Server rejected the upload.\nHTTP {response.status_code}: {response.text}")
        except requests.exceptions.RequestException as e:
            QMessageBox.critical(self, "Network Error", f"Could not reach the server.\nEnsure your internet is working and the DigitalOcean server is running.\nDetails: {e}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"An unexpected error occurred during sync:\n{e}")
        finally:
            self.force_sync_button.setText("☁️ Force Sync to Cloud")
            self.force_sync_button.setEnabled(True)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(sys.argv)
    editor = ConfigEditor()
    editor.show()
    sys.exit(app.exec())