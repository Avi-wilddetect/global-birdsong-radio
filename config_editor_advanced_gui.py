# FILE: config_editor_advanced_gui.py
# VERSION: 1.3 - "The Hybrid Elasticity Patch"
# RESPONSIBILITY: Houses the Advanced Settings dialog with a clean QTabWidget layout.
# UPDATED: Added 'Burst Zoom Elasticity' slider to control how Leaflet scales Spiderify radii during zoom events.

import os
import json
import copy
import psutil
from pathlib import Path

from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton, 
                             QMessageBox, QGroupBox, QCheckBox, QSpinBox, QLabel, 
                             QWidget, QFormLayout, QLineEdit, QDialogButtonBox, 
                             QFileDialog, QApplication, QTabWidget, QSlider, QDoubleSpinBox)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPalette, QColor

# Import utils
from config_editor_utils import ChromeMaintenance

# --- Configuration Paths ---
ROOT = Path(__file__).resolve().parent

class AdvancedSettingsDialog(QDialog):
    def __init__(self, loop_config, distance_config, map_config, cookie_config, browser_config, debug_enabled=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Advanced Settings & Maintenance")
        self.setMinimumSize(700, 700)

        self.distance_config = copy.deepcopy(distance_config)
        self.map_config = copy.deepcopy(map_config)
        self.cookie_config = copy.deepcopy(cookie_config)
        self.browser_config = copy.deepcopy(browser_config)
        
        self.debug_enabled = debug_enabled

        self.layout = QVBoxLayout(self)
        self.tabs = QTabWidget()

        # ==========================================
        # TAB 1: CORE SETTINGS
        # ==========================================
        self.tab_core = QWidget()
        core_layout = QVBoxLayout(self.tab_core)

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
        core_layout.addWidget(balance_group)

        # --- DISTANCE ESTIMATION ---
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
        core_layout.addWidget(distance_group)

        # --- COOKIES FALLBACK ---
        cookie_group = QGroupBox("YouTube Fallback (Cookies.txt)")
        cookie_layout = QFormLayout(cookie_group)
        cookie_path_layout = QHBoxLayout()
        self.cookies_file_edit = QLineEdit()
        self.browse_cookies_button = QPushButton("Browse...")
        self.browse_cookies_button.clicked.connect(self.browse_for_cookies_file)
        cookie_path_layout.addWidget(self.cookies_file_edit)
        cookie_path_layout.addWidget(self.browse_cookies_button)
        cookie_layout.addRow("Cookies File:", cookie_path_layout)
        core_layout.addWidget(cookie_group)

        # --- IDENTITY ENGINE ---
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
        core_layout.addWidget(maint_group)

        core_layout.addStretch()
        self.tabs.addTab(self.tab_core, "Core Settings")


        # ==========================================
        # TAB 2: MAP VISUALIZATION & GROUPING
        # ==========================================
        self.tab_map = QWidget()
        map_layout = QVBoxLayout(self.tab_map)

        # --- GENERAL MAP VISUALIZATION ---
        vis_group = QGroupBox("General Map Visualization")
        vis_layout = QFormLayout(vis_group)
        
        self.map_live_spin = QSpinBox()
        self.map_live_spin.setRange(10, 3600)
        self.map_live_spin.setValue(self.map_config.get("live_window_seconds", 120)) 
        self.map_live_spin.setToolTip("Detections younger than this will appear Red (Live) on the map.\nOlder ones turn Orange (Recent).")
        
        vis_layout.addRow("Map 'Live' Icon Duration (sec):", self.map_live_spin)
        map_layout.addWidget(vis_group)

        # --- GLOBAL FEED RULES ---
        feed_group = QGroupBox("Sidebar Feed Grouping Rules")
        feed_layout = QFormLayout(feed_group)
        
        self.chk_group_feed = QCheckBox("Group Entire Feed History (Ignores Max Age)")
        self.chk_group_feed.setToolTip("If checked, grouped frames will persist and organically slide down the historical sidebar feed instead of dissolving when they age past the max grouping window.")
        
        self.spin_group_age = QSpinBox()
        self.spin_group_age.setRange(1, 1440)
        self.spin_group_age.setSuffix(" mins")
        self.spin_group_age.setToolTip("How old a detection can be and still join a group. Disabled if 'Group Entire Feed History' is checked.")
        
        self.spin_group_time_gap = QSpinBox()
        self.spin_group_time_gap.setRange(1, 1440)
        self.spin_group_time_gap.setSuffix(" mins")
        self.spin_group_time_gap.setToolTip("Maximum chronological time gap allowed between consecutive detections for them to merge into the same Burst/Swarm group. Prevents 8am and 6pm detections from grouping together.")
        
        feed_layout.addRow("", self.chk_group_feed)
        feed_layout.addRow("Max Grouping Age:", self.spin_group_age)
        feed_layout.addRow("Max Time Gap Between Items:", self.spin_group_time_gap)
        map_layout.addWidget(feed_group)
        
        self.chk_group_feed.toggled.connect(lambda c: self.spin_group_age.setDisabled(c))

        # --- BIODIVERSITY BURSTS (SAME CAMERA) ---
        burst_group = QGroupBox("Biodiversity Bursts (Same Camera Grouping)")
        burst_group.setStyleSheet("QGroupBox { border: 1px solid #AB47BC; margin-top: 15px; color: #E1BEE7; font-weight: bold; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }")
        burst_layout = QVBoxLayout(burst_group)
        
        self.chk_enable_bursts = QCheckBox("Enable Biodiversity Bursts")
        burst_layout.addWidget(self.chk_enable_bursts)
        
        # ELASTICITY SLIDER
        elasticity_layout = QHBoxLayout()
        self.slider_elasticity = QSlider(Qt.Orientation.Horizontal)
        self.slider_elasticity.setRange(0, 100)
        self.slider_elasticity.setValue(self.map_config.get("burst_elasticity", 50))
        
        self.lbl_elasticity = QLabel()
        self.lbl_elasticity.setFixedWidth(200)
        self.lbl_elasticity.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        def update_elasticity_label(val):
            self.lbl_elasticity.setText(f"Zoom Elasticity: <b>{val}%</b>")
            
        self.slider_elasticity.valueChanged.connect(update_elasticity_label)
        update_elasticity_label(self.slider_elasticity.value())
        
        elasticity_layout.addWidget(QLabel("0% (Rigid Pixels)"))
        elasticity_layout.addWidget(self.slider_elasticity)
        elasticity_layout.addWidget(QLabel("100% (Earth Geo)"))
        elasticity_layout.addWidget(self.lbl_elasticity)
        
        burst_form = QFormLayout()
        self.spin_spiderify_radius = QDoubleSpinBox()
        self.spin_spiderify_radius.setRange(0.001, 0.100)
        self.spin_spiderify_radius.setDecimals(3)
        self.spin_spiderify_radius.setSingleStep(0.005)
        self.spin_spiderify_radius.setToolTip("The geographic radius used to push overlapping map icons apart into a visible ring (Only visible if Elasticity > 0%).")
        
        self.spin_burst_zoom = QSpinBox()
        self.spin_burst_zoom.setRange(5, 18)
        self.spin_burst_zoom.setToolTip("The Leaflet map zoom level triggered when a user clicks the purple Burst header.")
        
        burst_form.addRow("Ring Expansion Math:", elasticity_layout)
        burst_form.addRow("Geographic Base Radius:", self.spin_spiderify_radius)
        burst_form.addRow("Burst Auto-Zoom Level:", self.spin_burst_zoom)
        burst_layout.addLayout(burst_form)
        map_layout.addWidget(burst_group)

        self.chk_enable_bursts.toggled.connect(self.slider_elasticity.setEnabled)
        self.chk_enable_bursts.toggled.connect(self.spin_spiderify_radius.setEnabled)
        self.chk_enable_bursts.toggled.connect(self.spin_burst_zoom.setEnabled)

        # --- REGIONAL SWARMS (MULTIPLE CAMERAS) ---
        swarm_group = QGroupBox("Regional Swarms (Multi-Camera Grouping)")
        swarm_group.setStyleSheet("QGroupBox { border: 1px solid #00E5FF; margin-top: 15px; color: #B2EBF2; font-weight: bold; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }")
        swarm_layout = QFormLayout(swarm_group)
        
        self.chk_enable_swarms = QCheckBox("Enable Regional Swarms")
        
        self.spin_swarm_dist = QSpinBox()
        self.spin_swarm_dist.setRange(50, 5000)
        self.spin_swarm_dist.setSuffix(" km")
        self.spin_swarm_dist.setToolTip("The maximum Haversine distance allowed between two cameras for their identical species detections to merge into a single Swarm.")
        
        self.spin_swarm_padding = QDoubleSpinBox()
        self.spin_swarm_padding.setRange(0.1, 5.0)
        self.spin_swarm_padding.setDecimals(1)
        self.spin_swarm_padding.setSingleStep(0.1)
        self.spin_swarm_padding.setToolTip("The visual coordinate margin applied around the glowing cyan bounding box when zooming to a swarm.")
        
        self.spin_swarm_zoom = QSpinBox()
        self.spin_swarm_zoom.setRange(4, 12)
        self.spin_swarm_zoom.setToolTip("The absolute maximum zoom allowed when framing a swarm, preventing the map from zooming in too aggressively if the swarm covers a tiny area.")

        swarm_layout.addRow("", self.chk_enable_swarms)
        swarm_layout.addRow("Swarm Max Distance:", self.spin_swarm_dist)
        swarm_layout.addRow("Bounding Box Padding:", self.spin_swarm_padding)
        swarm_layout.addRow("Swarm Max Zoom Limit:", self.spin_swarm_zoom)
        map_layout.addWidget(swarm_group)
        
        self.chk_enable_swarms.toggled.connect(self.spin_swarm_dist.setEnabled)
        self.chk_enable_swarms.toggled.connect(self.spin_swarm_padding.setEnabled)
        self.chk_enable_swarms.toggled.connect(self.spin_swarm_zoom.setEnabled)

        map_layout.addStretch()
        self.tabs.addTab(self.tab_map, "Map Visualization & Grouping")


        self.layout.addWidget(self.tabs)

        # ==========================================
        # DIALOG BUTTONS
        # ==========================================
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
        
        self.cookies_file_edit.setText(self.cookie_config.get("youtube_cookies_file", ""))

        # Populate Grouping & Map Settings
        self.chk_group_feed.setChecked(self.map_config.get("group_feed_history", False))
        self.spin_group_age.setValue(self.map_config.get("max_grouping_age_mins", 60))
        self.spin_group_time_gap.setValue(self.map_config.get("group_time_gap_mins", 15))
        self.spin_group_age.setDisabled(self.chk_group_feed.isChecked())
        
        self.chk_enable_bursts.setChecked(self.map_config.get("enable_bursts", True))
        self.slider_elasticity.setValue(self.map_config.get("burst_elasticity", 50))
        self.spin_spiderify_radius.setValue(self.map_config.get("spiderify_radius", 0.025))
        self.spin_burst_zoom.setValue(self.map_config.get("burst_zoom_level", 13))
        self.slider_elasticity.setEnabled(self.chk_enable_bursts.isChecked())
        self.spin_spiderify_radius.setEnabled(self.chk_enable_bursts.isChecked())
        self.spin_burst_zoom.setEnabled(self.chk_enable_bursts.isChecked())
        
        self.chk_enable_swarms.setChecked(self.map_config.get("enable_swarms", True))
        self.spin_swarm_dist.setValue(self.map_config.get("swarm_max_distance_km", 500))
        self.spin_swarm_padding.setValue(self.map_config.get("swarm_bbox_padding", 1.5))
        self.spin_swarm_zoom.setValue(self.map_config.get("swarm_max_zoom", 6))
        self.spin_swarm_dist.setEnabled(self.chk_enable_swarms.isChecked())
        self.spin_swarm_padding.setEnabled(self.chk_enable_swarms.isChecked())
        self.spin_swarm_zoom.setEnabled(self.chk_enable_swarms.isChecked())

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
            "target_sidebar_capacity": self.spin_target_sidebar_capacity.value(),
            
            "group_feed_history": self.chk_group_feed.isChecked(),
            "max_grouping_age_mins": self.spin_group_age.value(),
            "group_time_gap_mins": self.spin_group_time_gap.value(),
            
            "enable_bursts": self.chk_enable_bursts.isChecked(),
            "burst_elasticity": self.slider_elasticity.value(),
            "spiderify_radius": self.spin_spiderify_radius.value(),
            "burst_zoom_level": self.spin_burst_zoom.value(),
            
            "enable_swarms": self.chk_enable_swarms.isChecked(),
            "swarm_max_distance_km": self.spin_swarm_dist.value(),
            "swarm_bbox_padding": self.spin_swarm_padding.value(),
            "swarm_max_zoom": self.spin_swarm_zoom.value()
        }
        
        return updated_map_config, updated_distance_config, updated_cookie_config, self.debug_enabled