# FILE: config_editor_dialogs.py
# VERSION: 12.6 - "The Unshackled Patch"
# UPDATED: Added a master switch (QCheckBox) to the Hardware Network Telemetry group box. When toggled off, it disables all Hydra PID throttling and load balancing, running SIMs at absolute maximum capacity. Safely preserves this flag across network settings saves.

import sys
import copy
import time
import math
import logging
import sqlite3
import json
import os
import psutil
import requests 
import shutil
import tempfile
import subprocess
import re
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter, defaultdict

from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton, 
                             QTextEdit, QMessageBox, QGroupBox, QCheckBox, 
                             QSpinBox, QLabel, QWidget, QFormLayout, QLineEdit,
                             QDialogButtonBox, QTableWidget, QTableWidgetItem, 
                             QHeaderView, QGridLayout, QComboBox, QFileDialog, QInputDialog,
                             QApplication, QTabWidget, QSizePolicy, QProgressBar, QDoubleSpinBox, QScrollArea, QSlider, QProgressDialog)
from PyQt6.QtCore import Qt, QTimer, QObject, QEvent, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QPalette, QBrush, QColorConstants

# Import utils
from config_editor_utils import haversine_distance, ChromeMaintenance
# Import network manager
import network_manager
# Import DB Connector (For Maintenance Hub)
import db_connector

# --- Configuration Paths ---
ROOT = Path(__file__).resolve().parent
DATABASE_PATH = ROOT / "detections.db"
BASELINE_CLIPS_DIR = ROOT / "baseline_clips"
CONFIG_FILE = ROOT / "birdnet_config.json"
LOG_ANALYZER_SETTINGS_FILE = ROOT / "log_analyzer_settings.json"
HYDRA_STATE_FILE = ROOT / "hydra_heat_state.json"


# ==============================================================================
# LOG COMPRESSOR & ANALYZER THREAD
# ==============================================================================
class LogParserWorker(QThread):
    progress_update = pyqtSignal(str, int)
    finished = pyqtSignal(str, bool)

    def __init__(self, monitor_path, vision_path, output_dir, timeframe_hours, filters, comp_settings):
        super().__init__()
        self.monitor_path = Path(monitor_path)
        self.vision_path = Path(vision_path)
        self.output_dir = Path(output_dir)
        self.timeframe_hours = timeframe_hours
        self.filters = filters
        self.comp_settings = comp_settings

    def _sanitize(self, text, is_traceback=False):
        clean_text = text.strip()
        
        if self.comp_settings.get("mask_data", True):
            clean_text = re.sub(r"https?://[^\s\"'>]+", "[URL]", clean_text)
            clean_text = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?\b", "[IP]", clean_text)
            clean_text = re.sub(r"0x[0-9a-fA-F]+", "[HEX]", clean_text)
            clean_text = re.sub(r"\[L\d+\]", "[L-X]", clean_text)

        if not is_traceback and self.comp_settings.get("truncate", True):
            if len(clean_text) > 250:
                clean_text = clean_text[:247] + "..."
                
        clean_text = clean_text.replace('\n', ' | ')
        return clean_text

    def run(self):
        try:
            cutoff_time = datetime.now() - timedelta(hours=self.timeframe_hours) if self.timeframe_hours > 0 else datetime.min
            
            categories = defaultdict(Counter)
            tracebacks = set()
            normal_sample =[]
            
            timestamp_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
            
            total_size = 0
            if self.monitor_path.exists(): total_size += self.monitor_path.stat().st_size
            if self.vision_path.exists(): total_size += self.vision_path.stat().st_size
            
            if total_size == 0:
                self.finished.emit("No log files found or files are empty.", False)
                return

            processed_bytes = 0

            def process_file(filepath):
                nonlocal processed_bytes
                if not filepath.exists(): return
                
                file_mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
                current_block = ""
                block_timestamp = None
                
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        processed_bytes += len(line.encode('utf-8'))
                        if processed_bytes % (1024 * 500) < 1000:
                            pct = int((processed_bytes / total_size) * 100)
                            self.progress_update.emit(f"Parsing {filepath.name}...", pct)

                        match = timestamp_pattern.match(line)
                        if match:
                            self._categorize_block(current_block, block_timestamp, cutoff_time, categories, tracebacks, normal_sample, fallback_ts=file_mtime)
                            try:
                                block_timestamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                            except:
                                block_timestamp = None
                            
                            clean_line = re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} -.*? - ", "", line)
                            current_block = clean_line
                        else:
                            current_block += line
                            
                self._categorize_block(current_block, block_timestamp, cutoff_time, categories, tracebacks, normal_sample, fallback_ts=file_mtime)

            process_file(self.monitor_path)
            process_file(self.vision_path)
            
            log_dir = ROOT / "app_logs"
            if log_dir.exists():
                for lf in log_dir.glob("*.log"):
                    process_file(lf)

            self.progress_update.emit("Generating summary file...", 99)
            
            out_filename = f"GBR_Log_Summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            out_path = self.output_dir / out_filename
            top_n = self.comp_settings.get("top_n", 15)
            
            with open(out_path, "w", encoding="utf-8") as out:
                out.write("=================================================================\n")
                out.write(f"GLOBAL BIRDSONG RADIO - COMPRESSED LOG DIAGNOSTIC\n")
                out.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                time_str = f"Last {self.timeframe_hours} Hours" if self.timeframe_hours > 0 else "All Time"
                out.write(f"Timeframe Analyzed: {time_str}\n")
                out.write("=================================================================\n\n")
                
                if self.filters.get("tracebacks", True):
                    out.write("--- CRITICAL TRACEBACKS, CRASHES & EMERGENCY BRAKES ---\n")
                    if tracebacks:
                        for tb in tracebacks:
                            out.write(tb.strip() + "\n\n")
                    else:
                        out.write("No critical crashes or tracebacks found.\n\n")
                        
                if self.filters.get("selenium", True):
                    self._write_category(out, "SELENIUM / WEBDRIVER CRASHES", categories["selenium"], top_n)
                    
                if self.filters.get("ffmpeg", True):
                    self._write_category(out, "FFMPEG / AUDIO CAPTURE ERRORS", categories["ffmpeg"], top_n)
                    
                if self.filters.get("network", True):
                    self._write_category(out, "NETWORK / PROXY / CLOUD SYNC ERRORS", categories["network"], top_n)
                    
                if self.filters.get("streams", True):
                    self._write_category(out, "STREAM STATUS & RESOLVER EVENTS", categories["streams"], top_n)
                    
                if self.filters.get("normal", True):
                    out.write("--- NORMAL SYSTEM ACTIVITY (PROOF OF LIFE SAMPLE) ---\n")
                    if normal_sample:
                        out.write(f"Showing {len(normal_sample)} sample events out of many:\n")
                        for ns in normal_sample:
                            out.write(ns.strip() + "\n")
                        out.write("\n")
                    else:
                        out.write("No normal activity (Alerts/Bio-Hits) detected in this timeframe.\n\n")

            self.progress_update.emit("Done!", 100)
            self.finished.emit(str(out_path), True)

        except Exception as e:
            import traceback as tb
            err = f"Failed to parse logs: {str(e)}\n{tb.format_exc()}"
            self.finished.emit(err, False)

    def _write_category(self, file_handle, title, counter_obj, top_n):
        file_handle.write(f"--- {title} ---\n")
        if not counter_obj:
            file_handle.write("No events found in this category.\n\n")
            return
            
        common_items = counter_obj.most_common(top_n)
        for msg, count in common_items:
            file_handle.write(f"[x{count}] {msg}\n")
            
        omitted = len(counter_obj) - top_n
        if omitted > 0:
            file_handle.write(f"... and {omitted} more unique error types hidden (Below Top {top_n}).\n")
            
        file_handle.write("\n")

    def _categorize_block(self, block, timestamp, cutoff, categories, tracebacks, normal_sample, fallback_ts=None):
        if not block.strip(): return
        
        l_block = block.lower()
        is_tb = (
            "traceback (most recent call last)" in l_block or 
            "critical startup error" in l_block or
            "process finished/died" in l_block or
            "emergency brake" in l_block or
            "syntaxerror:" in l_block or
            "nameerror:" in l_block or
            "typeerror:" in l_block or
            "valueerror:" in l_block or
            "exception:" in l_block
        )

        # If no timestamp but it's a traceback/crash, use the file modification time
        if not timestamp and is_tb and fallback_ts:
            timestamp = fallback_ts

        if not timestamp: return
        if timestamp < cutoff: return
        
        clean_block = self._sanitize(block, is_traceback=is_tb)
        
        if is_tb:
            tracebacks.add(f"[{timestamp.strftime('%Y-%m-%d %H:%M:%S')}] {clean_block}")
        elif "session not created" in l_block or "chrome not reachable" in l_block or "webdriver" in l_block or "undetected_chromedriver" in l_block:
            categories["selenium"][clean_block] += 1
        elif "ffmpeg error:" in l_block or "ffmpeg failed" in l_block:
            categories["ffmpeg"][clean_block] += 1
        elif "timeouterror" in l_block or "connection aborted" in l_block or "cloud sync" in l_block or "telegram error" in l_block or "https pivot" in l_block:
            categories["network"][clean_block] += 1
        elif "completely failed" in l_block or "skipping:" in l_block or "auto-resolver failed" in l_block or "link expiration" in l_block:
            categories["streams"][clean_block] += 1
        elif "alert:" in l_block or "bio-hit:" in l_block or "gemini public alert" in l_block:
            if len(normal_sample) < 30:
                normal_sample.append(f"[{timestamp.strftime('%H:%M:%S')}] {clean_block}")


# --- SCROLL WHEEL EVENT FILTER ---
class ScrollStealFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel:
            if not obj.hasFocus():
                event.ignore()
                return True
        return super().eventFilter(obj, event)


class NetworkTelemetryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Network Telemetry, Quotas & Hydra PID Tuning")
        self.setMinimumSize(750, 600)
        self.layout = QVBoxLayout(self)
        
        self.scroll_filter = ScrollStealFilter(self)
        self.interfaces = network_manager.get_active_interfaces()
        
        # Initialize Settings
        self.pid_settings = {"ema_alpha": 0.3, "soft_lockout_pct": 90, "global_brake_pct": 95, "throttling_enabled": True}
        if CONFIG_FILE.exists():
            try:
                cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                self.pid_settings.update(cfg.get("hydra_pid_settings", {}))
            except: pass

        self.tabs = QTabWidget()
        
        # --- TAB 1: SIM Quotas & Telemetry ---
        self.tab_quotas = QWidget()
        self.tab_quotas_layout = QVBoxLayout(self.tab_quotas)
        
        info = QLabel("<b>Set Data Limits & Billing Cycles.</b> Engines will automatically stop using a SIM if its quota is exceeded for the current billing cycle. Vision Engines will proportionally load-balance traffic toward the SIMs with the most remaining data.")
        info.setWordWrap(True)
        self.tab_quotas_layout.addWidget(info)
        
        self.lbl_global_heat = QLabel("<b>Main Pipeline Status:</b> Calculating...")
        self.lbl_global_heat.setStyleSheet("font-size: 14px; margin-top: 10px; margin-bottom: 10px; padding: 8px; border: 1px solid #444; border-radius: 4px; background-color: #2b2b2b;")
        
        self.legend_tooltip = (
            "<div style='background-color: #1e1e1e; color: #eee; padding: 4px;'>"
            "<b><u>Telemetry Legend & Guide</u></b><br><br>"
            "❌ <b>OFFLINE:</b> Adapter is physically disconnected from Windows.<br>"
            "🔥 <b>IP BANNED (403):</b> Blocked by YouTube. Needs physical replug.<br>"
            "⛔ <b>DATA DEPLETED:</b> Reached the monthly SIM GB plan limit.<br>"
            "🛑 <b>LOCAL SPEED BLOCKED:</b> Reached 100% heat (Hourly speed limit maxed).<br>"
            "🚨 <b>GLOBAL EMERGENCY BRAKE:</b> System paused to prevent massive failure.<br>"
            "🟠 <b>THROTTLED MAX:</b> 95%+ Heat (Soft-Lockout to prevent blocking).<br>"
            "🔺 <b>Heating Up:</b> Traffic average is currently increasing.<br>"
            "🔽 <b>Cooling Down:</b> Traffic average is currently decreasing.<br>"
            "➖ <b>Stable:</b> Nominal traffic load.<br><hr>"
            "🐦 <b>Audio %:</b> Est. bandwidth being used by BirdNET.<br>"
            "📷 <b>Vision %:</b> Est. bandwidth being used by Gemini."
            "</div>"
        )
        self.lbl_global_heat.setToolTip(self.legend_tooltip)
        self.tab_quotas_layout.addWidget(self.lbl_global_heat)
        
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.container_layout = QVBoxLayout(self.container)
        
        self.quota_inputs = {}
        self.dynamic_widgets = {}
        
        for iface in self.interfaces:
            name = iface['name']
            group = QGroupBox(f"Adapter: {name} ({iface['ip']})")
            
            group.setStyleSheet("""
                QGroupBox { 
                    border: 1px solid #555; 
                    border-radius: 4px; 
                    margin-top: 18px; 
                    padding-top: 15px; 
                    font-weight: bold; 
                } 
                QGroupBox::title { 
                    subcontrol-origin: margin; 
                    subcontrol-position: top left;
                    left: 10px; 
                    padding: 0 5px; 
                    color: #00E5FF; 
                }
            """)
            g_layout = QVBoxLayout()
            
            progress = QProgressBar()
            progress.setFixedHeight(25)
            progress.setValue(0)
            
            lbl_24h = QLabel("<b>Last 24h Traffic:</b> Calculating...")
            split_lbl = QLabel("<b><span style='color: #00E5FF;'>Current Speed (Last 60m):</span></b> Calculating...")
            heat_lbl = QLabel("<b>Network Heat Status:</b> Calculating...")
            
            self.dynamic_widgets[name] = {
                'progress': progress,
                'lbl_24h': lbl_24h,
                'split_lbl': split_lbl,
                'heat_lbl': heat_lbl
            }
            
            g_layout.addWidget(progress)
            g_layout.addWidget(lbl_24h)
            g_layout.addWidget(split_lbl)
            g_layout.addWidget(heat_lbl)
            
            form = QFormLayout()
            
            spin_plan = QDoubleSpinBox()
            spin_plan.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            spin_plan.installEventFilter(self.scroll_filter)
            spin_plan.setRange(0, 99999)
            spin_plan.setDecimals(1)
            spin_plan.setSuffix(" GB (0 = Unlimited)")
            
            spin_pct = QSpinBox()
            spin_pct.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            spin_pct.installEventFilter(self.scroll_filter)
            spin_pct.setRange(1, 100)
            spin_pct.setSuffix("%")
            
            lbl_effective = QLabel()
            
            spin_day = QSpinBox()
            spin_day.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            spin_day.installEventFilter(self.scroll_filter)
            spin_day.setRange(1, 31)
            spin_day.setSuffix(" (Day of Month)")
            
            spin_speed = QDoubleSpinBox()
            spin_speed.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            spin_speed.installEventFilter(self.scroll_filter)
            spin_speed.setRange(0, 9999)
            spin_speed.setDecimals(2)
            spin_speed.setSuffix(" GB/hr (0 = Unlimited)")
            spin_speed.setToolTip("Hard limit. If the rolling 60-minute average exceeds this, the Vision Engine will instantly drop this SIM until it cools down.")
            
            plan_size_gb = 0.0
            allowed_pct = 100
            reset_day = 1
            max_gb_hr = 0.0
            
            try:
                with db_connector.get_db_connection(force_local=True) as con:
                    cur = con.cursor()
                    try:
                        cur.execute("SELECT limit_gb, reset_day, plan_size_gb, allowed_percent, max_gb_per_hour FROM network_quotas WHERE interface_name = ?", (name,))
                        row = cur.fetchone()
                        if row:
                            limit_gb = float(row[0] or 0.0)
                            reset_day = int(row[1] or 1)
                            plan_size_gb = float(row[2]) if row[2] is not None else limit_gb
                            allowed_pct = int(row[3]) if row[3] is not None else 100
                            max_gb_hr = float(row[4]) if row[4] is not None else 0.0
                            if plan_size_gb == 0 and limit_gb > 0:
                                plan_size_gb = limit_gb
                    except sqlite3.OperationalError:
                        cur.execute("SELECT limit_gb, reset_day FROM network_quotas WHERE interface_name = ?", (name,))
                        row = cur.fetchone()
                        if row:
                            limit_gb = float(row[0] or 0.0)
                            plan_size_gb = limit_gb
                            reset_day = int(row[1] or 1)
                            allowed_pct = 100
                            max_gb_hr = 0.0
            except:
                pass
                
            spin_plan.setValue(plan_size_gb)
            spin_pct.setValue(allowed_pct)
            spin_day.setValue(reset_day)
            spin_speed.setValue(max_gb_hr)
            
            def create_update_func(l, p, a):
                def update(*args):
                    eff = p.value() * (a.value() / 100.0)
                    if eff > 0:
                        l.setText(f"<b>Effective Monthly Limit:</b> <span style='color: #00E676;'>{eff:.1f} GB</span>")
                    else:
                        l.setText("<b>Effective Monthly Limit:</b> <span style='color: #888;'>Unlimited</span>")
                return update
                
            updater = create_update_func(lbl_effective, spin_plan, spin_pct)
            spin_plan.valueChanged.connect(updater)
            spin_pct.valueChanged.connect(updater)
            updater()
            
            self.quota_inputs[name] = {'plan': spin_plan, 'pct': spin_pct, 'day': spin_day, 'speed': spin_speed}
            
            form.addRow("Total Plan Size:", spin_plan)
            form.addRow("Allowed Usage:", spin_pct)
            form.addRow("", lbl_effective)
            form.addRow("Billing Cycle Reset Day:", spin_day)
            form.addRow(QLabel("<hr>"))
            form.addRow("Max Allowed Speed:", spin_speed)
            
            g_layout.addSpacing(10)
            g_layout.addLayout(form)
            group.setLayout(g_layout)
            self.container_layout.addWidget(group)
            
        self.container_layout.addStretch()
        self.scroll.setWidget(self.container)
        self.tab_quotas_layout.addWidget(self.scroll)
        self.tabs.addTab(self.tab_quotas, "SIM Quotas & Telemetry")


        # --- TAB 2: Hydra PID Tuning & Resets ---
        self.tab_pid = QWidget()
        self.tab_pid_layout = QVBoxLayout(self.tab_pid)
        
        pid_info = QLabel("<b>Hydra PID Tuning & Algorithm Overrides</b><br>Fine-tune how the dynamic load balancer reacts to network heat, or manually reset the algorithms for debugging purposes.")
        pid_info.setWordWrap(True)
        self.tab_pid_layout.addWidget(pid_info)
        
        # Tuning Group
        tune_group = QGroupBox("Heating Control & Smoothing Parameters")
        tune_form = QFormLayout(tune_group)
        
        self.spin_ema_alpha = QDoubleSpinBox()
        self.spin_ema_alpha.setRange(0.01, 1.00)
        self.spin_ema_alpha.setSingleStep(0.05)
        self.spin_ema_alpha.setValue(self.pid_settings.get("ema_alpha", 0.3))
        self.spin_ema_alpha.setToolTip(
            "Exponential Moving Average (EMA) Alpha (0.01 - 1.00).\n"
            "Determines how quickly the system reacts to bandwidth spikes.\n\n"
            "• Lower (e.g., 0.1): Smoother, ignores short bursts, slower to engage brakes.\n"
            "• Higher (e.g., 0.8): Twitchy, reacts instantly to spikes, prevents accidental overages but may cause rapid toggling.\n\n"
            "Default: 0.3"
        )
        
        self.spin_soft_lockout = QSpinBox()
        self.spin_soft_lockout.setRange(50, 100)
        self.spin_soft_lockout.setSuffix("%")
        self.spin_soft_lockout.setValue(self.pid_settings.get("soft_lockout_pct", 90))
        self.spin_soft_lockout.setToolTip(
            "SIM Soft-Lockout Threshold (%).\n"
            "When a single SIM reaches this heat level, the Vision Engine will stop assigning new streams to it.\n\n"
            "This prevents the SIM from hitting 100% and triggering a hard OS-level block.\n\n"
            "Default: 90%"
        )
        
        self.spin_global_brake = QSpinBox()
        self.spin_global_brake.setRange(50, 100)
        self.spin_global_brake.setSuffix("%")
        self.spin_global_brake.setValue(self.pid_settings.get("global_brake_pct", 95))
        self.spin_global_brake.setToolTip(
            "Global Emergency Brake Cap (%).\n"
            "If the aggregate 'Main Pipe' heat across ALL SIMs reaches this level, the master Scheduler will forcefully pause ALL worker thread launches.\n\n"
            "This is the ultimate failsafe against catastrophic data leaks.\n\n"
            "Default: 95%"
        )
        
        tune_form.addRow("EMA Alpha (Smoothing Factor):", self.spin_ema_alpha)
        tune_form.addRow("SIM Soft-Lockout Threshold:", self.spin_soft_lockout)
        tune_form.addRow("Global Emergency Brake Cap:", self.spin_global_brake)
        self.tab_pid_layout.addWidget(tune_group)
        
        # Resets Group
        reset_group = QGroupBox("Manual Overrides & Debug Resets")
        reset_layout = QVBoxLayout(reset_group)
        
        btn_reset_ema = QPushButton("🧠 Reset PID/EMA Memory (Algorithm Debug)")
        btn_reset_ema.setToolTip("Deletes hydra_heat_state.json. Instantly wipes the algorithm's 'memory' (EMA). Heat snaps directly to the instantaneous raw speed. Does NOT touch SQLite or monthly data.")
        btn_reset_ema.setStyleSheet("background-color: #5C6BC0; color: white; font-weight: bold;")
        btn_reset_ema.clicked.connect(self.reset_ema_state)
        
        btn_reset_1h = QPushButton("⏱️ Reset 1-Hour Speed Window (Traffic Debug)")
        btn_reset_1h.setToolTip("Deletes records from the last 60 minutes in the SQLite logs. Drops your 'GB/hr' speedometer to 0 and clears 100% Heat lockouts. PRESERVES your Monthly Billing Quota.")
        btn_reset_1h.setStyleSheet("background-color: #00897B; color: white; font-weight: bold;")
        btn_reset_1h.clicked.connect(self.reset_1h_window)
        
        btn_nuke_all = QPushButton("☢️ Nuke ALL Data Usage (SIM Swap / Billing Reset)")
        btn_nuke_all.setToolTip("Wipes the SQLite hardware and app logs completely. Use this if you swap SIM cards or your provider resets your plan early.")
        btn_nuke_all.setStyleSheet("background-color: #D32F2F; color: white; font-weight: bold;")
        btn_nuke_all.clicked.connect(self.nuke_all_data)
        
        reset_layout.addWidget(btn_reset_ema)
        reset_layout.addSpacing(10)
        reset_layout.addWidget(btn_reset_1h)
        reset_layout.addSpacing(10)
        reset_layout.addWidget(btn_nuke_all)
        self.tab_pid_layout.addWidget(reset_group)
        
        self.tab_pid_layout.addStretch()
        self.tabs.addTab(self.tab_pid, "Hydra PID Tuning & Resets")

        self.layout.addWidget(self.tabs)
        
        # --- FOOTER ---
        btn_layout = QHBoxLayout()
        btn_save = QPushButton("Save Config & Quotas")
        btn_save.setStyleSheet("background-color: #0078d7; color: white; font-weight: bold; padding: 10px;")
        btn_save.clicked.connect(self.save_quotas)
        btn_close = QPushButton("Cancel")
        btn_close.clicked.connect(self.reject)
        
        btn_layout.addStretch()
        btn_layout.addWidget(btn_close)
        btn_layout.addWidget(btn_save)
        self.layout.addLayout(btn_layout)

        self.refresh_telemetry_data()
        self.live_timer = QTimer(self)
        self.live_timer.timeout.connect(self.refresh_telemetry_data)
        self.live_timer.start(10000)

    # --- RESET FUNCTIONS ---
    def reset_ema_state(self):
        msg = "Wipe the algorithm's memory (EMA)?\n\nThis will force the heat to snap to the raw instantaneous speed. It does NOT affect your monthly data."
        if QMessageBox.question(self, "Reset PID Memory", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            try:
                if HYDRA_STATE_FILE.exists():
                    HYDRA_STATE_FILE.unlink()
                QMessageBox.information(self, "Success", "PID memory wiped. Heat will recalculate on the next tick.")
                self.refresh_telemetry_data()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to reset EMA: {e}")

    def reset_1h_window(self):
        msg = "Reset the 1-Hour Speedometer?\n\nThis will delete the last 60 minutes of hardware logs, clearing 'Local Speed Blocked' statuses. It PRESERVES your monthly billing data.\n\nProceed?"
        if QMessageBox.question(self, "Reset 1H Speed", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            try:
                cutoff = time.time() - 3600
                with db_connector.get_db_connection(force_local=True) as con:
                    con.execute("DELETE FROM network_hardware_logs WHERE timestamp >= ?", (cutoff,))
                    con.execute("DELETE FROM network_app_logs WHERE timestamp >= ?", (cutoff,))
                if HYDRA_STATE_FILE.exists():
                    HYDRA_STATE_FILE.unlink()
                QMessageBox.information(self, "Success", "1-Hour speed window reset. Speed limits cleared.")
                self.refresh_telemetry_data()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to reset 1H window: {e}")

    def nuke_all_data(self):
        msg = "☢️ DANGER: NUKE ALL DATA USAGE ☢️\n\nThis will permanently delete all recorded hardware and application data logs from the database.\n\nOnly use this if you have swapped SIM cards or your billing cycle was manually reset by your provider early.\n\nProceed?"
        if QMessageBox.critical(self, "Nuke All Data", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            try:
                with db_connector.get_db_connection(force_local=True) as con:
                    con.execute("DELETE FROM network_hardware_logs")
                    con.execute("DELETE FROM network_app_logs")
                if HYDRA_STATE_FILE.exists():
                    HYDRA_STATE_FILE.unlink()
                QMessageBox.information(self, "Nuked", "All data usage records have been destroyed. You are starting fresh.")
                self.refresh_telemetry_data()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to nuke data: {e}")

    def refresh_telemetry_data(self):
        cutoff_24h = time.time() - 86400
        cutoff_1h = time.time() - 3600
        
        # Read config for throttle flag
        throttle_enabled = True
        try:
            if CONFIG_FILE.exists():
                cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                throttle_enabled = cfg.get("hydra_pid_settings", {}).get("throttling_enabled", True)
        except: pass
        
        try:
            heat_state = {}
            if HYDRA_STATE_FILE.exists():
                try:
                    heat_state = json.loads(HYDRA_STATE_FILE.read_text(encoding='utf-8'))
                except: pass

            # Global Heat
            g_data = heat_state.get("GLOBAL")
            if not throttle_enabled:
                g_ind = "<span style='color: #FF3D00; font-weight: bold;'>[⚠️ THROTTLING DISABLED]</span>"
            elif g_data:
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
            else:
                g_ind = "<span style='color: #888;'>Waiting for telemetry...</span>"
                
            self.lbl_global_heat.setText(f"<b>Main Pipeline Status:</b> {g_ind}")
            
            with db_connector.get_db_connection(force_local=True) as con:
                cur = con.cursor()
                
                for iface in self.interfaces:
                    name = iface['name']
                    if name not in self.dynamic_widgets:
                        continue
                    
                    widgets = self.dynamic_widgets[name]
                    used_bytes, limit_bytes, is_over = network_manager.get_interface_quota_status(name)
                    is_monthly_dead = (limit_bytes > 0 and used_bytes >= limit_bytes)
                    
                    # Interface Heat
                    i_data = heat_state.get(name)
                    if i_data:
                        heat = i_data['heat']
                        i_arrow = i_data['arrow']
                        
                        if "❌" in i_arrow:
                            i_ind = "<span style='color: #9E9E9E; font-weight: bold;'>[❌ OFFLINE (Disconnected)]</span>"
                        elif "🔥" in i_arrow:
                            i_ind = "<span style='color: #FF5722; font-weight: bold;'>[🔥 IP BANNED (Needs Replug)]</span>"
                        elif is_monthly_dead:
                            i_ind = "<span style='color: #E91E63; font-weight: bold;'>[⛔ DATA DEPLETED (Monthly Cap)]</span>"
                        elif heat >= 1.0:
                            i_ind = "<span style='color: #FF3D00; font-weight: bold;'>[🛑 LOCAL SPEED BLOCKED (100% Heat)]</span>"
                        elif heat >= 0.95:
                            i_ind = f"<span style='color: #FF9800; font-weight: bold;'>[🟠 THROTTLED MAX] ({heat*100:.1f}%)</span>"
                        elif "🔺" in i_arrow:
                            i_ind = f"<span style='color: #FF5252; font-weight: bold;'>[🔺 Heating Up] ({heat*100:.1f}%)</span>"
                        elif "🔽" in i_arrow:
                            i_ind = f"<span style='color: #448AFF; font-weight: bold;'>[🔽 Cooling Down] ({heat*100:.1f}%)</span>"
                        else:
                            i_ind = f"<span style='color: #4CAF50; font-weight: bold;'>[➖ Stable] ({heat*100:.1f}%)</span>"
                            
                        widgets['heat_lbl'].setText(f"<b>Network Heat Status:</b> {i_ind}")
                    else:
                        widgets['heat_lbl'].setText("<b>Network Heat Status:</b> <span style='color: #888;'>Waiting...</span>")
                    
                    cur.execute("SELECT SUM(bytes_sent + bytes_recv) FROM network_hardware_logs WHERE interface_name = ? AND timestamp >= ?", (name, cutoff_24h))
                    row_24h = cur.fetchone()
                    last_24h_bytes = row_24h[0] if row_24h and row_24h[0] else 0
                    used_24h_gb = last_24h_bytes / (1024**3)
                    
                    cur.execute("SELECT SUM(bytes_sent + bytes_recv) FROM network_hardware_logs WHERE interface_name = ? AND timestamp >= ?", (name, cutoff_1h))
                    row_1h = cur.fetchone()
                    last_1h_bytes = row_1h[0] if row_1h and row_1h[0] else 0
                    used_1h_gb = last_1h_bytes / (1024**3)
                    
                    cur.execute("SELECT engine_type, SUM(bytes_used) FROM network_app_logs WHERE interface_name = ? AND timestamp >= ? GROUP BY engine_type", (name, cutoff_1h))
                    audio_1h_bytes = 0
                    vision_1h_bytes = 0
                    for r in cur.fetchall():
                        if r[0] == 'audio': audio_1h_bytes = r[1]
                        elif r[0] == 'vision': vision_1h_bytes = r[1]
                    
                    if limit_bytes > 0:
                        pct = int((used_bytes / limit_bytes) * 100)
                        widgets['progress'].setValue(min(pct, 100))
                        if is_monthly_dead:
                            widgets['progress'].setStyleSheet("QProgressBar::chunk { background-color: #E91E63; }") # Pink/Red for monthly death
                        elif pct >= 80:
                            widgets['progress'].setStyleSheet("QProgressBar::chunk { background-color: #FBC02D; }")
                        else:
                            widgets['progress'].setStyleSheet("QProgressBar::chunk { background-color: #388E3C; }")
                        widgets['progress'].setFormat(f"{used_bytes / (1024**3):.2f} GB / {limit_bytes / (1024**3):.2f} GB Used This Cycle ({pct}%)")
                    else:
                        widgets['progress'].setValue(0)
                        widgets['progress'].setFormat(f"{used_bytes / (1024**3):.2f} GB Used This Cycle (No Monthly Limit)")
                        
                    total_app_bytes = audio_1h_bytes + vision_1h_bytes
                    audio_pct = int((audio_1h_bytes / total_app_bytes) * 100) if total_app_bytes > 0 else 0
                    vision_pct = int((vision_1h_bytes / total_app_bytes) * 100) if total_app_bytes > 0 else 0
                    
                    widgets['lbl_24h'].setText(f"<b>Last 24h Traffic:</b> {used_24h_gb:.2f} GB")
                    
                    speed_color = "#FF3D00" if (is_over and limit_bytes <= 0) else "#00E5FF" 
                    widgets['split_lbl'].setText(f"<b><span style='color: {speed_color};'>Current Speed (Last 60m):</span></b> {used_1h_gb:.3f} GB/hr (Est. Split: 🐦 Audio {audio_pct}% | 📷 Vision {vision_pct}%)")
                    
        except Exception as e:
            logging.error(f"Live telemetry refresh error: {e}")

    def save_quotas(self):
        try:
            # 1. Save SQLite Quotas
            with db_connector.get_db_connection(force_local=True) as con:
                for name, inputs in self.quota_inputs.items():
                    plan_size = inputs['plan'].value()
                    allowed_pct = inputs['pct'].value()
                    reset_day = inputs['day'].value()
                    max_speed = inputs['speed'].value()
                    limit_gb = plan_size * (allowed_pct / 100.0)
                    
                    con.execute("REPLACE INTO network_quotas (interface_name, limit_gb, reset_day, plan_size_gb, allowed_percent, max_gb_per_hour) VALUES (?, ?, ?, ?, ?, ?)", 
                                (name, limit_gb, reset_day, plan_size, allowed_pct, max_speed))
                                
            # 2. Save PID Parameters to JSON
            if CONFIG_FILE.exists():
                try:
                    cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                    
                    # PRESERVE THE MASTER SWITCH FLAG
                    existing_pid = cfg.get("hydra_pid_settings", {})
                    throttle_flag = existing_pid.get("throttling_enabled", True)
                    
                    cfg["hydra_pid_settings"] = {
                        "ema_alpha": self.spin_ema_alpha.value(),
                        "soft_lockout_pct": self.spin_soft_lockout.value(),
                        "global_brake_pct": self.spin_global_brake.value(),
                        "throttling_enabled": throttle_flag
                    }
                    tmp_file = CONFIG_FILE.with_suffix('.tmp')
                    tmp_file.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
                    os.replace(tmp_file, CONFIG_FILE)
                except Exception as e:
                    logging.error(f"Failed to save PID settings: {e}")

            QMessageBox.information(self, "Success", "Network Quotas and PID parameters saved successfully.")
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save: {e}")


class StreamAuditDialog(QDialog):
    def __init__(self, stream_data_list, current_report_config, audio_all, audio_dsp, audio_multi, audio_birdnet, parent=None):
        super().__init__(parent)
        self.streams = copy.deepcopy(stream_data_list)
        self.report_config = copy.deepcopy(current_report_config)
        
        self.audio_all = audio_all
        self.audio_dsp = audio_dsp
        self.audio_multi = audio_multi
        self.audio_birdnet = audio_birdnet
        
        self.parent_editor = parent 
        self.setWindowTitle("Stream Audit & Diagnostics")
        self.setMinimumSize(850, 750)
        self.layout = QVBoxLayout(self)

        # --- THE TELEGRAM TOOLTIP LEGEND ---
        self.legend_tooltip = (
            "<div style='background-color: #2b2b2b; color: #eee; padding: 4px;'>"
            "<b><u>Telemetry Legend & Guide</u></b><br><br>"
            "❌ <b>OFFLINE:</b> Adapter is physically disconnected from Windows.<br>"
            "🔥 <b>IP BANNED (403):</b> Blocked by YouTube. Needs physical replug.<br>"
            "⛔ <b>DATA DEPLETED:</b> Reached the monthly SIM GB plan limit.<br>"
            "🛑 <b>LOCAL SPEED BLOCKED:</b> Reached 100% heat (Hourly speed limit maxed).<br>"
            "🚨 <b>GLOBAL EMERGENCY BRAKE:</b> System paused to prevent massive failure.<br>"
            "🟠 <b>THROTTLED MAX:</b> 95%+ Heat (Soft-Lockout to prevent blocking).<br>"
            "🔺 <b>Heating Up:</b> Traffic average is currently increasing.<br>"
            "🔽 <b>Cooling Down:</b> Traffic average is currently decreasing.<br>"
            "➖ <b>Stable:</b> Nominal traffic load.<br><hr>"
            "🐦 <b>Audio %:</b> Est. bandwidth being used by BirdNET.<br>"
            "📷 <b>Vision %:</b> Est. bandwidth being used by Gemini."
            "</div>"
        )

        # --- TOP CONTROLS ---
        top_layout = QHBoxLayout()
        self.btn_run_diag = QPushButton("Run Data Diagnostics")
        self.btn_run_diag.clicked.connect(self.run_diagnostics)
        
        self.btn_health = QPushButton("Mass Network Health Check")
        self.btn_health.clicked.connect(self.run_mass_health_check)
        self.btn_health.setStyleSheet("font-weight: bold;")
        
        self.btn_reconstruct = QPushButton("Reconstruct Timeline (DB)")
        self.btn_reconstruct.clicked.connect(self.reconstruct_timeline)
        self.btn_reconstruct.setToolTip("Queries the database to find the first detection date for each stream and updates the 'Date Created' field.")
        
        self.btn_reset_counts = QPushButton("Reset Scheduler Fairness (Total Amnesty)")
        self.btn_reset_counts.clicked.connect(self.reset_scheduler_counts)
        self.btn_reset_counts.setStyleSheet("background-color: #AB47BC; color: white; font-weight: bold;")
        self.btn_reset_counts.setToolTip("Resets all counts AND clears all penalties. Forces immediate re-check of ALL enabled streams.")
        
        top_layout.addWidget(self.btn_run_diag)
        top_layout.addWidget(self.btn_health)
        top_layout.addWidget(self.btn_reconstruct)
        top_layout.addWidget(self.btn_reset_counts) 
        self.layout.addLayout(top_layout)

        # --- REPORT CONFIGURATION ---
        report_group = QGroupBox("Automated Telegram Reporting")
        report_main_layout = QVBoxLayout()
        
        report_top_layout = QHBoxLayout()
        self.cb_report_enable = QCheckBox("Broadcast Periodic Health Status to Telegram")
        self.cb_report_enable.setChecked(self.report_config.get('enabled', True))
        
        report_top_layout.addWidget(self.cb_report_enable)
        report_top_layout.addStretch()
        
        report_top_layout.addWidget(QLabel("Frequency:"))
        self.combo_report_freq = QComboBox()
        
        self.freq_map = {
            "Every 30 Minutes": 0.5,
            "Every 1 Hour": 1.0,
            "Every 2 Hours": 2.0,
            "Every 3 Hours": 3.0,
            "Every 6 Hours": 6.0,
            "Every 12 Hours": 12.0,
            "Every 24 Hours": 24.0
        }
        
        self.combo_report_freq.addItems(self.freq_map.keys())
        current_hours = float(self.report_config.get('interval_hours', 12.0))
        closest_text = "Every 12 Hours"
        min_diff = float('inf')
        
        for text, hours in self.freq_map.items():
            diff = abs(hours - current_hours)
            if diff < min_diff:
                min_diff = diff
                closest_text = text
                
        self.combo_report_freq.setCurrentText(closest_text)
        report_top_layout.addWidget(self.combo_report_freq)
        
        self.cb_species_alerts_enable = QCheckBox("Enable Real-Time Species Alerts (Uncheck to receive ONLY Periodic Health Status)")
        self.cb_species_alerts_enable.setChecked(self.report_config.get('send_species_alerts', True))
        
        # New Quota Alerts
        self.cb_alert_hourly_speed = QCheckBox("Alert on SIM Hourly Speed Limit Drop/Recovery")
        self.cb_alert_hourly_speed.setChecked(self.report_config.get('alert_hourly_speed', True))
        
        self.cb_alert_monthly_quota = QCheckBox("Alert on SIM Monthly Quota Drop/Recovery")
        self.cb_alert_monthly_quota.setChecked(self.report_config.get('alert_monthly_quota', True))

        # --- 4 GRANULAR AUDIO CHECKBOXES ---
        audio_group = QGroupBox("Telegram Media Attachments (Save Bandwidth)")
        audio_grid = QGridLayout()
        
        self.cb_send_audio_all = QCheckBox("Send ALL Audio Clips (Master Override)")
        self.cb_send_audio_all.setChecked(self.audio_all)
        self.cb_send_audio_all.setStyleSheet("font-weight: bold; color: #EF5350;")
        
        self.cb_send_audio_dsp = QCheckBox("Send DSP Audio Clips (Recommended)")
        self.cb_send_audio_dsp.setChecked(self.audio_dsp)
        
        self.cb_send_audio_multi = QCheckBox("Send Multimodal Audio Clips (Recommended)")
        self.cb_send_audio_multi.setChecked(self.audio_multi)
        
        self.cb_send_audio_birdnet = QCheckBox("Send BirdNET Audio Clips (High Bandwidth)")
        self.cb_send_audio_birdnet.setChecked(self.audio_birdnet)
        
        audio_grid.addWidget(self.cb_send_audio_all, 0, 0, 1, 2)
        audio_grid.addWidget(self.cb_send_audio_dsp, 1, 0)
        audio_grid.addWidget(self.cb_send_audio_multi, 1, 1)
        audio_grid.addWidget(self.cb_send_audio_birdnet, 2, 0)
        
        audio_group.setLayout(audio_grid)
        
        self.cb_send_audio_all.toggled.connect(self._toggle_audio_master)
        self._toggle_audio_master(self.cb_send_audio_all.isChecked())
        
        report_main_layout.addLayout(report_top_layout)
        report_main_layout.addWidget(self.cb_species_alerts_enable)
        report_main_layout.addWidget(self.cb_alert_hourly_speed)
        report_main_layout.addWidget(self.cb_alert_monthly_quota)
        report_main_layout.addWidget(audio_group)
        
        report_group.setLayout(report_main_layout)
        self.layout.addWidget(report_group)

        # --- HARDWARE TELEMETRY SUMMARY ---
        telemetry_group = QGroupBox("📡 Hardware Network Telemetry (SIM Usage)")
        telemetry_group.setStyleSheet("""
            QGroupBox { 
                border: 1px solid #00897B; 
                border-radius: 4px; 
                margin-top: 18px; 
                padding-top: 15px; 
            } 
            QGroupBox::title { 
                subcontrol-origin: margin; 
                subcontrol-position: top left;
                left: 10px; 
                padding: 0 5px; 
                color: #00E5FF; 
                font-weight: bold; 
            }
        """)
        telemetry_layout = QVBoxLayout(telemetry_group)
        
        # ADDED TOP ROW FOR MASTER SWITCH
        telemetry_top_lay = QHBoxLayout()
        telemetry_top_lay.addStretch()
        self.chk_enable_throttling = QCheckBox("🛡️ Enable Hydra Auto-Throttling & Balancing")
        
        throttle_state = True
        try:
            if CONFIG_FILE.exists():
                cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                throttle_state = cfg.get("hydra_pid_settings", {}).get("throttling_enabled", True)
        except: pass
        
        self.chk_enable_throttling.setChecked(throttle_state)
        self.chk_enable_throttling.setStyleSheet(f"font-weight: bold; color: {'#00E676' if throttle_state else '#FF3D00'};")
        self.chk_enable_throttling.toggled.connect(self._toggle_throttling)
        
        telemetry_top_lay.addWidget(self.chk_enable_throttling)
        telemetry_layout.addLayout(telemetry_top_lay)
        
        self.lbl_telemetry_summary = QLabel("Loading telemetry...")
        self.lbl_telemetry_summary.setWordWrap(True)
        self.lbl_telemetry_summary.setToolTip(self.legend_tooltip)
        telemetry_layout.addWidget(self.lbl_telemetry_summary)
        
        self.btn_open_telemetry = QPushButton("📊 View Detailed Telemetry & Set Quotas...")
        self.btn_open_telemetry.setStyleSheet("background-color: #00897B; color: white; font-weight: bold; padding: 8px;")
        self.btn_open_telemetry.clicked.connect(self.open_telemetry_dialog)
        telemetry_layout.addWidget(self.btn_open_telemetry)
        
        self.layout.addWidget(telemetry_group)

        # --- LOG OUTPUT ---
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setStyleSheet("font-family: Consolas, monospace; font-size: 10pt;")
        self.layout.addWidget(self.log_area)

        self.btn_close = QPushButton("Close (Applies Report Settings)")
        self.btn_close.clicked.connect(self.accept)
        self.layout.addWidget(self.btn_close)
        
        self.load_telemetry_summary()
        self.run_diagnostics()

        self.telemetry_timer = QTimer(self)
        self.telemetry_timer.timeout.connect(self.load_telemetry_summary)
        self.telemetry_timer.start(10000)

    def _toggle_throttling(self, checked):
        self.chk_enable_throttling.setStyleSheet(f"font-weight: bold; color: {'#00E676' if checked else '#FF3D00'};")
        try:
            if CONFIG_FILE.exists():
                cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                if "hydra_pid_settings" not in cfg:
                    cfg["hydra_pid_settings"] = {}
                cfg["hydra_pid_settings"]["throttling_enabled"] = checked
                
                tmp_file = CONFIG_FILE.with_suffix('.tmp')
                tmp_file.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
                os.replace(tmp_file, CONFIG_FILE)
                self.load_telemetry_summary() # Refresh UI instantly
        except Exception as e:
            logging.error(f"Failed to toggle throttling: {e}")

    def load_telemetry_summary(self):
        try:
            interfaces = network_manager.get_active_interfaces()
            summary_parts =[]
            cutoff_24h = time.time() - 86400
            cutoff_1h = time.time() - 3600
            
            heat_state = {}
            if HYDRA_STATE_FILE.exists():
                try:
                    heat_state = json.loads(HYDRA_STATE_FILE.read_text(encoding='utf-8'))
                except: pass
                
            # Read config for throttle flag
            throttle_enabled = True
            try:
                if CONFIG_FILE.exists():
                    cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                    throttle_enabled = cfg.get("hydra_pid_settings", {}).get("throttling_enabled", True)
            except: pass
                
            # Global Heat
            g_data = heat_state.get("GLOBAL")
            
            if not throttle_enabled:
                g_ind = "<span style='color: #FF3D00; font-weight: bold;'>[⚠️ THROTTLING DISABLED]</span>"
            elif g_data:
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
            else:
                g_ind = "<span style='color: #888;'>Waiting for telemetry...</span>"
                
            summary_parts.append(f"<b>Main Pipeline Status:</b> {g_ind}<br>")

            with db_connector.get_db_connection(force_local=True) as con:
                cur = con.cursor()
                for iface in interfaces:
                    name = iface['name']
                    
                    used_bytes, limit_bytes, is_over = network_manager.get_interface_quota_status(name)
                    is_monthly_dead = (limit_bytes > 0 and used_bytes >= limit_bytes)
                    
                    # Interface Heat
                    i_data = heat_state.get(name)
                    if i_data:
                        heat = i_data['heat']
                        i_arrow = i_data['arrow']
                        
                        if "❌" in i_arrow:
                            i_ind = "<span style='color: #9E9E9E; font-weight: bold;'>[❌ OFFLINE (Disconnected)]</span>"
                        elif "🔥" in i_arrow:
                            i_ind = "<span style='color: #FF5722; font-weight: bold;'>[🔥 IP BANNED (403)]</span>"
                        elif is_monthly_dead:
                            i_ind = "<span style='color: #E91E63; font-weight: bold;'>[⛔ DATA DEPLETED (Monthly Cap)]</span>"
                        elif heat >= 1.0:
                            i_ind = "<span style='color: #FF3D00; font-weight: bold;'>[🛑 LOCAL SPEED BLOCKED (100% Heat)]</span>"
                        elif heat >= 0.95:
                            i_ind = f"<span style='color: #FF9800; font-weight: bold;'>[🟠 THROTTLED MAX] ({heat*100:.1f}%)</span>"
                        elif "🔺" in i_arrow:
                            i_ind = f"<span style='color: #FF5252; font-weight: bold;'>[🔺 Heating Up] ({heat*100:.1f}%)</span>"
                        elif "🔽" in i_arrow:
                            i_ind = f"<span style='color: #448AFF; font-weight: bold;'>[🔽 Cooling Down] ({heat*100:.1f}%)</span>"
                        else:
                            i_ind = f"<span style='color: #4CAF50; font-weight: bold;'>[➖ Stable] ({heat*100:.1f}%)</span>"
                    else:
                        i_ind = "[Waiting...]"
                    
                    cur.execute("SELECT SUM(bytes_sent + bytes_recv) FROM network_hardware_logs WHERE interface_name = ? AND timestamp >= ?", (name, cutoff_24h))
                    row_24h = cur.fetchone()
                    used_24h = row_24h[0] if row_24h and row_24h[0] else 0
                    used_24h_gb = used_24h / (1024**3)
                    
                    cur.execute("SELECT SUM(bytes_sent + bytes_recv) FROM network_hardware_logs WHERE interface_name = ? AND timestamp >= ?", (name, cutoff_1h))
                    row_1h = cur.fetchone()
                    used_1h = row_1h[0] if row_1h and row_1h[0] else 0
                    used_1h_gb = used_1h / (1024**3)
                    
                    cur.execute("SELECT engine_type, SUM(bytes_used) FROM network_app_logs WHERE interface_name = ? AND timestamp >= ? GROUP BY engine_type", (name, cutoff_1h))
                    audio_1h = 0
                    vision_1h = 0
                    for r in cur.fetchall():
                        if r[0] == 'audio': audio_1h = r[1]
                        elif r[0] == 'vision': vision_1h = r[1]
                        
                    total_app_1h = audio_1h + vision_1h
                    audio_pct_1h = int((audio_1h / total_app_1h) * 100) if total_app_1h > 0 else 0
                    vision_pct_1h = int((vision_1h / total_app_1h) * 100) if total_app_1h > 0 else 0
                    
                    split_str = f"(🐦 {audio_pct_1h}% | 📷 {vision_pct_1h}%)" if total_app_1h > 0 else "(No Activity)"
                    
                    summary_parts.append(
                        f"• <b>{name}</b> {i_ind}: {used_24h_gb:.2f} GB (24h) "
                        f"| <span style='color: #00E5FF;'><b>SPEED:</b> {used_1h_gb:.3f} GB/hr {split_str}</span>"
                    )
                    
            if not summary_parts:
                self.lbl_telemetry_summary.setText("No active interfaces or telemetry data found.")
            else:
                self.lbl_telemetry_summary.setText("<br>".join(summary_parts))
        except Exception as e:
            self.lbl_telemetry_summary.setText(f"Error loading telemetry: {e}")

    def open_telemetry_dialog(self):
        d = NetworkTelemetryDialog(self)
        d.exec()
        self.load_telemetry_summary()

    def _toggle_audio_master(self, checked):
        self.cb_send_audio_dsp.setEnabled(not checked)
        self.cb_send_audio_multi.setEnabled(not checked)
        self.cb_send_audio_birdnet.setEnabled(not checked)

    def get_report_config(self):
        rep_conf = {
            "enabled": self.cb_report_enable.isChecked(),
            "interval_hours": self.freq_map[self.combo_report_freq.currentText()],
            "send_species_alerts": self.cb_species_alerts_enable.isChecked(),
            "alert_hourly_speed": self.cb_alert_hourly_speed.isChecked(),
            "alert_monthly_quota": self.cb_alert_monthly_quota.isChecked()
        }
        return (
            rep_conf, 
            self.cb_send_audio_all.isChecked(), 
            self.cb_send_audio_dsp.isChecked(), 
            self.cb_send_audio_multi.isChecked(), 
            self.cb_send_audio_birdnet.isChecked()
        )

    def log(self, msg, color="black"):
        self.log_area.append(f"<font color='{color}'>{msg}</font>")
        QApplication.processEvents()

    def run_diagnostics(self):
        self.log_area.clear()
        self.log("<b>--- STARTING DATA DIAGNOSTICS ---</b>", "blue")
        issues_found = 0

        self.log("<b>1. Proximity Check (&lt; 1km):</b>")
        clusters =[]
        checked = set()
        for i, s1 in enumerate(self.streams):
            if i in checked: continue
            lat1, lon1 = s1.get('lat', 0), s1.get('lon', 0)
            if lat1 == 0 and lon1 == 0: continue
            
            nearby =[s1['name']]
            for j, s2 in enumerate(self.streams):
                if i == j: continue
                lat2, lon2 = s2.get('lat', 0), s2.get('lon', 0)
                if lat2 == 0 and lon2 == 0: continue
                
                dist = haversine_distance(lat1, lon1, lat2, lon2)
                if dist < 1.0:
                    nearby.append(f"{s2['name']} ({dist:.2f} km)")
                    checked.add(j)
            
            if len(nearby) > 1:
                clusters.append(nearby)
        
        if clusters:
            for c in clusters:
                self.log(f"⚠ Cluster found: <br>&nbsp;&nbsp;{', '.join(c)}", "orange")
                issues_found += 1
        else:
            self.log("✔ No proximity conflicts found.", "green")

        self.log("<br><b>2. Duplicate URL Check:</b>")
        urls =[s.get('page_url', '').strip() for s in self.streams]
        cnt = Counter(urls)
        dupes =[url for url, count in cnt.items() if count > 1 and url]
        
        if dupes:
            for d in dupes:
                names =[s['name'] for s in self.streams if s.get('page_url', '').strip() == d]
                self.log(f"⚠ Duplicate URL: <b>{d}</b><br>&nbsp;&nbsp;Used by: {', '.join(names)}", "red")
                issues_found += 1
        else:
            self.log("✔ No duplicate URLs found.", "green")

        self.log("<br><b>3. Missing Coordinates:</b>")
        zeros =[s['name'] for s in self.streams if s.get('lat', 0) == 0 and s.get('lon', 0) == 0]
        if zeros:
            self.log(f"⚠ Missing Lat/Lon: {', '.join(zeros)}", "orange")
            issues_found += len(zeros)
        else:
            self.log("✔ All streams have coordinates.", "green")

        if issues_found == 0:
            self.log("<br><b>--- DATA HEALTHY ---</b>", "green")
        else:
            self.log(f"<br><b>--- {issues_found} DATA ISSUES FOUND ---</b>", "red")

    def run_mass_health_check(self):
        self.log_area.clear()
        self.log("<b>--- STARTING NETWORK HEALTH CHECK ---</b>", "blue")
        self.log("<i>Pinging all streams (Timeout: 3s)... This may take a minute.</i><br>")
        
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        total = len(self.streams)
        dead_count = 0
        
        for i, stream in enumerate(self.streams):
            name = stream.get('name', 'Unknown')
            url = stream.get('page_url', '')
            
            if not url:
                self.log(f"[{i+1}/{total}] ⚠ {name}: No URL", "orange")
                continue
                
            self.log(f"[{i+1}/{total}] Checking {name}...", "gray")
            
            try:
                r = requests.get(url, headers=headers, timeout=3, stream=True)
                r.close()
                
                code = r.status_code
                if 200 <= code < 400:
                    self.log(f"&nbsp;&nbsp;✔ ALIVE ({code})", "green")
                else:
                    self.log(f"&nbsp;&nbsp;❌ DEAD ({code}) - {url}", "red")
                    dead_count += 1
            except requests.exceptions.Timeout:
                self.log(f"&nbsp;&nbsp;❌ TIMEOUT (Server Unresponsive) - {url}", "red")
                dead_count += 1
            except requests.exceptions.ConnectionError:
                self.log(f"&nbsp;&nbsp;❌ CONNECTION ERROR (DNS/Refused) - {url}", "red")
                dead_count += 1
            except Exception as e:
                self.log(f"&nbsp;&nbsp;❌ ERROR: {str(e)}", "red")
                dead_count += 1
                
        self.log(f"<br><b>--- CHECK COMPLETE ---</b>", "blue")
        if dead_count == 0:
            self.log("✔ All streams appear to be online.", "green")
        else:
            self.log(f"⚠ Found {dead_count} potentially dead streams.", "red")

    def reconstruct_timeline(self):
        if not os.path.exists(DATABASE_PATH):
            QMessageBox.critical(self, "Error", "Database not found.")
            return

        reply = QMessageBox.question(self, "Confirm", "This will scan the database for the FIRST detection of each stream and update its 'Date Created' timestamp.\n\nThis is a one-time retroactive fix.\nProceed?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.No: return

        self.log("<br><b>--- RECONSTRUCTING TIMELINE ---</b>", "blue")
        try:
            con = sqlite3.connect(DATABASE_PATH)
            cur = con.cursor()
            
            updated_count = 0
            for data in self.streams:
                url = data.get('page_url')
                
                cur.execute("SELECT MIN(timestamp) FROM detections WHERE channel_url = ?", (url,))
                row = cur.fetchone()
                
                if row and row[0]:
                    first_seen = row[0]
                    data['created_at'] = first_seen
                    date_str = datetime.fromtimestamp(first_seen).strftime('%Y-%m-%d')
                    self.log(f"Updated '{data['name']}' -> {date_str}", "green")
                    updated_count += 1
                
                QApplication.processEvents()
            
            con.close()
            self.log(f"<b>Done. Updated {updated_count} streams.</b>", "blue")
            
            if updated_count > 0:
                self.parent_editor.apply_audit_updates(self.streams)
                QMessageBox.information(self, "Success", f"Timeline reconstructed for {updated_count} streams.\n\nIMPORTANT: Please click 'Save All Configuration' in the main window to make these dates permanent.")

        except Exception as e:
            self.log(f"CRITICAL ERROR: {e}", "red")
            logging.error("Reconstruct crash:", exc_info=True)

    def reset_scheduler_counts(self):
        reply = QMessageBox.question(
            self, "Confirm Total Amnesty",
            "This will perform a TOTAL RESET of the Scheduler queue:\n\n"
            "1. Reset all 'Check Counts' to 0.\n"
            "2. CLEAR ALL PENALTIES (Set wait time to 0).\n"
            "3. Reset all status notes.\n\n"
            "The system will immediately try to check ALL enabled streams as if it was the first time running.\n\n"
            "Proceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            self.log("<br><b>--- EXECUTING TOTAL AMNESTY ---</b>", "blue")
            try:
                with db_connector.get_db_connection() as con:
                    con.execute("UPDATE stream_queue SET check_count = 0, next_eligible_ts = 0, status_note = NULL")
                
                self.log("✔ SUCCESS: All counts and penalties cleared.", "green")
                self.log("The Scheduler will now re-evaluate all streams immediately.", "green")
                QMessageBox.information(self, "Success", "Total Amnesty Applied.\nThe Scheduler has a clean slate.")
            except Exception as e:
                self.log(f"❌ ERROR: {e}", "red")
                QMessageBox.critical(self, "Error", f"Failed to reset: {e}")


# ==============================================================================
# HOUSEKEEPING MANAGER DIALOG
# ==============================================================================
class HousekeepingManagerDialog(QDialog):
    def __init__(self, debug_enabled, hk_config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Log & Storage Housekeeping Manager")
        self.setMinimumSize(850, 750)
        self.layout = QVBoxLayout(self)
        
        # Load maintenance state
        self.last_server_maintenance_ts = hk_config.get('last_server_maintenance_ts', 0)
        self.server_interval_days = hk_config.get('server_maintenance_interval_days', 30)
        self.last_local_db_maintenance_ts = hk_config.get('last_local_db_maintenance_ts', 0)
        self.local_db_interval_days = hk_config.get('local_db_interval_days', 30)
        
        self.tabs = QTabWidget()
        
        # --- TAB 1: LOG ANALYZER & COMPRESSOR ---
        analyzer_widget = QWidget()
        analyzer_layout = QVBoxLayout(analyzer_widget)
        
        # Target Files Group
        group_files = QGroupBox("Target Files")
        form_files = QFormLayout(group_files)

        self.edit_monitor = QLineEdit()
        btn_monitor = QPushButton("Browse")
        btn_monitor.clicked.connect(lambda: self.browse_file(self.edit_monitor, "Select Monitor Log", "Text Files (*.txt)"))
        lay_m = QHBoxLayout(); lay_m.addWidget(self.edit_monitor); lay_m.addWidget(btn_monitor)
        form_files.addRow("monitor_debug.txt:", lay_m)

        self.edit_vision = QLineEdit()
        btn_vision = QPushButton("Browse")
        btn_vision.clicked.connect(lambda: self.browse_file(self.edit_vision, "Select Vision Log", "Text Files (*.txt)"))
        lay_v = QHBoxLayout(); lay_v.addWidget(self.edit_vision); lay_v.addWidget(btn_vision)
        form_files.addRow("vision_debug.txt:", lay_v)

        self.edit_out = QLineEdit()
        btn_out = QPushButton("Browse")
        btn_out.clicked.connect(lambda: self.browse_dir(self.edit_out, "Select Output Directory"))
        lay_o = QHBoxLayout(); lay_o.addWidget(self.edit_out); lay_o.addWidget(btn_out)
        form_files.addRow("Save Summary To:", lay_o)

        analyzer_layout.addWidget(group_files)
        
        # Smart Compression Group
        group_comp = QGroupBox("Smart Text Compression")
        form_comp = QFormLayout(group_comp)
        
        comp_info = QLabel("Aggressively strips variable data (URLs, IPs, Hex codes) to force the parser to group identical errors into a single, highly compressed line.")
        comp_info.setWordWrap(True)
        comp_info.setStyleSheet("color: #aaa; margin-bottom: 5px;")
        form_comp.addRow(comp_info)
        
        self.chk_mask_data = QCheckBox("Mask Dynamic Data (Replaces URLs, IPs, and Hex with tags)")
        self.chk_mask_data.setStyleSheet("color: #00E676; font-weight: bold;")
        
        self.chk_truncate = QCheckBox("Truncate Long Errors (Caps spam to 250 chars. Protects Tracebacks)")
        
        self.spin_top_n = QSpinBox()
        self.spin_top_n.setRange(1, 100)
        self.spin_top_n.setSuffix(" Errors")
        
        form_comp.addRow("", self.chk_mask_data)
        form_comp.addRow("", self.chk_truncate)
        form_comp.addRow("Max Unique Items per Category (Top N):", self.spin_top_n)
        
        analyzer_layout.addWidget(group_comp)

        # Filters Group
        group_filters = QGroupBox("Extraction Filters")
        v_filters = QVBoxLayout(group_filters)

        time_lay = QHBoxLayout()
        time_lay.addWidget(QLabel("Timeframe to Analyze:"))
        
        self.spin_time = QSpinBox()
        self.spin_time.setRange(0, 720)
        self.spin_time.setSpecialValueText("All Time")
        self.spin_time.setSuffix(" Hours")
        time_lay.addWidget(self.spin_time)
        
        time_lay.addStretch()
        v_filters.addLayout(time_lay)

        v_filters.addSpacing(10)
        v_filters.addWidget(QLabel("<b>Include in Summary:</b>"))
        
        self.chk_tracebacks = QCheckBox("Critical Tracebacks, Crashes & Emergency Brakes") # UPDATED LABEL
        self.chk_selenium = QCheckBox("Selenium & WebDriver Crashes")
        self.chk_ffmpeg = QCheckBox("FFmpeg & Audio Capture Errors")
        self.chk_network = QCheckBox("Network, Proxy, and Cloud Sync Timeouts")
        self.chk_streams = QCheckBox("Stream Drops & Resolver Fails")
        self.chk_normal = QCheckBox("Sample of Normal Activity (Alerts/Bio-Hits)")

        grid = QHBoxLayout()
        col1 = QVBoxLayout()
        col2 = QVBoxLayout()
        col1.addWidget(self.chk_tracebacks); col1.addWidget(self.chk_selenium); col1.addWidget(self.chk_ffmpeg)
        col2.addWidget(self.chk_network); col2.addWidget(self.chk_streams); col2.addWidget(self.chk_normal)
        grid.addLayout(col1); grid.addLayout(col2)
        v_filters.addLayout(grid)
        
        analyzer_layout.addWidget(group_filters)
        analyzer_layout.addStretch()

        # Execution
        self.lbl_status = QLabel("Ready to analyze.")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        analyzer_layout.addWidget(self.lbl_status)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        analyzer_layout.addWidget(self.progress)

        self.btn_run = QPushButton("⚡ COMPRESS & ANALYZE LOGS")
        self.btn_run.setStyleSheet("background-color: #00E5FF; color: black; font-size: 16px; padding: 15px;")
        self.btn_run.clicked.connect(self.run_analysis)
        analyzer_layout.addWidget(self.btn_run)
        
        self.tabs.addTab(analyzer_widget, "Log Compressor & Analyzer")
        
        
        # --- TAB 2: STORAGE DEEP CLEAN & SETTINGS ---
        clean_widget = QWidget()
        clean_layout = QVBoxLayout(clean_widget)

        # Extract values
        janitor_int = hk_config.get('janitor_interval_hours', 1)
        log_ret = hk_config.get('legacy_log_retention_hours', 72)
        temp_ret = hk_config.get('temp_retention_hours', 24)
        auto_py = hk_config.get('auto_wipe_python_caches', True)
        auto_clips = hk_config.get('auto_wipe_orphaned_clips', True)

        # Settings Group
        settings_group = QGroupBox("Auto-Cleaning & Debug Settings")
        settings_layout = QFormLayout(settings_group)

        self.debug_cb = QCheckBox("Enable Legacy Debug Logging")
        self.debug_cb.setChecked(debug_enabled)
        self.debug_cb.setToolTip("If checked, writes proxy and system debug info to monitor_debug.txt and proxy_debug.txt")

        self.spin_janitor = QSpinBox()
        self.spin_janitor.setRange(1, 720)
        self.spin_janitor.setValue(janitor_int)
        self.spin_janitor.setSuffix(" hours")
        self.spin_janitor.setToolTip("How often the background cleaning thread wakes up to run these rules.")

        self.spin_log = QSpinBox()
        self.spin_log.setRange(1, 720) 
        self.spin_log.setValue(log_ret)
        self.spin_log.setSuffix(" hours")
        self.spin_log.setToolTip("Text logs (monitor_debug, proxy_debug, app_logs) older than this will be deleted.")

        self.spin_temp = QSpinBox()
        self.spin_temp.setRange(1, 720) 
        self.spin_temp.setValue(temp_ret)
        self.spin_temp.setSuffix(" hours")
        self.spin_temp.setToolTip("Orphaned Chrome/Audio files in Windows %TEMP% older than this will be obliterated.")

        self.cb_py = QCheckBox("Auto-Wipe Python & PIP Caches")
        self.cb_py.setChecked(auto_py)
        
        self.cb_clips = QCheckBox("Auto-Wipe Orphaned Audio Clips (Keeps Golden Anchors)")
        self.cb_clips.setChecked(auto_clips)
        
        settings_layout.addRow("", self.debug_cb)
        settings_layout.addRow(QLabel("<hr>"))
        settings_layout.addRow("Janitor Wake Interval:", self.spin_janitor)
        settings_layout.addRow("Text Logs Retention:", self.spin_log)
        settings_layout.addRow("Windows %TEMP% Retention:", self.spin_temp)
        settings_layout.addRow("", self.cb_py)
        settings_layout.addRow("", self.cb_clips)
        clean_layout.addWidget(settings_group)
        
        # --- NEW: Maintenance Tracking ---
        tracker_layout = QHBoxLayout()
        
        # 1. DigitalOcean Server OS
        server_group = QGroupBox("DigitalOcean Server OS")
        server_group.setStyleSheet("QGroupBox { border: 1px solid #448AFF; color: #448AFF; font-weight: bold; margin-top: 15px; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }")
        server_layout = QFormLayout(server_group)

        self.spin_server_interval = QSpinBox()
        self.spin_server_interval.setRange(1, 365)
        self.spin_server_interval.setValue(self.server_interval_days)
        self.spin_server_interval.setSuffix(" days")
        self.spin_server_interval.valueChanged.connect(self.update_server_status)

        self.lbl_server_status = QLabel()
        self.update_server_status()

        self.btn_show_ssh = QPushButton("🖥️ View SSH Maintenance Commands")
        self.btn_show_ssh.setStyleSheet("background-color: #5C6BC0; color: white;")
        self.btn_show_ssh.clicked.connect(self.show_ssh_commands)
        
        self.btn_mark_cleaned = QPushButton("✅ Mark as Cleaned Today")
        self.btn_mark_cleaned.setStyleSheet("background-color: #00897B; color: white;")
        self.btn_mark_cleaned.clicked.connect(self.mark_server_cleaned)

        server_layout.addRow("Reminder Interval:", self.spin_server_interval)
        server_layout.addRow("Current Status:", self.lbl_server_status)
        server_layout.addRow(self.btn_show_ssh)
        server_layout.addRow(self.btn_mark_cleaned)
        tracker_layout.addWidget(server_group)

        # 2. Local & Cloud Database Maintenance
        local_group = QGroupBox("Local & Cloud Database Maintenance")
        local_group.setStyleSheet("QGroupBox { border: 1px solid #FF9800; color: #FF9800; font-weight: bold; margin-top: 15px; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }")
        local_layout = QFormLayout(local_group)
        
        self.spin_local_interval = QSpinBox()
        self.spin_local_interval.setRange(1, 365)
        self.spin_local_interval.setValue(self.local_db_interval_days)
        self.spin_local_interval.setSuffix(" days")
        self.spin_local_interval.valueChanged.connect(self.update_local_db_status)
        
        self.lbl_local_status = QLabel()
        self.update_local_db_status()
        
        self.btn_clear_db_bloat = QPushButton("🗑️ CLEAR DATABASE BLOAT (Requires Engine Stop)")
        self.btn_clear_db_bloat.setStyleSheet("background-color: #F57C00; color: white; font-weight: bold; padding: 6px; font-size: 13px;")
        self.btn_clear_db_bloat.clicked.connect(self.clear_database_bloat)
        
        self.btn_mark_local_cleaned = QPushButton("✅ Mark as Cleaned Today")
        self.btn_mark_local_cleaned.setStyleSheet("background-color: #00897B; color: white;")
        self.btn_mark_local_cleaned.clicked.connect(self.mark_local_cleaned)
        
        local_layout.addRow("Reminder Interval:", self.spin_local_interval)
        local_layout.addRow("Current Status:", self.lbl_local_status)
        local_layout.addRow(self.btn_clear_db_bloat)
        local_layout.addRow(self.btn_mark_local_cleaned)
        tracker_layout.addWidget(local_group)
        
        clean_layout.addLayout(tracker_layout)

        # --- Manual Cleaning Group ---
        manual_group = QGroupBox("Manual Deep Clean")
        manual_layout = QVBoxLayout(manual_group)

        info_lbl = QLabel("Instantly wipe legacy logs or eradicate abandoned Chrome/Audio/Python cache files from your local drive. This cannot be undone.")
        info_lbl.setWordWrap(True)
        info_lbl.setStyleSheet("color: #aaa; margin-bottom: 10px;")
        manual_layout.addWidget(info_lbl)

        log_layout = QHBoxLayout()
        self.btn_clear_monitor = QPushButton("📄 Clear Audio Log")
        self.btn_clear_monitor.clicked.connect(lambda: self.clear_file(ROOT / "monitor_debug.txt"))

        self.btn_clear_proxy = QPushButton("📄 Clear Proxy/Hydra Log")
        self.btn_clear_proxy.clicked.connect(lambda: self.clear_file(ROOT / "proxy_debug.txt"))

        self.btn_clear_app_logs = QPushButton("📂 Clear Worker Logs")
        self.btn_clear_app_logs.clicked.connect(self.clear_app_logs)

        log_layout.addWidget(self.btn_clear_monitor)
        log_layout.addWidget(self.btn_clear_proxy)
        log_layout.addWidget(self.btn_clear_app_logs)
        manual_layout.addLayout(log_layout)

        py_layout = QHBoxLayout()
        self.btn_clear_pycache = QPushButton("🧹 Clear Python Cache (__pycache__ & .pyc)")
        self.btn_clear_pycache.setStyleSheet("background-color: #5C6BC0; color: white;")
        self.btn_clear_pycache.clicked.connect(self.clear_pycache)

        self.btn_clear_pip = QPushButton("📦 Clear PIP Download Cache")
        self.btn_clear_pip.setStyleSheet("background-color: #26A69A; color: white;")
        self.btn_clear_pip.clicked.connect(self.clear_pip_cache)

        py_layout.addWidget(self.btn_clear_pycache)
        py_layout.addWidget(self.btn_clear_pip)
        manual_layout.addLayout(py_layout)
        
        self.btn_clear_clips = QPushButton("🎵 Clear Orphaned Audio Clips")
        self.btn_clear_clips.setStyleSheet("background-color: #8E24AA; color: white;")
        self.btn_clear_clips.clicked.connect(self.clear_orphaned_clips)
        manual_layout.addWidget(self.btn_clear_clips)

        self.btn_clear_temp = QPushButton("🔥 Clear Windows %TEMP% (Orphaned Chrome/Audio files)")
        self.btn_clear_temp.setStyleSheet("background-color: #E53935; color: white; font-weight: bold; margin-top: 10px;")
        self.btn_clear_temp.clicked.connect(self.clear_temp_bloat)
        manual_layout.addWidget(self.btn_clear_temp)

        clean_layout.addWidget(manual_group)
        clean_layout.addStretch()
        
        self.tabs.addTab(clean_widget, "Storage Deep Clean & Settings")
        
        self.layout.addWidget(self.tabs)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.layout.addWidget(self.buttons)
        
        self.worker = None
        self.load_analyzer_settings()

    def update_server_status(self):
        interval = self.spin_server_interval.value()
        if self.last_server_maintenance_ts == 0:
            self.lbl_server_status.setText("<span style='color: #FF9800; font-weight: bold;'>OVERDUE (Never Cleaned)</span>")
            return
            
        days_since = (time.time() - self.last_server_maintenance_ts) / 86400.0
        if days_since >= interval:
            self.lbl_server_status.setText(f"<span style='color: #F57C00; font-weight: bold;'>OVERDUE ({int(days_since)} days since last clean)</span>")
        else:
            days_left = interval - days_since
            self.lbl_server_status.setText(f"<span style='color: #00E676; font-weight: bold;'>Healthy ({int(days_left)} days until next clean)</span>")

    def mark_server_cleaned(self):
        self.last_server_maintenance_ts = time.time()
        self.update_server_status()
        QMessageBox.information(self, "Updated", "Server maintenance date recorded for today.")

    def update_local_db_status(self):
        interval = self.spin_local_interval.value()
        if self.last_local_db_maintenance_ts == 0:
            self.lbl_local_status.setText("<span style='color: #FF9800; font-weight: bold;'>OVERDUE (Never Cleaned)</span>")
            return
            
        days_since = (time.time() - self.last_local_db_maintenance_ts) / 86400.0
        if days_since >= interval:
            self.lbl_local_status.setText(f"<span style='color: #F57C00; font-weight: bold;'>OVERDUE ({int(days_since)} days since last clean)</span>")
        else:
            days_left = interval - days_since
            self.lbl_local_status.setText(f"<span style='color: #00E676; font-weight: bold;'>Healthy ({int(days_left)} days until next clean)</span>")

    def mark_local_cleaned(self):
        self.last_local_db_maintenance_ts = time.time()
        self.update_local_db_status()
        QMessageBox.information(self, "Updated", "Local Database maintenance date recorded for today.")

    def show_ssh_commands(self):
        d = QDialog(self)
        d.setWindowTitle("DigitalOcean Server SSH Maintenance Commands")
        d.resize(800, 500)
        lay = QVBoxLayout(d)
        
        info = QLabel("Log into your DigitalOcean droplet via SSH and run the following commands to clear Docker bloat and OS logs.")
        info.setWordWrap(True)
        lay.addWidget(info)
        
        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setStyleSheet("font-family: Consolas, monospace; background-color: #1e1e1e; color: #00E676; padding: 10px;")
        
        commands = (
            "# 1. Check current disk space BEFORE pruning (Note the 'Avail' column for /dev/vda1)\n"
            "df -h /\n\n"
            "# 2. Remove all unused Docker containers, networks, images, and build cache\n"
            "# (This includes stopped containers and dangling images eating up space)\n"
            "docker system prune -a -f --volumes\n\n"
            "# 3. Clear old systemd journal logs, keeping only the last 7 days to prevent OS log bloat\n"
            "journalctl --vacuum-time=7d\n\n"
            "# 4. Check disk space AFTER pruning to see how much space was recovered\n"
            "df -h /\n"
        )
        txt.setPlainText(commands)
        lay.addWidget(txt)
        
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(d.accept)
        lay.addWidget(btn_close)
        
        d.exec()

    def browse_file(self, line_edit, title, f_filter):
        start_dir = line_edit.text() or str(ROOT)
        path, _ = QFileDialog.getOpenFileName(self, title, start_dir, f_filter)
        if path: line_edit.setText(path)

    def browse_dir(self, line_edit, title):
        start_dir = line_edit.text() or str(ROOT)
        path = QFileDialog.getExistingDirectory(self, title, start_dir)
        if path: line_edit.setText(path)

    def load_analyzer_settings(self):
        if not LOG_ANALYZER_SETTINGS_FILE.exists():
            self.edit_monitor.setText(str(ROOT / "monitor_debug.txt"))
            self.edit_vision.setText(str(ROOT / "vision_debug.txt"))
            self.edit_out.setText(str(ROOT))
            self.spin_time.setValue(4)
            self.chk_tracebacks.setChecked(True)
            self.chk_selenium.setChecked(True)
            self.chk_ffmpeg.setChecked(True)
            self.chk_network.setChecked(True)
            self.chk_streams.setChecked(True)
            self.chk_normal.setChecked(True)
            self.chk_mask_data.setChecked(True)
            self.chk_truncate.setChecked(True)
            self.spin_top_n.setValue(15)
            return
            
        try:
            with open(LOG_ANALYZER_SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            self.edit_monitor.setText(s.get("monitor_path", str(ROOT / "monitor_debug.txt")))
            self.edit_vision.setText(s.get("vision_path", str(ROOT / "vision_debug.txt")))
            self.edit_out.setText(s.get("out_dir", str(ROOT)))
            self.spin_time.setValue(s.get("timeframe_hours", 4))
            
            f_set = s.get("filters", {})
            self.chk_tracebacks.setChecked(f_set.get("tracebacks", True))
            self.chk_selenium.setChecked(f_set.get("selenium", True))
            self.chk_ffmpeg.setChecked(f_set.get("ffmpeg", True))
            self.chk_network.setChecked(f_set.get("network", True))
            self.chk_streams.setChecked(f_set.get("streams", True))
            self.chk_normal.setChecked(f_set.get("normal", True))
            
            c_set = s.get("compression", {})
            self.chk_mask_data.setChecked(c_set.get("mask_data", True))
            self.chk_truncate.setChecked(c_set.get("truncate", True))
            self.spin_top_n.setValue(c_set.get("top_n", 15))
        except Exception as e:
            logging.error(f"Failed to load analyzer settings: {e}")

    def save_analyzer_settings(self):
        try:
            s = {
                "monitor_path": self.edit_monitor.text(),
                "vision_path": self.edit_vision.text(),
                "out_dir": self.edit_out.text(),
                "timeframe_hours": self.spin_time.value(),
                "filters": {
                    "tracebacks": self.chk_tracebacks.isChecked(),
                    "selenium": self.chk_selenium.isChecked(),
                    "ffmpeg": self.chk_ffmpeg.isChecked(),
                    "network": self.chk_network.isChecked(),
                    "streams": self.chk_streams.isChecked(),
                    "normal": self.chk_normal.isChecked()
                },
                "compression": {
                    "mask_data": self.chk_mask_data.isChecked(),
                    "truncate": self.chk_truncate.isChecked(),
                    "top_n": self.spin_top_n.value()
                }
            }
            with open(LOG_ANALYZER_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2)
        except Exception as e:
            logging.error(f"Failed to save analyzer settings: {e}")

    def run_analysis(self):
        m_path = self.edit_monitor.text().strip()
        v_path = self.edit_vision.text().strip()
        o_dir = self.edit_out.text().strip()

        if not os.path.exists(m_path) and not os.path.exists(v_path):
            QMessageBox.critical(self, "Error", "Neither log file exists at the specified paths.")
            return
        if not os.path.isdir(o_dir):
            QMessageBox.critical(self, "Error", "Output directory does not exist.")
            return

        self.save_analyzer_settings()
        
        filters = {
            "tracebacks": self.chk_tracebacks.isChecked(),
            "selenium": self.chk_selenium.isChecked(),
            "ffmpeg": self.chk_ffmpeg.isChecked(),
            "network": self.chk_network.isChecked(),
            "streams": self.chk_streams.isChecked(),
            "normal": self.chk_normal.isChecked()
        }
        
        comp_settings = {
            "mask_data": self.chk_mask_data.isChecked(),
            "truncate": self.chk_truncate.isChecked(),
            "top_n": self.spin_top_n.value()
        }
        
        hours = self.spin_time.value()

        self.btn_run.setEnabled(False)
        self.btn_run.setText("⏳ PARSING LOGS...")
        self.progress.setValue(0)

        self.worker = LogParserWorker(m_path, v_path, o_dir, hours, filters, comp_settings)
        self.worker.progress_update.connect(self.update_progress)
        self.worker.finished.connect(self.on_analysis_complete)
        self.worker.start()

    def update_progress(self, msg, val):
        self.lbl_status.setText(msg)
        self.progress.setValue(val)

    def on_analysis_complete(self, result_path_or_err, success):
        self.btn_run.setEnabled(True)
        self.btn_run.setText("⚡ COMPRESS & ANALYZE LOGS")
        self.progress.setValue(100)
        
        if success:
            self.lbl_status.setText("Success! Opening summary file...")
            try:
                if sys.platform == "win32":
                    os.startfile(result_path_or_err)
                elif sys.platform == "darwin":
                    subprocess.call(["open", result_path_or_err])
                else:
                    subprocess.call(["xdg-open", result_path_or_err])
            except Exception as e:
                QMessageBox.information(self, "Success", f"File saved to:\n{result_path_or_err}\n\n(Could not auto-open: {e})")
        else:
            self.lbl_status.setText("Analysis failed.")
            QMessageBox.critical(self, "Error", result_path_or_err)

    def clear_file(self, filepath):
        if not filepath.exists():
            QMessageBox.information(self, "Info", f"{filepath.name} does not exist.")
            return
        if QMessageBox.question(self, "Confirm", f"Truncate {filepath.name} to 0 bytes?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            try:
                with open(filepath, "w", encoding='utf-8') as f: f.truncate(0)
                QMessageBox.information(self, "Success", f"{filepath.name} cleared.")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to clear {filepath.name}: {e}")

    def clear_app_logs(self):
        log_dir = ROOT / "app_logs"
        if not log_dir.exists():
            QMessageBox.information(self, "Info", "app_logs folder does not exist.")
            return
        if QMessageBox.question(self, "Confirm", "Delete all .log files in the app_logs folder?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            deleted = 0
            try:
                for lf in log_dir.glob("*.log"):
                    try:
                        lf.unlink()
                        deleted += 1
                    except Exception as e:
                        try:
                            with open(lf, "w", encoding='utf-8') as f: f.truncate(0)
                            deleted += 1
                        except: pass
                QMessageBox.information(self, "Success", f"Cleaned {deleted} log files.")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to clean folder: {e}")

    def clear_pycache(self):
        msg = "This will scan the project directory and delete all compiled Python cache files (__pycache__ and .pyc).\n\nProceed?"
        if QMessageBox.question(self, "Confirm Clean", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            deleted_dirs = 0
            deleted_files = 0
            freed_bytes = 0
            try:
                for p in ROOT.rglob("*.pyc"):
                    if p.is_file():
                        try:
                            freed_bytes += p.stat().st_size
                            p.unlink(missing_ok=True)
                            deleted_files += 1
                        except: pass
                
                for p in ROOT.rglob("__pycache__"):
                    if p.is_dir():
                        try:
                            shutil.rmtree(p, ignore_errors=True)
                            deleted_dirs += 1
                        except: pass
            finally:
                QApplication.restoreOverrideCursor()
            
            mb_freed = freed_bytes / (1024 * 1024)
            QMessageBox.information(self, "Clean Complete", f"Successfully deleted {deleted_dirs} directories and {deleted_files} files.\nFreed {mb_freed:.2f} MB.")

    def clear_pip_cache(self):
        msg = "This will clear your local PIP download cache, freeing up space from old package installers.\n\nProceed?"
        if QMessageBox.question(self, "Confirm Clean", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                res = subprocess.run([sys.executable, "-m", "pip", "cache", "purge"], 
                    capture_output=True, text=True, creationflags=flags)
                
                output = res.stdout.strip()
                if not output: output = res.stderr.strip()
                if not output: output = "Cache purged successfully."
                
                QMessageBox.information(self, "PIP Cache Cleaned", output)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to clear PIP cache: {e}")
            finally:
                QApplication.restoreOverrideCursor()

    def clear_database_bloat(self):
        # 1. Check if scheduler is running
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if 'python' in p.info['name'].lower() and p.info['cmdline'] and any("scheduler.py" in cmd.lower() for cmd in p.info['cmdline']):
                    QMessageBox.critical(self, "Engine Running", "CRITICAL: The Monitoring Engine is currently RUNNING!\n\nYou MUST click 'STOP Monitoring' on the main dashboard before running the Database Bloat Cleaner to prevent database corruption and locks.")
                    return
            except: pass

        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            active_urls = set()
            for s in cfg.get('streams',[]):
                if s.get('page_url'): active_urls.add(s['page_url'])
                if s.get('original_url'): active_urls.add(s['original_url'])

            db_urls = set()
            det_counts_map = {}
            with db_connector.get_db_connection(force_local=True) as con:
                cur = con.cursor()
                for table, col in[('stream_health_events', 'stream_url'), 
                                   ('stream_noise_profiles', 'stream_url'), 
                                   ('audio_hashes', 'stream_url'), 
                                   ('species_stream_profiles', 'stream_url'), 
                                   ('detections', 'channel_url')]:
                    try:
                        cur.execute(f"SELECT DISTINCT {col} FROM {table}")
                        db_urls.update([r[0] for r in cur.fetchall() if r[0]])
                    except: pass
                    
                # Pre-fetch detection counts to avoid 180k individual queries
                try:
                    cur.execute("SELECT channel_url, COUNT(*) FROM detections GROUP BY channel_url")
                    for row in cur.fetchall():
                        det_counts_map[row[0]] = row[1]
                except: pass

            ghost_urls = list(db_urls - active_urls)

            if not ghost_urls:
                QMessageBox.information(self, "All Clean", "No orphaned ghost URLs found in the database.")
                self.mark_local_cleaned()
                return

            # Filter ghost urls that have 0 detections or are non-youtube
            urls_to_delete =[]
            for g in ghost_urls:
                count = det_counts_map.get(g, 0)
                is_youtube = "youtube.com" in g or "youtu.be" in g
                if count == 0 or not is_youtube:
                    urls_to_delete.append(g)

            if not urls_to_delete:
                QMessageBox.information(self, "All Clean", "No zero-detection ghost URLs found to purge.")
                self.mark_local_cleaned()
                return

            chunk_size = 500
            total_chunks = math.ceil(len(urls_to_delete) / chunk_size)
            eta_seconds = total_chunks * 0.5  # Approx 0.5s per chunk dual-write
            eta_mins = int(eta_seconds // 60)
            eta_secs = int(eta_seconds % 60)
            eta_str = f"{eta_mins}m {eta_secs}s" if eta_mins > 0 else f"{eta_secs}s"

            msg = (
                "<b>Why do this?</b> IP Camera streams (HLS/.m3u8) use temporary security tokens that expire. When the engine auto-resolves a new token, the old URL is left behind in the database as a 'ghost' with 0 detections. Over time, this bloats your database file size and drastically slows down SQL query performance.<br><br>"
                "<b>What it does:</b> Safely bulk-deletes these ghost records from your Local database first, then immediately mirrors the deletion to the Cloud Database.<br><br>"
                f"<b>Found {len(urls_to_delete)} ghosts. Estimated time to complete: {eta_str}.</b><br><br>"
                "Proceed?"
            )
            
            if QMessageBox.warning(self, "Confirm Database Clean", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return

            progress = QProgressDialog(f"Purging Database Bloat...\n(ETA: {eta_str})", "Cancel", 0, len(urls_to_delete), self)
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setWindowTitle("Database Housekeeping")
            progress.setMinimumDuration(0)
            
            ghosts_removed = 0
            canceled = False

            local_con = db_connector.get_db_connection(force_local=True)
            cloud_con = db_connector.get_db_connection(force_local=False)
            is_cloud_active = getattr(cloud_con, 'db_type', 'sqlite') == 'postgres'
            
            l_cur = local_con.cursor()
            c_cur = cloud_con.cursor() if is_cloud_active else None

            tables_to_clean =[
                ('stream_health_events', 'stream_url'),
                ('stream_noise_profiles', 'stream_url'),
                ('audio_hashes', 'stream_url'),
                ('species_stream_profiles', 'stream_url'),
                ('detections', 'channel_url')
            ]

            start_time = time.time()

            for i in range(0, len(urls_to_delete), chunk_size):
                if progress.wasCanceled():
                    canceled = True
                    break
                    
                chunk = urls_to_delete[i:i+chunk_size]
                placeholders = ','.join(['?'] * len(chunk))
                
                # Bulk delete
                for table, col in tables_to_clean:
                    q = f"DELETE FROM {table} WHERE {col} IN ({placeholders})"
                    l_cur.execute(q, chunk)
                    if c_cur:
                        try:
                            c_cur.execute(q, chunk)
                        except Exception as e:
                            logging.error(f"Cloud delete chunk error: {e}")

                local_con.commit()
                if is_cloud_active:
                    cloud_con.commit()
                    
                ghosts_removed += len(chunk)
                progress.setValue(ghosts_removed)
                
                # Calculate dynamic ETA
                elapsed = time.time() - start_time
                avg_time = elapsed / max(1, ghosts_removed)
                remaining = avg_time * (len(urls_to_delete) - ghosts_removed)
                rem_m = int(remaining // 60)
                rem_s = int(remaining % 60)
                rem_str = f"{rem_m}m {rem_s}s" if rem_m > 0 else f"{rem_s}s"
                progress.setLabelText(f"Purging Database Bloat...\nChunk {i//chunk_size + 1} of {total_chunks} (ETA: {rem_str})")
                QApplication.processEvents()

            local_con.close()
            if is_cloud_active:
                cloud_con.close()
            
            if canceled:
                QMessageBox.warning(self, "Purge Canceled", 
                    f"Operation canceled by user.\n"
                    f"Successfully removed {ghosts_removed} ghost streams before stopping.\n"
                    f"Partial progress has been safely saved to both Local and Cloud databases.")
            else:
                QMessageBox.information(self, "Database Cleaned", 
                    f"Successfully bulk-removed {ghosts_removed} ghost stream URLs.\n"
                    f"(Synchronized to Cloud Database: {is_cloud_active})")
                
                self.mark_local_cleaned()

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to clean database: {e}")
            logging.error(f"DB Bloat clean error: {e}", exc_info=True)

    def clear_orphaned_clips(self):
        msg = "This will DELETE all audio clips (.mp3/.wav) that are NOT currently serving as 'Golden Anchors' for the AI.\n\nProceed?"
        if QMessageBox.question(self, "Confirm Sync", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                if not BASELINE_CLIPS_DIR.exists():
                    QMessageBox.information(self, "Info", "baseline_clips folder does not exist.")
                    return
                
                with db_connector.get_db_connection(force_local=True) as con:
                    cur = con.cursor()
                    cur.execute("SELECT stream_url, species_name, baseline_detection_id FROM species_stream_profiles WHERE baseline_detection_id IS NOT NULL")
                    to_remove =[]
                    for row in cur.fetchall():
                        if not ((BASELINE_CLIPS_DIR / f"detection_{row[2]}.mp3").exists() or (BASELINE_CLIPS_DIR / f"detection_{row[2]}.wav").exists()):
                            to_remove.append((row[0], row[1]))
                    if to_remove: 
                        for tr in to_remove:
                            cur.execute("DELETE FROM species_stream_profiles WHERE stream_url = ? AND species_name = ?", (tr[0], tr[1]))
                        con.commit()
                    
                    cur.execute("SELECT DISTINCT baseline_detection_id FROM species_stream_profiles WHERE baseline_detection_id IS NOT NULL")
                    valid_ids = {str(r[0]) for r in cur.fetchall()}
                    
                    files_removed = 0
                    freed_bytes = 0
                    for f in BASELINE_CLIPS_DIR.glob("detection_*.*"):
                        if f.suffix in['.mp3', '.wav']:
                            try:
                                clip_id = f.stem.split('_')[1].split('.')[0]
                                if clip_id not in valid_ids:
                                    freed_bytes += f.stat().st_size
                                    f.unlink()
                                    files_removed += 1
                            except: pass
                    
                    mb_freed = freed_bytes / (1024 * 1024)
                    QMessageBox.information(self, "Sync Complete", f"Removed {len(to_remove)} DB records.\nDeleted {files_removed} orphaned clips.\nFreed {mb_freed:.2f} MB.")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to clean clips: {e}")
            finally:
                QApplication.restoreOverrideCursor()

    def clear_temp_bloat(self):
        temp_dir = Path(tempfile.gettempdir())
        if not temp_dir.exists():
            QMessageBox.information(self, "Info", "Temp directory not found.")
            return
            
        msg = ("This will scan your Windows %TEMP% folder and permanently delete orphaned folders/files "
               "created by Chrome, Selenium, and FFmpeg (e.g., scoped_dir*, uc_*, tmp*.wav).\n\n"
               "Files currently in use will be safely skipped.\nProceed?")
               
        if QMessageBox.question(self, "Confirm Deep Clean", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            deleted_count = 0
            freed_bytes = 0
            
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                for item in temp_dir.iterdir():
                    try:
                        is_target = item.name.startswith(('scoped_dir', 'uc_', 'yt-dlp')) or (item.name.startswith('tmp') and item.name.endswith('.wav'))
                        if is_target:
                            item_size = sum(f.stat().st_size for f in item.glob('**/*') if f.is_file()) if item.is_dir() else item.stat().st_size
                            
                            if item.is_dir():
                                shutil.rmtree(item, ignore_errors=True)
                            else:
                                item.unlink(missing_ok=True)
                            
                            if not item.exists():
                                deleted_count += 1
                                freed_bytes += item_size
                    except Exception:
                        pass 
            finally:
                QApplication.restoreOverrideCursor()
                
            mb_freed = freed_bytes / (1024 * 1024)
            QMessageBox.information(self, "Clean Complete", f"Successfully deleted {deleted_count} orphaned items.\nFreed approximately {mb_freed:.2f} MB of space.")

    def get_values(self):
        hk_conf = {
            'janitor_interval_hours': self.spin_janitor.value(),
            'legacy_log_retention_hours': self.spin_log.value(),
            'temp_retention_hours': self.spin_temp.value(),
            'auto_wipe_python_caches': self.cb_py.isChecked(),
            'auto_wipe_orphaned_clips': self.cb_clips.isChecked(),
            'server_maintenance_interval_days': self.spin_server_interval.value(),
            'last_server_maintenance_ts': self.last_server_maintenance_ts,
            'local_db_interval_days': self.spin_local_interval.value(),
            'last_local_db_maintenance_ts': self.last_local_db_maintenance_ts
        }
        return self.debug_cb.isChecked(), hk_conf


# ==============================================================================
# ADVANCED SETTINGS DIALOG
# ==============================================================================
class AdvancedSettingsDialog(QDialog):
    def __init__(self, loop_config, distance_config, map_config, cookie_config, browser_config, debug_enabled=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Advanced Settings & Maintenance")
        self.setMinimumSize(650, 500)

        self.distance_config = copy.deepcopy(distance_config)
        self.map_config = copy.deepcopy(map_config)
        self.cookie_config = copy.deepcopy(cookie_config)
        self.browser_config = copy.deepcopy(browser_config)
        
        self.debug_enabled = debug_enabled

        self.layout = QVBoxLayout(self)

        # --- THE NEW MAP BALANCE SLIDER ---
        balance_group = QGroupBox("Map Alert Balance (Audio vs. Vision)")
        balance_layout = QVBoxLayout(balance_group)
        
        info_lbl = QLabel("Controls the ideal ratio of alerts displayed on the Web Map and sidebar. If one engine is quiet, the other will automatically fill the empty slots to keep the map active up to your Target Capacity.")
        info_lbl.setWordWrap(True)
        info_lbl.setStyleSheet("color: #aaa; margin-bottom: 5px;")
        balance_layout.addWidget(info_lbl)
        
        slider_layout = QHBoxLayout()
        self.slider_ratio = QSlider(Qt.Orientation.Horizontal)
        self.slider_ratio.setRange(0, 100)
        self.slider_ratio.setValue(self.map_config.get("audio_vision_ratio", 70))
        
        self.lbl_ratio = QLabel()
        self.lbl_ratio.setFixedWidth(250)
        self.lbl_ratio.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        def update_ratio_label(val):
            self.lbl_ratio.setText(f"Target Ratio: <b>{100-val}% Vision</b> / <b>{val}% Audio</b>")
            
        self.slider_ratio.valueChanged.connect(update_ratio_label)
        update_ratio_label(self.slider_ratio.value())
        
        slider_layout.addWidget(QLabel("100% Vision"))
        slider_layout.addWidget(self.slider_ratio)
        slider_layout.addWidget(QLabel("100% Audio"))
        
        balance_layout.addLayout(slider_layout)
        balance_layout.addWidget(self.lbl_ratio, alignment=Qt.AlignmentFlag.AlignCenter)
        
        # --- TARGET CAPACITIES (FORM LAYOUT) ---
        capacity_form = QFormLayout()
        
        self.spin_target_map_capacity = QSpinBox()
        self.spin_target_map_capacity.setRange(10, 250) # INCREASED TO 250
        self.spin_target_map_capacity.setValue(self.map_config.get("target_map_capacity", 50))
        self.spin_target_map_capacity.setToolTip("The ideal total number of pins on the map. The system will dynamically allocate these slots between Audio and Vision based on the ratio above.")
        
        self.spin_target_sidebar_capacity = QSpinBox()
        self.spin_target_sidebar_capacity.setRange(10, 500)
        self.spin_target_sidebar_capacity.setValue(self.map_config.get("target_sidebar_capacity", 50))
        self.spin_target_sidebar_capacity.setToolTip("The maximum number of chronological alerts to display in the scrolling sidebar feed.")
        
        capacity_form.addRow("Target Map Capacity (Total Pins):", self.spin_target_map_capacity)
        capacity_form.addRow("Target Sidebar Capacity (Feed Items):", self.spin_target_sidebar_capacity)
        
        balance_layout.addLayout(capacity_form)
        
        self.layout.addWidget(balance_group)

        distance_group = QGroupBox("Audio (BirdNET) Distance Estimation Alerts")
        distance_layout = QHBoxLayout()
        self.dist_enable_cb = QCheckBox("Enable")
        distance_layout.addWidget(self.dist_enable_cb)
        distance_layout.addStretch()
        self.dist_filters_widget = QWidget()
        dist_filters_layout = QHBoxLayout()
        dist_filters_layout.setContentsMargins(0,0,0,0)
        
        self.dist_vn_cb = QCheckBox("Very Near"); self.dist_n_cb = QCheckBox("Near")
        self.dist_m_cb = QCheckBox("Mid-range"); self.dist_f_cb = QCheckBox("Far")
        
        dist_filters_layout.addWidget(self.dist_vn_cb); dist_filters_layout.addWidget(self.dist_n_cb)
        dist_filters_layout.addWidget(self.dist_m_cb); dist_filters_layout.addWidget(self.dist_f_cb)
        self.dist_filters_widget.setLayout(dist_filters_layout)
        distance_layout.addWidget(self.dist_filters_widget)
        distance_group.setLayout(distance_layout)
        self.dist_enable_cb.toggled.connect(self.toggle_distance_filters)
        
        dist_main_lay = QVBoxLayout()
        dist_main_lay.addLayout(distance_layout)
        note_lbl = QLabel("<i>Note: Vision (3D depth) rules are handled separately in the Intelligence Hub.</i>")
        note_lbl.setStyleSheet("color: #888;")
        dist_main_lay.addWidget(note_lbl)
        distance_group.setLayout(dist_main_lay)
        
        self.layout.addWidget(distance_group)

        map_group = QGroupBox("Map Visualization")
        map_layout = QHBoxLayout()
        map_layout.addWidget(QLabel("Map 'Live' Icon Duration (sec):"))
        self.map_live_spin = QSpinBox()
        self.map_live_spin.setRange(10, 3600)
        self.map_live_spin.setValue(120) 
        self.map_live_spin.setToolTip("Detections younger than this will appear Red (Live) on the map.\nOlder ones turn Orange (Recent).")
        map_layout.addWidget(self.map_live_spin)
        map_layout.addStretch()
        map_group.setLayout(map_layout)
        self.layout.addWidget(map_group)

        cookie_group = QGroupBox("YouTube Fallback (Cookies.txt)")
        cookie_layout = QFormLayout(cookie_group)
        cookie_path_layout = QHBoxLayout()
        self.cookies_file_edit = QLineEdit()
        self.browse_cookies_button = QPushButton("Browse...")
        self.browse_cookies_button.clicked.connect(self.browse_for_cookies_file)
        cookie_path_layout.addWidget(self.cookies_file_edit)
        cookie_path_layout.addWidget(self.browse_cookies_button)
        cookie_layout.addRow("Cookies File:", cookie_path_layout)
        self.layout.addWidget(cookie_group)

        maint_group = QGroupBox("Identity Engine")
        maint_group.setStyleSheet("QGroupBox { font-weight: bold; color: #2196F3; border: 1px solid #2196F3; margin-top: 15px; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; }")
        maint_layout = QVBoxLayout(maint_group)
        
        info_label = QLabel("Use these tools if you encounter 'Session Not Created', 'Version Mismatch', or '429 Too Many Requests' errors.")
        info_label.setWordWrap(True)
        maint_layout.addWidget(info_label)

        btn_layout = QHBoxLayout()
        
        self.btn_help = QPushButton("❓ HELP: How to use this?")
        self.btn_help.clicked.connect(self.show_maintenance_help)
        self.btn_help.setStyleSheet("background-color: #607D8B; color: white;")
        
        self.btn_repair = QPushButton("🔧 Auto-Repair Driver")
        self.btn_repair.clicked.connect(self.auto_repair_driver)
        self.btn_repair.setStyleSheet("background-color: #FF9800; color: black; font-weight: bold;")
        
        self.btn_refresh_identity = QPushButton("🎭 Refresh Bot Identity")
        self.btn_refresh_identity.clicked.connect(self.refresh_identity)
        self.btn_refresh_identity.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")

        btn_layout.addWidget(self.btn_help)
        btn_layout.addWidget(self.btn_repair)
        btn_layout.addWidget(self.btn_refresh_identity)
        maint_layout.addLayout(btn_layout)
        self.layout.addWidget(maint_group)

        self.layout.addStretch()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.layout.addWidget(self.buttons)

        self.populate_ui_from_config()

    def show_maintenance_help(self):
        msg = (
            "<h3>System Maintenance Guide</h3>"
            "<p><b>1. Auto-Repair Driver (The Engine):</b><br>"
            "Use this if you see <i>'Session Not Created'</i> or <i>'Version Mismatch'</i> errors. "
            "It scans your installed Chrome version and automatically downloads the matching 'chromedriver.exe', "
            "killing any stuck processes in the way.</p>"
            "<hr>"
            "<p><b>2. Refresh Bot Identity (The License):</b><br>"
            "Use this if you see <i>'429 Too Many Requests'</i> or <i>'Sign in to confirm'</i> errors. "
            "It opens a browser window using your configured 'BirdsongRadio' profile. "
            "<b>Action:</b> Watch a video for 10 seconds, then close the window. This refreshes the 'Trust Token' the bot uses.</p>"
            "<hr>"
            "<p><b>Recommended Routine:</b><br>"
            "If the monitoring crashes, click <b>Repair</b> first, wait for success, then click <b>Refresh</b>."
        )
        QMessageBox.information(self, "Help", msg)

    def auto_repair_driver(self):
        self.btn_repair.setText("Scanning..."); self.btn_repair.setEnabled(False); QApplication.processEvents()
        for proc in psutil.process_iter(['pid', 'name']):
            if proc.info['name'] and 'chromedriver' in proc.info['name'].lower():
                try: proc.kill()
                except: pass
        chrome_ver = ChromeMaintenance.get_installed_chrome_version()
        if not chrome_ver:
            QMessageBox.critical(self, "Error", "Could not detect Chrome version. Is Google Chrome installed?"); self.btn_repair.setText("🔧 Auto-Repair Driver"); self.btn_repair.setEnabled(True); return

        self.btn_repair.setText(f"Found v{chrome_ver}..."); QApplication.processEvents()
        success, msg = ChromeMaintenance.download_driver(chrome_ver)
        if success:
            QMessageBox.information(self, "Success", f"Driver repaired successfully!\n{msg}")
        else:
            QMessageBox.critical(self, "Failed", f"Could not repair driver:\n{msg}")
        self.btn_repair.setText("🔧 Auto-Repair Driver"); self.btn_repair.setEnabled(True)

    def refresh_identity(self):
        profile_path = self.browser_config.get("chrome_profile_path", "")
        if not profile_path or not os.path.exists(profile_path):
            QMessageBox.critical(self, "Error", "Invalid Profile Path. Please check the main config window.")
            return

        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.service import Service
            for proc in psutil.process_iter(['pid', 'name']):
                if proc.info['name'] and 'chrome' in proc.info['name'].lower() and '--headless' in str(proc.cmdline()):
                    try: proc.kill()
                    except: pass
            QMessageBox.information(self, "Instructions", 
                "A Chrome window will now open using the Bot's profile.\n\n"
                "1. If prompted, click 'Yes, I'm in' or verify login.\n"
                "2. Watch any YouTube video for 10-20 seconds.\n"
                "3. Close the window to save the session.")

            service = Service(executable_path=str(ROOT / "chromedriver.exe"))
            options = webdriver.ChromeOptions()
            options.add_argument(f"--user-data-dir={profile_path}")
            options.add_experimental_option("detach", True) 
            driver = webdriver.Chrome(service=service, options=options)
            driver.get("https://www.youtube.com")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to launch browser: {e}\n\nTry 'Auto-Repair Driver' first.")

    def browse_for_cookies_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select YouTube Cookies File", "", "Text Files (cookies.txt *.txt)")
        if file_path: self.cookies_file_edit.setText(file_path)

    def toggle_distance_filters(self, enabled): 
        self.dist_filters_widget.setEnabled(enabled)

    def populate_ui_from_config(self):
        self.dist_enable_cb.setChecked(self.distance_config.get("enabled", False))
        alert_distances = self.distance_config.get("alert_distances", [])
        for cb, dist in[(self.dist_vn_cb, "Very Near"), (self.dist_n_cb, "Near"), (self.dist_m_cb, "Mid-range"), (self.dist_f_cb, "Far")]:
            cb.setChecked(dist in alert_distances)
        self.toggle_distance_filters(self.dist_enable_cb.isChecked())
        self.map_live_spin.setValue(self.map_config.get("live_window_seconds", 120))
        self.cookies_file_edit.setText(self.cookie_config.get("youtube_cookies_file", ""))

    def get_updated_configs(self):
        updated_distance_config = {
            "enabled": self.dist_enable_cb.isChecked(),
            "alert_distances": sorted([cb.text() for cb in[self.dist_vn_cb, self.dist_n_cb, self.dist_m_cb, self.dist_f_cb] if cb.isChecked()])
        }
        updated_cookie_config = {
            "youtube_cookies_file": self.cookies_file_edit.text().strip(),
            "cookie_alert": self.cookie_config.get("cookie_alert", {}) 
        }
        updated_map_config = {
            "live_window_seconds": self.map_live_spin.value(),
            "audio_vision_ratio": self.slider_ratio.value(),
            "target_map_capacity": self.spin_target_map_capacity.value(),
            "target_sidebar_capacity": self.spin_target_sidebar_capacity.value()
        }
        
        return updated_map_config, updated_distance_config, updated_cookie_config, self.debug_enabled

class SaveConfirmDialog(QDialog):
    def __init__(self, changes, widths=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Confirm Configuration Changes")
        self.setMinimumSize(700, 400)
        layout = QVBoxLayout()
        intro = QLabel("The following changes have been detected. Click 'Save' to apply them.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Setting", "Old Value", "New Value", "When Effective"])
        
        for i in range(self.table.columnCount()):
            self.table.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)

        if widths and len(widths) == self.table.columnCount():
            for i, width in enumerate(widths):
                self.table.setColumnWidth(i, width)
        
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setRowCount(len(changes))
        for i, change in enumerate(changes):
            self.table.setItem(i, 0, QTableWidgetItem(change['setting']))
            self.table.setItem(i, 1, QTableWidgetItem(change['old']))
            self.table.setItem(i, 2, QTableWidgetItem(change['new']))
            
            effective_item = QTableWidgetItem(change['effective'])
            if "Restart" in change['effective']:
                effective_item.setForeground(QColor('red'))
            self.table.setItem(i, 3, effective_item)
            
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        self.save_button = QPushButton("Save"); self.save_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("Cancel"); self.cancel_button.clicked.connect(self.reject)
        buttons.addStretch(); buttons.addWidget(self.cancel_button); buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self.setLayout(layout)
        self.table.horizontalHeader().sectionResized.connect(self._save_column_widths)

    def get_column_widths(self):
        return[self.table.columnWidth(i) for i in range(self.table.columnCount())]
    
    def _save_column_widths(self, *args):
        main_window = self.parent()
        if main_window:
            main_window.unsaved_save_dialog_widths = self.get_column_widths()
            main_window._save_gui_preferences()

class TieredCooldownDialog(QDialog):
    def __init__(self, config, widths=None, parent=None):
        super().__init__(parent)
        self.config = copy.deepcopy(config)
        self.initial_widths = widths
        self._is_programmatic_change = False
        self._selected_row = -1
        self.setWindowTitle("Configure Tiered Cooldowns")
        self.setMinimumSize(800, 600)
        self.init_ui()
        self.populate_ui_from_config()

    def init_ui(self):
        layout = QVBoxLayout(self)
        top_group = QGroupBox("Frequency Calculation & Overrides")
        top_layout = QGridLayout()

        self.calc_period_spin = QSpinBox(); self.calc_period_spin.setRange(1, 90)
        top_layout.addWidget(QLabel("Calculate species frequency over the last:"), 0, 0)
        top_layout.addWidget(self.calc_period_spin, 0, 1); top_layout.addWidget(QLabel("days"), 0, 2)

        self.suggest_button = QPushButton("Suggest & Recalculate Thresholds From Database")
        self.suggest_button.clicked.connect(self.suggest_and_recalculate)
        top_layout.addWidget(self.suggest_button, 0, 3, 1, 2)

        self.high_thresh_spin = QSpinBox(); self.high_thresh_spin.setRange(2, 99999)
        top_layout.addWidget(QLabel("\"Extremely High\" Threshold (> X detections):"), 1, 0)
        top_layout.addWidget(self.high_thresh_spin, 1, 1)

        self.low_thresh_spin = QSpinBox(); self.low_thresh_spin.setRange(1, 99999)
        top_layout.addWidget(QLabel("\"Extremely Low\" Threshold (<= X detections):"), 2, 0)
        top_layout.addWidget(self.low_thresh_spin, 2, 1)
        
        self.override_cb = QCheckBox("Always alert for first")
        self.override_spin = QSpinBox(); self.override_spin.setRange(1, 10)
        override_layout = QHBoxLayout(); override_layout.addWidget(self.override_cb)
        override_layout.addWidget(self.override_spin); override_layout.addWidget(QLabel("sightings of a species on any stream."))
        override_layout.addStretch(); top_layout.addLayout(override_layout, 3, 0, 1, 5)

        self.lock_cb = QCheckBox("Lock Current Thresholds (Disables Automatic Learning)")
        self.lock_cb.setToolTip("When locked, the system uses these exact thresholds. When unlocked, it will auto-adjust them daily based on new data.")
        top_layout.addWidget(self.lock_cb, 4, 0, 1, 5)
        top_group.setLayout(top_layout)
        layout.addWidget(top_group)
        
        presets_group = QGroupBox("Cooldown Presets")
        presets_layout = QHBoxLayout()
        presets_layout.addWidget(QLabel("<b>Quick Setup:</b>"))
        self.presets_combo = QComboBox()
        self.presets_combo.addItems(["Custom", "Aggressive", "Attentive", "Normal (Default)", "Relaxed", "Hands-Off"])
        self.reset_to_default_button = QPushButton("Reset to Default")
        presets_layout.addWidget(self.presets_combo)
        presets_layout.addStretch()
        presets_layout.addWidget(self.reset_to_default_button)
        presets_group.setLayout(presets_layout)
        layout.addWidget(presets_group)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Tier Name", "Min Detections", "Max Detections", "Simple Cooldown (min)", "Alert After X Detections"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, 5): self.table.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setStyleSheet("QTableWidget { selection-background-color: transparent; }")

        if self.initial_widths: self.set_table_widths(self.initial_widths)
        layout.addWidget(self.table)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept); self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        
        self.high_thresh_spin.valueChanged.connect(self.auto_recalculate_from_spin)
        self.low_thresh_spin.valueChanged.connect(self.auto_recalculate_from_spin)
        
        self.presets_combo.currentIndexChanged.connect(self._apply_preset)
        self.reset_to_default_button.clicked.connect(self._reset_to_default)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.horizontalHeader().sectionResized.connect(self._save_column_widths)

    def _on_selection_changed(self):
        selected_items = self.table.selectedItems()
        if selected_items:
            self._selected_row = selected_items[0].row()
        else:
            self._selected_row = -1
        self._refresh_table_visuals()

    def _refresh_table_visuals(self):
        selected_widget_style = "background-color: transparent; color: white;"
        selected_bg_color = QBrush(QColor("#0078d7"))
        
        for row in range(self.table.rowCount()):
            is_selected = (row == self._selected_row)
            for col in range(self.table.columnCount()):
                item = self.table.item(row, col)
                if item:
                    item.setBackground(selected_bg_color if is_selected else QBrush(QColor("transparent")))
                    item.setForeground(QBrush(QColorConstants.White) if is_selected else QBrush(self.palette().color(QPalette.ColorRole.Text)))
                
                if col in[3, 4]:
                    widget = self.table.cellWidget(row, col)
                    if widget:
                        widget.setStyleSheet(selected_widget_style if is_selected else "")

    def _apply_preset(self, index):
        if self._is_programmatic_change: return
        preset_name = self.presets_combo.itemText(index)
        
        presets = {
            "Aggressive":       [[0, 1],[0, 1],[0, 1],[0, 1],[0, 1],[0, 1],[0, 1]],
            "Attentive":        [[720, 3],[360, 2],[120, 1],[60, 1],[30, 1],[5, 1],[0, 1]],
            "Normal (Default)": [[1440, 5],[720, 3],[360, 1],[120, 1],[60, 1],[10, 1],[0, 1]],
            "Relaxed":          [[2880, 5],[1440, 3],[720, 2],[240, 1],[120, 1],[30, 1],[5, 1]],
            "Hands-Off":        [[4320, 7],[2880, 5],[1440, 3],[720, 2],[360, 1],[60, 1],[15, 1]]
        }
        
        if preset_name in presets and preset_name != "Custom":
            for r in range(self.table.rowCount()):
                for c in[3, 4]:
                    widget = self.table.cellWidget(r, c)
                    if widget: widget.blockSignals(True)

            values = presets[preset_name]
            for i, (cooldown, alert_after) in enumerate(values):
                self.table.cellWidget(i, 3).setValue(cooldown)
                self.table.cellWidget(i, 4).setValue(alert_after)

            for r in range(self.table.rowCount()):
                for c in[3, 4]:
                    widget = self.table.cellWidget(r, c)
                    if widget: widget.blockSignals(False)

    def _reset_to_default(self):
        self.presets_combo.setCurrentText("Normal (Default)")

    def _mark_as_custom(self, *args):
        if self._is_programmatic_change: return
        if self.presets_combo.currentIndex() != 0:
            self._is_programmatic_change = True
            self.presets_combo.setCurrentIndex(0)
            self._is_programmatic_change = False
        self._refresh_table_visuals()

    def populate_ui_from_config(self):
        self._is_programmatic_change = True
        
        self.calc_period_spin.setValue(self.config.get("calculation_period_days", 14))
        override_cfg = self.config.get("first_sighting_override", {})
        self.override_cb.setChecked(override_cfg.get("enabled", True))
        self.override_spin.setValue(override_cfg.get("count", 2))
        self.lock_cb.setChecked(self.config.get("thresholds_locked", False))
        
        tiers = self.config.get("tiers",[])
        if tiers:
            self.high_thresh_spin.setValue(tiers[0].get("min_detections", 500))
            self.low_thresh_spin.setValue(tiers[-1].get("max_detections", 2))
        
        self.table.setRowCount(len(tiers))
        for i, tier in enumerate(tiers):
            self.table.setItem(i, 0, QTableWidgetItem(tier["name"]))
            self.table.setItem(i, 1, QTableWidgetItem(str(tier["min_detections"])))
            self.table.setItem(i, 2, QTableWidgetItem(str(tier["max_detections"]) if tier["max_detections"] is not None else "∞"))
            
            if not self.table.cellWidget(i, 3):
                cd_spin = QSpinBox(); cd_spin.setRange(0, 99999)
                cd_spin.valueChanged.connect(self._mark_as_custom)
                self.table.setCellWidget(i, 3, cd_spin)
            self.table.cellWidget(i, 3).setValue(tier["simple_minutes"])

            if not self.table.cellWidget(i, 4):
                alert_spin = QSpinBox(); alert_spin.setRange(1, 100)
                alert_spin.valueChanged.connect(self._mark_as_custom)
                self.table.setCellWidget(i, 4, alert_spin)
            self.table.setCellWidget(i, 4).setValue(tier["alert_threshold_count"])
        
        preset_name = self.config.get("active_preset", "Custom")
        if self.presets_combo.currentText() != preset_name:
            self.presets_combo.setCurrentText(preset_name)
        
        self._is_programmatic_change = False
        self._refresh_table_visuals()
            
    def suggest_and_recalculate(self):
        self._mark_as_custom()
        try:
            if not DATABASE_PATH.exists(): QMessageBox.warning(self, "No Data", "The detections database does not exist yet."); return
            con = sqlite3.connect(DATABASE_PATH); cur = con.cursor()
            period_days = self.calc_period_spin.value()
            cutoff_time = time.time() - (period_days * 86400)
            cur.execute("SELECT COUNT(*) FROM detections WHERE timestamp > ? GROUP BY channel_url, species", (cutoff_time,))
            counts = [row[0] for row in cur.fetchall()]; con.close()
            
            if not counts: QMessageBox.information(self, "Insufficient Data", f"No detections found in the last {period_days} days."); return
            counts.sort()
            suggested_high = counts[min(int(len(counts) * 0.95), len(counts) - 1)]
            
            safe_high = max(suggested_high, self.low_thresh_spin.value() + 10)

            msg = f"Based on the last {period_days} days, a new 'Extremely High' threshold of >{safe_high} is suggested.\n\nApply this and recalculate intermediate tiers?"
            if QMessageBox.question(self, "Suggestion Found", msg) == QMessageBox.StandardButton.Yes:
                self.high_thresh_spin.blockSignals(True)
                self.high_thresh_spin.setValue(safe_high)
                self.high_thresh_spin.blockSignals(False)
                self.auto_recalculate_from_spin()
        except Exception as e:
            QMessageBox.critical(self, "Database Error", f"Could not query the database.\n\nError: {e}")
            logging.error("Error suggesting thresholds:", exc_info=True)
            
    def auto_recalculate_from_spin(self, *args):
        self._mark_as_custom()
        if self.high_thresh_spin.signalsBlocked(): return
        self.auto_recalculate_tiers()
        self.populate_ui_from_config()

    def auto_recalculate_tiers(self):
        low_max = self.low_thresh_spin.value()
        high_min = self.high_thresh_spin.value()
        if high_min <= low_max: return

        num_intermediate = 5
        log_low = math.log(low_max + 1)
        log_high = math.log(high_min)
        if log_high == log_low: return
        log_step = (log_high - log_low) / (num_intermediate + 1)

        boundaries =[low_max] +[int(round(math.exp(log_low + i * log_step))) for i in range(1, num_intermediate + 1)]

        self.config["tiers"][6]["max_detections"] = boundaries[0]
        for i in range(num_intermediate):
            tier_index = num_intermediate - i
            min_det = boundaries[i] + 1
            max_det = boundaries[i+1]
            self.config["tiers"][tier_index]["min_detections"] = min_det
            self.config["tiers"][tier_index]["max_detections"] = max_det
        self.config["tiers"][0]["min_detections"] = high_min

    def get_updated_config(self):
        updated_config = {}
        updated_config["active_preset"] = self.presets_combo.currentText()
        updated_config["calculation_period_days"] = self.calc_period_spin.value()
        updated_config["first_sighting_override"] = {
            "enabled": self.override_cb.isChecked(),
            "count": self.override_spin.value()
        }
        updated_config["thresholds_locked"] = self.lock_cb.isChecked()
        
        tiers = copy.deepcopy(self.config.get("tiers",[]))
        tiers[0]["min_detections"] = self.high_thresh_spin.value()
        tiers[-1]["max_detections"] = self.low_thresh_spin.value()
        
        low_max = tiers[-1]["max_detections"]
        high_min = tiers[0]["min_detections"]
        if high_min > low_max:
            num_intermediate = 5
            log_low = math.log(low_max + 1)
            log_high = math.log(high_min)
            if log_high != log_low:
                log_step = (log_high - log_low) / (num_intermediate + 1)
                boundaries =[low_max] +[int(round(math.exp(log_low + i * log_step))) for i in range(1, num_intermediate + 1)]
                for i in range(num_intermediate):
                    tier_index = num_intermediate - i
                    min_det = boundaries[i] + 1
                    max_det = boundaries[i+1]
                    tiers[tier_index]["min_detections"] = min_det
                    tiers[tier_index]["max_detections"] = max_det

        for i in range(len(tiers)):
            tiers[i]["simple_minutes"] = self.table.cellWidget(i, 3).value()
            tiers[i]["alert_threshold_count"] = self.table.cellWidget(i, 4).value()

        updated_config["tiers"] = tiers
        return updated_config

    def get_column_widths(self): 
        return[self.table.columnWidth(i) for i in range(self.table.columnCount())]
        
    def set_table_widths(self, widths):
        if len(widths) == self.table.columnCount():
            for i, width in enumerate(widths): self.table.setColumnWidth(i, width)

    def _save_column_widths(self, *args):
        main_window = self.parent()
        if main_window:
            main_window.unsaved_cooldown_widths = self.get_column_widths()
            main_window._save_gui_preferences()
        
    @staticmethod
    def get_default_tiered_config():
        return {
            "calculation_period_days": 14,
            "first_sighting_override": {"enabled": True, "count": 2},
            "thresholds_locked": False,
            "active_preset": "Normal (Default)",
            "tiers":[
                {"name": "Extremely High", "min_detections": 500, "max_detections": None, "simple_minutes": 1440, "alert_threshold_count": 5},
                {"name": "Very High", "min_detections": 92, "max_detections": 499, "simple_minutes": 720, "alert_threshold_count": 3},
                {"name": "High", "min_detections": 40, "max_detections": 91, "simple_minutes": 360, "alert_threshold_count": 1},
                {"name": "Medium", "min_detections": 18, "max_detections": 39, "simple_minutes": 120, "alert_threshold_count": 1},
                {"name": "Low", "min_detections": 8, "max_detections": 17, "simple_minutes": 60, "alert_threshold_count": 1},
                {"name": "Very Low", "min_detections": 3, "max_detections": 7, "simple_minutes": 10, "alert_threshold_count": 1},
                {"name": "Extremely Low", "min_detections": 0, "max_detections": 2, "simple_minutes": 0, "alert_threshold_count": 1}
            ]
        }

class ResetConfirmDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Confirm Factory Reset")
        layout = QVBoxLayout(self)

        warning_label = QLabel("⚠️ This will permanently delete ALL settings and your stream list. This action cannot be undone.")
        warning_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #f44336;")
        warning_label.setWordWrap(True)
        prompt_label = QLabel("To confirm, please type <b>RESET</b> in the box below and click the button.")
        self.confirm_edit = QLineEdit()
        self.confirm_edit.textChanged.connect(self.check_text)
        self.reset_button = QPushButton("Factory Reset (DELETE MY DATA)")
        self.reset_button.setEnabled(False)
        self.reset_button.setStyleSheet("background-color: #cccccc;")
        self.reset_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        button_layout.addWidget(self.cancel_button)
        button_layout.addWidget(self.reset_button)
        layout.addWidget(warning_label)
        layout.addWidget(prompt_label)
        layout.addWidget(self.confirm_edit)
        layout.addLayout(button_layout)

    def check_text(self, text):
        is_match = text == "RESET"
        self.reset_button.setEnabled(is_match)
        self.reset_button.setStyleSheet("background-color: #f44336; color: white;" if is_match else "background-color: #cccccc;")

class EngineConfigDialog(QDialog):
    def __init__(self, current_config, network_map, loop_config, current_listeners, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Engine & Network Configuration (Mission Control)")
        self.setMinimumSize(700, 600)
        self.layout = QVBoxLayout(self)
        self.config = current_config 
        self.network_map = network_map
        self.loop_config = loop_config
        self.total_listeners = current_listeners
        
        self.tabs = QTabWidget()
        
        # --- TAB 1: PERFORMANCE ---
        perf_tab = QWidget()
        perf_layout = QFormLayout()
        
        self.spin_batch = QSpinBox()
        self.spin_batch.setRange(1, 100)
        self.spin_batch.setValue(self.config.get('batch_size', 20))
        self.spin_listeners = QSpinBox()
        self.spin_listeners.setRange(1, 32)
        self.spin_listeners.setValue(self.total_listeners)
        self.spin_listeners.setToolTip("The number of independent worker processes running in parallel.\n\nConsequence: Higher = Faster scanning but higher CPU/RAM usage.\nHardware Limit: Max 12 for your i7-1260P.")
        
        self.spin_capture = QSpinBox()
        self.spin_capture.setRange(3, 60)
        self.spin_capture.setValue(self.config.get('capture_seconds', 12))
        self.spin_capture.setToolTip("Length of audio clip analyzed per scan.\n\nConsequence: Longer = Better chance of catching a song, but slower cycle speed.")
        
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(10, 600)
        self.spin_interval.setValue(self.config.get('interval_seconds', 300))
        self.spin_interval.setToolTip("The ideal time it should take to scan ALL streams exactly once. The system forces listeners to wait in line to maintain this pace.\n\nConsequence:\n- High (600s): Safe, low bandwidth, low risk of blocking.\n- Low (120s): Aggressive, fast alerts, higher risk.")
        
        self.spin_jitter = QSpinBox()
        self.spin_jitter.setRange(0, 120)
        self.spin_jitter.setValue(self.config.get('interval_jitter', 15))
        self.spin_jitter.setToolTip("Adds random variation to the 'Turnstile' wait time.\n\nConsequence: Higher jitter (>15s) makes traffic look more human and prevents 'robot rhythm' bursts.")
        
        self.spin_threshold = QSpinBox()
        self.spin_threshold.setRange(10, 99)
        self.spin_threshold.setValue(int(self.config.get('score_threshold', 0.55)*100))
        self.spin_threshold.setSuffix("%")
        self.spin_threshold.setToolTip("Minimum AI confidence required to trigger an alert.")
        
        perf_layout.addRow("Total Listeners:", self.spin_listeners)
        perf_layout.addRow("Batch Size (Streams per Worker):", self.spin_batch)
        perf_layout.addRow("Capture Duration (s):", self.spin_capture)
        perf_layout.addRow("Target Global Cycle (s):", self.spin_interval)
        perf_layout.addRow("Interval Jitter (+/- s):", self.spin_jitter)
        perf_layout.addRow("Confidence Threshold:", self.spin_threshold)
        
        perf_tab.setLayout(perf_layout)
        self.tabs.addTab(perf_tab, "Performance")
        
        # --- TAB NEW: EXTRACTION STRATEGY ---
        ext_tab = QWidget()
        ext_layout = QFormLayout()
        
        ext_cfg = self.config.get('extraction_strategy', {})
        
        self.cb_fast_mode = QCheckBox("⚡ Enable Fast Mode (Bypass Heavy Browser Resolution)")
        self.cb_fast_mode.setChecked(self.config.get('fast_mode_enabled', True))
        self.cb_fast_mode.setToolTip("When Fast Mode is ON, the engines use lightweight HTTP extraction to heal dead links (~1 second).\nWhen OFF, the engines will silently launch Headless Google Chrome to sniff network traffic (~25 seconds, massive CPU/RAM cost).\nKeep this ENABLED for maximum map speed. Only disable this if you are debugging a stubborn stream.")
        self.cb_fast_mode.toggled.connect(self._warn_fast_mode)
        
        self.edit_player_client = QLineEdit()
        self.edit_player_client.setText(ext_cfg.get('player_client', 'web'))
        self.edit_player_client.setToolTip("Comma-separated list of clients for yt-dlp (e.g., ios, android, web). Helps spoof identity if YouTube blocks a specific platform.")
        
        self.edit_user_agent = QLineEdit()
        self.edit_user_agent.setText(ext_cfg.get('ffmpeg_user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'))
        self.edit_user_agent.setToolTip("The browser User-Agent string used by FFmpeg and requests. Prevents blocks from outdated browser signatures.")
        
        self.cb_audio_norm = QCheckBox("Enable Audio Normalization (-af dynaudnorm)")
        self.cb_audio_norm.setChecked(ext_cfg.get('audio_normalization', True))
        self.cb_audio_norm.setToolTip("Dynamically normalizes audio volume in FFmpeg. Uncheck if background noise (wind/waterfalls) is being artificially amplified too much.")
        
        self.spin_frame_quality = QSpinBox()
        self.spin_frame_quality.setRange(2, 5)
        self.spin_frame_quality.setValue(ext_cfg.get('frame_quality', 2))
        self.spin_frame_quality.setToolTip("FFmpeg JPEG extraction quality (-q:v). 2 = High Quality/Large File, 5 = Compressed/Small File. Balances Gemini accuracy against hard drive bloat.")
        
        ext_layout.addRow("", self.cb_fast_mode)
        ext_layout.addRow("API Client Spoofing:", self.edit_player_client)
        ext_layout.addRow("FFmpeg User-Agent:", self.edit_user_agent)
        ext_layout.addRow("", self.cb_audio_norm)
        ext_layout.addRow("Video Frame Quality (2-5):", self.spin_frame_quality)
        
        ext_tab.setLayout(ext_layout)
        self.tabs.addTab(ext_tab, "Extraction Strategy")

        # --- TAB 3: HEALTH & STRATEGY ---
        strat_tab = QWidget()
        strat_layout = QVBoxLayout()
        
        # --- REORGANIZED GROUP 1: GLOBAL DISCONNECTS ---
        global_group = QGroupBox("Global Disconnects (Halts Both Engines)")
        global_group.setStyleSheet("QGroupBox { border: 1px solid #B71C1C; margin-top: 15px; color: #EF5350; font-weight: bold; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }")
        global_form = QFormLayout()

        self.spin_unresponsive_penalty = QSpinBox()
        self.spin_unresponsive_penalty.setRange(1, 1440)
        self.spin_unresponsive_penalty.setValue(self.config.get('unresponsive_penalty_minutes', 240))
        self.spin_unresponsive_penalty.setSuffix(" min")
        
        self.spin_suspended_penalty = QSpinBox()
        self.spin_suspended_penalty.setRange(1, 40320)
        self.spin_suspended_penalty.setValue(self.config.get('suspended_penalty_minutes', 1440))
        self.spin_suspended_penalty.setSuffix(" min")

        self.spin_terminal_penalty = QSpinBox()
        self.spin_terminal_penalty.setRange(1, 525600)
        self.spin_terminal_penalty.setValue(self.config.get('terminal_penalty_minutes', 10080))
        self.spin_terminal_penalty.setSuffix(" min")

        global_form.addRow("Unresponsive Penalty (Tier 3):", self.spin_unresponsive_penalty)
        global_form.addRow("Suspended Penalty (Stream Offline):", self.spin_suspended_penalty)
        global_form.addRow("Fatal Penalty (Account Gone):", self.spin_terminal_penalty)
        
        global_group.setLayout(global_form)
        strat_layout.addWidget(global_group)

        # --- REORGANIZED GROUP 2: AUDIO MUTES ---
        audio_group = QGroupBox("Audio-Specific Mutes (Vision Keeps Running)")
        audio_group.setStyleSheet("QGroupBox { border: 1px solid #00897B; margin-top: 15px; color: #00E5FF; font-weight: bold; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }")
        audio_form = QFormLayout()

        self.spin_hiccup_penalty = QSpinBox()
        self.spin_hiccup_penalty.setRange(1, 60)
        self.spin_hiccup_penalty.setValue(self.config.get('hiccup_penalty_minutes', 1))
        self.spin_hiccup_penalty.setSuffix(" min")

        self.spin_fail_penalty = QSpinBox()
        self.spin_fail_penalty.setRange(1, 1440)
        self.spin_fail_penalty.setValue(self.config.get('failure_penalty', 30))
        self.spin_fail_penalty.setSuffix(" min")
        
        self.spin_intermittent_penalty = QSpinBox()
        self.spin_intermittent_penalty.setRange(1, 1440)
        self.spin_intermittent_penalty.setValue(self.config.get('intermittent_penalty_minutes', 120))
        self.spin_intermittent_penalty.setSuffix(" min")
        
        self.spin_loop_penalty = QSpinBox()
        self.spin_loop_penalty.setRange(1, 1440)
        self.spin_loop_penalty.setValue(self.config.get('loop_penalty', 360))
        self.spin_loop_penalty.setSuffix(" min")

        self.spin_silent_penalty = QSpinBox()
        self.spin_silent_penalty.setRange(1, 10080)
        self.spin_silent_penalty.setValue(self.config.get('silent_penalty_minutes', 60))
        self.spin_silent_penalty.setSuffix(" min")

        self.spin_silent_visual_penalty = QSpinBox()
        self.spin_silent_visual_penalty.setRange(1, 10080)
        self.spin_silent_visual_penalty.setValue(self.config.get('silent_visual_penalty_minutes', 720))
        self.spin_silent_visual_penalty.setSuffix(" min")

        audio_form.addRow("Network Glitch Penalty:", self.spin_hiccup_penalty)
        audio_form.addRow("Standard Penalty (1st Fail):", self.spin_fail_penalty)
        audio_form.addRow("Intermittent Penalty (Tier 2):", self.spin_intermittent_penalty)
        audio_form.addRow("Loop Penalty:", self.spin_loop_penalty)
        audio_form.addRow("Audio Pause (No Audio & No Visuals):", self.spin_silent_penalty)
        audio_form.addRow("Audio Pause (No Audio, BUT Visually Active):", self.spin_silent_visual_penalty)
        
        audio_group.setLayout(audio_form)
        strat_layout.addWidget(audio_group)

        # --- Thresholds ---
        thresh_group = QGroupBox("Long-Term Health Thresholds")
        thresh_form = QFormLayout()
        
        self.spin_intermittent = QSpinBox()
        self.spin_intermittent.setRange(1, 99)
        self.spin_intermittent.setValue(self.config.get('intermittent_threshold', 2))
        
        self.spin_unresponsive = QSpinBox()
        self.spin_unresponsive.setRange(2, 100)
        self.spin_unresponsive.setValue(self.config.get('unresponsive_threshold', 5))
        
        thresh_form.addRow("Flag as Intermittent (Fails):", self.spin_intermittent)
        thresh_form.addRow("Flag as Unresponsive (Fails):", self.spin_unresponsive)
        thresh_group.setLayout(thresh_form)
        strat_layout.addWidget(thresh_group)
        
        loop_group = QGroupBox("Loop Detection Analysis")
        loop_layout = QHBoxLayout()
        self.cb_loop_enable = QCheckBox("Enable Audio Hashing")
        self.cb_loop_enable.setChecked(self.loop_config.get("enabled", True))
        
        self.spin_loop_watch = QSpinBox()
        self.spin_loop_watch.setRange(1, 168)
        self.spin_loop_watch.setValue(self.loop_config.get("watch_period_hours", 48))
        
        self.spin_loop_quar = QSpinBox()
        self.spin_loop_quar.setRange(1, 168)
        self.spin_loop_quar.setValue(self.loop_config.get("quarantine_check_hours", 12))
        
        loop_layout.addWidget(self.cb_loop_enable)
        loop_layout.addStretch()
        loop_layout.addWidget(QLabel("Watch (h):"))
        loop_layout.addWidget(self.spin_loop_watch)
        loop_layout.addWidget(QLabel("Check Loop Every (h):"))
        loop_layout.addWidget(self.spin_loop_quar)
        loop_group.setLayout(loop_layout)
        strat_layout.addWidget(loop_group)
        
        strat_tab.setLayout(strat_layout)
        self.tabs.addTab(strat_tab, "Health & Strategy")
        
        # --- TAB 4: NETWORK MAP (THE HYDRA) ---
        net_tab = QWidget()
        net_layout = QVBoxLayout()
        
        net_header = QHBoxLayout()
        self.lbl_net_status = QLabel("Scanning interfaces...")
        self.btn_refresh_net = QPushButton("Refresh Adapters")
        self.btn_refresh_net.clicked.connect(self.scan_network)
        net_header.addWidget(self.lbl_net_status)
        net_header.addStretch()
        net_header.addWidget(self.btn_refresh_net)
        net_layout.addLayout(net_header)
        
        self.net_table = QTableWidget()
        self.net_table.setColumnCount(3)
        self.net_table.setHorizontalHeaderLabels(["Listener ID", "Assigned Interface", "Bind IP"])
        self.net_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        net_layout.addWidget(self.net_table)
        
        net_tab.setLayout(net_layout)
        self.tabs.addTab(net_tab, "Network Map")
        
        self.layout.addWidget(self.tabs)
        
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        self.layout.addWidget(btns)
        
        self.spin_listeners.valueChanged.connect(self.refresh_network_table)
        
        self.available_interfaces =[]
        self.combos =[]
        self.scan_network()
        
    def _warn_fast_mode(self, checked):
        if not checked:
            QMessageBox.warning(self, "Performance Warning", 
                "You are disabling Fast Mode.\n\n"
                "When OFF, the engines will launch Headless Google Chrome to sniff network traffic for dead links. "
                "This takes ~25 seconds per stream and has a massive CPU/RAM cost.\n\n"
                "Only disable this if you are actively debugging a stubborn stream that yt-dlp cannot read. "
                "Keep this ENABLED for maximum map speed.")

    def scan_network(self):
        self.lbl_net_status.setText("Scanning...")
        QApplication.processEvents()
        self.available_interfaces = network_manager.get_active_interfaces()
        self.lbl_net_status.setText(f"Found {len(self.available_interfaces)} active interfaces.")
        self.refresh_network_table()
        
    def refresh_network_table(self):
        num = self.spin_listeners.value()
        self.net_table.setRowCount(num)
        self.combos =[]
        
        for i in range(num):
            lid = f"L{i+1}"
            self.net_table.setItem(i, 0, QTableWidgetItem(lid))
            
            combo = QComboBox()
            combo.addItem("Default (OS Decision)", None)
            
            current_bind = self.network_map.get(lid)
            sel_idx = 0
            
            for iface in self.available_interfaces:
                combo.addItem(f"{iface['name']} ({iface['ip']})", iface['name'])
                if current_bind == iface['name']:
                    sel_idx = combo.count() - 1
            
            combo.setCurrentIndex(sel_idx)
            combo.currentIndexChanged.connect(lambda idx, r=i: self.update_net_ip(r))
            self.net_table.setCellWidget(i, 1, combo)
            self.combos.append(combo)
            
            self.update_net_ip(i)
            
    def update_net_ip(self, row):
        combo = self.net_table.cellWidget(row, 1)
        name = combo.currentData()
        ip = "Automatic"
        if name:
            for iface in self.available_interfaces:
                if iface['name'] == name: 
                    ip = iface['ip']
                    break
        self.net_table.setItem(row, 2, QTableWidgetItem(ip))
        
    def get_results(self):
        new_conf = {
            'fast_mode_enabled': self.cb_fast_mode.isChecked(),
            'capture_seconds': self.spin_capture.value(),
            'interval_seconds': self.spin_interval.value(), 
            'interval_jitter': self.spin_jitter.value(),
            'score_threshold': self.spin_threshold.value() / 100.0,
            'distribution_strategy': "Dynamic Dispatch",
            'shuffle_playback': True,
            'quarantine_reserve': 0,
            'quarantine_freq': 0,
            
            'batch_size': self.spin_batch.value(),
            'hiccup_penalty_minutes': self.spin_hiccup_penalty.value(),
            'failure_penalty': self.spin_fail_penalty.value(),
            'intermittent_penalty_minutes': self.spin_intermittent_penalty.value(), 
            'unresponsive_penalty_minutes': self.spin_unresponsive_penalty.value(), 
            'loop_penalty': self.spin_loop_penalty.value(),
            'intermittent_threshold': self.spin_intermittent.value(),
            'unresponsive_threshold': self.spin_unresponsive.value(),
            
            'suspended_penalty_minutes': self.spin_suspended_penalty.value(),
            'terminal_penalty_minutes': self.spin_terminal_penalty.value(),
            'silent_penalty_minutes': self.spin_silent_penalty.value(),
            'silent_visual_penalty_minutes': self.spin_silent_visual_penalty.value(),
            
            'extraction_strategy': {
                'player_client': self.edit_player_client.text().strip(),
                'ffmpeg_user_agent': self.edit_user_agent.text().strip(),
                'audio_normalization': self.cb_audio_norm.isChecked(),
                'frame_quality': self.spin_frame_quality.value()
            }
        }
        
        new_listeners = self.spin_listeners.value()
        
        new_loop = {
            'enabled': self.cb_loop_enable.isChecked(),
            'watch_period_hours': self.spin_loop_watch.value(),
            'quarantine_check_hours': self.spin_loop_quar.value()
        }
        
        new_map = {}
        for i in range(new_listeners):
            lid = f"L{i+1}"
            val = self.combos[i].currentData()
            if val: new_map[lid] = val
            
        return new_conf, new_listeners, new_loop, new_map