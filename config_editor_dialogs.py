# FILE: config_editor_dialogs.py
# VERSION: 13.1 - "The Dialog Button Patch"
# RESPONSIBILITY: Houses the Core Engine, Cooldowns, and Advanced Settings dialogs.
# System and Housekeeping dialogs have been safely migrated to config_editor_sys_dialogs.py.
# UPDATED: Fixed a typo in EngineConfigDialog where 'btns' was incorrectly referenced as 'self.buttons'.

import sys
import copy
import math
import logging
import sqlite3
import json
import os
import psutil
from pathlib import Path

from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton, 
                             QMessageBox, QGroupBox, QCheckBox, QSpinBox, QLabel, 
                             QWidget, QFormLayout, QLineEdit, QDialogButtonBox, 
                             QTableWidget, QTableWidgetItem, QHeaderView, QGridLayout, 
                             QComboBox, QFileDialog, QInputDialog, QApplication, 
                             QTabWidget, QSlider, QDoubleSpinBox)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

# Import utils and network manager
from config_editor_utils import ChromeMaintenance
import network_manager
import db_connector

# --- Configuration Paths ---
ROOT = Path(__file__).resolve().parent
DATABASE_PATH = ROOT / "detections.db"
CONFIG_FILE = ROOT / "birdnet_config.json"


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

        # --- MAP BALANCE SLIDER ---
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
        self.spin_target_map_capacity.setRange(10, 250) 
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
                    item.setForeground(QBrush(QColor(Qt.GlobalColor.white)) if is_selected else QBrush(self.palette().color(QPalette.ColorRole.Text)))
                
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
        
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.layout.addWidget(self.buttons)
        
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