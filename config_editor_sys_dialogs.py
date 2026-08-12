# FILE: config_editor_sys_dialogs.py
# VERSION: 1.8 - "The Clean Nuke Patch"
# RESPONSIBILITY: Houses all System, Maintenance, Auditing, and Telemetry dialogs for the Config Editor.
# UPDATED: Removed massive AI-hallucinated class duplications. Restored clean file structure. Added the targetted "Nuke Detection Logs" button to the manual clean section.

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
                             QApplication, QTabWidget, QSizePolicy, QProgressBar, QDoubleSpinBox, QScrollArea, QSlider, QProgressDialog, QRadioButton, QButtonGroup)
from PyQt6.QtCore import Qt, QTimer, QObject, QEvent, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QPalette, QBrush, QColorConstants

# Import utils
from config_editor_utils import haversine_distance, ChromeMaintenance
import network_manager
import db_connector

# --- Configuration Paths ---
ROOT = Path(__file__).resolve().parent
DATABASE_PATH = ROOT / "detections.db"
BASELINE_CLIPS_DIR = ROOT / "baseline_clips"
CONFIG_FILE = ROOT / "birdnet_config.json"
LOG_ANALYZER_SETTINGS_FILE = ROOT / "log_analyzer_settings.json"
HYDRA_STATE_FILE = ROOT / "hydra_heat_state.json"

def safe_timestamp_format(ts, fmt='%Y-%m-%d %H:%M:%S'):
    if not ts: return "N/A"
    try: return datetime.fromtimestamp(float(ts)).strftime(fmt)
    except: return str(ts)

# ==============================================================================
# 1. LOG COMPRESSOR & ANALYZER THREAD
# ==============================================================================
class LogParserWorker(QThread):
    progress_update = pyqtSignal(str, int)
    finished = pyqtSignal(str, bool)

    def __init__(self, monitor_path, vision_path, app_logs_dir, output_dir, timeframe_hours, filters, comp_settings, tail_settings):
        super().__init__()
        self.monitor_path = Path(monitor_path)
        self.vision_path = Path(vision_path)
        self.app_logs_dir = Path(app_logs_dir)
        self.output_dir = Path(output_dir)
        self.timeframe_hours = timeframe_hours
        self.filters = filters
        self.comp_settings = comp_settings
        self.tail_settings = tail_settings

    def _sanitize(self, text, is_traceback=False):
        clean_text = text.strip()
        if self.comp_settings.get("mask_data", True):
            clean_text = re.sub(r"https?://[^\s\"'>]+", "[URL]", clean_text)
            clean_text = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?\b", "[IP]", clean_text)
            clean_text = re.sub(r"0x[0-9a-fA-F]+", "[HEX]", clean_text)
            clean_text = re.sub(r"\[L\d+\]", "[L-X]", clean_text)
        if not is_traceback and self.comp_settings.get("truncate", True):
            if len(clean_text) > 250: clean_text = clean_text[:247] + "..."
        clean_text = clean_text.replace('\n', ' | ')
        return clean_text

    def _extract_tail(self, filepath, out_handle):
        if not filepath.exists() or filepath.stat().st_size == 0: return
        mode = self.tail_settings.get('mode', 'lines')
        lines_val = self.tail_settings.get('lines', 500)
        pct_val = self.tail_settings.get('pct', 10)
        max_mb = self.tail_settings.get('max_mb', 5.0)
        add_headers = self.tail_settings.get('add_headers', True)
        
        max_bytes = max_mb * 1024 * 1024
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                all_lines = f.readlines()
        except Exception as e: 
            out_handle.write(f"\n[Failed to read {filepath.name}: {e}]\n")
            return
        
        total_lines = len(all_lines)
        if total_lines == 0: return
        
        if mode == 'lines': target_lines = min(lines_val, total_lines)
        else: target_lines = max(1, int(total_lines * (pct_val / 100.0)))
            
        tail_lines = all_lines[-target_lines:]
        
        final_lines = []
        current_bytes = 0
        for line in reversed(tail_lines):
            line_bytes = len(line.encode('utf-8'))
            if current_bytes + line_bytes > max_bytes: break
            final_lines.insert(0, line)
            current_bytes += line_bytes
            
        if add_headers:
            out_handle.write(f"\n{'='*70}\n")
            out_handle.write(f"--- TAIL OF: {filepath.name} ---\n")
            out_handle.write(f"--- Showing last {len(final_lines)} lines ({current_bytes/1024:.1f} KB) ---\n")
            out_handle.write(f"{'='*70}\n\n")
            
        out_handle.writelines(final_lines)
        out_handle.write("\n")

    def run(self):
        try:
            timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')
            target_files = []
            if self.monitor_path.exists(): target_files.append(self.monitor_path)
            if self.vision_path.exists(): target_files.append(self.vision_path)
            if self.app_logs_dir.exists():
                for lf in self.app_logs_dir.glob("*.log"): target_files.append(lf)

            if not target_files:
                self.finished.emit("No log files found at the specified locations.", False)
                return

            run_analyzer = self.comp_settings.get("enable_analyzer", True)
            run_tails = self.tail_settings.get("enable_tails", True)
            
            if not run_analyzer and not run_tails:
                self.finished.emit("Both Analyzer and Tail Extractor are disabled. Nothing to do.", False)
                return

            generated_files = []

            # RUN RAW TAIL EXTRACTOR
            if run_tails:
                self.progress_update.emit("Extracting Raw Log Tails...", 10)
                tail_filename = f"GBR_Raw_Log_Tails_{timestamp_str}.txt"
                tail_path = self.output_dir / tail_filename
                
                with open(tail_path, "w", encoding="utf-8") as out_tail:
                    out_tail.write("=================================================================\n")
                    out_tail.write(f"GLOBAL BIRDSONG RADIO - RAW LOG TAIL EXPORT\n")
                    out_tail.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    out_tail.write("=================================================================\n\n")
                    
                    for f_idx, tf in enumerate(target_files):
                        pct = 10 + int((f_idx / len(target_files)) * 20)
                        self.progress_update.emit(f"Extracting tail: {tf.name}...", pct)
                        self._extract_tail(tf, out_tail)
                        
                generated_files.append(tail_path.name)

            # RUN LOG ANALYZER & COMPRESSOR
            if run_analyzer:
                self.progress_update.emit("Running Log Analyzer...", 35)
                cutoff_time = datetime.now() - timedelta(hours=self.timeframe_hours) if self.timeframe_hours > 0 else datetime.min
                categories = defaultdict(Counter)
                tracebacks = set()
                normal_sample = []
                timestamp_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
                total_size = sum(f.stat().st_size for f in target_files)
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
                                pct = 35 + int((processed_bytes / total_size) * 55)
                                self.progress_update.emit(f"Parsing {filepath.name}...", pct)

                            match = timestamp_pattern.match(line)
                            if match:
                                self._categorize_block(current_block, block_timestamp, cutoff_time, categories, tracebacks, normal_sample, fallback_ts=file_mtime)
                                try: block_timestamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                                except: block_timestamp = None
                                clean_line = re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} -.*? - ", "", line)
                                current_block = clean_line
                            else:
                                current_block += line
                    self._categorize_block(current_block, block_timestamp, cutoff_time, categories, tracebacks, normal_sample, fallback_ts=file_mtime)

                for tf in target_files: process_file(tf)

                self.progress_update.emit("Generating summary file...", 95)
                analyzer_filename = f"GBR_Log_Summary_{timestamp_str}.txt"
                analyzer_path = self.output_dir / analyzer_filename
                top_n = self.comp_settings.get("top_n", 15)
                
                with open(analyzer_path, "w", encoding="utf-8") as out:
                    out.write("=================================================================\n")
                    out.write(f"GLOBAL BIRDSONG RADIO - COMPRESSED LOG DIAGNOSTIC\n")
                    out.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    time_str = f"Last {self.timeframe_hours} Hours" if self.timeframe_hours > 0 else "All Time"
                    out.write(f"Timeframe Analyzed: {time_str}\n")
                    out.write("=================================================================\n\n")
                    
                    try:
                        if CONFIG_FILE.exists():
                            cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                            out.write("--- SYSTEM CONFIGURATION SNAPSHOT ---\n")
                            
                            ext = cfg.get("extraction_strategy", {})
                            out.write(f"Fast Mode (Auto-Healer): {cfg.get('fast_mode_enabled', True)}\n")
                            out.write(f"API Client Spoofing:     {ext.get('player_client', 'web')}\n")
                            out.write(f"FFmpeg User-Agent:       {ext.get('ffmpeg_user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')}\n")
                            out.write(f"Audio Normalization:     {ext.get('audio_normalization', True)}\n")
                            out.write(f"Video Frame Quality:     {ext.get('frame_quality', 2)}\n")
                            
                            has_cookies = bool(cfg.get("youtube_cookies_file", "").strip())
                            out.write(f"Cookies.txt Configured:  {has_cookies}\n")
                            ba = cfg.get("browser_automation", {})
                            out.write(f"Browser Auto (Selenium): {ba.get('enabled', False)}\n")
                            
                            pid = cfg.get("hydra_pid_settings", {})
                            out.write(f"Hydra Throttling:        {pid.get('throttling_enabled', True)}\n")
                            
                            out.write("\n--- NETWORK MAP (HYDRA BINDINGS) ---\n")
                            n_map = cfg.get("network_map", {})
                            if not n_map:
                                out.write("No specific bindings configured. All Listeners on Default/OS.\n")
                            else:
                                for lid in sorted(n_map.keys()):
                                    out.write(f"{lid}: {n_map[lid]}\n")
                            
                            out.write("\n=================================================================\n\n")
                    except Exception as e:
                        out.write(f"--- SYSTEM CONFIGURATION SNAPSHOT ---\nFailed to read config: {e}\n\n=================================================================\n\n")

                    if self.filters.get("tracebacks", True):
                        out.write("--- CRITICAL TRACEBACKS, CRASHES & EMERGENCY BRAKES ---\n")
                        if tracebacks:
                            for tb in tracebacks: out.write(tb.strip() + "\n\n")
                        else: out.write("No critical crashes or tracebacks found.\n\n")
                            
                    if self.filters.get("selenium", True): self._write_category(out, "SELENIUM / WEBDRIVER CRASHES", categories["selenium"], top_n)
                    if self.filters.get("ffmpeg", True): self._write_category(out, "FFMPEG / AUDIO CAPTURE ERRORS", categories["ffmpeg"], top_n)
                    if self.filters.get("network", True): self._write_category(out, "NETWORK / PROXY / CLOUD SYNC ERRORS", categories["network"], top_n)
                    if self.filters.get("streams", True): self._write_category(out, "STREAM STATUS & RESOLVER EVENTS", categories["streams"], top_n)
                    if self.filters.get("normal", True):
                        out.write("--- NORMAL SYSTEM ACTIVITY (PROOF OF LIFE SAMPLE) ---\n")
                        if normal_sample:
                            out.write(f"Showing {len(normal_sample)} sample events out of many:\n")
                            for ns in normal_sample: out.write(ns.strip() + "\n")
                            out.write("\n")
                        else: out.write("No normal activity (Alerts/Bio-Hits) detected in this timeframe.\n\n")
                            
                generated_files.append(analyzer_path.name)

            self.progress_update.emit("Done!", 100)
            if len(generated_files) == 2: final_msg = f"Generated 2 files in:\n{self.output_dir}\n\n- {generated_files[0]}\n- {generated_files[1]}"
            else: final_msg = f"Generated file in:\n{self.output_dir}\n\n- {generated_files[0]}"
            self.finished.emit(final_msg, True)

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
        for msg, count in common_items: file_handle.write(f"[x{count}] {msg}\n")
        omitted = len(counter_obj) - top_n
        if omitted > 0: file_handle.write(f"... and {omitted} more unique error types hidden (Below Top {top_n}).\n")
        file_handle.write("\n")

    def _categorize_block(self, block, timestamp, cutoff, categories, tracebacks, normal_sample, fallback_ts=None):
        if not block.strip(): return
        l_block = block.lower()
        is_tb = (
            "traceback (most recent call last)" in l_block or "critical startup error" in l_block or
            "process finished/died" in l_block or "emergency brake" in l_block or
            "syntaxerror:" in l_block or "nameerror:" in l_block or "typeerror:" in l_block or
            "valueerror:" in l_block or "exception:" in l_block
        )
        if not timestamp and is_tb and fallback_ts: timestamp = fallback_ts
        if not timestamp: return
        if timestamp < cutoff: return
        
        clean_block = self._sanitize(block, is_traceback=is_tb)
        if is_tb: tracebacks.add(f"[{timestamp.strftime('%Y-%m-%d %H:%M:%S')}] {clean_block}")
        elif "session not created" in l_block or "chrome not reachable" in l_block or "webdriver" in l_block or "undetected_chromedriver" in l_block: categories["selenium"][clean_block] += 1
        elif "ffmpeg error:" in l_block or "ffmpeg failed" in l_block: categories["ffmpeg"][clean_block] += 1
        elif "timeouterror" in l_block or "connection aborted" in l_block or "cloud sync" in l_block or "telegram error" in l_block or "https pivot" in l_block: categories["network"][clean_block] += 1
        elif "completely failed" in l_block or "skipping:" in l_block or "auto-resolver failed" in l_block or "link expiration" in l_block: categories["streams"][clean_block] += 1
        elif "alert:" in l_block or "bio-hit:" in l_block or "gemini public alert" in l_block:
            if len(normal_sample) < 30: normal_sample.append(f"[{timestamp.strftime('%H:%M:%S')}] {clean_block}")

