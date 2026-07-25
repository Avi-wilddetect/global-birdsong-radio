# FILE: intelligence_hub_dialogs.py
# VERSION: 15.15 (The Atomic Reset Patch)
# RESPONSIBILITY: Stores all pop-up dialogs for the Intelligence Hub.
# UPDATED: The "Reset Exhausted" button now performs an immediate, atomic write directly to birdnet_config.json on the hard drive, bypassing all GUI save buffers. This permanently prevents background engine threads from overwriting the reset with stale memory.

import json
import time
import os
from pathlib import Path
from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QPushButton, 
                             QDialog, QFormLayout, QSpinBox, QDoubleSpinBox, 
                             QComboBox, QDialogButtonBox, QTableWidget, 
                             QTableWidgetItem, QHeaderView, QTabWidget,
                             QCheckBox, QLineEdit, QListWidget, QAbstractItemView, QListWidgetItem, QWidget, QGroupBox, QTextEdit, QMessageBox)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor

# --- CONFIGURATION ---
ROOT = Path(__file__).resolve().parent
HUB_SETTINGS_FILE = ROOT / "intelligence_hub_settings.json"
CONFIG_FILE = ROOT / "birdnet_config.json"

# NEW V40.0 SCALES
FRAME_LEVELS =["Massive", "Huge", "Large", "Medium", "Small", "Tiny", "Speck"]
DEPTH_LEVELS =["Point Blank", "Very Near", "Near", "Mid-ground", "Background", "Deep Background", "Horizon"]


# --- COMBOBOX HELPERS ---
def populate_size_combo(combo, defaults_list, levels_list, is_depth=False):
    """Populates a combobox with user-friendly formatting but stores the raw value as ItemData."""
    for d in defaults_list:
        combo.addItem(d, d)
    for i, val in enumerate(levels_list):
        num = 7 - i
        if is_depth:
            tag = " (Closest/Strictest)" if i == 0 else (" (Farthest/Loosest)" if i == len(levels_list)-1 else "")
        else:
            tag = " (Strictest)" if i == 0 else (" (Loosest)" if i == len(levels_list)-1 else "")
        # Add formatted text, but store raw 'val' in the Qt.ItemDataRole.UserRole
        combo.addItem(f"{num} - {val}{tag}", val)

def set_combo_by_data(combo, target_data):
    """Selects a combobox item by matching its hidden ItemData instead of the visible text."""
    if not target_data:
        combo.setCurrentIndex(0)
        return
    target_str = str(target_data).lower()
    for idx in range(combo.count()):
        c_data = combo.itemData(idx)
        if c_data and str(c_data).lower() == target_str:
            combo.setCurrentIndex(idx)
            return
    combo.setCurrentIndex(0)


class StreamOverrideDialog(QDialog):
    def __init__(self, stream_name, current_overrides, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"AI Size & Environment Overrides: {stream_name}")
        self.setMinimumWidth(450)
        self.layout = QVBoxLayout(self)

        form = QFormLayout()

        # --- ENVIRONMENT DROPDOWN ---
        self.combo_env = QComboBox()
        self.combo_env.addItems(["Auto-Detect", "Surface (Land/Air)", "Underwater", "Hybrid/Variable"])
        self.combo_env.setToolTip("Overrides the AI's Night/Low-Vis detection penalty. 'Underwater' allows hazy/blue footage without triggering night mode.")

        self.chk_single = QCheckBox("Override Minimum Size (Singles)")
        self.combo_f_s = QComboBox()
        populate_size_combo(self.combo_f_s,["Default (Auto)"], FRAME_LEVELS)
        self.combo_d_s = QComboBox()
        populate_size_combo(self.combo_d_s,["Default (Auto)"], DEPTH_LEVELS, is_depth=True)
        
        self.chk_flock_dist = QCheckBox("Override Minimum Size (Flock/Herd)")
        self.combo_f_f = QComboBox()
        populate_size_combo(self.combo_f_f,["Default (Auto)"], FRAME_LEVELS)
        self.combo_d_f = QComboBox()
        populate_size_combo(self.combo_d_f,["Default (Auto)"], DEPTH_LEVELS, is_depth=True)
        
        self.chk_flock = QCheckBox("Override 'Flock' Minimum Count")
        self.spin_flock = QSpinBox(); self.spin_flock.setRange(2, 50)

        # Load Existing Environment
        set_combo_by_data(self.combo_env, current_overrides.get("environment_type", "Auto-Detect"))

        # Load Existing or Disable
        if "min_frame_single" in current_overrides or "min_depth_single" in current_overrides:
            self.chk_single.setChecked(True)
            set_combo_by_data(self.combo_f_s, current_overrides.get("min_frame_single", "Default (Auto)"))
            set_combo_by_data(self.combo_d_s, current_overrides.get("min_depth_single", "Default (Auto)"))
        else:
            self.combo_f_s.setEnabled(False)
            self.combo_d_s.setEnabled(False)

        if "min_frame_flock" in current_overrides or "min_depth_flock" in current_overrides:
            self.chk_flock_dist.setChecked(True)
            set_combo_by_data(self.combo_f_f, current_overrides.get("min_frame_flock", "Default (Auto)"))
            set_combo_by_data(self.combo_d_f, current_overrides.get("min_depth_flock", "Default (Auto)"))
        else:
            self.combo_f_f.setEnabled(False)
            self.combo_d_f.setEnabled(False)

        if "flock_minimum_count" in current_overrides:
            self.chk_flock.setChecked(True)
            self.spin_flock.setValue(current_overrides["flock_minimum_count"])
        else:
            self.spin_flock.setValue(4)
            self.spin_flock.setEnabled(False)

        def toggle_single(checked):
            self.combo_f_s.setEnabled(checked); self.combo_d_s.setEnabled(checked)
        def toggle_flock(checked):
            self.combo_f_f.setEnabled(checked); self.combo_d_f.setEnabled(checked)

        self.chk_single.toggled.connect(toggle_single)
        self.chk_flock_dist.toggled.connect(toggle_flock)
        self.chk_flock.toggled.connect(self.spin_flock.setEnabled)

        form.addRow("Environment Type:", self.combo_env)
        form.addRow(QLabel("<hr>"))
        form.addRow(self.chk_single, QLabel(""))
        form.addRow("Min Frame Size (Single):", self.combo_f_s)
        form.addRow("Min Depth (Single):", self.combo_d_s)
        form.addRow(QLabel("<hr>"))
        form.addRow(self.chk_flock_dist, QLabel(""))
        form.addRow("Min Frame Size (Flock):", self.combo_f_f)
        form.addRow("Min Depth (Flock):", self.combo_d_f)
        form.addRow(QLabel("<hr>"))
        form.addRow(self.chk_flock, self.spin_flock)

        self.layout.addLayout(form)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.layout.addWidget(self.buttons)

    def get_overrides(self):
        res = {}
        
        if self.combo_env.currentText() != "Auto-Detect":
            res["environment_type"] = self.combo_env.currentText()

        if self.chk_single.isChecked():
            if self.combo_f_s.currentData() != "Default (Auto)": res["min_frame_single"] = self.combo_f_s.currentData()
            if self.combo_d_s.currentData() != "Default (Auto)": res["min_depth_single"] = self.combo_d_s.currentData()
            
        if self.chk_flock_dist.isChecked():
            if self.combo_f_f.currentData() != "Default (Auto)": res["min_frame_flock"] = self.combo_f_f.currentData()
            if self.combo_d_f.currentData() != "Default (Auto)": res["min_depth_flock"] = self.combo_d_f.currentData()
            
        if self.chk_flock.isChecked(): 
            res["flock_minimum_count"] = self.spin_flock.value()
        return res


