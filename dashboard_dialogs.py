# FILE: dashboard_dialogs.py
# VERSION: 9.3 - "The Human Vocal Patch"
# RESPONSIBILITY: Pop-up dialogs for Dashboard.
# UPDATED: Added "Human vocal" and "Human footstep" to the hardcoded noise list to hide them from the Rate/Health dialogs.

import sys
import math
import time
import json
import logging
from datetime import datetime
from pathlib import Path
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, 
                             QGroupBox, QComboBox, QCheckBox, QScrollArea, QDialogButtonBox, 
                             QSpinBox, QProgressBar, QMessageBox, QListWidget, QWidget, QSizePolicy, QFrame)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QPalette

# Import Utils
from dashboard_utils import ProgressPie, get_db_connection, ROOT

# Matplotlib check
try:
    import matplotlib
    import numpy as np
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    matplotlib.use('QtAgg')
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

# --- CONFIGURATION ---
TARGETS_FILE = ROOT / "bioacoustic_targets.json"

# --- MASTER NOISE EXCLUSION LIST (Mirrored from GUI) ---
BIRDNET_NOISE_CLASSES =[
    "Siren", "Dog", "Motor vehicle (road)", "Car alarm", "Human voice", 
    "Human narrator", "Human whistling", "Human vocal", "Human footstep", 
    "Engine", "Wind", "Rain", "Gunshot, gunfire", "Fireworks", "Noise", "Car"
]

def get_dialog_filter_sql(mode, col_name="species"):
    """
    Constructs the dynamic WHERE clause for Noise Exclusion and Master Data Toggles.
    Returns: (sql_string, params_list)
    """
    sql_parts = [f"{col_name} NOT IN ({','.join(['?']*len(BIRDNET_NOISE_CLASSES))})"]
    params = list(BIRDNET_NOISE_CLASSES)
    
    bio_targets = set()
    if TARGETS_FILE.exists():
        try:
            raw = json.loads(TARGETS_FILE.read_text(encoding='utf-8'))
            if raw.get("_version") == 2:
                assignments = raw.get("assignments", {})
                for url, targets in assignments.items():
                    for t in targets:
                        if isinstance(t, dict) and 'display' in t:
                            bio_targets.add(t['display'].strip())
            else:
                # Legacy fallback
                for val in raw.values():
                    if isinstance(val, list):
                        for v in val:
                            bio_targets.add(v['display'].strip() if isinstance(v, dict) else v.strip())
                    elif isinstance(val, str):
                        bio_targets.add(val.strip())
        except Exception as e:
            logging.error(f"Error parsing targets file for dialog filter: {e}")
        
    if "Birds Only" in mode:
        if bio_targets:
            sql_parts.append(f"{col_name} NOT IN ({','.join(['?']*len(bio_targets))})")
            params.extend(list(bio_targets))
    elif "Bioacoustics Only" in mode:
        if bio_targets:
            sql_parts.append(f"{col_name} IN ({','.join(['?']*len(bio_targets))})")
            params.extend(list(bio_targets))
        else:
            sql_parts.append("1=0") 
            
    return " AND ".join(sql_parts), params