# ==============================================================================
# 2. SCROLL STEAL FILTER
# ==============================================================================
class ScrollStealFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel:
            if not obj.hasFocus():
                event.ignore()
                return True
        return super().eventFilter(obj, event)

# ==============================================================================
# 3. NETWORK TELEMETRY DIALOG
# ==============================================================================
class NetworkTelemetryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Network Telemetry, Quotas & Hydra PID Tuning")
        self.setMinimumSize(750, 600)
        self.layout = QVBoxLayout(self)
        
        self.scroll_filter = ScrollStealFilter(self)
        self.interfaces = network_manager.get_active_interfaces()
        
        self.pid_settings = {"ema_alpha": 0.3, "soft_lockout_pct": 90, "global_brake_pct": 95, "throttling_enabled": True}
        if CONFIG_FILE.exists():
            try:
                cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                self.pid_settings.update(cfg.get("hydra_pid_settings", {}))
            except: pass

        self.tabs = QTabWidget()
        
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
            group.setStyleSheet("QGroupBox { border: 1px solid #555; border-radius: 4px; margin-top: 18px; padding-top: 15px; font-weight: bold; } QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 10px; padding: 0 5px; color: #00E5FF; }")
            g_layout = QVBoxLayout()
            
            progress = QProgressBar()
            progress.setFixedHeight(25)
            progress.setValue(0)
            lbl_24h = QLabel("<b>Last 24h Traffic:</b> Calculating...")
            split_lbl = QLabel("<b><span style='color: #00E5FF;'>Current Speed (Last 60m):</span></b> Calculating...")
            heat_lbl = QLabel("<b>Network Heat Status:</b> Calculating...")
            
            self.dynamic_widgets[name] = {'progress': progress, 'lbl_24h': lbl_24h, 'split_lbl': split_lbl, 'heat_lbl': heat_lbl}
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
            
            plan_size_gb, allowed_pct, reset_day, max_gb_hr = 0.0, 100, 1, 0.0
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
            except: pass
                
            spin_plan.setValue(plan_size_gb)
            spin_pct.setValue(allowed_pct)
            spin_day.setValue(reset_day)
            spin_speed.setValue(max_gb_hr)
            
            def create_update_func(l, p, a):
                def update(*args):
                    eff = p.value() * (a.value() / 100.0)
                    if eff > 0: l.setText(f"<b>Effective Monthly Limit:</b> <span style='color: #00E676;'>{eff:.1f} GB</span>")
                    else: l.setText("<b>Effective Monthly Limit:</b> <span style='color: #888;'>Unlimited</span>")
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

        self.tab_pid = QWidget()
        self.tab_pid_layout = QVBoxLayout(self.tab_pid)
        pid_info = QLabel("<b>Hydra PID Tuning & Algorithm Overrides</b><br>Fine-tune how the dynamic load balancer reacts to network heat, or manually reset the algorithms for debugging purposes.")
        pid_info.setWordWrap(True)
        self.tab_pid_layout.addWidget(pid_info)
        
        tune_group = QGroupBox("Heating Control & Smoothing Parameters")
        tune_form = QFormLayout(tune_group)
        self.spin_ema_alpha = QDoubleSpinBox()
        self.spin_ema_alpha.setRange(0.01, 1.00)
        self.spin_ema_alpha.setSingleStep(0.05)
        self.spin_ema_alpha.setValue(self.pid_settings.get("ema_alpha", 0.3))
        
        self.spin_soft_lockout = QSpinBox()
        self.spin_soft_lockout.setRange(50, 100)
        self.spin_soft_lockout.setSuffix("%")
        self.spin_soft_lockout.setValue(self.pid_settings.get("soft_lockout_pct", 90))
        
        self.spin_global_brake = QSpinBox()
        self.spin_global_brake.setRange(50, 100)
        self.spin_global_brake.setSuffix("%")
        self.spin_global_brake.setValue(self.pid_settings.get("global_brake_pct", 95))
        
        tune_form.addRow("EMA Alpha (Smoothing Factor):", self.spin_ema_alpha)
        tune_form.addRow("SIM Soft-Lockout Threshold:", self.spin_soft_lockout)
        tune_form.addRow("Global Emergency Brake Cap:", self.spin_global_brake)
        self.tab_pid_layout.addWidget(tune_group)
        
        reset_group = QGroupBox("Manual Overrides & Debug Resets")
        reset_layout = QVBoxLayout(reset_group)
        
        btn_reset_ema = QPushButton("🧠 Reset PID/EMA Memory (Algorithm Debug)")
        btn_reset_ema.setStyleSheet("background-color: #5C6BC0; color: white; font-weight: bold;")
        btn_reset_ema.clicked.connect(self.reset_ema_state)
        
        btn_reset_1h = QPushButton("⏱️ Reset 1-Hour Speed Window (Traffic Debug)")
        btn_reset_1h.setStyleSheet("background-color: #00897B; color: white; font-weight: bold;")
        btn_reset_1h.clicked.connect(self.reset_1h_window)
        
        btn_nuke_all = QPushButton("☢️ Nuke ALL Data Usage (SIM Swap / Billing Reset)")
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

    def reset_ema_state(self):
        if QMessageBox.question(self, "Reset PID Memory", "Wipe the algorithm's memory (EMA)?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            try:
                if HYDRA_STATE_FILE.exists(): HYDRA_STATE_FILE.unlink()
                QMessageBox.information(self, "Success", "PID memory wiped.")
                self.refresh_telemetry_data()
            except Exception as e: QMessageBox.critical(self, "Error", f"Failed to reset EMA: {e}")

    def reset_1h_window(self):
        if QMessageBox.question(self, "Reset 1H Speed", "Reset the 1-Hour Speedometer?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            try:
                cutoff = time.time() - 3600
                with db_connector.get_db_connection(force_local=True) as con:
                    con.execute("DELETE FROM network_hardware_logs WHERE timestamp >= ?", (cutoff,))
                    con.execute("DELETE FROM network_app_logs WHERE timestamp >= ?", (cutoff,))
                if HYDRA_STATE_FILE.exists(): HYDRA_STATE_FILE.unlink()
                QMessageBox.information(self, "Success", "1-Hour speed window reset.")
                self.refresh_telemetry_data()
            except Exception as e: QMessageBox.critical(self, "Error", f"Failed to reset 1H window: {e}")

    def nuke_all_data(self):
        if QMessageBox.critical(self, "Nuke All Data", "DANGER: Delete all recorded hardware and application data logs?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            try:
                with db_connector.get_db_connection(force_local=True) as con:
                    con.execute("DELETE FROM network_hardware_logs")
                    con.execute("DELETE FROM network_app_logs")
                if HYDRA_STATE_FILE.exists(): HYDRA_STATE_FILE.unlink()
                QMessageBox.information(self, "Nuked", "All data usage records have been destroyed.")
                self.refresh_telemetry_data()
            except Exception as e: QMessageBox.critical(self, "Error", f"Failed to nuke data: {e}")

    def refresh_telemetry_data(self):
        cutoff_24h = time.time() - 86400
        cutoff_1h = time.time() - 3600
        throttle_enabled = True
        try:
            if CONFIG_FILE.exists():
                cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                throttle_enabled = cfg.get("hydra_pid_settings", {}).get("throttling_enabled", True)
        except: pass
        try:
            heat_state = {}
            if HYDRA_STATE_FILE.exists():
                try: heat_state = json.loads(HYDRA_STATE_FILE.read_text(encoding='utf-8'))
                except: pass

            g_data = heat_state.get("GLOBAL")
            if not throttle_enabled: g_ind = "<span style='color: #FF3D00; font-weight: bold;'>[⚠️ THROTTLING DISABLED]</span>"
            elif g_data:
                global_heat = g_data['heat']
                g_arrow = g_data['arrow']
                if "🚨" in g_arrow: g_ind = f"<span style='color: #FF1744; font-weight: bold;'>[{g_arrow}]</span>"
                elif global_heat >= 1.0: g_ind = "<span style='color: #FF3D00; font-weight: bold;'>[🛑 GLOBAL SPEED BLOCKED (100% Heat)]</span>"
                elif global_heat >= 0.95: g_ind = f"<span style='color: #FF9800; font-weight: bold;'>[🟠 GLOBAL THROTTLED MAX] ({global_heat*100:.1f}%)</span>"
                elif "🔺" in g_arrow: g_ind = f"<span style='color: #FF5252; font-weight: bold;'>[🔺 Heating Up] ({global_heat*100:.1f}%)</span>"
                elif "🔽" in g_arrow: g_ind = f"<span style='color: #448AFF; font-weight: bold;'>[🔽 Cooling Down] ({global_heat*100:.1f}%)</span>"
                else: g_ind = f"<span style='color: #4CAF50; font-weight: bold;'>[➖ Stable] ({global_heat*100:.1f}%)</span>"
            else: g_ind = "<span style='color: #888;'>Waiting for telemetry...</span>"
            self.lbl_global_heat.setText(f"<b>Main Pipeline Status:</b> {g_ind}")
            
            with db_connector.get_db_connection(force_local=True) as con:
                cur = con.cursor()
                for iface in self.interfaces:
                    name = iface['name']
                    if name not in self.dynamic_widgets: continue
                    widgets = self.dynamic_widgets[name]
                    used_bytes, limit_bytes, is_over = network_manager.get_interface_quota_status(name)
                    is_monthly_dead = (limit_bytes > 0 and used_bytes >= limit_bytes)
                    i_data = heat_state.get(name)
                    if i_data:
                        heat = i_data['heat']
                        i_arrow = i_data['arrow']
                        if "❌" in i_arrow: i_ind = "<span style='color: #9E9E9E; font-weight: bold;'>[❌ OFFLINE (Disconnected)]</span>"
                        elif "🔥" in i_arrow: i_ind = "<span style='color: #FF5722; font-weight: bold;'>[🔥 IP BANNED (403)]</span>"
                        elif is_monthly_dead: i_ind = "<span style='color: #E91E63; font-weight: bold;'>[⛔ DATA DEPLETED (Monthly Cap)]</span>"
                        elif heat >= 1.0: i_ind = "<span style='color: #FF3D00; font-weight: bold;'>[🛑 LOCAL SPEED BLOCKED (100% Heat)]</span>"
                        elif heat >= 0.95: i_ind = f"<span style='color: #FF9800; font-weight: bold;'>[🟠 THROTTLED MAX] ({heat*100:.1f}%)</span>"
                        elif "🔺" in i_arrow: i_ind = f"<span style='color: #FF5252; font-weight: bold;'>[🔺 Heating Up] ({heat*100:.1f}%)</span>"
                        elif "🔽" in i_arrow: i_ind = f"<span style='color: #448AFF; font-weight: bold;'>[🔽 Cooling Down] ({heat*100:.1f}%)</span>"
                        else: i_ind = f"<span style='color: #4CAF50; font-weight: bold;'>[➖ Stable] ({heat*100:.1f}%)</span>"
                        widgets['heat_lbl'].setText(f"<b>Network Heat Status:</b> {i_ind}")
                    else: widgets['heat_lbl'].setText("<b>Network Heat Status:</b> <span style='color: #888;'>Waiting...</span>")
                    
                    cur.execute("SELECT SUM(bytes_sent + bytes_recv) FROM network_hardware_logs WHERE interface_name = ? AND timestamp >= ?", (name, cutoff_24h))
                    row_24h = cur.fetchone()
                    used_24h_gb = (row_24h[0] if row_24h and row_24h[0] else 0) / (1024**3)
                    
                    cur.execute("SELECT SUM(bytes_sent + bytes_recv) FROM network_hardware_logs WHERE interface_name = ? AND timestamp >= ?", (name, cutoff_1h))
                    row_1h = cur.fetchone()
                    used_1h_gb = (row_1h[0] if row_1h and row_1h[0] else 0) / (1024**3)
                    
                    cur.execute("SELECT engine_type, SUM(bytes_used) FROM network_app_logs WHERE interface_name = ? AND timestamp >= ? GROUP BY engine_type", (name, cutoff_1h))
                    audio_1h_bytes = 0
                    vision_1h_bytes = 0
                    for r in cur.fetchall():
                        if r[0] == 'audio': audio_1h_bytes = r[1]
                        elif r[0] == 'vision': vision_1h_bytes = r[1]
                        
                    total_app_bytes = audio_1h_bytes + vision_1h_bytes
                    audio_pct = int((audio_1h_bytes / total_app_bytes) * 100) if total_app_bytes > 0 else 0
                    vision_pct = int((vision_1h_bytes / total_app_bytes) * 100) if total_app_bytes > 0 else 0
                    split_str = f"(🐦 {audio_pct}% | 📷 {vision_pct}%)" if total_app_bytes > 0 else "(No Activity)"
                    
                    if limit_bytes > 0:
                        pct = int((used_bytes / limit_bytes) * 100)
                        widgets['progress'].setValue(min(pct, 100))
                        if is_monthly_dead: widgets['progress'].setStyleSheet("QProgressBar::chunk { background-color: #E91E63; }")
                        elif pct >= 80: widgets['progress'].setStyleSheet("QProgressBar::chunk { background-color: #FBC02D; }")
                        else: widgets['progress'].setStyleSheet("QProgressBar::chunk { background-color: #388E3C; }")
                        widgets['progress'].setFormat(f"{used_bytes / (1024**3):.2f} GB / {limit_bytes / (1024**3):.2f} GB Used This Cycle ({pct}%)")
                    else:
                        widgets['progress'].setValue(0)
                        widgets['progress'].setFormat(f"{used_bytes / (1024**3):.2f} GB Used This Cycle (No Monthly Limit)")
                        
                    widgets['lbl_24h'].setText(f"<b>Last 24h Traffic:</b> {used_24h_gb:.2f} GB")
                    speed_color = "#FF3D00" if (is_over and limit_bytes <= 0) else "#00E5FF" 
                    widgets['split_lbl'].setText(f"<b><span style='color: {speed_color};'>Current Speed (Last 60m):</span></b> {used_1h_gb:.3f} GB/hr {split_str}")
                    
        except Exception as e: 
            logging.error(f"Live telemetry refresh error: {e}")

    def save_quotas(self):
        try:
            with db_connector.get_db_connection(force_local=True) as con:
                for name, inputs in self.quota_inputs.items():
                    plan_size = inputs['plan'].value()
                    allowed_pct = inputs['pct'].value()
                    reset_day = inputs['day'].value()
                    max_speed = inputs['speed'].value()
                    limit_gb = plan_size * (allowed_pct / 100.0)
                    con.execute("REPLACE INTO network_quotas (interface_name, limit_gb, reset_day, plan_size_gb, allowed_percent, max_gb_per_hour) VALUES (?, ?, ?, ?, ?, ?)", (name, limit_gb, reset_day, plan_size, allowed_pct, max_speed))
            if CONFIG_FILE.exists():
                try:
                    cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                    throttle_flag = cfg.get("hydra_pid_settings", {}).get("throttling_enabled", True)
                    cfg["hydra_pid_settings"] = {"ema_alpha": self.spin_ema_alpha.value(), "soft_lockout_pct": self.spin_soft_lockout.value(), "global_brake_pct": self.spin_global_brake.value(), "throttling_enabled": throttle_flag}
                    tmp_file = CONFIG_FILE.with_suffix('.tmp')
                    tmp_file.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
                    os.replace(tmp_file, CONFIG_FILE)
                except Exception as e: logging.error(f"Failed to save PID settings: {e}")
            QMessageBox.information(self, "Success", "Network Quotas and PID parameters saved successfully.")
            self.accept()
        except Exception as e: QMessageBox.critical(self, "Error", f"Failed to save: {e}")

# ==============================================================================
# 4. STREAM AUDIT DIALOG
# ==============================================================================
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

        top_layout = QHBoxLayout()
        self.btn_run_diag = QPushButton("Run Data Diagnostics")
        self.btn_run_diag.clicked.connect(self.run_diagnostics)
        
        self.btn_health = QPushButton("Mass Network Health Check")
        self.btn_health.clicked.connect(self.run_mass_health_check)
        self.btn_health.setStyleSheet("font-weight: bold;")
        
        self.btn_reconstruct = QPushButton("Reconstruct Timeline (DB)")
        self.btn_reconstruct.clicked.connect(self.reconstruct_timeline)
        
        self.btn_reset_counts = QPushButton("Reset Scheduler Fairness (Total Amnesty)")
        self.btn_reset_counts.clicked.connect(self.reset_scheduler_counts)
        self.btn_reset_counts.setStyleSheet("background-color: #AB47BC; color: white; font-weight: bold;")
        
        top_layout.addWidget(self.btn_run_diag)
        top_layout.addWidget(self.btn_health)
        top_layout.addWidget(self.btn_reconstruct)
        top_layout.addWidget(self.btn_reset_counts) 
        self.layout.addLayout(top_layout)

        report_group = QGroupBox("Automated Telegram Reporting")
        report_main_layout = QVBoxLayout()
        report_top_layout = QHBoxLayout()
        self.cb_report_enable = QCheckBox("Broadcast Periodic Health Status to Telegram")
        self.cb_report_enable.setChecked(self.report_config.get('enabled', True))
        report_top_layout.addWidget(self.cb_report_enable)
        report_top_layout.addStretch()
        report_top_layout.addWidget(QLabel("Frequency:"))
        self.combo_report_freq = QComboBox()
        self.freq_map = { "Every 30 Minutes": 0.5, "Every 1 Hour": 1.0, "Every 2 Hours": 2.0, "Every 3 Hours": 3.0, "Every 6 Hours": 6.0, "Every 12 Hours": 12.0, "Every 24 Hours": 24.0 }
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
        self.cb_alert_hourly_speed = QCheckBox("Alert on SIM Hourly Speed Limit Drop/Recovery")
        self.cb_alert_hourly_speed.setChecked(self.report_config.get('alert_hourly_speed', True))
        self.cb_alert_monthly_quota = QCheckBox("Alert on SIM Monthly Quota Drop/Recovery")
        self.cb_alert_monthly_quota.setChecked(self.report_config.get('alert_monthly_quota', True))

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

        telemetry_group = QGroupBox("📡 Hardware Network Telemetry (SIM Usage)")
        telemetry_group.setStyleSheet("QGroupBox { border: 1px solid #00897B; border-radius: 4px; margin-top: 18px; padding-top: 15px; } QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 10px; padding: 0 5px; color: #00E5FF; font-weight: bold; }")
        telemetry_layout = QVBoxLayout(telemetry_group)
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
                if "hydra_pid_settings" not in cfg: cfg["hydra_pid_settings"] = {}
                cfg["hydra_pid_settings"]["throttling_enabled"] = checked
                tmp_file = CONFIG_FILE.with_suffix('.tmp')
                tmp_file.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
                os.replace(tmp_file, CONFIG_FILE)
                self.load_telemetry_summary() 
        except Exception as e: logging.error(f"Failed to toggle throttling: {e}")

    def load_telemetry_summary(self):
        try:
            interfaces = network_manager.get_active_interfaces()
            summary_parts =[]
            cutoff_24h = time.time() - 86400
            cutoff_1h = time.time() - 3600
            heat_state = {}
            if HYDRA_STATE_FILE.exists():
                try: heat_state = json.loads(HYDRA_STATE_FILE.read_text(encoding='utf-8'))
                except: pass
                
            throttle_enabled = True
            try:
                if CONFIG_FILE.exists():
                    cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                    throttle_enabled = cfg.get("hydra_pid_settings", {}).get("throttling_enabled", True)
            except: pass
                
            g_data = heat_state.get("GLOBAL")
            if not throttle_enabled: g_ind = "<span style='color: #FF3D00; font-weight: bold;'>[⚠️ THROTTLING DISABLED]</span>"
            elif g_data:
                global_heat = g_data['heat']
                g_arrow = g_data['arrow']
                if "🚨" in g_arrow: g_ind = f"<span style='color: #FF1744; font-weight: bold;'>[{g_arrow}]</span>"
                elif global_heat >= 1.0: g_ind = "<span style='color: #FF3D00; font-weight: bold;'>[🛑 GLOBAL SPEED BLOCKED (100% Heat)]</span>"
                elif global_heat >= 0.95: g_ind = f"<span style='color: #FF9800; font-weight: bold;'>[🟠 GLOBAL THROTTLED MAX] ({global_heat*100:.1f}%)</span>"
                elif "🔺" in g_arrow: g_ind = f"<span style='color: #FF5252; font-weight: bold;'>[🔺 Heating Up] ({global_heat*100:.1f}%)</span>"
                elif "🔽" in g_arrow: g_ind = f"<span style='color: #448AFF; font-weight: bold;'>[🔽 Cooling Down] ({global_heat*100:.1f}%)</span>"
                else: g_ind = f"<span style='color: #4CAF50; font-weight: bold;'>[➖ Stable] ({global_heat*100:.1f}%)</span>"
            else: g_ind = "<span style='color: #888;'>Waiting for telemetry...</span>"
            self.lbl_telemetry_summary.setText(f"<b>Main Pipeline Status:</b> {g_ind}")
            
            with db_connector.get_db_connection(force_local=True) as con:
                cur = con.cursor()
                for iface in interfaces:
                    name = iface['name']
                    used_bytes, limit_bytes, is_over = network_manager.get_interface_quota_status(name)
                    is_monthly_dead = (limit_bytes > 0 and used_bytes >= limit_bytes)
                    i_data = heat_state.get(name)
                    if i_data:
                        heat = i_data['heat']
                        i_arrow = i_data['arrow']
                        if "❌" in i_arrow: i_ind = "<span style='color: #9E9E9E; font-weight: bold;'>[❌ OFFLINE (Disconnected)]</span>"
                        elif "🔥" in i_arrow: i_ind = "<span style='color: #FF5722; font-weight: bold;'>[🔥 IP BANNED (403)]</span>"
                        elif is_monthly_dead: i_ind = "<span style='color: #E91E63; font-weight: bold;'>[⛔ DATA DEPLETED (Monthly Cap)]</span>"
                        elif heat >= 1.0: i_ind = "<span style='color: #FF3D00; font-weight: bold;'>[🛑 LOCAL SPEED BLOCKED (100% Heat)]</span>"
                        elif heat >= 0.95: i_ind = f"<span style='color: #FF9800; font-weight: bold;'>[🟠 THROTTLED MAX] ({heat*100:.1f}%)</span>"
                        elif "🔺" in i_arrow: i_ind = f"<span style='color: #FF5252; font-weight: bold;'>[🔺 Heating Up] ({heat*100:.1f}%)</span>"
                        elif "🔽" in i_arrow: i_ind = f"<span style='color: #448AFF; font-weight: bold;'>[🔽 Cooling Down] ({heat*100:.1f}%)</span>"
                        else: i_ind = f"<span style='color: #4CAF50; font-weight: bold;'>[➖ Stable] ({heat*100:.1f}%)</span>"
                    else: i_ind = "[Waiting...]"
                    
                    cur.execute("SELECT SUM(bytes_sent + bytes_recv) FROM network_hardware_logs WHERE interface_name = ? AND timestamp >= ?", (name, cutoff_24h))
                    row_24h = cur.fetchone()
                    used_24h_gb = (row_24h[0] if row_24h and row_24h[0] else 0) / (1024**3)
                    
                    cur.execute("SELECT SUM(bytes_sent + bytes_recv) FROM network_hardware_logs WHERE interface_name = ? AND timestamp >= ?", (name, cutoff_1h))
                    row_1h = cur.fetchone()
                    used_1h_gb = (row_1h[0] if row_1h and row_1h[0] else 0) / (1024**3)
                    
                    cur.execute("SELECT engine_type, SUM(bytes_used) FROM network_app_logs WHERE interface_name = ? AND timestamp >= ? GROUP BY engine_type", (name, cutoff_1h))
                    audio_1h_bytes = 0
                    vision_1h_bytes = 0
                    for r in cur.fetchall():
                        if r[0] == 'audio': audio_1h_bytes = r[1]
                        elif r[0] == 'vision': vision_1h_bytes = r[1]
                        
                    total_app_bytes = audio_1h_bytes + vision_1h_bytes
                    audio_pct = int((audio_1h_bytes / total_app_bytes) * 100) if total_app_bytes > 0 else 0
                    vision_pct = int((vision_1h_bytes / total_app_bytes) * 100) if total_app_bytes > 0 else 0
                    split_str = f"(🐦 {audio_pct}% | 📷 {vision_pct}%)" if total_app_bytes > 0 else "(No Activity)"
                    
                    speed_color = "#FF3D00" if (is_over and limit_bytes <= 0) else "#00E5FF" 
                    summary_parts.append(f"• <b>{name}</b> {i_ind}: {used_24h_gb:.2f} GB (24h) | <span style='color: {speed_color};'><b>SPEED:</b> {used_1h_gb:.3f} GB/hr {split_str}</span>")
                    
            if not summary_parts: self.lbl_telemetry_summary.setText("No active interfaces or telemetry data found.")
            else: self.lbl_telemetry_summary.setText("<br>".join(summary_parts))
        except Exception as e: self.lbl_telemetry_summary.setText(f"Error loading telemetry: {e}")

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
        return (rep_conf, self.cb_send_audio_all.isChecked(), self.cb_send_audio_dsp.isChecked(), self.cb_send_audio_multi.isChecked(), self.cb_send_audio_birdnet.isChecked())

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
            if len(nearby) > 1: clusters.append(nearby)
        
        if clusters:
            for c in clusters: 
                self.log(f"⚠ Cluster found: <br>&nbsp;&nbsp;{', '.join(c)}", "orange")
                issues_found += 1
        else: self.log("✔ No proximity conflicts found.", "green")

        self.log("<br><b>2. Duplicate URL Check:</b>")
        urls =[s.get('page_url', '').strip() for s in self.streams]
        cnt = Counter(urls)
        dupes =[url for url, count in cnt.items() if count > 1 and url]
        if dupes:
            for d in dupes:
                names =[s['name'] for s in self.streams if s.get('page_url', '').strip() == d]
                self.log(f"⚠ Duplicate URL: <b>{d}</b><br>&nbsp;&nbsp;Used by: {', '.join(names)}", "red")
                issues_found += 1
        else: self.log("✔ No duplicate URLs found.", "green")

        self.log("<br><b>3. Missing Coordinates:</b>")
        zeros =[s['name'] for s in self.streams if s.get('lat', 0) == 0 and s.get('lon', 0) == 0]
        if zeros: 
            self.log(f"⚠ Missing Lat/Lon: {', '.join(zeros)}", "orange")
            issues_found += len(zeros)
        else: self.log("✔ All streams have coordinates.", "green")

        if issues_found == 0: self.log("<br><b>--- DATA HEALTHY ---</b>", "green")
        else: self.log(f"<br><b>--- {issues_found} DATA ISSUES FOUND ---</b>", "red")

    def run_mass_health_check(self):
        self.log_area.clear()
        self.log("<b>--- STARTING NETWORK HEALTH CHECK ---</b>", "blue")
        self.log("<i>Pinging all streams (Timeout: 3s)... This may take a minute.</i><br>")
        import requests
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
                if 200 <= code < 400: self.log(f"&nbsp;&nbsp;✔ ALIVE ({code})", "green")
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
        if dead_count == 0: self.log("✔ All streams appear to be online.", "green")
        else: self.log(f"⚠ Found {dead_count} potentially dead streams.", "red")

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
        reply = QMessageBox.question(self, "Confirm Total Amnesty", "This will perform a TOTAL RESET of the Scheduler queue:\n\n1. Reset all 'Check Counts' to 0.\n2. CLEAR ALL PENALTIES (Set wait time to 0).\n3. Reset all status notes.\n\nThe system will immediately try to check ALL enabled streams as if it was the first time running.\n\nProceed?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.log("<br><b>--- EXECUTING TOTAL AMNESTY ---</b>", "blue")
            try:
                with db_connector.get_db_connection(force_local=True) as con: 
                    con.execute("UPDATE stream_queue SET check_count = 0, next_eligible_ts = 0, status_note = NULL")
                self.log("✔ SUCCESS: All counts and penalties cleared.", "green")
                self.log("The Scheduler will now re-evaluate all streams immediately.", "green")
                QMessageBox.information(self, "Success", "Total Amnesty Applied.\nThe Scheduler has a clean slate.")
            except Exception as e: 
                self.log(f"❌ ERROR: {e}", "red")
                QMessageBox.critical(self, "Error", f"Failed to reset: {e}")

# ==============================================================================
# 5. HOUSEKEEPING MANAGER DIALOG
# ==============================================================================
class HousekeepingManagerDialog(QDialog):
    def __init__(self, debug_enabled, hk_config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Log & Storage Housekeeping Manager")
        self.setMinimumSize(850, 750)
        self.layout = QVBoxLayout(self)
        
        self.last_server_maintenance_ts = hk_config.get('last_server_maintenance_ts', 0)
        self.server_interval_days = hk_config.get('server_maintenance_interval_days', 30)
        self.last_local_db_maintenance_ts = hk_config.get('last_local_db_maintenance_ts', 0)
        self.local_db_interval_days = hk_config.get('local_db_interval_days', 30)
        
        self.tabs = QTabWidget()
        
        # --- TAB 1: LOG ANALYZER & COMPRESSOR ---
        analyzer_widget = QWidget()
        analyzer_layout = QVBoxLayout(analyzer_widget)
        
        group_files = QGroupBox("Target Files")
        form_files = QFormLayout(group_files)
        self.edit_monitor = QLineEdit()
        btn_monitor = QPushButton("Browse")
        btn_monitor.clicked.connect(lambda: self.browse_file(self.edit_monitor, "Select Monitor Log", "Text Files (*.txt)"))
        lay_m = QHBoxLayout()
        lay_m.addWidget(self.edit_monitor)
        lay_m.addWidget(btn_monitor)
        form_files.addRow("monitor_debug.txt:", lay_m)
        
        self.edit_vision = QLineEdit()
        btn_vision = QPushButton("Browse")
        btn_vision.clicked.connect(lambda: self.browse_file(self.edit_vision, "Select Vision Log", "Text Files (*.txt)"))
        lay_v = QHBoxLayout()
        lay_v.addWidget(self.edit_vision)
        lay_v.addWidget(btn_vision)
        form_files.addRow("vision_debug.txt:", lay_v)
        
        self.edit_app_logs = QLineEdit()
        btn_app_logs = QPushButton("Browse")
        btn_app_logs.clicked.connect(lambda: self.browse_dir(self.edit_app_logs, "Select App Logs Folder"))
        lay_a = QHBoxLayout()
        lay_a.addWidget(self.edit_app_logs)
        lay_a.addWidget(btn_app_logs)
        form_files.addRow("Worker Logs Folder (app_logs):", lay_a)
        
        self.edit_out = QLineEdit()
        btn_out = QPushButton("Browse")
        btn_out.clicked.connect(lambda: self.browse_dir(self.edit_out, "Select Output Directory"))
        lay_o = QHBoxLayout()
        lay_o.addWidget(self.edit_out)
        lay_o.addWidget(btn_out)
        form_files.addRow("Save Summary To:", lay_o)
        analyzer_layout.addWidget(group_files)
        
        group_comp = QGroupBox("Smart Text Compression")
        form_comp = QFormLayout(group_comp)
        comp_info = QLabel("Aggressively strips variable data (URLs, IPs, Hex codes) to force the parser to group identical errors into a single, highly compressed line.")
        comp_info.setWordWrap(True)
        comp_info.setStyleSheet("color: #aaa; margin-bottom: 5px;")
        form_comp.addRow(comp_info)
        
        self.chk_enable_analyzer = QCheckBox("Run Analyzer/Compressor")
        self.chk_enable_analyzer.setChecked(True)
        self.chk_enable_analyzer.setStyleSheet("color: #00E676; font-weight: bold;")
        
        self.chk_mask_data = QCheckBox("Mask Dynamic Data (Replaces URLs, IPs, and Hex with tags)")
        self.chk_mask_data.setStyleSheet("color: #00E676; font-weight: bold;")
        
        self.chk_truncate = QCheckBox("Truncate Long Errors (Caps spam to 250 chars. Protects Tracebacks)")
        
        self.spin_top_n = QSpinBox()
        self.spin_top_n.setRange(1, 100)
        self.spin_top_n.setSuffix(" Errors")
        
        form_comp.addRow("", self.chk_enable_analyzer)
        form_comp.addRow("", self.chk_mask_data)
        form_comp.addRow("", self.chk_truncate)
        form_comp.addRow("Max Unique Items per Category (Top N):", self.spin_top_n)
        analyzer_layout.addWidget(group_comp)
        
        group_tail = QGroupBox("Raw Log Extraction (File Tails)")
        form_tail = QVBoxLayout(group_tail)
        
        chk_lay = QHBoxLayout()
        self.chk_enable_tail = QCheckBox("Extract Raw Log Tails into a single file")
        self.chk_enable_tail.setChecked(True)
        self.chk_enable_tail.setStyleSheet("color: #00E5FF; font-weight: bold;")
        
        self.chk_tail_headers = QCheckBox("Inject File Name Headers between sections")
        self.chk_tail_headers.setChecked(True)
        
        chk_lay.addWidget(self.chk_enable_tail)
        chk_lay.addWidget(self.chk_tail_headers)
        chk_lay.addStretch()
        form_tail.addLayout(chk_lay)
        
        rb_lay = QHBoxLayout()
        self.rb_tail_lines = QRadioButton("By Line Count:")
        self.rb_tail_lines.setChecked(True)
        self.spin_tail_lines = QSpinBox()
        self.spin_tail_lines.setRange(10, 10000)
        self.spin_tail_lines.setValue(500)
        
        self.rb_tail_pct = QRadioButton("By Percentage:")
        self.spin_tail_pct = QSpinBox()
        self.spin_tail_pct.setRange(1, 100)
        self.spin_tail_pct.setSuffix(" %")
        self.spin_tail_pct.setValue(10)
        
        rb_lay.addWidget(self.rb_tail_lines)
        rb_lay.addWidget(self.spin_tail_lines)
        rb_lay.addSpacing(20)
        rb_lay.addWidget(self.rb_tail_pct)
        rb_lay.addWidget(self.spin_tail_pct)
        rb_lay.addStretch()
        form_tail.addLayout(rb_lay)
        
        size_lay = QHBoxLayout()
        self.spin_tail_mb = QDoubleSpinBox()
        self.spin_tail_mb.setRange(0.1, 50.0)
        self.spin_tail_mb.setSuffix(" MB")
        self.spin_tail_mb.setValue(5.0)
        self.spin_tail_mb.setToolTip("Hard safety cap. Will not read more than this amount of data from the end of any individual file.")
        size_lay.addWidget(QLabel("Max Size Cap Per File:"))
        size_lay.addWidget(self.spin_tail_mb)
        size_lay.addStretch()
        form_tail.addLayout(size_lay)
        
        bg_tail = QButtonGroup(self)
        bg_tail.addButton(self.rb_tail_lines)
        bg_tail.addButton(self.rb_tail_pct)
        analyzer_layout.addWidget(group_tail)

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
        
        self.chk_tracebacks = QCheckBox("Critical Tracebacks, Crashes & Emergency Brakes")
        self.chk_selenium = QCheckBox("Selenium & WebDriver Crashes")
        self.chk_ffmpeg = QCheckBox("FFmpeg & Audio Capture Errors")
        self.chk_network = QCheckBox("Network, Proxy, and Cloud Sync Timeouts")
        self.chk_streams = QCheckBox("Stream Drops & Resolver Fails")
        self.chk_normal = QCheckBox("Sample of Normal Activity (Alerts/Bio-Hits)")
        
        grid = QHBoxLayout()
        col1 = QVBoxLayout()
        col2 = QVBoxLayout()
        col1.addWidget(self.chk_tracebacks)
        col1.addWidget(self.chk_selenium)
        col1.addWidget(self.chk_ffmpeg)
        col2.addWidget(self.chk_network)
        col2.addWidget(self.chk_streams)
        col2.addWidget(self.chk_normal)
        grid.addLayout(col1)
        grid.addLayout(col2)
        v_filters.addLayout(grid)
        analyzer_layout.addWidget(group_filters)
        analyzer_layout.addStretch()

        self.lbl_status = QLabel("Ready to analyze.")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        analyzer_layout.addWidget(self.lbl_status)
        
        self.progress = QProgressBar()
        self.progress.setValue(0)
        analyzer_layout.addWidget(self.progress)
        
        self.btn_run = QPushButton("⚡ EXTRACT RAW TAILS & ANALYZE LOGS")
        self.btn_run.setStyleSheet("background-color: #00E5FF; color: black; font-size: 16px; padding: 15px; font-weight: bold;")
        self.btn_run.clicked.connect(self.run_analysis)
        analyzer_layout.addWidget(self.btn_run)
        
        self.tabs.addTab(analyzer_widget, "Log Compressor & Analyzer")
        
        # --- TAB 2: STORAGE DEEP CLEAN & SETTINGS ---
        clean_widget = QWidget()
        clean_layout = QVBoxLayout(clean_widget)
        
        janitor_int = hk_config.get('janitor_interval_hours', 1)
        log_ret = hk_config.get('legacy_log_retention_hours', 72)
        temp_ret = hk_config.get('temp_retention_hours', 24)
        auto_py = hk_config.get('auto_wipe_python_caches', True)
        auto_clips = hk_config.get('auto_wipe_orphaned_clips', True)

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
        
        tracker_layout = QHBoxLayout()
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

        manual_group = QGroupBox("Manual Deep Clean")
        manual_layout = QVBoxLayout(manual_group)
        info_lbl = QLabel("Instantly wipe legacy logs or eradicate abandoned Chrome/Audio/Python cache files from your local drive. This cannot be undone.")
        info_lbl.setWordWrap(True)
        info_lbl.setStyleSheet("color: #aaa; margin-bottom: 10px;")
        manual_layout.addWidget(info_lbl)

        self.btn_nuke_all_logs = QPushButton("🌋 Nuke Detection Logs (Audio/Vision/Proxy/Workers)")
        self.btn_nuke_all_logs.setStyleSheet("background-color: #d84315; color: white; font-weight: bold; padding: 8px; margin-bottom: 5px;")
        self.btn_nuke_all_logs.clicked.connect(self.nuke_all_logs)
        manual_layout.addWidget(self.btn_nuke_all_logs)

        log_layout1 = QHBoxLayout()
        self.btn_clear_monitor = QPushButton("📄 Clear Audio Log")
        self.btn_clear_monitor.clicked.connect(lambda: self.clear_file(ROOT / "monitor_debug.txt"))
        self.btn_clear_vision = QPushButton("📷 Clear Vision Log")
        self.btn_clear_vision.clicked.connect(lambda: self.clear_file(ROOT / "vision_debug.txt"))
        log_layout1.addWidget(self.btn_clear_monitor)
        log_layout1.addWidget(self.btn_clear_vision)
        
        log_layout2 = QHBoxLayout()
        self.btn_clear_proxy = QPushButton("📄 Clear Proxy/Hydra Log")
        self.btn_clear_proxy.clicked.connect(lambda: self.clear_file(ROOT / "proxy_debug.txt"))
        self.btn_clear_app_logs = QPushButton("📂 Clear Worker Logs")
        self.btn_clear_app_logs.clicked.connect(self.clear_app_logs)
        log_layout2.addWidget(self.btn_clear_proxy)
        log_layout2.addWidget(self.btn_clear_app_logs)
        
        manual_layout.addLayout(log_layout1)
        manual_layout.addLayout(log_layout2)

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
       
        self.worker = None
        self.load_analyzer_settings()

    def update_server_status(self):
        interval = self.spin_server_interval.value()
        if self.last_server_maintenance_ts == 0: self.lbl_server_status.setText("<span style='color: #FF9800; font-weight: bold;'>OVERDUE (Never Cleaned)</span>"); return
        days_since = (time.time() - self.last_server_maintenance_ts) / 86400.0
        if days_since >= interval: self.lbl_server_status.setText(f"<span style='color: #F57C00; font-weight: bold;'>OVERDUE ({int(days_since)} days since last clean)</span>")
        else: days_left = interval - days_since; self.lbl_server_status.setText(f"<span style='color: #00E676; font-weight: bold;'>Healthy ({int(days_left)} days until next clean)</span>")

    def mark_server_cleaned(self):
        self.last_server_maintenance_ts = time.time()
        self.update_server_status()
        QMessageBox.information(self, "Updated", "Server maintenance date recorded for today.")

    def update_local_db_status(self):
        interval = self.spin_local_interval.value()
        if self.last_local_db_maintenance_ts == 0: self.lbl_local_status.setText("<span style='color: #FF9800; font-weight: bold;'>OVERDUE (Never Cleaned)</span>"); return
        days_since = (time.time() - self.last_local_db_maintenance_ts) / 86400.0
        if days_since >= interval: self.lbl_local_status.setText(f"<span style='color: #F57C00; font-weight: bold;'>OVERDUE ({int(days_since)} days since last clean)</span>")
        else: days_left = interval - days_since; self.lbl_local_status.setText(f"<span style='color: #00E676; font-weight: bold;'>Healthy ({int(days_left)} days until next clean)</span>")

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
        commands = ("# 1. Check current disk space BEFORE pruning (Note the 'Avail' column for /dev/vda1)\ndf -h /\n\n# 2. Remove all unused Docker containers, networks, images, and build cache\n# (This includes stopped containers and dangling images eating up space)\ndocker system prune -a -f --volumes\n\n# 3. Clear old systemd journal logs, keeping only the last 7 days to prevent OS log bloat\njournalctl --vacuum-time=7d\n\n# 4. Check disk space AFTER pruning to see how much space was recovered\ndf -h /\n")
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
            self.edit_app_logs.setText(str(ROOT / "app_logs"))
            self.edit_out.setText(str(ROOT / "Debug"))
            
            self.chk_enable_analyzer.setChecked(True)
            self.chk_enable_tail.setChecked(True)
            self.chk_tail_headers.setChecked(True)
            self.rb_tail_lines.setChecked(True)
            self.spin_tail_lines.setValue(500)
            self.spin_tail_pct.setValue(10)
            self.spin_tail_mb.setValue(5.0)
            self.spin_time.setValue(15)
            
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
            with open(LOG_ANALYZER_SETTINGS_FILE, "r", encoding="utf-8") as f: s = json.load(f)
            self.edit_monitor.setText(s.get("monitor_path", str(ROOT / "monitor_debug.txt")))
            self.edit_vision.setText(s.get("vision_path", str(ROOT / "vision_debug.txt")))
            self.edit_app_logs.setText(s.get("app_logs_dir", str(ROOT / "app_logs")))
            self.edit_out.setText(s.get("out_dir", str(ROOT / "Debug")))
            self.spin_time.setValue(s.get("timeframe_hours", 15))
            
            f_set = s.get("filters", {})
            self.chk_tracebacks.setChecked(f_set.get("tracebacks", True))
            self.chk_selenium.setChecked(f_set.get("selenium", True))
            self.chk_ffmpeg.setChecked(f_set.get("ffmpeg", True))
            self.chk_network.setChecked(f_set.get("network", True))
            self.chk_streams.setChecked(f_set.get("streams", True))
            self.chk_normal.setChecked(f_set.get("normal", True))
            
            c_set = s.get("compression", {})
            self.chk_enable_analyzer.setChecked(c_set.get("enable_analyzer", True))
            self.chk_mask_data.setChecked(c_set.get("mask_data", True))
            self.chk_truncate.setChecked(c_set.get("truncate", True))
            self.spin_top_n.setValue(c_set.get("top_n", 15))
            
            t_set = s.get("tail_extraction", {})
            self.chk_enable_tail.setChecked(t_set.get("enable_tails", True))
            self.chk_tail_headers.setChecked(t_set.get("add_headers", True))
            mode = t_set.get("mode", "lines")
            if mode == "lines": self.rb_tail_lines.setChecked(True)
            else: self.rb_tail_pct.setChecked(True)
            self.spin_tail_lines.setValue(t_set.get("lines", 500))
            self.spin_tail_pct.setValue(t_set.get("pct", 10))
            self.spin_tail_mb.setValue(t_set.get("max_mb", 5.0))
        except Exception as e: logging.error(f"Failed to load analyzer settings: {e}")

    def save_analyzer_settings(self):
        try:
            s = {
                "monitor_path": self.edit_monitor.text(), 
                "vision_path": self.edit_vision.text(), 
                "app_logs_dir": self.edit_app_logs.text(), 
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
                    "enable_analyzer": self.chk_enable_analyzer.isChecked(), 
                    "mask_data": self.chk_mask_data.isChecked(), 
                    "truncate": self.chk_truncate.isChecked(), 
                    "top_n": self.spin_top_n.value() 
                },
                "tail_extraction": { 
                    "enable_tails": self.chk_enable_tail.isChecked(), 
                    "add_headers": self.chk_tail_headers.isChecked(), 
                    "mode": "lines" if self.rb_tail_lines.isChecked() else "pct", 
                    "lines": self.spin_tail_lines.value(), 
                    "pct": self.spin_tail_pct.value(), 
                    "max_mb": self.spin_tail_mb.value() 
                }
            }
            with open(LOG_ANALYZER_SETTINGS_FILE, "w", encoding="utf-8") as f: json.dump(s, f, indent=2)
        except Exception as e: logging.error(f"Failed to save analyzer settings: {e}")

    def run_analysis(self):
        m_path = self.edit_monitor.text().strip()
        v_path = self.edit_vision.text().strip()
        a_dir = self.edit_app_logs.text().strip()
        o_dir = self.edit_out.text().strip()
        
        if not os.path.exists(m_path) and not os.path.exists(v_path) and not os.path.exists(a_dir): 
            QMessageBox.critical(self, "Error", "No log files or directories exist at the specified paths.")
            return
            
        if not os.path.isdir(o_dir):
            try: os.makedirs(o_dir)
            except Exception as e: 
                QMessageBox.critical(self, "Error", f"Failed to create output directory: {e}")
                return
                
        self.save_analyzer_settings()
        filters = { "tracebacks": self.chk_tracebacks.isChecked(), "selenium": self.chk_selenium.isChecked(), "ffmpeg": self.chk_ffmpeg.isChecked(), "network": self.chk_network.isChecked(), "streams": self.chk_streams.isChecked(), "normal": self.chk_normal.isChecked() }
        comp_settings = { "enable_analyzer": self.chk_enable_analyzer.isChecked(), "mask_data": self.chk_mask_data.isChecked(), "truncate": self.chk_truncate.isChecked(), "top_n": self.spin_top_n.value() }
        tail_settings = { "enable_tails": self.chk_enable_tail.isChecked(), "add_headers": self.chk_tail_headers.isChecked(), "mode": "lines" if self.rb_tail_lines.isChecked() else "pct", "lines": self.spin_tail_lines.value(), "pct": self.spin_tail_pct.value(), "max_mb": self.spin_tail_mb.value() }
        hours = self.spin_time.value()
        
        self.btn_run.setEnabled(False)
        self.btn_run.setText("⏳ PARSING LOGS...")
        self.progress.setValue(0)
        
        self.worker = LogParserWorker(m_path, v_path, a_dir, o_dir, hours, filters, comp_settings, tail_settings)
        self.worker.progress_update.connect(self.update_progress)
        self.worker.finished.connect(self.on_analysis_complete)
        self.worker.start()

    def update_progress(self, msg, val): 
        self.lbl_status.setText(msg)
        self.progress.setValue(val)

    def on_analysis_complete(self, result_path_or_err, success):
        self.btn_run.setEnabled(True)
        self.btn_run.setText("⚡ EXTRACT RAW TAILS & ANALYZE LOGS")
        self.progress.setValue(100)
        
        if success:
            self.lbl_status.setText("Success! Review popup generated.")
            QMessageBox.information(self, "Extraction Complete", result_path_or_err)
            try:
                o_dir = self.edit_out.text().strip()
                if sys.platform == "win32": os.startfile(o_dir)
                elif sys.platform == "darwin": subprocess.call(["open", o_dir])
                else: subprocess.call(["xdg-open", o_dir])
            except: pass
        else: 
            self.lbl_status.setText("Analysis failed.")
            QMessageBox.critical(self, "Error", result_path_or_err)

    def nuke_all_logs(self):
        msg = (
            "This will instantly truncate and permanently delete the contents of the following files/folders:\n\n"
            "- monitor_debug.txt (Audio Engine Log)\n"
            "- vision_debug.txt (Vision Engine Log)\n"
            "- proxy_debug.txt (Hydra Network Log)\n"
            "- app_logs folder (All individual worker logs)\n\n"
            "This will ONLY delete diagnostic text logs, not your audio clips or database records.\n"
            "Proceed with full log wipe?"
        )
        if QMessageBox.warning(self, "Confirm Log Nuke", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            deleted_files = 0
            cleared_files = 0
            
            for fname in ["monitor_debug.txt", "vision_debug.txt", "proxy_debug.txt"]:
                p = ROOT / fname
                if p.exists():
                    try:
                        with open(p, "w", encoding='utf-8') as f: f.truncate(0)
                        cleared_files += 1
                    except: pass
            
            log_dir = ROOT / "app_logs"
            if log_dir.exists():
                for lf in log_dir.glob("*.log"):
                    try: 
                        lf.unlink()
                        deleted_files += 1
                    except Exception:
                        try:
                            with open(lf, "w", encoding='utf-8') as f: f.truncate(0)
                            cleared_files += 1
                        except: pass
            
            QMessageBox.information(self, "Success", f"Cleared {cleared_files} root text logs and deleted {deleted_files} worker logs.")

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
            deleted_dirs = 0; deleted_files = 0; freed_bytes = 0
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
            finally: QApplication.restoreOverrideCursor()
            mb_freed = freed_bytes / (1024 * 1024)
            QMessageBox.information(self, "Clean Complete", f"Successfully deleted {deleted_dirs} directories and {deleted_files} files.\nFreed {mb_freed:.2f} MB.")

    def clear_pip_cache(self):
        msg = "This will clear your local PIP download cache, freeing up space from old package installers.\n\nProceed?"
        if QMessageBox.question(self, "Confirm Clean", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                res = subprocess.run([sys.executable, "-m", "pip", "cache", "purge"], capture_output=True, text=True, creationflags=flags)
                output = res.stdout.strip()
                if not output: output = res.stderr.strip()
                if not output: output = "Cache purged successfully."
                QMessageBox.information(self, "PIP Cache Cleaned", output)
            except Exception as e: 
                QMessageBox.critical(self, "Error", f"Failed to clear PIP cache: {e}")
            finally: QApplication.restoreOverrideCursor()

    def clear_database_bloat(self):
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if 'python' in p.info['name'].lower() and p.info['cmdline'] and any("scheduler.py" in cmd.lower() for cmd in p.info['cmdline']):
                    QMessageBox.critical(self, "Engine Running", "CRITICAL: The Monitoring Engine is currently RUNNING!\n\nYou MUST click 'STOP Monitoring' on the main dashboard before running the Database Bloat Cleaner to prevent database corruption and locks.")
                    return
            except: pass
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f: cfg = json.load(f)
            active_urls = set()
            for s in cfg.get('streams',[]):
                if s.get('page_url'): active_urls.add(s['page_url'])
                if s.get('original_url'): active_urls.add(s['original_url'])
            db_urls = set(); det_counts_map = {}
            with db_connector.get_db_connection(force_local=True) as con:
                cur = con.cursor()
                for table, col in[('stream_health_events', 'stream_url'), ('stream_noise_profiles', 'stream_url'), ('audio_hashes', 'stream_url'), ('species_stream_profiles', 'stream_url'), ('detections', 'channel_url')]:
                    try: 
                        cur.execute(f"SELECT DISTINCT {col} FROM {table}")
                        db_urls.update([r[0] for r in cur.fetchall() if r[0]])
                    except: pass
                try:
                    cur.execute("SELECT channel_url, COUNT(*) FROM detections GROUP BY channel_url")
                    for row in cur.fetchall(): det_counts_map[row[0]] = row[1]
                except: pass

            ghost_urls = list(db_urls - active_urls)
            if not ghost_urls: 
                QMessageBox.information(self, "All Clean", "No orphaned ghost URLs found in the database.")
                self.mark_local_cleaned()
                return
                
            urls_to_delete =[]
            for g in ghost_urls:
                count = det_counts_map.get(g, 0)
                is_youtube = "youtube.com" in g or "youtu.be" in g
                if count == 0 or not is_youtube: urls_to_delete.append(g)

            if not urls_to_delete: 
                QMessageBox.information(self, "All Clean", "No zero-detection ghost URLs found to purge.")
                self.mark_local_cleaned()
                return

            chunk_size = 500
            total_chunks = math.ceil(len(urls_to_delete) / chunk_size)
            eta_seconds = total_chunks * 0.5
            eta_mins = int(eta_seconds // 60)
            eta_secs = int(eta_seconds % 60)
            eta_str = f"{eta_mins}m {eta_secs}s" if eta_mins > 0 else f"{eta_secs}s"

            msg = ("<b>Why do this?</b> IP Camera streams (HLS/.m3u8) use temporary security tokens that expire. When the engine auto-resolves a new token, the old URL is left behind in the database as a 'ghost' with 0 detections. Over time, this bloats your database file size and drastically slows down SQL query performance.<br><br>"
                   "<b>What it does:</b> Safely bulk-deletes these ghost records from your Local database first, then immediately mirrors the deletion to the Cloud Database.<br><br>"
                   f"<b>Found {len(urls_to_delete)} ghosts. Estimated time to complete: {eta_str}.</b><br><br>Proceed?")
            
            if QMessageBox.warning(self, "Confirm Database Clean", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes: return

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

            tables_to_clean =[('stream_health_events', 'stream_url'), ('stream_noise_profiles', 'stream_url'), ('audio_hashes', 'stream_url'), ('species_stream_profiles', 'stream_url'), ('detections', 'channel_url')]
            start_time = time.time()

            for i in range(0, len(urls_to_delete), chunk_size):
                if progress.wasCanceled(): 
                    canceled = True
                    break
                chunk = urls_to_delete[i:i+chunk_size]
                placeholders = ','.join(['?'] * len(chunk))
                for table, col in tables_to_clean:
                    q = f"DELETE FROM {table} WHERE {col} IN ({placeholders})"
                    l_cur.execute(q, chunk)
                    if c_cur:
                        try: c_cur.execute(q, chunk)
                        except Exception as e: logging.error(f"Cloud delete chunk error: {e}")
                local_con.commit()
                if is_cloud_active: cloud_con.commit()
                ghosts_removed += len(chunk)
                progress.setValue(ghosts_removed)
                
                elapsed = time.time() - start_time
                avg_time = elapsed / max(1, ghosts_removed)
                remaining = avg_time * (len(urls_to_delete) - ghosts_removed)
                rem_m = int(remaining // 60)
                rem_s = int(remaining % 60)
                rem_str = f"{rem_m}m {rem_s}s" if rem_m > 0 else f"{rem_s}s"
                progress.setLabelText(f"Purging Database Bloat...\nChunk {i//chunk_size + 1} of {total_chunks} (ETA: {rem_str})")
                QApplication.processEvents()

            local_con.close()
            if is_cloud_active: cloud_con.close()
            
            if canceled: 
                QMessageBox.warning(self, "Purge Canceled", f"Operation canceled by user.\nSuccessfully removed {ghosts_removed} ghost streams before stopping.\nPartial progress has been safely saved to both Local and Cloud databases.")
            else: 
                QMessageBox.information(self, "Database Cleaned", f"Successfully bulk-removed {ghosts_removed} ghost stream URLs.\n(Synchronized to Cloud Database: {is_cloud_active})")
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
            finally: QApplication.restoreOverrideCursor()

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
                    except Exception: pass 
            finally: QApplication.restoreOverrideCursor()
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
        
    def closeEvent(self, event):
        self.accept()