class AnimalTuningDialog(QDialog):
    def __init__(self, stream_name, registry_animals, day_rules, night_rules, preselect_animal=None, preselect_night=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Tune Specific Animal: {stream_name}")
        self.setMinimumWidth(500)
        self.layout = QVBoxLayout(self)

        self.day_rules = day_rules
        self.night_rules = night_rules

        form = QFormLayout()

        self.chk_night_mode = QCheckBox("Apply to Night/Low-Vis Mode instead of Daytime")
        self.chk_night_mode.setStyleSheet("font-weight: bold; color: #b39ddb;")
        self.chk_night_mode.setChecked(preselect_night)
        self.chk_night_mode.toggled.connect(lambda: self.on_animal_changed(self.combo_animal.currentText()))

        self.combo_animal = QComboBox()
        self.combo_animal.addItems(sorted(registry_animals))

        self.combo_f_s = QComboBox()
        populate_size_combo(self.combo_f_s, ["Don't change (Use Global Defaults)"], FRAME_LEVELS)
        self.combo_d_s = QComboBox()
        populate_size_combo(self.combo_d_s,["Don't change (Use Global Defaults)"], DEPTH_LEVELS, is_depth=True)
        self.combo_f_f = QComboBox()
        populate_size_combo(self.combo_f_f,["Don't change (Use Global Defaults)"], FRAME_LEVELS)
        self.combo_d_f = QComboBox()
        populate_size_combo(self.combo_d_f,["Don't change (Use Global Defaults)"], DEPTH_LEVELS, is_depth=True)

        self.edit_prompt = QLineEdit()
        self.edit_prompt.setPlaceholderText("e.g. 'Ignore the raccoon-shaped rock on the right.'")

        self.combo_animal.currentTextChanged.connect(self.on_animal_changed)

        form.addRow("", self.chk_night_mode)
        form.addRow("Select Animal:", self.combo_animal)
        form.addRow(QLabel("<hr>"))
        form.addRow("Min Frame Size (Single):", self.combo_f_s)
        form.addRow("Min Depth (Single):", self.combo_d_s)
        form.addRow("Min Frame Size (Flock):", self.combo_f_f)
        form.addRow("Min Depth (Flock):", self.combo_d_f)
        form.addRow(QLabel("<hr>"))
        form.addRow("Custom Negative Prompt:", self.edit_prompt)

        self.layout.addLayout(form)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.layout.addWidget(self.buttons)

        if preselect_animal:
            idx = self.combo_animal.findText(preselect_animal)
            if idx >= 0:
                self.combo_animal.setCurrentIndex(idx)
        elif registry_animals:
            self.on_animal_changed(registry_animals[0])
            
        # Explicit trigger to ensure correct rule set is loaded on start
        self.on_animal_changed(self.combo_animal.currentText())

    def on_animal_changed(self, animal):
        if not animal: return
        rules = self.night_rules.get(animal, {}) if self.chk_night_mode.isChecked() else self.day_rules.get(animal, {})
        
        set_combo_by_data(self.combo_f_s, rules.get("min_frame_single", "Don't change (Use Global Defaults)"))
        set_combo_by_data(self.combo_d_s, rules.get("min_depth_single", "Don't change (Use Global Defaults)"))
        set_combo_by_data(self.combo_f_f, rules.get("min_frame_flock", "Don't change (Use Global Defaults)"))
        set_combo_by_data(self.combo_d_f, rules.get("min_depth_flock", "Don't change (Use Global Defaults)"))
        
        self.edit_prompt.setText(rules.get("prompt", ""))

    def get_tuning(self):
        return (
            self.combo_animal.currentText(),
            self.combo_f_s.currentData(),
            self.combo_d_s.currentData(),
            self.combo_f_f.currentData(),
            self.combo_d_f.currentData(),
            self.edit_prompt.text().strip(),
            self.chk_night_mode.isChecked()
        )


class GlobalAudioSettingsDialog(QDialog):
    def __init__(self, current_globals, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Global Bioacoustic Defaults")
        self.setMinimumWidth(400)
        self.layout = QVBoxLayout(self)
        
        form = QFormLayout()
        self.spin_target = QSpinBox(); self.spin_target.setRange(0, 9999); self.spin_target.setValue(current_globals.get("cooldown_target_minutes", 60))
        self.spin_stream = QSpinBox(); self.spin_stream.setRange(0, 9999); self.spin_stream.setValue(current_globals.get("cooldown_stream_minutes", 0))
        self.spin_species = QSpinBox(); self.spin_species.setRange(0, 9999); self.spin_species.setValue(current_globals.get("cooldown_species_minutes", 0))
        
        self.combo_snr = QComboBox()
        self.combo_snr.addItems(["Disabled (Use Bio-Match)", "Enabled (True Distance Math)"])
        self.combo_snr.setCurrentIndex(1 if current_globals.get("enable_adaptive_snr", False) else 0)
        
        form.addRow("Default Target Cooldown (mins):", self.spin_target)
        form.addRow("Default Stream Mute (mins):", self.spin_stream)
        form.addRow("Default Species Global Mute (mins):", self.spin_species)
        form.addRow("Default Adaptive SNR:", self.combo_snr)
        
        self.layout.addLayout(form)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.layout.addWidget(self.buttons)
        
        try:
            if HUB_SETTINGS_FILE.exists():
                s = json.loads(HUB_SETTINGS_FILE.read_text())
                g = s.get('audio_settings_geometry')
                if g: self.setGeometry(g['x'], g['y'], g['w'], g['h'])
        except: pass

    def closeEvent(self, event):
        try:
            s = json.loads(HUB_SETTINGS_FILE.read_text()) if HUB_SETTINGS_FILE.exists() else {}
            geom = self.geometry()
            s['audio_settings_geometry'] = {'x': geom.x(), 'y': geom.y(), 'w': geom.width(), 'height': geom.height()}
            HUB_SETTINGS_FILE.write_text(json.dumps(s, indent=2))
        except: pass
        super().closeEvent(event)

    def get_values(self):
        return {
            "cooldown_target_minutes": self.spin_target.value(),
            "cooldown_stream_minutes": self.spin_stream.value(),
            "cooldown_species_minutes": self.spin_species.value(),
            "enable_adaptive_snr": self.combo_snr.currentIndex() == 1
        }


class GlobalVisionSettingsDialog(QDialog):
    def __init__(self, current_globals, vision_defaults, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Global Vision AI Defaults & Maintenance")
        self.setMinimumWidth(800)
        self.main_window = parent
        self.layout = QVBoxLayout(self)
        
        self.tabs = QTabWidget()

        # ==========================================
        # TAB 1: AI REASONING
        # ==========================================
        tab1 = QWidget()
        tab1_layout = QVBoxLayout(tab1)

        ai_group = QGroupBox("AI Reasoning & Spatial Filters")
        ai_form = QFormLayout()

        # --- THE MASTER SYSTEM PROMPT ---
        self.edit_master_prompt = QTextEdit()
        self.edit_master_prompt.setPlaceholderText("e.g. ANTI-PAREIDOLIA GUARDRAIL: You are looking at a wildlife camera...")
        self.edit_master_prompt.setFixedHeight(80)
        self.edit_master_prompt.setPlainText(vision_defaults.get("master_system_prompt", ""))
        self.edit_master_prompt.setToolTip("Global prompt injected into EVERY camera before specific rules. Leave blank to use system default anti-pareidolia.")

        # --- THE FORBIDDEN WORDS INPUT ---
        self.edit_forbidden_words = QLineEdit()
        self.edit_forbidden_words.setPlaceholderText("e.g. unidentified, unknown, or, likely, possible")
        current_forbidden = vision_defaults.get("forbidden_words",[])
        self.edit_forbidden_words.setText(", ".join(current_forbidden))
        self.edit_forbidden_words.setToolTip("Comma-separated list of words the AI is absolutely forbidden from using in species names.")

        self.spin_temp = QDoubleSpinBox()
        self.spin_temp.setRange(0.0, 1.0)
        self.spin_temp.setSingleStep(0.05)
        self.spin_temp.setValue(current_globals.get("ai_temperature", 0.15))
        self.spin_temp.setToolTip("Lower = Robotic/Strict. Higher = Creative/Loose.")

        self.spin_speck = QSpinBox()
        self.spin_speck.setRange(1, 20)
        self.spin_speck.setSuffix("% of frame")
        self.spin_speck.setValue(current_globals.get("speck_size_percent", 3))

        self.spin_flock = QSpinBox()
        self.spin_flock.setRange(2, 50)
        self.spin_flock.setValue(current_globals.get("flock_minimum_count", 4))

        self.combo_min_f_s = QComboBox()
        populate_size_combo(self.combo_min_f_s,[], FRAME_LEVELS)
        set_combo_by_data(self.combo_min_f_s, current_globals.get("min_frame_single", "Large"))
        
        self.combo_min_d_s = QComboBox()
        populate_size_combo(self.combo_min_d_s,[], DEPTH_LEVELS, is_depth=True)
        set_combo_by_data(self.combo_min_d_s, current_globals.get("min_depth_single", "Near"))
        
        self.combo_min_f_f = QComboBox()
        populate_size_combo(self.combo_min_f_f,[], FRAME_LEVELS)
        set_combo_by_data(self.combo_min_f_f, current_globals.get("min_frame_flock", "Small"))
        
        self.combo_min_d_f = QComboBox()
        populate_size_combo(self.combo_min_d_f,[], DEPTH_LEVELS, is_depth=True)
        set_combo_by_data(self.combo_min_d_f, current_globals.get("min_depth_flock", "Background"))

        self.chk_ignore_general = QCheckBox("Ignore 'General' animal detections globally")
        self.chk_ignore_general.setChecked(vision_defaults.get("ignore_general_animals", False))
        
        self.spin_default_mute = QSpinBox()
        self.spin_default_mute.setRange(0, 99999)
        self.spin_default_mute.setValue(vision_defaults.get("default_taxonomy_mute", 60))
        self.spin_default_mute.setSuffix(" mins")

        ai_form.addRow("Master System Prompt:", self.edit_master_prompt)
        ai_form.addRow("Forbidden Name Words (CSV):", self.edit_forbidden_words)
        ai_form.addRow("AI Creativity (Temperature):", self.spin_temp)
        ai_form.addRow("Minimum 'Speck' Size:", self.spin_speck)
        ai_form.addRow("Flock/Herd Minimum Count:", self.spin_flock)
        ai_form.addRow(QLabel("<hr>"))
        ai_form.addRow("Default Frame Size (Singles):", self.combo_min_f_s)
        ai_form.addRow("Default Depth (Singles):", self.combo_min_d_s)
        ai_form.addRow("Default Frame Size (Flock/Herd):", self.combo_min_f_f)
        ai_form.addRow("Default Depth (Flock/Herd):", self.combo_min_d_f)
        ai_form.addRow(QLabel("<hr>"))
        ai_form.addRow("Default Taxonomy Mute (New Animals):", self.spin_default_mute)
        ai_form.addRow("", self.chk_ignore_general)
        
        ai_group.setLayout(ai_form)
        tab1_layout.addWidget(ai_group)
        tab1_layout.addStretch()
        
        self.tabs.addTab(tab1, "AI Reasoning & Filters")


        # ==========================================
        # TAB 2: ENGINE & MAINTENANCE
        # ==========================================
        tab2 = QWidget()
        tab2_layout = QVBoxLayout(tab2)

        params_group = QGroupBox("Engine Parameters")
        form = QFormLayout()
        
        self.spin_vision_workers = QSpinBox()
        self.spin_vision_workers.setRange(1, 10)
        self.spin_vision_workers.setValue(current_globals.get("vision_workers", 1))
        self.spin_vision_workers.setToolTip("Number of cameras the Vision Engine will analyze simultaneously.\nHigher = Faster cycle times, but uses more CPU/RAM and Network Bandwidth.\nRecommended: 1 to 3.")
        
        self.spin_predator = QSpinBox()
        self.spin_predator.setRange(1, 100)
        self.spin_predator.setValue(current_globals.get("predator_reflex_threshold", 85))
        self.spin_predator.setSuffix("% confidence")
        self.spin_predator.setToolTip("Minimum BirdNET confidence required to interrupt the Vision Engine's normal patrol and force an immediate visual scan of the camera. (Default: 85%)")

        self.spin_vision_interval = QSpinBox(); self.spin_vision_interval.setRange(10, 3600); self.spin_vision_interval.setValue(current_globals.get("cycle_interval_seconds", 60)); self.spin_vision_interval.setSuffix(" seconds")
        self.chk_use_motion = QCheckBox("Enable Local Motion Trigger (Saves API Quota)")
        self.chk_use_motion.setChecked(current_globals.get("use_motion_detector", True)); self.chk_use_motion.setStyleSheet("font-weight: bold; color: #4CAF50;")
        
        self.spin_dormancy_threshold = QSpinBox()
        self.spin_dormancy_threshold.setRange(0, 1440)
        self.spin_dormancy_threshold.setValue(current_globals.get("vision_dormancy_threshold_mins", 60))
        self.spin_dormancy_threshold.setSuffix(" mins (0 = Disabled)")
        self.spin_dormancy_threshold.setToolTip("If a stream has NO detections (Audio or Vision) for this many minutes, it enters 'Dormant' mode to save API quota, CPU, and bandwidth.")

        self.spin_dormant_interval = QSpinBox()
        self.spin_dormant_interval.setRange(1, 1440)
        self.spin_dormant_interval.setValue(current_globals.get("vision_dormant_interval_mins", 20))
        self.spin_dormant_interval.setSuffix(" mins")
        self.spin_dormant_interval.setToolTip("How often to check a Dormant stream. If it hears/sees life, it instantly wakes up to full speed.")

        self.spin_motion_sens = QDoubleSpinBox(); self.spin_motion_sens.setRange(0.1, 100.0); self.spin_motion_sens.setSingleStep(0.5); self.spin_motion_sens.setSuffix("% screen change"); self.spin_motion_sens.setValue(current_globals.get("motion_sensitivity_percent", 5.0))
        self.chk_use_motion.toggled.connect(self.spin_motion_sens.setEnabled); self.spin_motion_sens.setEnabled(self.chk_use_motion.isChecked())
        
        self.spin_vision_retention = QSpinBox(); self.spin_vision_retention.setRange(1, 100); self.spin_vision_retention.setValue(current_globals.get("retention_limit", 5)); self.spin_vision_retention.setSuffix(" images max per stream")
        self.chk_hide_console = QCheckBox("Hide Vision Console on Start"); self.chk_hide_console.setChecked(current_globals.get("hide_console", False))
        
        form.addRow("Vision Workers (Concurrent):", self.spin_vision_workers)
        form.addRow("Predator Reflex Acoustic Threshold:", self.spin_predator)
        form.addRow("Scan Interval:", self.spin_vision_interval)
        form.addRow(QLabel("<hr>"))
        form.addRow("Dormancy Threshold (Idle Time):", self.spin_dormancy_threshold)
        form.addRow("Dormant Scan Interval:", self.spin_dormant_interval)
        form.addRow(QLabel("<hr>"))
        form.addRow("", self.chk_use_motion)
        form.addRow("Motion Sensitivity:", self.spin_motion_sens)
        form.addRow("Vault Retention Limit:", self.spin_vision_retention)
        form.addRow("", self.chk_hide_console)
        params_group.setLayout(form); tab2_layout.addWidget(params_group)

        # --- THE NEW TELEGRAM ALERT PREFERENCES ---
        telegram_group = QGroupBox("Telegram Alert Preferences (Visual AI & Multimodal)")
        telegram_group.setStyleSheet("QGroupBox { border: 1px solid #03A9F4; margin-top: 15px; color: #03A9F4; font-weight: bold; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }")
        telegram_layout = QVBoxLayout(telegram_group)
        
        tg_prefs = current_globals.get("telegram_alerts", {})
        
        self.chk_tg_master = QCheckBox("Enable Telegram Alerts from the Vision Engine")
        self.chk_tg_master.setStyleSheet("font-weight: bold; font-size: 13px; color: white;")
        self.chk_tg_master.setChecked(tg_prefs.get("enabled", True))
        
        self.chk_tg_multi = QCheckBox("Alert on Multimodal Confirmations (🔥 Audio + Vision)")
        self.chk_tg_multi.setChecked(tg_prefs.get("alert_multimodal", True))
        
        self.chk_tg_public = QCheckBox("Alert on Public Vision Detections (📷 Sent to Web Map)")
        self.chk_tg_public.setChecked(tg_prefs.get("alert_public", True))
        
        self.chk_tg_filtered = QCheckBox("Alert on Filtered/Blocked Detections (🛑 Admin Debugging)")
        self.chk_tg_filtered.setChecked(tg_prefs.get("alert_filtered", False))
        self.chk_tg_filtered.setStyleSheet("color: #FFB74D;")
        
        self.chk_tg_image = QCheckBox("Attach Image (.jpg) to the Alert")
        self.chk_tg_image.setChecked(tg_prefs.get("attach_image", True))
        
        self.chk_tg_reasoning = QCheckBox("Include AI Diagnostic Notes & Features in Caption")
        self.chk_tg_reasoning.setChecked(tg_prefs.get("include_reasoning", True))
        
        def toggle_tg_options(checked):
            self.chk_tg_multi.setEnabled(checked)
            self.chk_tg_public.setEnabled(checked)
            self.chk_tg_filtered.setEnabled(checked)
            self.chk_tg_image.setEnabled(checked)
            self.chk_tg_reasoning.setEnabled(checked)
            
        self.chk_tg_master.toggled.connect(toggle_tg_options)
        toggle_tg_options(self.chk_tg_master.isChecked())

        telegram_layout.addWidget(self.chk_tg_master)
        telegram_layout.addWidget(QLabel("<hr>"))
        telegram_layout.addWidget(self.chk_tg_multi)
        telegram_layout.addWidget(self.chk_tg_public)
        telegram_layout.addWidget(self.chk_tg_filtered)
        telegram_layout.addWidget(QLabel("<hr>"))
        telegram_layout.addWidget(self.chk_tg_image)
        telegram_layout.addWidget(self.chk_tg_reasoning)
        
        tab2_layout.addWidget(telegram_group)

        cleaning_group = QGroupBox("Maintenance & Automated Cleaning")
        cleaning_layout = QFormLayout()
        self.spin_sweep_vault = QSpinBox(); self.spin_sweep_vault.setRange(1, 720); self.spin_sweep_vault.setValue(current_globals.get("auto_sweep_vault_hours", 24)); self.spin_sweep_vault.setSuffix(" hours")
        self.spin_log_retention = QSpinBox(); self.spin_log_retention.setRange(1, 720); self.spin_log_retention.setValue(current_globals.get("log_retention_hours", 48)); self.spin_log_retention.setSuffix(" hours")
        cleaning_layout.addRow("Auto-Sweep Orphaned Snapshots:", self.spin_sweep_vault); cleaning_layout.addRow("Auto-Clear Vision Log:", self.spin_log_retention)
        
        btn_sweep_vault = QPushButton("🧹 Sweep Orphaned Snapshots Now"); btn_sweep_vault.clicked.connect(self.main_window.sweep_orphaned_snapshots)
        btn_clear_vision_log = QPushButton("📄 Clear Vision Log Now"); btn_clear_vision_log.clicked.connect(self.main_window.clear_vision_log)
        manual_clean_layout = QHBoxLayout(); manual_clean_layout.addWidget(btn_sweep_vault); manual_clean_layout.addWidget(btn_clear_vision_log)
        cleaning_layout.addRow(manual_clean_layout)
        cleaning_group.setLayout(cleaning_layout); tab2_layout.addWidget(cleaning_group)
        
        tab2_layout.addStretch()
        self.tabs.addTab(tab2, "Engine & Maintenance")


        # ==========================================
        # TAB 3: API KEY VAULT & DATA THROTTLE
        # ==========================================
        tab3 = QWidget()
        tab3_layout = QVBoxLayout(tab3)

        vault_info = QLabel("The engine uses the top key first. If a Free key hits its daily limit, it is tagged 'Exhausted' and the engine seamlessly switches to the next key.")
        vault_info.setWordWrap(True); vault_info.setStyleSheet("color: #aaa; font-style: italic; margin-bottom: 10px;")
        tab3_layout.addWidget(vault_info)
        
        self.keys_table = QTableWidget()
        self.keys_table.setColumnCount(5)
        self.keys_table.setHorizontalHeaderLabels(["API Key", "Tier", "Status", "Requests", "Est. Cost"])
        self.keys_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.keys_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.keys_table.setMaximumHeight(200)
        tab3_layout.addWidget(self.keys_table)
        
        btn_layout = QHBoxLayout()
        self.btn_add_key = QPushButton("+ Add Key"); self.btn_add_key.clicked.connect(self.add_key_row)
        self.btn_remove_key = QPushButton("- Remove Key"); self.btn_remove_key.clicked.connect(self.remove_key_row)
        
        # --- THE RESET EXHAUSTED BUTTON ---
        self.btn_reset_keys = QPushButton("🔄 Reset Exhausted")
        self.btn_reset_keys.setStyleSheet("background-color: #F57C00; color: white; font-weight: bold;")
        self.btn_reset_keys.clicked.connect(self.reset_exhausted_keys)
        self.btn_reset_keys.setToolTip("Manually clear the 24-hour timeout memory for all 'Exhausted' keys and drop their usage counts to zero.")
        
        btn_layout.addWidget(self.btn_add_key)
        btn_layout.addWidget(self.btn_remove_key)
        btn_layout.addWidget(self.btn_reset_keys)
        tab3_layout.addLayout(btn_layout)

        # --- DATA THROTTLE PANEL ---
        throttle_group = QGroupBox("Visual Data Throttle & Network Fallback")
        throttle_group.setStyleSheet("QGroupBox { border: 1px solid #FF9800; margin-top: 10px; color: #FF9800; font-weight: bold; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }")
        throttle_layout = QFormLayout()

        throttle_desc = QLabel("<b>Why am I bleeding data?</b> If a SIM/Proxy drops temporarily, Windows automatically forces the massive video download through your default OS Gateway (Home Router). Use these controls to stop the leak.")
        throttle_desc.setWordWrap(True)
        throttle_desc.setStyleSheet("color: #ccc; margin-bottom: 5px;")
        
        self.combo_resolution = QComboBox()
        self.combo_resolution.addItems([
            "1080p (Max Detail, Extreme Data)",
            "720p (Recommended Balance)",
            "480p (Eco-Mode, Low Data)",
            "360p (Strict Minimum Data)"
        ])
        self.combo_resolution.setToolTip("Capping resolution to 720p or 480p cuts data usage by 50-80% without seriously impacting Gemini's ability to see animals.")
        
        curr_res = current_globals.get("vision_resolution", "720p")
        for i in range(self.combo_resolution.count()):
            if curr_res in self.combo_resolution.itemText(i):
                self.combo_resolution.setCurrentIndex(i)
                break

        self.chk_strict_proxy = QCheckBox("Strict Proxy Enforcement (Block Windows OS Fallback)")
        self.chk_strict_proxy.setChecked(current_globals.get("strict_proxy", False))
        self.chk_strict_proxy.setToolTip("If checked, when a SIM proxy times out, the Engine will safely abort the scan rather than downloading the video on your Home Router.")

        throttle_layout.addRow(throttle_desc)
        throttle_layout.addRow("Video Resolution Cap:", self.combo_resolution)
        throttle_layout.addRow("", self.chk_strict_proxy)
        
        throttle_group.setLayout(throttle_layout)
        tab3_layout.addWidget(throttle_group)

        import copy
        self.api_keys_data = copy.deepcopy(current_globals.get("api_keys",[]))
        if not self.api_keys_data and current_globals.get("api_key"):
            self.api_keys_data =[{"key": current_globals.get("api_key"), "tier": "Free", "status": "Active", "usage_count": 0, "exhausted_until": 0}]
        self.refresh_keys_table()
        tab3_layout.addStretch()
        
        self.tabs.addTab(tab3, "API Vault & Data Throttle")

        self.layout.addWidget(self.tabs)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept); self.buttons.rejected.connect(self.reject)
        self.layout.addWidget(self.buttons)
        
        try:
            if HUB_SETTINGS_FILE.exists():
                s = json.loads(HUB_SETTINGS_FILE.read_text())
                g = s.get('vision_settings_geometry')
                if g: 
                    self.resize(g['w'], g['h'])
        except: pass

    def closeEvent(self, event):
        try:
            s = json.loads(HUB_SETTINGS_FILE.read_text()) if HUB_SETTINGS_FILE.exists() else {}
            geom = self.geometry()
            s['vision_settings_geometry'] = {'x': geom.x(), 'y': geom.y(), 'w': geom.width(), 'height': geom.height()}
            HUB_SETTINGS_FILE.write_text(json.dumps(s, indent=2))
        except: pass
        super().closeEvent(event)

    def refresh_keys_table(self):
        self.keys_table.setRowCount(len(self.api_keys_data))
        for i, kdata in enumerate(self.api_keys_data):
            key_item = QTableWidgetItem(kdata.get("key", "")); self.keys_table.setItem(i, 0, key_item)
            combo = QComboBox(); combo.addItems(["Free", "Paid"]); combo.setCurrentText(kdata.get("tier", "Free"))
            combo.currentIndexChanged.connect(lambda idx, r=i: self.update_cost_display(r)); self.keys_table.setCellWidget(i, 1, combo)
            status = kdata.get("status", "Active"); status_item = QTableWidgetItem(status); status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if status == "Exhausted":
                status_item.setForeground(QBrush(QColor("#EF5350"))); font = status_item.font(); font.setBold(True); status_item.setFont(font)
            else:
                status_item.setForeground(QBrush(QColor("#00E676")))
            self.keys_table.setItem(i, 2, status_item)
            usage = kdata.get("usage_count", 0); usage_item = QTableWidgetItem(str(usage)); usage_item.setFlags(usage_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.keys_table.setItem(i, 3, usage_item)
            key_item.setData(Qt.ItemDataRole.UserRole, kdata); self.update_cost_display(i)

    def update_cost_display(self, row):
        combo = self.keys_table.cellWidget(row, 1)
        if not combo: return
        tier = combo.currentText(); usage_item = self.keys_table.item(row, 3); usage = int(usage_item.text()) if usage_item else 0
        cost_str = f"${usage * 0.00005:.4f}" if tier == "Paid" else "$0.00"
        cost_item = QTableWidgetItem(cost_str); cost_item.setFlags(cost_item.flags() & ~Qt.ItemFlag.ItemIsEditable); self.keys_table.setItem(row, 4, cost_item)

    def add_key_row(self): self.api_keys_data.append({"key": "", "tier": "Free", "status": "Active", "usage_count": 0, "exhausted_until": 0}); self.refresh_keys_table()
    
    def remove_key_row(self):
        sel = self.keys_table.selectedItems()
        if not sel: return
        for r in sorted(list(set([item.row() for item in sel])), reverse=True): self.api_keys_data.pop(r)
        self.refresh_keys_table()
        
    def reset_exhausted_keys(self):
        msg = "Are you sure you want to force-reset all 'Exhausted' keys back to 'Active'?\n\nIf Google's 24-hour daily quota ban hasn't actually expired for your free accounts, they will just fail again."
        if QMessageBox.question(self, "Confirm Reset", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            reset_count = 0
            # Iterate through the VISUAL table rows
            for row in range(self.keys_table.rowCount()):
                status_item = self.keys_table.item(row, 2)
                if status_item and status_item.text() == "Exhausted":
                    # --- THE ATOMIC WRITE PATCH ---
                    # Physically update the table cell so it looks green to the user
                    status_item.setText("Active")
                    status_item.setForeground(QBrush(QColor("#00E676")))
                    font = status_item.font()
                    font.setBold(False)
                    status_item.setFont(font)
                    
                    # Zero out the usage count cell
                    usage_item = self.keys_table.item(row, 3)
                    if usage_item:
                        usage_item.setText("0")
                        
                    # Update underlying data just in case
                    kdata = self.keys_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                    if kdata:
                        kdata["status"] = "Active"
                        kdata["exhausted_until"] = 0
                        kdata["usage_count"] = 0
                        
                    reset_count += 1
                    
            if reset_count > 0:
                # INSTANT ATOMIC WRITE TO DISK
                try:
                    if CONFIG_FILE.exists():
                        cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                        keys = cfg.setdefault("vision_ai", {}).get("api_keys", [])
                        for k in keys:
                            if k.get("status") == "Exhausted":
                                k["status"] = "Active"
                                k["exhausted_until"] = 0
                                k["usage_count"] = 0
                        
                        tmp_file = CONFIG_FILE.with_suffix('.tmp')
                        tmp_file.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
                        os.replace(tmp_file, CONFIG_FILE)
                except Exception as e:
                    QMessageBox.warning(self, "Warning", f"UI reset succeeded, but failed to write directly to disk: {e}")
                    
                QMessageBox.information(self, "Success", f"Successfully reset {reset_count} keys to Active status.\n\nThey have been automatically saved to the configuration file.")
            else:
                QMessageBox.information(self, "No Action", "No keys are currently marked as Exhausted on the screen.")

    def get_values(self):
        keys =[]
        for i in range(self.keys_table.rowCount()):
            key_text = self.keys_table.item(i, 0).text().strip()
            if not key_text: continue
            
            # --- THE VAULT SAVE STATE PATCH ---
            # Instead of trusting the hidden dictionary data which might not be updated,
            # we read the EXACT string currently displayed in the "Status" column of the UI table.
            status_item = self.keys_table.item(i, 2)
            visible_status = status_item.text().strip() if status_item else "Active"
            
            combo = self.keys_table.cellWidget(i, 1)
            tier = combo.currentText()
            
            kdata = self.keys_table.item(i, 0).data(Qt.ItemDataRole.UserRole)
            if not kdata: 
                kdata = {}
            kdata["key"] = key_text
            kdata["tier"] = tier
            
            kdata["status"] = visible_status
            
            if "usage_count" not in kdata: kdata["usage_count"] = 0
            if "exhausted_until" not in kdata: kdata["exhausted_until"] = 0
            keys.append(kdata)
            
        keys.sort(key=lambda x: 0 if x.get("tier") == "Free" else 1)
        
        fw_list =[w.strip() for w in self.edit_forbidden_words.text().split(",") if w.strip()]
        
        telegram_alerts_data = {
            "enabled": self.chk_tg_master.isChecked(),
            "alert_multimodal": self.chk_tg_multi.isChecked(),
            "alert_public": self.chk_tg_public.isChecked(),
            "alert_filtered": self.chk_tg_filtered.isChecked(),
            "attach_image": self.chk_tg_image.isChecked(),
            "include_reasoning": self.chk_tg_reasoning.isChecked()
        }
        
        return {
            "api_keys": keys, "api_key": keys[0]["key"] if keys else "",
            "vision_workers": self.spin_vision_workers.value(),
            "predator_reflex_threshold": self.spin_predator.value(),
            "cycle_interval_seconds": self.spin_vision_interval.value(), 
            "use_motion_detector": self.chk_use_motion.isChecked(),
            "motion_sensitivity_percent": self.spin_motion_sens.value(), 
            
            # --- THE DORMANCY CONTROLS ---
            "vision_dormancy_threshold_mins": self.spin_dormancy_threshold.value(),
            "vision_dormant_interval_mins": self.spin_dormant_interval.value(),
            
            "retention_limit": self.spin_vision_retention.value(),
            "log_retention_hours": self.spin_log_retention.value(), 
            "auto_sweep_vault_hours": self.spin_sweep_vault.value(),
            "hide_console": self.chk_hide_console.isChecked(),
            "ai_temperature": self.spin_temp.value(),
            "speck_size_percent": self.spin_speck.value(),
            "flock_minimum_count": self.spin_flock.value(),
            "min_frame_single": self.combo_min_f_s.currentData(),
            "min_depth_single": self.combo_min_d_s.currentData(),
            "min_frame_flock": self.combo_min_f_f.currentData(),
            "min_depth_flock": self.combo_min_d_f.currentData(),
            "vision_resolution": self.combo_resolution.currentText().split(" ")[0],
            "strict_proxy": self.chk_strict_proxy.isChecked(),
            "telegram_alerts": telegram_alerts_data
        }, { 
            "ignore_general_animals": self.chk_ignore_general.isChecked(),
            "default_taxonomy_mute": self.spin_default_mute.value(),
            "master_system_prompt": self.edit_master_prompt.toPlainText().strip(),
            "forbidden_words": fw_list
        }

class EditVisionRegistryDialog(QDialog):
    def __init__(self, name, data, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit Taxonomic Anchor: {name}")
        self.setMinimumWidth(550)
        self.layout = QVBoxLayout(self)
        form = QFormLayout()
        
        self.edit_name = QLineEdit(name)
        if data: self.edit_name.setEnabled(False)
        else: self.edit_name.setPlaceholderText("e.g. African Elephant")
            
        self.combo_type = QComboBox()
        self.combo_type.addItems(["Specific", "General"])
        if data and data.get("type") == "General": self.combo_type.setCurrentIndex(1)

        self.edit_synonyms = QLineEdit()
        if data: self.edit_synonyms.setText(", ".join(data.get("synonyms",[])))
        self.edit_synonyms.setPlaceholderText("e.g. panthera leo, african lion")

        self.combo_behavior = QComboBox()
        self.combo_behavior.addItems(["Alert", "Silent Log", "Ignore"])
        b = data.get("behavior", "Alert") if data else "Alert"
        if b == "Silent Log": self.combo_behavior.setCurrentIndex(1)
        elif b == "Ignore": self.combo_behavior.setCurrentIndex(2)

        self.spin_cooldown = QSpinBox()
        self.spin_cooldown.setRange(0, 99999)
        self.spin_cooldown.setValue(data.get("cooldown_minutes", 60) if data else 60)
        self.spin_cooldown.setSuffix(" mins")
        
        self.chk_public = QCheckBox("Broadcast to Public Web Map")
        self.chk_public.setChecked(data.get("show_on_map", True) if data else True)
        self.chk_public.setStyleSheet("font-weight: bold; color: #4CAF50;")

        self.edit_global_prompt = QLineEdit()
        if data: self.edit_global_prompt.setText(data.get("global_prompt", ""))
        self.edit_global_prompt.setPlaceholderText("e.g. 'Must have red tail'")

        self.combo_f_s = QComboBox()
        populate_size_combo(self.combo_f_s, ["Default (Auto)"], FRAME_LEVELS)
        set_combo_by_data(self.combo_f_s, data.get("min_frame_single", "Default (Auto)") if data else "Default (Auto)")
        
        self.combo_d_s = QComboBox()
        populate_size_combo(self.combo_d_s, ["Default (Auto)"], DEPTH_LEVELS, is_depth=True)
        set_combo_by_data(self.combo_d_s, data.get("min_depth_single", "Default (Auto)") if data else "Default (Auto)")
        
        self.combo_f_f = QComboBox()
        populate_size_combo(self.combo_f_f, ["Default (Auto)"], FRAME_LEVELS)
        set_combo_by_data(self.combo_f_f, data.get("min_frame_flock", "Default (Auto)") if data else "Default (Auto)")
        
        self.combo_d_f = QComboBox()
        populate_size_combo(self.combo_d_f,["Default (Auto)"], DEPTH_LEVELS, is_depth=True)
        set_combo_by_data(self.combo_d_f, data.get("min_depth_flock", "Default (Auto)") if data else "Default (Auto)")

        form.addRow("Canonical Name:", self.edit_name)
        form.addRow("Taxonomy Level:", self.combo_type)
        form.addRow("Synonyms (comma separated):", self.edit_synonyms)
        form.addRow(QLabel("<hr>"))
        form.addRow("Action Behavior:", self.combo_behavior)
        form.addRow("Global Cooldown:", self.spin_cooldown)
        form.addRow("", self.chk_public)
        form.addRow(QLabel("<hr>"))
        form.addRow("Global Prompt (Optional):", self.edit_global_prompt)
        form.addRow(QLabel("<hr>"))
        form.addRow("Min Frame Size (Single):", self.combo_f_s)
        form.addRow("Min Depth (Single):", self.combo_d_s)
        form.addRow("Min Frame Size (Flock):", self.combo_f_f)
        form.addRow("Min Depth (Flock):", self.combo_d_f)

        self.layout.addLayout(form)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.layout.addWidget(self.buttons)

    def get_data(self):
        syns =[s.strip().lower() for s in self.edit_synonyms.text().split(",") if s.strip()]
        
        res = {
            "type": self.combo_type.currentText(),
            "synonyms": syns,
            "behavior": self.combo_behavior.currentText(),
            "cooldown_minutes": self.spin_cooldown.value(),
            "show_on_map": self.chk_public.isChecked(),
            "global_prompt": self.edit_global_prompt.text().strip(),
            "min_frame_single": self.combo_f_s.currentData(),
            "min_depth_single": self.combo_d_s.currentData(),
            "min_frame_flock": self.combo_f_f.currentData(),
            "min_depth_flock": self.combo_d_f.currentData()
        }
            
        return self.edit_name.text().strip(), res


class BatchEditVisionRegistryDialog(QDialog):
    def __init__(self, count, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Batch Edit {count} Taxonomies")
        self.setMinimumWidth(450)
        self.layout = QVBoxLayout(self)
        
        info = QLabel(f"Applying settings to <b>{count}</b> selected animals.<br>Leave a field as 'Leave Unchanged' to keep its current value.")
        self.layout.addWidget(info)
        
        form = QFormLayout()
        
        self.combo_behavior = QComboBox()
        self.combo_behavior.addItems(["Leave Unchanged", "Alert", "Silent Log", "Ignore"])
        
        self.spin_cooldown = QSpinBox()
        self.spin_cooldown.setRange(-1, 99999)
        self.spin_cooldown.setValue(-1)
        self.spin_cooldown.setSpecialValueText("Leave Unchanged")
        self.spin_cooldown.setSuffix(" mins")
        
        self.combo_public = QComboBox()
        self.combo_public.addItems(["Leave Unchanged", "Public (Show on Map)", "Private (Hide)"])
        
        self.combo_f_s = QComboBox(); populate_size_combo(self.combo_f_s, ["Leave Unchanged", "Default (Auto)"], FRAME_LEVELS)
        self.combo_d_s = QComboBox(); populate_size_combo(self.combo_d_s,["Leave Unchanged", "Default (Auto)"], DEPTH_LEVELS, is_depth=True)
        self.combo_f_f = QComboBox(); populate_size_combo(self.combo_f_f,["Leave Unchanged", "Default (Auto)"], FRAME_LEVELS)
        self.combo_d_f = QComboBox(); populate_size_combo(self.combo_d_f,["Leave Unchanged", "Default (Auto)"], DEPTH_LEVELS, is_depth=True)
        
        form.addRow("Action Behavior:", self.combo_behavior)
        form.addRow("Global Cooldown:", self.spin_cooldown)
        form.addRow("Map Visibility:", self.combo_public)
        form.addRow(QLabel("<hr>"))
        form.addRow("Min Frame Size (Single):", self.combo_f_s)
        form.addRow("Min Depth (Single):", self.combo_d_s)
        form.addRow("Min Frame Size (Flock):", self.combo_f_f)
        form.addRow("Min Depth (Flock):", self.combo_d_f)
        
        self.layout.addLayout(form)
        
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.layout.addWidget(self.buttons)

    def get_data(self):
        return {
            'behavior': self.combo_behavior.currentText(),
            'cooldown_minutes': self.spin_cooldown.value(),
            'show_on_map': self.combo_public.currentText(),
            'min_frame_single': self.combo_f_s.currentData(),
            'min_depth_single': self.combo_d_s.currentData(),
            'min_frame_flock': self.combo_f_f.currentData(),
            'min_depth_flock': self.combo_d_f.currentData()
        }


class AssignVisionTargetDialog(QDialog):
    def __init__(self, master_registry, existing_assignments, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Assign Stream Rules")
        self.setMinimumSize(400, 500)
        self.layout = QVBoxLayout(self)
        
        self.layout.addWidget(QLabel("Select Rule Type:"))
        self.combo_rule = QComboBox()
        self.combo_rule.addItems(["Enforce Target (Inject into AI Prompt)", "Local Ignore (Blind spot for this camera)"])
        self.combo_rule.setStyleSheet("font-weight: bold; font-size: 13px;")
        self.layout.addWidget(self.combo_rule)
        
        self.layout.addWidget(QLabel("Select animals to apply this rule to:"))
        
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search Registry...")
        self.search_box.textChanged.connect(self.filter_list)
        self.layout.addWidget(self.search_box)
        
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        
        existing_names =[t["name"] for t in existing_assignments]
        
        for canonical_name, data in sorted(master_registry.items()):
            if canonical_name not in existing_names:
                item = QListWidgetItem(canonical_name)
                if data.get('type') == 'General':
                    item.setForeground(QBrush(QColor("#888888")))
                    font = item.font(); font.setItalic(True); item.setFont(font)
                else:
                    item.setForeground(QBrush(QColor("#00E676")))
                    font = item.font(); font.setBold(True); item.setFont(font)
                self.list_widget.addItem(item)
                
        self.layout.addWidget(self.list_widget)
        
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.layout.addWidget(self.buttons)

    def filter_list(self):
        text = self.search_box.text().lower()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            item.setHidden(text not in item.text().lower())

    def get_selected(self):
        rule = "Enforce Target" if self.combo_rule.currentIndex() == 0 else "Local Ignore"
        return[{"name": item.text(), "rule": rule} for item in self.list_widget.selectedItems()]


class AddAnimalDialog(QDialog):
    def __init__(self, master_animals, existing_targets, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Assign Bioacoustic Targets")
        self.setMinimumSize(400, 500)
        self.layout = QVBoxLayout(self)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search Animals...")
        self.search_box.textChanged.connect(self.filter_list)
        self.layout.addWidget(self.search_box)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        
        for display_name, profile_code in sorted(master_animals.items()):
            if display_name not in existing_targets:
                item = QListWidgetItem(display_name)
                item.setData(Qt.ItemDataRole.UserRole, profile_code)
                self.list_widget.addItem(item)

        self.layout.addWidget(self.list_widget)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.layout.addWidget(self.buttons)

    def filter_list(self):
        text = self.search_box.text().lower()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            item.setHidden(text not in item.text().lower())

    def get_selected(self):
        return[{'profile': item.data(Qt.ItemDataRole.UserRole), 'display': item.text()} for item in self.list_widget.selectedItems()]


class TaxonomyAuditorDialog(QDialog):
    """Wizard Dialog to present Gemini's General vs Specific recommendations."""
    def __init__(self, recommendations, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI Taxonomy Auditor: Review Recommendations")
        self.resize(800, 600)
        self.recommendations = recommendations
        self.accepted_changes = {}
        
        layout = QVBoxLayout(self)
        
        info = QLabel("<b>Gemini has analyzed your unsilenced taxonomy.</b><br>Review the proposed changes below. Check the boxes for the ones you want to apply.")
        info.setWordWrap(True)
        layout.addWidget(info)
        
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Apply", "Animal Name", "Current Type", "Suggested Type"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.table)
        
        self.populate_table()
        
        btn_layout = QHBoxLayout()
        self.btn_select_all = QPushButton("Select All")
        self.btn_select_all.clicked.connect(lambda: self.toggle_all(True))
        self.btn_deselect_all = QPushButton("Deselect All")
        self.btn_deselect_all.clicked.connect(lambda: self.toggle_all(False))
        
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_apply = QPushButton("Apply Selected Changes")
        self.btn_apply.setStyleSheet("background-color: #00897B; color: white; font-weight: bold;")
        self.btn_apply.clicked.connect(self.apply_changes)
        
        btn_layout.addWidget(self.btn_select_all)
        btn_layout.addWidget(self.btn_deselect_all)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_apply)
        layout.addLayout(btn_layout)

    def populate_table(self):
        self.table.setRowCount(len(self.recommendations))
        for i, (animal, current_type, suggested_type) in enumerate(self.recommendations):
            chk = QCheckBox()
            chk.setChecked(True)
            w_chk = QWidget(); l_chk = QHBoxLayout(w_chk); l_chk.addWidget(chk); l_chk.setAlignment(Qt.AlignmentFlag.AlignCenter); l_chk.setContentsMargins(0,0,0,0)
            self.table.setCellWidget(i, 0, w_chk)
            
            self.table.setItem(i, 1, QTableWidgetItem(animal))
            self.table.setItem(i, 2, QTableWidgetItem(current_type))
            
            sug_item = QTableWidgetItem(suggested_type)
            sug_item.setForeground(QBrush(QColor("#00E676")))
            font = sug_item.font(); font.setBold(True); sug_item.setFont(font)
            self.table.setItem(i, 3, sug_item)

    def toggle_all(self, state):
        for i in range(self.table.rowCount()):
            widget = self.table.cellWidget(i, 0)
            chk = widget.layout().itemAt(0).widget()
            chk.setChecked(state)

    def apply_changes(self):
        for i in range(self.table.rowCount()):
            widget = self.table.cellWidget(i, 0)
            chk = widget.layout().itemAt(0).widget()
            if chk.isChecked():
                animal = self.table.item(i, 1).text()
                suggested_type = self.table.item(i, 3).text()
                self.accepted_changes[animal] = suggested_type
        self.accept()


class ForbiddenPurgeDialog(QDialog):
    def __init__(self, hits, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Forbidden Words Purge ({len(hits)} Found)")
        self.resize(700, 500)
        self.layout = QVBoxLayout(self)

        info = QLabel(f"<b>Found {len(hits)} taxonomic entries violating your Forbidden Words list.</b><br>Review the list below. Do you want to permanently PURGE these from the database, registry, and delete their images?")
        info.setWordWrap(True)
        self.layout.addWidget(info)

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Animal Name", "Violation Reason"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        self.table.setRowCount(len(hits))
        for i, (name, reason) in enumerate(hits):
            self.table.setItem(i, 0, QTableWidgetItem(name))
            self.table.setItem(i, 1, QTableWidgetItem(reason))

        self.layout.addWidget(self.table)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Yes).setText("Purge All Shown")
        self.buttons.button(QDialogButtonBox.StandardButton.Yes).setStyleSheet("background-color: #D32F2F; color: white; font-weight: bold;")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.layout.addWidget(self.buttons)