class DetailsDialog(QDialog):
    def __init__(self, title, details, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.layout = QVBoxLayout(self)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.details_label = QLabel(details)
        self.details_label.setWordWrap(True)
        self.details_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details_label.setTextFormat(Qt.TextFormat.RichText)
        self.scroll_area.setWidget(self.details_label)
        self.layout.addWidget(self.scroll_area)
        self.ok_button = QPushButton("OK")
        self.ok_button.clicked.connect(self.accept)
        self.layout.addWidget(self.ok_button)
        screen = self.screen()
        if not screen: return
        screen_geometry = screen.availableGeometry()
        ideal_size = self.sizeHint()
        max_width = int(screen_geometry.width() * 0.65)
        max_height = int(screen_geometry.height() * 0.75)
        min_width, min_height = 550, 150
        final_width = min(max(ideal_size.width(), min_width), max_width)
        final_height = min(max(ideal_size.height(), min_height), max_height)
        self.resize(final_width, final_height)

class RateAnalysisDialog(QDialog):
    def __init__(self, cycle_time_secs, last_period_index, last_per_index, last_profile_checked, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Detection & Alert Rate Analysis")
        self.setMinimumSize(800, 650)
        
        # Default, overwritten by parent immediately after init
        self.target_filter_mode = "View: All Valid Targets" 
        
        self.cycle_time_secs = cycle_time_secs
        self.max_graph_bars = 100
        self.time_units_in_seconds = {
            "Minute": 60, "Cycle": self.cycle_time_secs, "3 Cycles": self.cycle_time_secs * 3,
            "Hour": 3600, "Day": 86400, "Week": 604800,
            "Month": 2592000, "Year": 31536000
        }
        main_layout = QVBoxLayout(self)
        controls_frame = QGroupBox("Analysis Period")
        controls_layout = QVBoxLayout()
        top_controls_layout = QHBoxLayout()
        top_controls_layout.addWidget(QLabel("Analyze data from the last:"))
        self.period_combo = QComboBox()
        self.period_combo.addItems(["Cycle", "3 Cycles", "Hour", "Day", "Week", "Month"])
        top_controls_layout.addWidget(self.period_combo)
        top_controls_layout.addSpacing(20)
        self.profile_checkbox = QCheckBox("Show 24-Hour Activity Profile")
        top_controls_layout.addWidget(self.profile_checkbox)
        top_controls_layout.addStretch()
        controls_layout.addLayout(top_controls_layout)
        bottom_controls_layout = QHBoxLayout()
        bottom_controls_layout.addWidget(QLabel("Show average rate per:"))
        self.per_combo = QComboBox()
        self.per_combo.addItems(["Minute", "Cycle", "3 Cycles", "Hour", "Day", "Week", "Month", "Year"])
        bottom_controls_layout.addWidget(self.per_combo)
        bottom_controls_layout.addStretch()
        controls_layout.addLayout(bottom_controls_layout)
        controls_frame.setLayout(controls_layout)
        main_layout.addWidget(controls_frame)
        if last_period_index is not None: self.period_combo.setCurrentIndex(last_period_index)
        if last_per_index is not None: self.per_combo.setCurrentIndex(last_per_index)
        if last_profile_checked is not None: self.profile_checkbox.setChecked(last_profile_checked)
        self.results_frame = QGroupBox("Calculated Averages")
        results_layout = QVBoxLayout()
        self.detections_label = QLabel("<b>Average Detections:</b> N/A")
        self.alerts_label = QLabel("<b>Average Alerts:</b> N/A")
        results_layout.addWidget(self.detections_label)
        results_layout.addWidget(self.alerts_label)
        self.results_frame.setLayout(results_layout)
        main_layout.addWidget(self.results_frame)
        graph_frame = QGroupBox("Detections & Alerts Over Time")
        graph_layout = QVBoxLayout()
        self.figure = Figure(figsize=(5, 3))
        self.canvas = FigureCanvas(self.figure)
        self.ax = self.figure.subplots()
        graph_layout.addWidget(self.canvas)
        graph_frame.setLayout(graph_layout)
        main_layout.addWidget(graph_frame, stretch=1)
        self.period_combo.currentIndexChanged.connect(self._update_per_combo_state)
        self.period_combo.currentIndexChanged.connect(self.update_analysis)
        self.per_combo.currentIndexChanged.connect(self.update_analysis)
        self.profile_checkbox.toggled.connect(self.update_analysis)
        QTimer.singleShot(0, self._update_per_combo_state)
        QTimer.singleShot(0, self.update_analysis)
    
    def get_selections(self):
        return self.period_combo.currentIndex(), self.per_combo.currentIndex(), self.profile_checkbox.isChecked()

    def _update_per_combo_state(self):
        analysis_period_str = self.period_combo.currentText()
        analysis_seconds = self.time_units_in_seconds.get(analysis_period_str, 0)
        self.per_combo.blockSignals(True)
        current_selection_text = self.per_combo.currentText()
        new_selection_index = -1
        first_valid_index = -1
        for i in range(self.per_combo.count()):
            item_text = self.per_combo.itemText(i)
            item = self.per_combo.model().item(i)
            per_seconds = self.time_units_in_seconds.get(item_text, float('inf'))
            num_bars = analysis_seconds / per_seconds if per_seconds > 0 else float('inf')
            is_valid = per_seconds <= analysis_seconds and num_bars <= self.max_graph_bars
            item.setEnabled(is_valid)
            if is_valid and first_valid_index == -1:
                first_valid_index = i
            if item_text == current_selection_text and is_valid:
                new_selection_index = i
        if new_selection_index != -1:
            self.per_combo.setCurrentIndex(new_selection_index)
        elif first_valid_index != -1:
            self.per_combo.setCurrentIndex(first_valid_index)
        self.per_combo.blockSignals(False)

    def update_analysis(self):
        period_str = self.period_combo.currentText()
        per_str = self.per_combo.currentText()
        is_profile_view = self.profile_checkbox.isChecked()
        self.results_frame.setVisible(not is_profile_view)
        self.per_combo.setEnabled(not is_profile_view)
        if not self.per_combo.isEnabled():
             self.ax.clear()
        analysis_seconds = self.time_units_in_seconds[period_str]
        per_seconds = self.time_units_in_seconds[per_str]
        cutoff_ts = time.time() - analysis_seconds
        try:
            con = get_db_connection()
            cur = con.cursor()
            
            # --- FILTER INTEGRATION ---
            filter_sql, filter_params = get_dialog_filter_sql(self.target_filter_mode, "species")
            
            cur.execute(f"SELECT MIN(timestamp) FROM detections WHERE {filter_sql}", filter_params)
            min_ts_row = cur.fetchone()
            min_ts = min_ts_row[0] if min_ts_row and min_ts_row[0] else time.time()
            
            if cutoff_ts < min_ts:
                self.detections_label.setText(f"<b>Average Detections:</b> N/A (Not enough filtered data)")
                self.alerts_label.setText(f"<b>Average Alerts:</b> N/A")
                self.ax.clear(); self.ax.text(0.5, 0.5, 'Not enough data exists for this period/filter.', ha='center', va='center'); self.canvas.draw()
                con.close()
                return
            
            # SQLite-only logic (Safe, fast, no transactions to lock)
            query = f"SELECT timestamp, alert_sent FROM detections WHERE timestamp >= ? AND {filter_sql}"
            params = [cutoff_ts] + filter_params
            cur.execute(query, params)
            
            raw_data = cur.fetchall()
            data =[]
            for ts, alert_val in raw_data:
                # Strictly evaluate the SQLite boolean representation (usually 0/1)
                is_alert = 1 if (alert_val == 1 or str(alert_val).lower() == 'true') else 0
                data.append((ts, is_alert))
            
            con.close()
        except Exception as e:
            self.detections_label.setText(f"<b>Error:</b> {e}")
            self.alerts_label.setText("")
            return
        if is_profile_view: self.plot_24_hour_profile(data, period_str)
        else: self.plot_sequential_view(data, analysis_seconds, per_seconds, period_str, per_str)

    def plot_sequential_view(self, data, analysis_seconds, per_seconds, period_str, per_str):
        total_detections = len(data)
        total_alerts = sum(1 for row in data if row[1])
        rate_multiplier = per_seconds / analysis_seconds if analysis_seconds > 0 else 0
        avg_detections = total_detections * rate_multiplier
        avg_alerts = total_alerts * rate_multiplier
        self.detections_label.setText(f"<b>Average Detections:</b> {avg_detections:.2f} per {per_str}")
        self.alerts_label.setText(f"<b>Average Alerts:</b> {avg_alerts:.2f} per {per_str}")
        now = time.time()
        num_bins = math.ceil(analysis_seconds / per_seconds) if per_seconds > 0 else 0
        if num_bins == 0:
            self.ax.clear(); self.ax.text(0.5, 0.5, 'Invalid time period selection.', ha='center', va='center'); self.canvas.draw()
            return
        bins_detections, bins_alerts, labels = [0] * num_bins, [0] * num_bins,[]
        for i in range(num_bins):
            bin_end, bin_start = now - (i * per_seconds), now - ((i + 1) * per_seconds)
            if per_seconds >= self.time_units_in_seconds["Day"]:
                labels.append(datetime.fromtimestamp(bin_start).strftime('%Y-%m-%d'))
            else:
                labels.append(datetime.fromtimestamp(bin_start).strftime('%m-%d %H:%M'))
            for ts, alert_sent in data:
                if bin_start <= ts < bin_end:
                    bins_detections[i] += 1
                    if alert_sent: bins_alerts[i] += 1
        labels.reverse(); bins_detections.reverse(); bins_alerts.reverse()
        self.ax.clear()
        x = np.arange(len(labels)); width = 0.35
        self.ax.bar(x - width/2, bins_detections, width, label='Detections', color='#007acc')
        self.ax.bar(x + width/2, bins_alerts, width, label='Alerts Sent', color='#fd7e14')
        self.ax.set_ylabel('Count')
        self.ax.set_title(f'Activity in the Last {period_str} ({self.target_filter_mode})')
        if len(labels) > 24:
            tick_spacing = math.ceil(len(labels) / 12)
            display_ticks, display_labels = x[::tick_spacing], labels[::tick_spacing]
            self.ax.set_xticks(display_ticks, display_labels, rotation=45, ha="right")
        else:
            self.ax.set_xticks(x, labels, rotation=45, ha="right")
        self.ax.legend(); self.figure.tight_layout(); self.canvas.draw()

    def plot_24_hour_profile(self, data, period_str):
        bins_detections, bins_alerts = [0] * 24, [0] * 24
        for ts, alert_sent in data:
            hour = datetime.fromtimestamp(ts).hour
            bins_detections[hour] += 1
            if alert_sent: bins_alerts[hour] += 1
        labels =[f"{h:02d}:00" for h in range(24)]
        self.ax.clear(); x = np.arange(len(labels)); width = 0.35
        self.ax.bar(x - width/2, bins_detections, width, label='Total Detections', color='#007acc')
        self.ax.bar(x + width/2, bins_alerts, width, label='Total Alerts Sent', color='#fd7e14')
        self.ax.set_ylabel('Total Count')
        self.ax.set_title(f'24-Hour Profile (Last {period_str} - {self.target_filter_mode})')
        self.ax.set_xticks(x, labels, rotation=45, ha="right")
        self.ax.legend(); self.figure.tight_layout(); self.canvas.draw()

class HealthSettingsDialog(QDialog):
    def __init__(self, current_agg_mode, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Health Analysis Settings")
        self.setMinimumWidth(800) 
        self.setMinimumHeight(500)
        self.layout = QVBoxLayout(self)
        self.listener_widgets = {}
        
        short_term_group = QGroupBox("Listener Progress & Analysis Mode")
        short_term_layout = QVBoxLayout()
        
        counting_mode_layout = QHBoxLayout()
        counting_mode_layout.addWidget(QLabel("<b>Analysis Method:</b>"))
        self.agg_mode_combo = QComboBox()
        self.agg_mode_combo.addItems([
            "Count all individual failures (High Sensitivity)",
            "Use most recent status per cycle (Outcome-Focused)"
        ])
        self.agg_mode_combo.setCurrentIndex(current_agg_mode)
        self.agg_mode_combo.setToolTip(
            "<b>High Sensitivity:</b> Shows 'Unresponsive' if a stream fails multiple times, even if it eventually succeeds.\n\n"
            "<b>Outcome-Focused:</b> Shows 'Good' as long as the very last attempt was a success."
        )
        counting_mode_layout.addWidget(self.agg_mode_combo, 1)
        short_term_layout.addLayout(counting_mode_layout)
        short_term_layout.addSpacing(15)
        
        progress_label = QLabel("<b>Live Listener Status (Active Batches):</b>")
        short_term_layout.addWidget(progress_label)
        
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setMinimumHeight(300) 
        scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        
        progress_container = QWidget()
        progress_container.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding) 
        
        self.progress_area_layout = QVBoxLayout(progress_container)
        self.progress_area_layout.setAlignment(Qt.AlignmentFlag.AlignTop) 
        self.progress_area_layout.setSpacing(15)
        
        self.status_message_label = QLabel("<i>Initializing...</i>")
        self.progress_area_layout.addWidget(self.status_message_label)
        
        scroll_area.setWidget(progress_container)
        short_term_layout.addWidget(scroll_area)
        
        short_term_group.setLayout(short_term_layout)
        self.layout.addWidget(short_term_group)
        
        note_label = QLabel("<i>Note: Health Thresholds (Intermittent/Unresponsive) are now managed in the Config Editor.</i>")
        note_label.setStyleSheet("color: gray;")
        note_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.layout.addWidget(note_label)

        self.buttons = QDialogButtonBox()
        self.buttons.addButton(QDialogButtonBox.StandardButton.Ok); self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept); self.buttons.rejected.connect(self.reject)
        self.layout.addWidget(self.buttons)
        self.progress_timer = QTimer(self); self.progress_timer.timeout.connect(self.update_progress_gauges); self.progress_timer.start(2000)
        self.update_progress_gauges()

    def get_values(self):
        return self.agg_mode_combo.currentIndex()

    def update_progress_gauges(self):
        try:
            con = get_db_connection(); cur = con.cursor()
            cur.execute("SELECT listener_id, cycle_start_time, total_cycle_seconds, managed_streams_json, cycle_count FROM scheduler_status")
            listeners_status = cur.fetchall(); con.close()
            db_listeners = {row[0]: row for row in listeners_status}
            
            if not db_listeners:
                self.status_message_label.setText("<i>Monitoring engine not running or status not available.</i>")
                self.status_message_label.show()
                for lid in list(self.listener_widgets.keys()):
                    widgets = self.listener_widgets.pop(lid)
                    widgets['frame'].setParent(None)
                    widgets['frame'].deleteLater()
                return
            else:
                self.status_message_label.hide()

            current_ids = list(self.listener_widgets.keys())
            for lid in current_ids:
                if lid not in db_listeners:
                    widgets = self.listener_widgets.pop(lid)
                    widgets['frame'].setParent(None)
                    widgets['frame'].deleteLater()
            
            for listener_id, data in sorted(db_listeners.items()):
                _, start_ts, total_secs, streams_json, cycle_count = data
                
                stream_text = "Idle"
                try:
                    streams = json.loads(streams_json)
                    if isinstance(streams, list) and streams:
                        if len(streams) == 1:
                            stream_text = streams[0] 
                        else:
                            clean_names =[s.replace(" (YouTube)", "").replace(" (Audio Only)", "") for s in streams]
                            count = len(clean_names)
                            if count > 5:
                                stream_text = f"Queue ({count} streams): " + ", ".join(clean_names[:3]) + f"..."
                            else:
                                stream_text = "Queue: " + ", ".join(clean_names)
                    elif isinstance(streams, list):
                        stream_text = "Waiting for work..."
                    else:
                        stream_text = str(streams_json)
                except:
                    stream_text = "Status Error"

                if listener_id not in self.listener_widgets:
                    frame = QFrame()
                    frame.setFrameShape(QFrame.Shape.StyledPanel)
                    frame.setStyleSheet("QFrame { background-color: #2b2b2b; border: 1px solid #444; border-radius: 4px; }")
                    frame_layout = QVBoxLayout(frame)
                    frame_layout.setContentsMargins(10, 8, 10, 8)
                    
                    top_row = QHBoxLayout()
                    label = QLabel(f"<b>{listener_id}:</b>")
                    label.setFixedWidth(40)
                    label.setStyleSheet("color: #fff;") 
                    
                    pie_icon = ProgressPie()
                    progress_bar = QProgressBar()
                    progress_bar.setTextVisible(True)
                    progress_bar.setFixedHeight(15)
                    cycle_label = QLabel()
                    cycle_label.setFixedWidth(80)
                    cycle_label.setStyleSheet("color: #ccc;") 
                    cycle_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    
                    top_row.addWidget(label)
                    top_row.addWidget(pie_icon)
                    top_row.addWidget(progress_bar, 1)
                    top_row.addWidget(cycle_label)
                    
                    stream_label = QLabel(stream_text)
                    stream_label.setWordWrap(True)
                    stream_label.setStyleSheet("color: #ccc; font-size: 11px; margin-left: 65px;") 
                    
                    frame_layout.addLayout(top_row)
                    frame_layout.addWidget(stream_label)
                    
                    self.progress_area_layout.addWidget(frame)
                    
                    self.listener_widgets[listener_id] = {
                        'frame': frame, 'pie': pie_icon, 
                        'bar': progress_bar, 'cycle_label': cycle_label,
                        'stream_label': stream_label,
                        'last_batch': cycle_count, 'cooldown': 0
                    }
                
                widgets = self.listener_widgets[listener_id]
                widgets['stream_label'].setText(stream_text)

                last_known = widgets.get('last_batch', -1)
                
                if cycle_count > last_known:
                    progress_percent = 100
                    widgets['bar'].setStyleSheet("QProgressBar::chunk { background-color: #00e676; }")
                    widgets['bar'].setFormat("BATCH COMPLETE!")
                    widgets['cooldown'] = 2 
                    widgets['last_batch'] = cycle_count
                
                elif widgets.get('cooldown', 0) > 0:
                    progress_percent = 100
                    widgets['cooldown'] -= 1
                
                else:
                    if not start_ts or not total_secs or total_secs <= 0: progress_percent = 0
                    else:
                        elapsed = time.time() - start_ts
                        progress_percent = min(int((elapsed / total_secs) * 100), 100)
                    widgets['bar'].setStyleSheet("")
                    widgets['bar'].setFormat("%p%")

                widgets['pie'].setValue(progress_percent)
                widgets['bar'].setValue(progress_percent)
                widgets['cycle_label'].setText(f"Batch: {cycle_count}")
                
        except Exception as e:
            self.status_message_label.setText(f"<i>Error loading status: {e}</i>")
            self.status_message_label.show()