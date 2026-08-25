# FILE: stream_discovery_gui.py
# VERSION: 14.17 - "The Mother Channel Fallback Patch"
# RESPONSIBILITY: Triage Ward, Graveyard, Discovery Radar, and the Unified Lifecycle & Sync Engine.
# UPDATED: Fixed a bug where new_channel_name defaulted to "Unknown Channel" instead of inheriting the known Mother Channel. Injected plain English issue descriptions into the DiffViewer header.

import sys
import json
import logging
import time
import difflib
import re
import os
import webbrowser
import requests
import html as htmllib
from pathlib import Path
from collections import Counter

from PyQt6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QHBoxLayout, 
                             QLabel, QPushButton, QTableWidget, QTableWidgetItem, 
                             QHeaderView, QLineEdit, QTabWidget, QWidget, QProgressBar, QProgressDialog,
                             QMessageBox, QFormLayout, QAbstractItemView, QDialogButtonBox,
                             QCheckBox, QGroupBox, QSizePolicy, QInputDialog, QTreeWidget, QTreeWidgetItem, QSplitter, QComboBox, QSpinBox, QListWidget, QListWidgetItem, QTextEdit, QMenu, QCompleter, QToolButton, QScrollArea)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QColor, QBrush, QFont, QColorConstants, QAction

import yt_dlp

# --- INTERNAL MODULES ---
import stream_migrator
import db_connector
import yt_sync_engine

# --- CONFIGURATION ---
ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "birdnet_config.json"
SYNC_PROPOSALS_FILE = ROOT / "sync_proposals.json"
METADATA_CACHE_FILE = ROOT / "youtube_metadata_cache.json"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [DISCOVERY] - %(message)s')

# ==============================================================================
# WORKER THREADS
# ==============================================================================

class TranslationWorker(QThread):
    result_ready = pyqtSignal(dict)
    
    def __init__(self, title, description, url=None):
        super().__init__()
        self.title = title
        self.description = description
        self.url = url
        
    def run(self):
        # --- THE LIVE DESCRIPTION FETCHER PATCH ---
        if self.url and (not self.description or "No description available" in self.description):
            try:
                res = requests.get(self.url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
                match = re.search(r'<meta (?:name|property)="o?g?:?description" content="([^"]+)">', res.text, re.IGNORECASE)
                if not match:
                    match = re.search(r'"shortDescription":"(.*?)"', res.text)
                
                if match:
                    raw_desc = match.group(1)
                    raw_desc = raw_desc.replace('\\"', '"').replace('\\n', '\n')
                    self.description = htmllib.unescape(raw_desc)
            except Exception as e:
                logging.warning(f"Failed to fetch live description for translation: {e}")

        if not self.title and not self.description:
            self.result_ready.emit({'error': 'No text provided'})
            return
            
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            self.result_ready.emit({'error': 'google-genai library missing'})
            return
            
        api_key = None
        try:
            if CONFIG_FILE.exists():
                cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                keys = cfg.get("vision_ai", {}).get("api_keys",[])
                for k in keys:
                    if k.get("status") != "Exhausted" and k.get("key", "").strip():
                        api_key = k["key"].strip()
                        break
        except Exception as e:
            self.result_ready.emit({'error': f'Config read error: {e}'})
            return
            
        if not api_key:
            self.result_ready.emit({'error': 'No active Gemini API key found in config'})
            return
            
        try:
            client = genai.Client(api_key=api_key)
            prompt = f"""
            You are a professional translator. Identify the language of the following live stream title and description.
            Translate both to English. If they are already in English, just return English and the original text.
            If the description is missing or blank, translate the title only and return an empty string for the translated description.
            
            Title: {self.title}
            Description: {self.description}
            
            Return ONLY a valid JSON dictionary with EXACTLY these keys:
            {{"language": "Detected Language", "translated_title": "...", "translated_description": "..."}}
            """
            
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
                
            data = json.loads(raw_text)
            data['original_description'] = self.description 
            self.result_ready.emit(data)
        except Exception as e:
            self.result_ready.emit({'error': f'API Error: {str(e)}'})


class ResolutionThread(QThread):
    result_ready = pyqtSignal(bool, list, str, str, str, str)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            import stream_resolver
        except ImportError:
            self.result_ready.emit(False,[], "error", "Resolver module missing.", "", "")
            return
            
        title = ""
        uploader = ""
        try:
            if "youtube.com" in self.url or "youtu.be" in self.url:
                ydl_opts = {'quiet': True, 'skip_download': True, 'no_warnings': True, 'extract_flat': False}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(self.url, download=False)
                    if info:
                        if 'title' in info: title = info['title']
                        if 'uploader' in info: 
                            uploader = info['uploader']
                        elif 'channel' in info:
                            uploader = info['channel']
        except Exception as e:
            logging.warning(f"Failed to extract title/uploader in resolver thread: {e}")
        
        try:
            links, stype, msg = stream_resolver.resolve_stream_url(self.url, fast_mode=True)
            if links:
                self.result_ready.emit(True, links, stype, msg, title, uploader)
            else:
                self.result_ready.emit(False,[], "error", msg, title, uploader)
        except Exception as e:
            self.result_ready.emit(False,[], "error", str(e), title, uploader)


class DiscoveryWorker(QThread):
    progress_msg = pyqtSignal(str)
    result_ready = pyqtSignal(list)

    def __init__(self, search_term, result_count=15):
        super().__init__()
        self.search_term = search_term
        self.result_count = result_count

    def run(self):
        results =[]
        self.progress_msg.emit(f"Scanning YouTube for '{self.search_term}'...")
        
        ydl_opts = {
            'extract_flat': True,
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': True,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }

        query = f"ytsearch{self.result_count}:{self.search_term} live"
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(query, download=False)
                if info and 'entries' in info:
                    for entry in info['entries']:
                        if not entry: continue
                        
                        if entry.get('live_status') == 'was_live':
                            continue
                            
                        title = entry.get('title', '')
                        url = entry.get('url', '')
                        uploader = entry.get('uploader', 'Unknown')
                        
                        if not url.startswith('http'):
                            url = f"https://www.youtube.com/watch?v={url}"
                            
                        results.append({
                            'title': title,
                            'url': url,
                            'uploader': uploader
                        })
        except Exception as e:
            logging.error(f"Discovery search failed: {e}")
            self.progress_msg.emit(f"Error: {e}")
            return
            
        self.progress_msg.emit(f"Found {len(results)} potential streams.")
        self.result_ready.emit(results)


class SyncEngineWorker(QThread):
    progress_update = pyqtSignal(int, int, str)
    finished = pyqtSignal(list)
    
    def __init__(self, target_channels=None):
        super().__init__()
        self.target_channels = target_channels
        
    def run(self):
        try:
            engine = yt_sync_engine.YouTubeSyncEngine()
            
            def callback(current, total, msg):
                self.progress_update.emit(current, total, msg)
                
            engine.run_sync(progress_callback=callback, target_channels=self.target_channels)
            self.finished.emit(engine.proposals)
        except Exception as e:
            self.progress_update.emit(0, 1, f"FATAL ERROR in Sync Engine: {e}")
            self.finished.emit([])


class ChannelSyncWorker(QThread):
    progress_update = pyqtSignal(int, int, str)
    finished = pyqtSignal(list)
    
    def __init__(self, target_url):
        super().__init__()
        self.target_url = target_url
        
    def run(self):
        try:
            engine = yt_sync_engine.YouTubeSyncEngine()
            def cb(c, t, m): self.progress_update.emit(c, t, m)
            engine.run_sync(progress_callback=cb, target_channels=[self.target_url])
            self.finished.emit(engine.proposals)
        except Exception as e:
            logging.error(f"ChannelSyncWorker Error: {e}")
            self.finished.emit([])

# ==============================================================================
# UI DIALOGS
# ==============================================================================

class LegendDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Master Legend & Key")
        self.resize(600, 480)
        self.layout = QVBoxLayout(self)
        
        text = """
        <h3 style='color:#00E5FF; margin-bottom: 2px;'>Sync Strategy Legend</h3>
        <ul style='margin-top: 0px;'>
            <li><b>Auto:</b> Scans channel unless live stream count exceeds the Max Channel Streams threshold.</li>
            <li><b>Always Scan:</b> Bypasses threshold limits and forces a scan.</li>
            <li><b>Never Scan:</b> Completely ignores this channel during background syncs.</li>
        </ul>
        <hr>
        <h3 style='color:#00E5FF; margin-bottom: 2px;'>Proposal Types & Action Required</h3>
        <ul style='margin-top: 0px;'>
            <li>⚡ <b>Auto-Heal Eligible:</b> Safe to automate (Title updates, same-channel URL changes, highly confident Graveyard Resurrections).</li>
            <li>✋ <b>Manual Review Required:</b> Needs human confirmation (Dead streams, cross-channel migrations, new streams).</li>
        </ul>
        <hr>
        <h3 style='color:#00E5FF; margin-bottom: 2px;'>Status Icons (Cases)</h3>
        <ul style='margin-top: 0px;'>
            <li>💀 <b>[Case A] Dead Stream:</b> Source is offline/private.</li>
            <li>🔄 <b>[Case B, D, E, F, G, H] Migrated:</b> URL changed.</li>
            <li>🏢 <b>[Case C, D, F, H] Channel Renamed:</b> The parent channel changed its name.</li>
            <li>✏️ <b>[Case C, D, F, H] Title Updated:</b> The stream title changed.</li>
            <li>✨ <b>[Case N] New Stream:</b> Brand new stream discovered on a monitored channel.</li>
            <li>🧟 <b>[Case R] Resurrection:</b> A new stream matched the "DNA" of a dead stream in your Graveyard. It will be restored with its history intact.</li>
            <li>🪄 <b>[Case M] Manual Edit:</b> User manually pasted and resolved a new URL to heal a dead stream.</li>
        </ul>
        <hr>
        <h3 style='color:#00E5FF; margin-bottom: 2px;'>Text & Row Colors</h3>
        <ul style='margin-top: 0px;'>
            <li><span style="color:#EF5350; font-weight:bold;">Red:</span> Destructive/Dead (Case A, Reject).</li>
            <li><span style="color:#00E676; font-weight:bold;">Green:</span> Safe/Approved (Title updates, Accept).</li>
            <li><span style="color:#FFB74D; font-weight:bold;">Orange:</span> Migrated/Moved/Manually Edited (URL changes).</li>
            <li><span style="color:#AB47BC; font-weight:bold;">Purple:</span> Channel Renamed (Parent channel changed its name).</li>
            <li><span style="color:#00E5FF; font-weight:bold;">Cyan:</span> New Stream (Case N) or Auto-suggested coordinate match.</li>
            <li><span style="color:#64DD17; font-weight:bold;">Lime:</span> Graveyard Resurrection (Case R).</li>
        </ul>
        """
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        self.layout.addWidget(lbl)
        
        btn_layout = QHBoxLayout()
        btn_close = QPushButton("Close Legend")
        btn_close.setStyleSheet("background-color: #333; color: white; font-weight: bold; padding: 8px;")
        btn_close.clicked.connect(self.accept)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_close)
        self.layout.addLayout(btn_layout)

class SyncSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lifecycle & Sync Automation Settings")
        self.resize(450, 250)
        self.layout = QVBoxLayout(self)
        
        info = QLabel("Configure how the Sync Engine operates in the background.")
        info.setWordWrap(True)
        self.layout.addWidget(info)
        
        self.chk_auto_heal = QCheckBox("Automatically apply 'Auto-Heal Eligible' (⚡) proposals during background scans.")
        self.chk_auto_heal.setStyleSheet("font-weight: bold; color: #00E676;")
        self.chk_auto_heal.setToolTip("Cases like simple Title updates, same-channel URL migrations, and high-confidence Graveyard Resurrections will be applied seamlessly without manual review.")
        
        form = QFormLayout()
        
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(1, 168)
        self.spin_interval.setSuffix(" hours")
        form.addRow("Background Scan Interval:", self.spin_interval)
        
        self.spin_max_streams = QSpinBox()
        self.spin_max_streams.setRange(1, 999)
        self.spin_max_streams.setSuffix(" live streams")
        self.spin_max_streams.setToolTip("If a channel has more than this number of live streams, the Sync Engine will Auto-Ignore it to prevent API spam and UI flooding.")
        form.addRow("Auto-Ignore Channel Threshold:", self.spin_max_streams)
        
        self.layout.addWidget(self.chk_auto_heal)
        self.layout.addLayout(form)
        
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        self.layout.addWidget(btns)
        
        self.load_config()
        
    def load_config(self):
        if CONFIG_FILE.exists():
            try:
                cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                s_cfg = cfg.get("sync_engine_settings", {})
                self.chk_auto_heal.setChecked(s_cfg.get("auto_heal_enabled", False))
                self.spin_interval.setValue(s_cfg.get("scan_interval_hours", 24))
                self.spin_max_streams.setValue(s_cfg.get("max_channel_streams", 15))
            except: pass
            
    def get_values(self):
        return {
            "auto_heal_enabled": self.chk_auto_heal.isChecked(),
            "scan_interval_hours": self.spin_interval.value(),
            "max_channel_streams": self.spin_max_streams.value()
        }


class BlacklistManagerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Blacklist Manager (Ignored Streams)")
        self.resize(800, 500)
        self.layout = QVBoxLayout(self)

        info = QLabel("These streams were manually <b>Rejected & Ignored</b>. The Sync Engine currently hides them from the Resolution Center to prevent clutter.<br><br>Click <b>'Restore'</b> to un-ignore a stream so it can be discovered again on the next sync.")
        info.setWordWrap(True)
        self.layout.addWidget(info)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Stream Title", "Clean URL", "Action"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.layout.addWidget(self.table)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        self.layout.addWidget(btn_close)

        self.load_blacklist()

    def load_blacklist(self):
        self.table.setRowCount(0)
        if not CONFIG_FILE.exists(): return
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            ignored = cfg.get("sync_engine_settings", {}).get("ignored_proposals", {})
            
            self.table.setRowCount(len(ignored))
            row = 0
            for url, title in ignored.items():
                self.table.setItem(row, 0, QTableWidgetItem(title))
                
                url_item = QTableWidgetItem(url)
                url_item.setForeground(QBrush(QColor("#aaaaaa")))
                font = url_item.font()
                font.setFamily("monospace")
                url_item.setFont(font)
                self.table.setItem(row, 1, url_item)
                
                btn_restore = QPushButton("🔄 Restore (Un-Ignore)")
                btn_restore.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
                btn_restore.clicked.connect(lambda chk, u=url, r=row: self.restore_stream(u, r))
                self.table.setCellWidget(row, 2, btn_restore)
                row += 1
        except Exception as e:
            logging.error(f"Failed to load blacklist: {e}")

    def restore_stream(self, url, row):
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            if "sync_engine_settings" in cfg and "ignored_proposals" in cfg["sync_engine_settings"]:
                if url in cfg["sync_engine_settings"]["ignored_proposals"]:
                    del cfg["sync_engine_settings"]["ignored_proposals"][url]
                    
                    tmp_file = CONFIG_FILE.with_suffix('.tmp')
                    with open(tmp_file, 'w', encoding='utf-8') as f:
                        json.dump(cfg, f, indent=2)
                    os.replace(tmp_file, CONFIG_FILE)
                    
                    QMessageBox.information(self, "Restored", "Stream removed from Blacklist!\nIt will appear as a proposal on the next Sync Engine run.")
                    self.load_blacklist() 
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to restore stream: {e}")


class DiffViewerDialog(QDialog):
    def __init__(self, proposal, parent=None):
        super().__init__(parent)
        self.proposal = proposal
        self.result_action = 'cancel' 
        self.needs_resolution = False # <--- GUARDRAIL FLAG
        
        case_id = proposal['case']
        self.setWindowTitle(f"Resolution Center: Case {case_id} ({proposal.get('case_desc', 'Unknown')})")
        self.resize(950, 650)
        
        # --- THE SCROLL AREA PATCH ---
        self.main_layout = QVBoxLayout(self)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QAbstractItemView.Shape.NoFrame)
        
        self.content_widget = QWidget()
        self.layout = QVBoxLayout(self.content_widget)

        is_auto = proposal.get('auto_heal_eligible', False)
        heal_icon = "⚡" if is_auto else "✋"

        header = QLabel(f"<h2 style='margin-bottom: 5px;'>{heal_icon} {proposal.get('friendly_name', 'Unknown Stream')}</h2>")
        self.layout.addWidget(header)
        
        # --- THE PLAIN ENGLISH ISSUE INJECTION PATCH ---
        case_desc = proposal.get('case_desc', 'Unknown Issue')
        info = QLabel(f"<b>Issue Detected:</b> <span style='color: #00E5FF;'>{case_desc}</span><br><br>"
                      "Review the changes below. The system has highlighted the differences. You can test the URLs before confirming.<br>"
                      "<span style='color: #FFB74D;'><b>Note: Your Friendly Map Name will NOT be changed by this operation.</b></span>")
        info.setStyleSheet("color: #aaa; margin-bottom: 10px;")
        self.layout.addWidget(info)
        
        if case_id == "A":
            warn_lbl = QLabel("<b>⚠️ CRITICAL ACTION REQUIRED:</b><br><br>This stream is marked as [DEAD]. You can either <b>Reject</b> it to leave it in the queue, <b>Accept</b> it to disable it (keeping history), or <b>Paste a new URL in the right panel and click 🪄 Resolve</b> to manually heal and migrate the history to a new link.")
            warn_lbl.setStyleSheet("color: white; background-color: #B71C1C; padding: 15px; border-radius: 6px; font-size: 14px; border: 2px solid #FF5252;")
            warn_lbl.setWordWrap(True)
            self.layout.addWidget(warn_lbl)
            
        if case_id == "N":
            new_group = QGroupBox("✨ Brand New Stream Discovered on Monitored Channel")
            new_group.setStyleSheet("QGroupBox { border: 2px solid #00E5FF; border-radius: 6px; font-weight: bold; color: #00E5FF; margin-top: 12px; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }")
            n_lay = QFormLayout(new_group)
            
            self.edit_orig_title = QLineEdit(self.proposal.get('new_title', 'N/A'))
            self.edit_orig_title.setReadOnly(True)
            self.edit_orig_title.setStyleSheet("color: #ccc; font-style: italic;")
            n_lay.addRow("Original Title:", self.edit_orig_title)
            
            orig_desc = self.proposal.get('description', '')
            self.text_orig_desc = QTextEdit(orig_desc if orig_desc else "No description available in payload.")
            self.text_orig_desc.setReadOnly(True)
            self.text_orig_desc.setMinimumHeight(40)
            self.text_orig_desc.setMaximumHeight(80) # Bound to prevent screen stretching
            self.text_orig_desc.setStyleSheet("color: #ccc; font-style: italic; font-size: 11px;")
            n_lay.addRow("Original Desc:", self.text_orig_desc)
            
            self.lbl_lang = QLabel("⏳ Detecting language, fetching details & translating via Gemini...")
            self.lbl_lang.setStyleSheet("color: #00E5FF; font-style: italic;")
            n_lay.addRow("Language:", self.lbl_lang)
            
            self.edit_trans_title = QLineEdit("Translating...")
            self.edit_trans_title.setReadOnly(True)
            self.edit_trans_title.setStyleSheet("color: #00E676; font-weight: bold;")
            n_lay.addRow("Translated Title:", self.edit_trans_title)
            
            self.text_trans_desc = QTextEdit("Translating...")
            self.text_trans_desc.setReadOnly(True)
            self.text_trans_desc.setMinimumHeight(60)
            self.text_trans_desc.setMaximumHeight(150) # Bound to prevent screen stretching
            self.text_trans_desc.setStyleSheet("color: #00E676;")
            n_lay.addRow("Translated Desc:", self.text_trans_desc)
            
            n_lay.addRow(QLabel("<hr>"))
            
            self.edit_friendly_name = QLineEdit(self.proposal.get('new_title', ''))
            n_lay.addRow("Friendly Name:", self.edit_friendly_name)
            
            c_url = self.proposal.get('new_channel')
            display_cname = self.proposal.get('new_channel_name', 'Unknown Channel')
            if CONFIG_FILE.exists():
                try:
                    live_cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                    for c_name, c_link in live_cfg.get('channels', {}).items():
                        if c_link == c_url:
                            display_cname = c_name
                            break
                except Exception: pass
                
            n_lay.addRow("Uploader Channel:", QLabel(display_cname))
            
            url_widget = QWidget()
            url_lay = QHBoxLayout(url_widget)
            url_lay.setContentsMargins(0,0,0,0)
            url_lbl = QLabel(self.proposal.get('new_url', 'N/A'))
            url_lbl.setStyleSheet("font-family: monospace; color: #aaa;")
            btn_test = QPushButton("🎬 Open URL")
            btn_test.clicked.connect(lambda: webbrowser.open(self.proposal.get('new_url', '')))
            url_lay.addWidget(url_lbl)
            url_lay.addWidget(btn_test)
            n_lay.addRow("Stream URL:", url_widget)
            
            n_lay.addRow(QLabel("<hr>"))
            n_lay.addRow(QLabel("To add this stream to your configuration, please provide coordinates:"))
            
            s_lat = self.proposal.get('suggested_lat', 0.0)
            s_lon = self.proposal.get('suggested_lon', 0.0)
            s_match = self.proposal.get('suggested_match_name', '')
            siblings = self.proposal.get('sibling_streams',[])
            
            self.edit_lat = QLineEdit(str(s_lat) if s_lat else "0.0")
            self.edit_lon = QLineEdit(str(s_lon) if s_lon else "0.0")
            
            self.edit_lat.textChanged.connect(self._handle_smart_lat_paste)

            self.combo_copy_gps = QComboBox()
            self.combo_copy_gps.addItem("-- Select Sibling Stream to Copy GPS --", {"lat": 0.0, "lon": 0.0})
            
            self.combo_global_gps = QComboBox()
            self.combo_global_gps.setEditable(True)
            self.combo_global_gps.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            self.combo_global_gps.addItem("-- Type to search all streams globally... --", {"lat": 0.0, "lon": 0.0})
            
            if CONFIG_FILE.exists():
                try:
                    live_cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                    sorted_streams = sorted(live_cfg.get('streams',[]), key=lambda x: x.get('name', '').lower())
                    
                    for s in sorted_streams:
                        lat_val, lon_val = s.get('lat', 0.0), s.get('lon', 0.0)
                        if lat_val != 0.0 or lon_val != 0.0:
                            self.combo_global_gps.addItem(s.get('name', 'Unknown'), {"lat": lat_val, "lon": lon_val})
                            
                        if s.get('channel_name') == display_cname or s.get('channel_name') == self.proposal.get('new_channel_name'):
                            if lat_val != 0.0 or lon_val != 0.0:
                                self.combo_copy_gps.addItem(s.get('name', 'Unknown'), {"lat": lat_val, "lon": lon_val})
                except Exception: pass
                
            self.combo_copy_gps.currentIndexChanged.connect(self._on_gps_combo_changed)
            n_lay.addRow("Sibling GPS:", self.combo_copy_gps)
            
            completer = self.combo_global_gps.completer()
            if completer:
                completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
                completer.setFilterMode(Qt.MatchFlag.MatchContains)
                completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
                
            self.combo_global_gps.currentIndexChanged.connect(self._on_global_gps_changed)
            n_lay.addRow("Search Global GPS:", self.combo_global_gps)
            
            if s_lat != 0.0 or s_lon != 0.0:
                self.edit_lat.setStyleSheet("color: #00E5FF; border: 1px solid #00E5FF;")
                self.edit_lon.setStyleSheet("color: #00E5FF; border: 1px solid #00E5FF;")
                match_lbl = QLabel(f"<span style='color:#00E5FF;'>🪄 Location matched via sibling: <b>{s_match}</b></span>")
                n_lay.addRow("Auto-Suggest:", match_lbl)

            coord_lay = QHBoxLayout()
            coord_lay.addWidget(QLabel("Lat:"))
            coord_lay.addWidget(self.edit_lat)
            coord_lay.addWidget(QLabel("Lon:"))
            coord_lay.addWidget(self.edit_lon)
            
            btn_map_preview = QPushButton("🗺️ Preview Map")
            btn_map_preview.clicked.connect(self.preview_map)
            coord_lay.addWidget(btn_map_preview)
            
            if siblings:
                btn_sibs = QPushButton(f"🔗 View {len(siblings)} Siblings")
                btn_sibs.clicked.connect(self.show_siblings)
                coord_lay.addWidget(btn_sibs)
                
            n_lay.addRow("Coordinates:", coord_lay)
            
            self.layout.addWidget(new_group)
            
            self.trans_worker = TranslationWorker(self.proposal.get('new_title', ''), orig_desc, self.proposal.get('new_url'))
            self.trans_worker.result_ready.connect(self._on_translation_ready)
            self.trans_worker.start()
            
        else:
            split = QHBoxLayout()
            
            def generate_diff_html(old_text, new_text, mode='word'):
                if mode == 'char':
                    old_tokens = list(old_text or "")
                    new_tokens = list(new_text or "")
                    join_char = ""
                    space_char = ""
                else:
                    old_tokens = (old_text or "").split()
                    new_tokens = (new_text or "").split()
                    join_char = " "
                    space_char = " "
                    
                matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens)
                
                old_diff = ""
                new_diff = ""
                
                for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                    if tag == 'equal':
                        old_diff += join_char.join(old_tokens[i1:i2]) + space_char
                        new_diff += join_char.join(new_tokens[j1:j2]) + space_char
                    elif tag == 'delete':
                        chunk = join_char.join(old_tokens[i1:i2])
                        old_diff += f"<span style='color: #EF5350; text-decoration: line-through; font-weight: bold; background-color: rgba(239,83,80,0.15);'>{chunk}</span>{space_char}"
                    elif tag == 'insert':
                        chunk = join_char.join(new_tokens[j1:j2])
                        new_diff += f"<span style='color: #00E676; font-weight: bold; background-color: rgba(0,230,118,0.15);'>{chunk}</span>{space_char}"
                    elif tag == 'replace':
                        chunk_o = join_char.join(old_tokens[i1:i2])
                        chunk_n = join_char.join(new_tokens[j1:j2])
                        old_diff += f"<span style='color: #EF5350; text-decoration: line-through; font-weight: bold; background-color: rgba(239,83,80,0.15);'>{chunk_o}</span>{space_char}"
                        new_diff += f"<span style='color: #00E676; font-weight: bold; background-color: rgba(0,230,118,0.15);'>{chunk_n}</span>{space_char}"
                
                return old_diff.strip(), new_diff.strip()

            old_title_html, new_title_html = generate_diff_html(self.proposal.get('old_title'), self.proposal.get('new_title'), mode='word')
            old_url_html, new_url_html = generate_diff_html(self.proposal.get('old_url'), self.proposal.get('new_url'), mode='char')
            
            old_cname_raw = self.proposal.get('old_channel_name')
            if not old_cname_raw or old_cname_raw == "Unknown Channel":
                old_cname_raw = "Unknown Channel"
            
            if CONFIG_FILE.exists():
                try:
                    live_cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                    for s in live_cfg.get('streams',[]):
                        clean_u = re.sub(r'[\?&]variant=\d+', '', s.get('page_url', ''))
                        if clean_u == self.proposal.get('old_url') or s.get('name') == self.proposal.get('friendly_name'):
                            live_cname = s.get('channel_name')
                            if live_cname:
                                old_cname_raw = live_cname
                            break
                except Exception as e:
                    logging.error(f"Failed live config lookup in Diff Viewer: {e}")
                    
            # --- THE MOTHER CHANNEL FALLBACK PATCH ---
            new_cname_raw = self.proposal.get('new_channel_name')
            if not new_cname_raw or new_cname_raw == "Unknown Channel":
                new_cname_raw = old_cname_raw
                    
            old_chan_html, new_chan_html = generate_diff_html(old_cname_raw, new_cname_raw, mode='word')

            # --- OLD GROUP BOX (LEFT) ---
            old_group_color = "#9E9E9E" if case_id == "R" else "#EF5350"
            old_group_title = "Dead Stream Configuration (Graveyard)" if case_id == "R" else "Current Configuration (Database)"
            
            old_group = QGroupBox(old_group_title)
            old_group.setStyleSheet(f"QGroupBox {{ border: 2px solid {old_group_color}; border-radius: 6px; font-weight: bold; color: {old_group_color}; margin-top: 12px; }} QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px; }}")
            old_lay = QFormLayout(old_group)
            
            old_title_lbl = QLabel(old_title_html if old_title_html else "N/A")
            old_title_lbl.setWordWrap(True)
            old_lay.addRow("Title:", old_title_lbl)
            
            chan_widget_old = QWidget()
            chan_lay_old = QVBoxLayout(chan_widget_old)
            chan_lay_old.setContentsMargins(0, 0, 0, 0)
            
            old_chan_lbl = QLabel(old_chan_html if old_chan_html else old_cname_raw)
            old_chan_lbl.setWordWrap(True)
            chan_lay_old.addWidget(old_chan_lbl)
            
            btn_old_chan = QPushButton("🏠 Open Channel")
            if self.proposal.get('old_channel'):
                btn_old_chan.clicked.connect(lambda: webbrowser.open(self.proposal['old_channel']))
            else:
                btn_old_chan.setEnabled(False)
            chan_lay_old.addWidget(btn_old_chan)
            old_lay.addRow("Channel:", chan_widget_old)
            
            url_widget_old = QWidget()
            url_lay_old = QVBoxLayout(url_widget_old)
            url_lay_old.setContentsMargins(0, 0, 0, 0)
            
            old_url_lbl = QLabel(old_url_html if old_url_html else self.proposal.get('old_url', 'N/A'))
            old_url_lbl.setWordWrap(True)
            old_url_lbl.setStyleSheet("font-family: monospace; font-size: 12px;")
            url_lay_old.addWidget(old_url_lbl)
            
            btn_old_vid = QPushButton("🎬 Open Stream URL")
            if self.proposal.get('old_url'):
                btn_old_vid.clicked.connect(lambda: webbrowser.open(self.proposal['old_url']))
            else:
                btn_old_vid.setEnabled(False)
            url_lay_old.addWidget(btn_old_vid)
            old_lay.addRow("URL Diff:", url_widget_old)
            
            split.addWidget(old_group)
            
            # --- NEW GROUP BOX (RIGHT) - EDITABLE ---
            color = "#00E676" if case_id != "A" else "#FFB74D" 
            if case_id == "R": color = "#64DD17" 
            new_group = QGroupBox("Suggested / Manual Update")
            new_group.setStyleSheet(f"QGroupBox {{ border: 2px solid {color}; border-radius: 6px; font-weight: bold; color: {color}; margin-top: 12px; }} QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px; }}")
            new_lay = QFormLayout(new_group)
            
            self.edit_new_title = QLineEdit(self.proposal.get('new_title', '') if case_id != 'A' else '')
            self.edit_new_title.setPlaceholderText("Title will auto-fill on resolve...")
            new_lay.addRow("Title:", self.edit_new_title)
            
            self.edit_new_chan = QComboBox()
            self.edit_new_chan.setEditable(True)
            if CONFIG_FILE.exists():
                try:
                    cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                    self.edit_new_chan.addItems(sorted(list(cfg.get('channels', {}).keys())))
                except: pass
            self.edit_new_chan.setCurrentText(new_cname_raw if case_id != 'A' else old_cname_raw)
            new_lay.addRow("Channel:", self.edit_new_chan)
            
            if case_id in["E", "F", "G", "H", "R"]:
                tag_text = "⚠️ CHANNEL URL MOVED" if case_id != "R" else "🧟 RESURRECT FROM GRAVEYARD"
                move_lbl = QLabel(f"<span style='color: #FFB74D; font-weight: bold;'>{tag_text}</span>")
                new_lay.addRow("", move_lbl)
            
            url_widget_new = QWidget()
            url_lay_new = QHBoxLayout(url_widget_new)
            url_lay_new.setContentsMargins(0, 0, 0, 0)
            
            self.edit_new_url = QLineEdit(self.proposal.get('new_url', '') if case_id != 'A' else '')
            self.edit_new_url.setPlaceholderText("Paste new URL here to manually heal...")
            self.edit_new_url.textEdited.connect(self.set_needs_resolution)
            
            self.btn_resolve_new = QToolButton()
            self.btn_resolve_new.setText("🪄 Resolve")
            self.btn_resolve_new.setStyleSheet("background-color: #FFA726; color: black; font-weight: bold; padding: 4px; border-radius: 3px;")
            self.btn_resolve_new.clicked.connect(self.resolve_manual_new_url)
            
            url_lay_new.addWidget(self.edit_new_url)
            url_lay_new.addWidget(self.btn_resolve_new)
            
            btn_new_vid = QPushButton("🎬 Test URL")
            btn_new_vid.clicked.connect(lambda: webbrowser.open(self.edit_new_url.text().strip()))
            url_lay_new.addWidget(btn_new_vid)
            
            new_lay.addRow("Stream URL:", url_widget_new)
            
            if case_id != "A":
                conf = self.proposal['confidence'] * 100
                conf_color = "#00E676" if conf > 85 else "#FFB74D"
                new_lay.addRow("DNA Match Score:", QLabel(f"<span style='color: {conf_color}; font-weight: bold;'>{conf:.1f}%</span>"))
                
            split.addWidget(new_group)
            self.layout.addLayout(split)
        
        # --- ASSEMBLE SCROLL AREA ---
        self.scroll_area.setWidget(self.content_widget)
        self.main_layout.addWidget(self.scroll_area)
        
        btn_lay = QHBoxLayout()
        
        self.btn_cancel = QPushButton("Cancel (Keep in Queue)")
        self.btn_cancel.clicked.connect(self.reject) 
        
        self.btn_discard = QPushButton("❌ Reject")
        self.btn_discard.setStyleSheet("background-color: #D32F2F; color: white; font-weight: bold; padding: 10px;")
        self.btn_discard.clicked.connect(self._do_discard)

        if case_id == "N":
            self.btn_ignore = QPushButton("🚫 Reject & Ignore")
            self.btn_ignore.setStyleSheet("background-color: #F57C00; color: white; font-weight: bold; padding: 10px;")
            self.btn_ignore.clicked.connect(self._do_ignore)
            btn_lay.addWidget(self.btn_ignore)

        self.btn_discard_next = QPushButton("❌ Reject & Next")
        self.btn_discard_next.setStyleSheet("background-color: #B71C1C; color: white; font-weight: bold; padding: 10px;")
        self.btn_discard_next.clicked.connect(self._do_discard_next)
        
        if case_id == "N": btn_accept_txt = "✔ Add Stream & Close"
        elif case_id == "R": btn_accept_txt = "✔ Resurrect & Close"
        else: btn_accept_txt = "✔ Accept & Close"
            
        self.btn_accept = QPushButton(btn_accept_txt)
        self.btn_accept.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold; padding: 10px;")
        self.btn_accept.clicked.connect(self._do_accept)
        
        if case_id == "N": btn_accept_next_txt = "⏭ Add Stream & Next"
        elif case_id == "R": btn_accept_next_txt = "⏭ Resurrect & Next"
        else: btn_accept_next_txt = "⏭ Accept & Next"
            
        self.btn_accept_next = QPushButton(btn_accept_next_txt)
        self.btn_accept_next.setStyleSheet("background-color: #00897B; color: white; font-weight: bold; padding: 10px;")
        self.btn_accept_next.clicked.connect(self._do_accept_next)
        
        btn_lay.insertWidget(0, self.btn_cancel)
        btn_lay.insertStretch(1)
        
        if case_id != "N":
            btn_lay.addWidget(self.btn_discard)
            btn_lay.addWidget(self.btn_discard_next)
            
        btn_lay.addWidget(self.btn_accept)
        btn_lay.addWidget(self.btn_accept_next)
        
        self.main_layout.addSpacing(15)
        self.main_layout.addLayout(btn_lay)

    def set_needs_resolution(self):
        self.needs_resolution = True

    def resolve_manual_new_url(self):
        url = self.edit_new_url.text().strip()
        if not url: return
        self.btn_resolve_new.setEnabled(False)
        self.edit_new_title.setText("Resolving...")
        
        self.resolver_thread = ResolutionThread(url)
        self.resolver_thread.result_ready.connect(self.on_manual_resolution_complete)
        self.resolver_thread.start()
        
    def on_manual_resolution_complete(self, success, links, stype, msg, title, uploader):
        self.btn_resolve_new.setEnabled(True)
        if success:
            selected_link = links[0]
            if len(links) > 1:
                item, ok = QInputDialog.getItem(self, "Select Stream", f"Found {len(links)} streams. Please select one:", links, 0, False)
                if ok and item: selected_link = item
                else:
                    self.edit_new_title.setText("Selection Cancelled")
                    return
            
            self.edit_new_url.setText(selected_link)
            self.edit_new_title.setText(title if title else "Resolved (No Title)")
            if uploader:
                self.edit_new_chan.setCurrentText(uploader)
                
            self.proposal['original_yt_title'] = title
            self.proposal['stream_type'] = stype
            
            # Upgrade Case A to a Migration Case
            if self.proposal['case'] == 'A':
                self.proposal['case'] = 'M' # Manual Migration
                
            self.needs_resolution = False # Reset guardrail flag
        else:
            self.edit_new_title.setText("Resolution Failed!")
            QMessageBox.warning(self, "Resolve Error", msg)

    def _check_duplicates(self):
        if self.proposal['case'] == 'N':
            return True 
            
        new_url = self.edit_new_url.text().strip()
        if not new_url: return True
        
        clean_new = re.sub(r'[\?&]variant=\d+', '', new_url)
        old_url = self.proposal.get('old_url', '')
        clean_old = re.sub(r'[\?&]variant=\d+', '', old_url) if old_url else ""
        
        if CONFIG_FILE.exists():
            try:
                cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                for s in cfg.get('streams', []):
                    ex_clean = re.sub(r'[\?&]variant=\d+', '', s.get('page_url', ''))
                    if ex_clean == clean_new and ex_clean != clean_old:
                        reply = QMessageBox.question(
                            self, "Duplicate Detected",
                            f"The URL '{clean_new}' is already used by another stream.\n\n"
                            "Do you want to add this as a NEW VIEW (Variant)?\n"
                            "(Appends ?variant=timestamp to bypass duplicate checks)",
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                        )
                        if reply == QMessageBox.StandardButton.Yes:
                            sep = "&" if "?" in new_url else "?"
                            self.edit_new_url.setText(f"{new_url}{sep}variant={int(time.time())}")
                            return True
                        else:
                            return False
            except: pass
        return True

    def _check_zero_coords(self):
        if self.proposal['case'] == 'N':
            try:
                lat = float(self.edit_lat.text().strip())
                lon = float(self.edit_lon.text().strip())
            except ValueError:
                lat, lon = 0.0, 0.0
            
            if lat == 0.0 and lon == 0.0:
                reply = QMessageBox.warning(
                    self, 
                    "Missing Coordinates", 
                    "You are about to add this stream with coordinates (0.0, 0.0).\n\n"
                    "This stream will be treated as 'Global' and placed at the center of the map (Null Island).\n\n"
                    "Are you sure you want to proceed without setting specific GPS coordinates?", 
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return False
        return True

    def _on_gps_combo_changed(self, idx):
        data = self.combo_copy_gps.currentData()
        if data and (data.get('lat') != 0.0 or data.get('lon') != 0.0):
            self.edit_lat.blockSignals(True)
            self.edit_lon.blockSignals(True)
            self.edit_lat.setText(str(data.get('lat', 0.0)))
            self.edit_lon.setText(str(data.get('lon', 0.0)))
            self.edit_lat.blockSignals(False)
            self.edit_lon.blockSignals(False)

    def _on_global_gps_changed(self, idx):
        data = self.combo_global_gps.currentData()
        if data and (data.get('lat') != 0.0 or data.get('lon') != 0.0):
            self.edit_lat.blockSignals(True)
            self.edit_lon.blockSignals(True)
            self.edit_lat.setText(str(data.get('lat', 0.0)))
            self.edit_lon.setText(str(data.get('lon', 0.0)))
            self.edit_lat.blockSignals(False)
            self.edit_lon.blockSignals(False)

    def _on_translation_ready(self, result):
        if 'error' in result:
            self.lbl_lang.setText(f"❌ Translation failed: {result['error']}")
            self.lbl_lang.setStyleSheet("color: #EF5350; font-style: italic;")
            self.edit_trans_title.setText("N/A")
            self.text_trans_desc.setPlainText("N/A")
        else:
            lang = result.get('language', 'Unknown')
            t_title = result.get('translated_title', '').replace('\n', ' ').replace('\r', '').strip()
            t_desc = result.get('translated_description', '')
            orig_desc = result.get('original_description', '')
            
            if orig_desc and "No description" in self.text_orig_desc.toPlainText():
                self.text_orig_desc.setPlainText(orig_desc)
            
            self.lbl_lang.setText(f"✅ Detected: {lang}")
            self.lbl_lang.setStyleSheet("color: #00E676; font-weight: bold;")
            self.edit_trans_title.setText(t_title)
            self.text_trans_desc.setPlainText(t_desc if t_desc else "No description provided.")
            
            if self.edit_friendly_name.text() == self.proposal.get('new_title', ''):
                self.edit_friendly_name.setText(t_title)

    def _handle_smart_lat_paste(self, text):
        if ',' in text:
            try:
                parts = text.split(',')
                if len(parts) >= 2:
                    lat_val = float(parts[0].strip())
                    lon_val = float(parts[1].strip())
                    self.edit_lat.blockSignals(True)
                    self.edit_lat.setText(f"{lat_val:.5f}")
                    self.edit_lon.setText(f"{lon_val:.5f}")
                    self.edit_lat.blockSignals(False)
            except ValueError:
                pass

    def preview_map(self):
        lat = self.edit_lat.text().strip()
        lon = self.edit_lon.text().strip()
        try:
            float(lat); float(lon)
            webbrowser.open(f"https://www.google.com/maps/search/?api=1&query={lat},{lon}")
        except:
            QMessageBox.warning(self, "Invalid", "Please enter valid numbers.")

    def show_siblings(self):
        siblings = self.proposal.get('sibling_streams',[])
        if not siblings: return
        
        d = QDialog(self)
        d.setWindowTitle("Matched Sibling Streams")
        d.resize(400, 300)
        lay = QVBoxLayout(d)
        
        info = QLabel("The following streams share the exact coordinates suggested:")
        info.setWordWrap(True)
        lay.addWidget(info)
        
        lw = QListWidget()
        for s in siblings:
            lw.addItem(QListWidgetItem(s))
        lay.addWidget(lw)
        
        btn = QPushButton("Close")
        btn.clicked.connect(d.accept)
        lay.addWidget(btn)
        d.exec()

    def _do_discard(self):
        self.result_action = 'discard'
        self.accept()

    def _do_discard_next(self):
        self.result_action = 'discard_next'
        self.accept()
        
    def _do_ignore(self):
        self.result_action = 'ignore'
        self.accept()

    def _do_accept(self):
        if not self._check_zero_coords(): return
        
        if getattr(self, 'needs_resolution', False) and self.proposal['case'] != 'N':
            QMessageBox.warning(self, "Resolution Required", "You have entered or modified the Stream URL.\n\nPlease click the '🪄 Resolve' button to test the stream and automatically fetch the official Title and Channel Name before accepting.")
            return
            
        if self.proposal['case'] != 'N':
            if not self._check_duplicates(): return
            self.proposal['new_url'] = self.edit_new_url.text().strip()
            self.proposal['new_channel_name'] = self.edit_new_chan.currentText().strip()
            self.proposal['new_title'] = self.edit_new_title.text().strip()
            
            if self.proposal['case'] == 'A' and self.proposal['new_url']:
                self.proposal['case'] = 'M' 
                
        self.result_action = 'accept'
        self.accept()

    def _do_accept_next(self):
        if not self._check_zero_coords(): return
        
        if getattr(self, 'needs_resolution', False) and self.proposal['case'] != 'N':
            QMessageBox.warning(self, "Resolution Required", "You have entered or modified the Stream URL.\n\nPlease click the '🪄 Resolve' button to test the stream and automatically fetch the official Title and Channel Name before accepting.")
            return
            
        if self.proposal['case'] != 'N':
            if not self._check_duplicates(): return
            self.proposal['new_url'] = self.edit_new_url.text().strip()
            self.proposal['new_channel_name'] = self.edit_new_chan.currentText().strip()
            self.proposal['new_title'] = self.edit_new_title.text().strip()
            
            if self.proposal['case'] == 'A' and self.proposal['new_url']:
                self.proposal['case'] = 'M' 
                
        self.result_action = 'accept_next'
        self.accept()


class QuickAddStreamDialog(QDialog):
    def __init__(self, title, url, parent=None):
        super().__init__(parent)
        self.original_yt_title = title  # Step 2.1 & 2.2: Save original YouTube title from Radar
        self.setWindowTitle("Add Discovered Stream")
        self.setMinimumWidth(550)
        self.layout = QVBoxLayout(self)
        
        form = QFormLayout()
        
        self.edit_name = QLineEdit(title)
        
        url_lay = QHBoxLayout()
        self.edit_url = QLineEdit(url)
        btn_test_url = QPushButton("🌐 Test Vid")
        btn_test_url.clicked.connect(lambda: webbrowser.open(self.edit_url.text().strip()))
        url_lay.addWidget(self.edit_url)
        url_lay.addWidget(btn_test_url)
        
        self.edit_lat = QLineEdit("0.0")
        self.edit_lon = QLineEdit("0.0")
        
        self.edit_lat.textChanged.connect(self._handle_smart_lat_paste)
        
        # --- THE UNIVERSAL GPS SEARCH PATCH ---
        self.combo_global_gps = QComboBox()
        self.combo_global_gps.setEditable(True)
        self.combo_global_gps.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo_global_gps.addItem("-- Type to search all streams globally... --", {"lat": 0.0, "lon": 0.0})
        
        if CONFIG_FILE.exists():
            try:
                live_cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                sorted_streams = sorted(live_cfg.get('streams',[]), key=lambda x: x.get('name', '').lower())
                
                for s in sorted_streams:
                    lat_val, lon_val = s.get('lat', 0.0), s.get('lon', 0.0)
                    if lat_val != 0.0 or lon_val != 0.0:
                        self.combo_global_gps.addItem(s.get('name', 'Unknown'), {"lat": lat_val, "lon": lon_val})
            except Exception: pass
            
        completer = self.combo_global_gps.completer()
        if completer:
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            
        self.combo_global_gps.currentIndexChanged.connect(self._on_global_gps_changed)
        form.addRow("Search Global GPS:", self.combo_global_gps)
        # --------------------------------------
        
        coord_lay = QHBoxLayout()
        coord_lay.addWidget(QLabel("Lat:"))
        coord_lay.addWidget(self.edit_lat)
        coord_lay.addWidget(QLabel("Lon:"))
        coord_lay.addWidget(self.edit_lon)
        btn_test_map = QPushButton("🗺️ Test Map")
        btn_test_map.clicked.connect(self.test_map)
        coord_lay.addWidget(btn_test_map)
        
        form.addRow("Friendly Name:", self.edit_name)
        form.addRow("URL:", url_lay)
        form.addRow("Coordinates:", coord_lay)
        
        self.layout.addLayout(form)
        
        info = QLabel("<i>Tip: Enter 0.0 / 0.0 if you want to classify this as a 'Global' stream without specific map coordinates.</i>")
        info.setWordWrap(True)
        info.setStyleSheet("color: #aaa; margin-top: 10px;")
        self.layout.addWidget(info)
        
        btns = QHBoxLayout()
        self.btn_save = QPushButton("Add to Configuration")
        self.btn_save.setStyleSheet("background-color: #00897B; color: white; font-weight: bold; padding: 8px;")
        self.btn_save.clicked.connect(self._do_accept)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        
        btns.addStretch()
        btns.addWidget(self.btn_cancel)
        btns.addWidget(self.btn_save)
        self.layout.addLayout(btns)

    def _do_accept(self):
        try:
            lat = float(self.edit_lat.text().strip())
            lon = float(self.edit_lon.text().strip())
        except ValueError:
            lat, lon = 0.0, 0.0
            
        if lat == 0.0 and lon == 0.0:
            reply = QMessageBox.warning(
                self, 
                "Missing Coordinates", 
                "You are about to add this stream with coordinates (0.0, 0.0).\n\n"
                "This stream will be treated as 'Global' and placed at the center of the map (Null Island).\n\n"
                "Are you sure you want to proceed without setting specific GPS coordinates?", 
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.accept()

    def _on_global_gps_changed(self, idx):
        data = self.combo_global_gps.currentData()
        if data and (data.get('lat') != 0.0 or data.get('lon') != 0.0):
            self.edit_lat.blockSignals(True)
            self.edit_lon.blockSignals(True)
            self.edit_lat.setText(str(data.get('lat', 0.0)))
            self.edit_lon.setText(str(data.get('lon', 0.0)))
            self.edit_lat.blockSignals(False)
            self.edit_lon.blockSignals(False)

    def _handle_smart_lat_paste(self, text):
        if ',' in text:
            try:
                parts = text.split(',')
                if len(parts) >= 2:
                    lat_val = float(parts[0].strip())
                    lon_val = float(parts[1].strip())
                    self.edit_lat.blockSignals(True)
                    self.edit_lat.setText(f"{lat_val:.5f}")
                    self.edit_lon.setText(f"{lon_val:.5f}")
                    self.edit_lat.blockSignals(False)
            except ValueError:
                pass

    def test_map(self):
        lat = self.edit_lat.text().strip()
        lon = self.edit_lon.text().strip()
        try:
            float(lat); float(lon)
            webbrowser.open(f"https://www.google.com/maps/search/?api=1&query={lat},{lon}")
        except:
            QMessageBox.warning(self, "Invalid Coordinates", "Please enter valid numbers for Latitude and Longitude.")
        
    def get_stream_data(self):
        try:
            lat = float(self.edit_lat.text().strip())
            lon = float(self.edit_lon.text().strip())
        except ValueError:
            lat, lon = 0.0, 0.0
            
        return {
            "name": self.edit_name.text().strip(),
            "original_yt_title": getattr(self, 'original_yt_title', ''), # Step 2.1 & 2.2: Save original YT title
            "page_url": self.edit_url.text().strip(),
            "lat": lat,
            "lon": lon,
            "enabled": True,
            "created_at": time.time(),
            "updated_at": time.time()
        }

# ==============================================================================
# MAIN HUB DIALOG
# ==============================================================================

class StreamDiscoveryHub(QDialog):
    def __init__(self, editor_ref=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Stream Maintenance & Discovery Hub")
        self.setMinimumSize(1250, 750)
        self.editor_ref = editor_ref 
        
        if hasattr(self.editor_ref, 'discovery_state_cache'):
            self.state = self.editor_ref.discovery_state_cache
            self.state.setdefault('radar_results',[])
            self.state.setdefault('added_urls', set())
            self.state.setdefault('marked_radar', set())
            
            self.state.setdefault('radar_sort_col', 1)
            self.state.setdefault('radar_sort_order', Qt.SortOrder.AscendingOrder)
            
            self.state.setdefault('sync_proposals',[])
        else:
            self.state = {
                'radar_results':[],
                'added_urls': set(),
                'marked_radar': set(),
                'radar_sort_col': 1,
                'radar_sort_order': Qt.SortOrder.AscendingOrder,
                'sync_proposals':[]
            }
            
        if not self.state['sync_proposals'] and SYNC_PROPOSALS_FILE.exists():
            try:
                disk_proposals = json.loads(SYNC_PROPOSALS_FILE.read_text(encoding='utf-8'))
                if isinstance(disk_proposals, list):
                    self.state['sync_proposals'] = disk_proposals
            except Exception as e:
                logging.error(f"Failed to load sync_proposals.json from disk: {e}")
                
        self.layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        
        # --- TAB 1: LIFECYCLE & SYNC ENGINE (NOW INDEX 0) ---
        self.tab_sync = QWidget()
        sync_layout = QVBoxLayout(self.tab_sync)
        
        sync_splitter = QSplitter(Qt.Orientation.Vertical)

        # TOP: Channel Roster
        roster_group = QGroupBox("Channel Roster & Sync Strategy")
        roster_layout = QVBoxLayout(roster_group)

        roster_header = QHBoxLayout()
        roster_info = QLabel("Manage background scanning behaviors per channel.")
        roster_header.addWidget(roster_info)
        roster_header.addStretch()
        
        self.btn_manage_blacklist = QPushButton("🚫 Manage Blacklist")
        self.btn_manage_blacklist.setStyleSheet("background-color: #F57C00; color: white; font-weight: bold;")
        self.btn_manage_blacklist.clicked.connect(self.open_blacklist_manager)
        roster_header.addWidget(self.btn_manage_blacklist)
        
        self.btn_legend = QPushButton("📖 View Legend & Key")
        self.btn_legend.setStyleSheet("background-color: #555; color: white;")
        self.btn_legend.clicked.connect(self.open_legend)
        roster_header.addWidget(self.btn_legend)
        
        self.btn_sync_settings = QPushButton("⚙️ Sync Settings")
        self.btn_sync_settings.clicked.connect(self.open_sync_settings)
        roster_header.addWidget(self.btn_sync_settings)
        roster_layout.addLayout(roster_header)
        
        roster_search_layout = QHBoxLayout()
        roster_search_layout.addWidget(QLabel("<b>Universal Search:</b>"))
        self.roster_search_box = QLineEdit()
        self.roster_search_box.setPlaceholderText("Filter channels by name...")
        self.roster_search_box.textChanged.connect(self.filter_roster_table)
        roster_search_layout.addWidget(self.roster_search_box)
        roster_layout.addLayout(roster_search_layout)

        self.roster_table = QTableWidget()
        self.roster_table.setColumnCount(5)
        self.roster_table.setHorizontalHeaderLabels(["Channel Name", "Configured Streams", "Live Streams", "Sync Strategy", "Action"])
        self.roster_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.roster_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.roster_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.roster_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.roster_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.roster_table.setSortingEnabled(True)
        
        self.roster_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.roster_table.customContextMenuRequested.connect(self.roster_context_menu)
        
        roster_layout.addWidget(self.roster_table)

        sync_splitter.addWidget(roster_group)

        # BOTTOM: Resolution Center
        res_group = QGroupBox("Resolution Center")
        res_layout = QVBoxLayout(res_group)

        res_header = QHBoxLayout()
        res_info = QLabel("Review proposed changes from the Sync Engine.")
        res_header.addWidget(res_info)
        res_header.addStretch()

        self.combo_sync_filter = QComboBox()
        self.combo_sync_filter.addItems([
            "Show All Proposals", 
            "Auto-Heal Eligible (⚡) Only", 
            "Manual Review Required (✋) Only",
            "New Streams (Case N) Only",
            "Modifications (Cases A-H, R, M) Only"
        ])
        self.combo_sync_filter.currentIndexChanged.connect(self.filter_sync_tree)
        res_header.addWidget(self.combo_sync_filter)

        # --- THE UNIVERSAL SEARCH PATCH ---
        self.sync_search_box = QLineEdit()
        self.sync_search_box.setPlaceholderText("Search proposals...")
        self.sync_search_box.textChanged.connect(self.filter_sync_tree)
        res_header.addWidget(self.sync_search_box)
        # ----------------------------------

        self.lbl_proposal_count = QLabel("Proposals: 0 remaining")
        self.lbl_proposal_count.setFixedWidth(280)
        self.lbl_proposal_count.setStyleSheet("color: #aaa; font-weight: bold; margin-left: 10px; margin-right: 10px;")
        res_header.addWidget(self.lbl_proposal_count)

        self.sync_progress_bar = QProgressBar()
        self.sync_progress_bar.setVisible(False)
        self.sync_progress_bar.setFixedWidth(250) 
        res_header.addWidget(self.sync_progress_bar)
        
        self.lbl_sync_status = QLabel("")
        self.lbl_sync_status.setVisible(False)
        self.lbl_sync_status.setMinimumWidth(350) 
        self.lbl_sync_status.setStyleSheet("color: #00E676; font-weight: bold; margin-left: 10px;")
        self.lbl_sync_status.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        res_header.addWidget(self.lbl_sync_status)

        self.btn_run_sync = QPushButton("▶ Run Full Sync")
        self.btn_run_sync.setStyleSheet("background-color: #5C6BC0; color: white; font-weight: bold;")
        self.btn_run_sync.clicked.connect(self.start_sync_engine)
        res_header.addWidget(self.btn_run_sync)
        res_layout.addLayout(res_header)

        self.tree_sync = QTreeWidget()
        self.tree_sync.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree_sync.setHeaderLabels(["Status / Proposal", "Case", "Similarity", "Action"])
        self.tree_sync.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree_sync.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree_sync.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree_sync.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.tree_sync.setIndentation(20)
        self.tree_sync.itemDoubleClicked.connect(self.handle_sync_click)
        
        self.tree_sync.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_sync.customContextMenuRequested.connect(self.sync_tree_context_menu)
        
        res_layout.addWidget(self.tree_sync)

        sync_splitter.addWidget(res_group)
        sync_layout.addWidget(sync_splitter)

        self.tabs.addTab(self.tab_sync, "🔄 Lifecycle & Sync Engine")


        # --- TAB 2: TRIAGE WARD ---
        self.tab_triage = QWidget()
        triage_layout = QVBoxLayout(self.tab_triage)
        
        toolbar_layout = QHBoxLayout()
        toolbar_layout.addWidget(QLabel("<b>Smart Select:</b>"))
        
        btn_terminal = QPushButton("[FATAL]"); btn_terminal.setStyleSheet("background-color: #B71C1C; color: white; font-weight: bold;")
        btn_terminal.clicked.connect(lambda: self.select_by_type(self.triage_table, "FATAL", 1, 5))
        
        btn_suspended = QPushButton("[SUSPENDED]"); btn_suspended.setStyleSheet("background-color: #AB47BC; color: white; font-weight: bold;")
        btn_suspended.clicked.connect(lambda: self.select_by_type(self.triage_table, "SUSPENDED", 1, 5))
        
        btn_unresponsive = QPushButton("[UNRESPONSIVE]"); btn_unresponsive.setStyleSheet("background-color: #EF5350; color: white; font-weight: bold;")
        btn_unresponsive.clicked.connect(lambda: self.select_by_type(self.triage_table, "UNRESPONSIVE", 1, 5))

        btn_intermittent = QPushButton("[INTERMITTENT]"); btn_intermittent.setStyleSheet("background-color: #FFB74D; color: black;")
        btn_intermittent.clicked.connect(lambda: self.select_by_type(self.triage_table, "INTERMITTENT", 1, 5))

        btn_failure = QPushButton("[FAILURE]"); btn_failure.setStyleSheet("background-color: #ffe0b2; color: black;")
        btn_failure.clicked.connect(lambda: self.select_by_type(self.triage_table, "FAILURE", 1, 5))
        
        btn_glitch = QPushButton("[GLITCH]"); btn_glitch.setStyleSheet("background-color: #80DEEA; color: black; font-weight: bold;")
        btn_glitch.clicked.connect(lambda: self.select_by_type(self.triage_table, "GLITCH", 1, 5))
        
        btn_loop = QPushButton("[LOOP]"); btn_loop.setStyleSheet("background-color: #e1bee7; color: black;")
        btn_loop.clicked.connect(lambda: self.select_by_type(self.triage_table, "LOOP", 1, 5))
        
        btn_silent_vis = QPushButton("[MIC DEAD (Vision Active)]"); btn_silent_vis.setStyleSheet("background-color: #00897B; color: white; font-weight: bold;")
        btn_silent_vis.clicked.connect(lambda: self.select_by_type(self.triage_table, "MIC DEAD (Vision Active)", 1, 5))
        
        btn_all_triage = QPushButton("[Select All]"); btn_all_triage.clicked.connect(lambda: self.select_all(self.triage_table, 5))
        btn_none_triage = QPushButton("[Deselect All]"); btn_none_triage.clicked.connect(lambda: self.deselect_all(self.triage_table, 5))
        
        toolbar_layout.addWidget(btn_all_triage)
        toolbar_layout.addWidget(btn_terminal); toolbar_layout.addWidget(btn_suspended); toolbar_layout.addWidget(btn_unresponsive)
        toolbar_layout.addWidget(btn_intermittent); toolbar_layout.addWidget(btn_failure); toolbar_layout.addWidget(btn_glitch); 
        toolbar_layout.addWidget(btn_loop); toolbar_layout.addWidget(btn_silent_vis)
        toolbar_layout.addWidget(btn_none_triage)
        toolbar_layout.addStretch()
        
        triage_layout.addLayout(toolbar_layout)

        # --- TRIAGE SEARCH LAYOUT ---
        triage_search_layout = QHBoxLayout()
        triage_search_layout.addWidget(QLabel("<b>Universal Search:</b>"))
        self.triage_search_box = QLineEdit()
        self.triage_search_box.setPlaceholderText("Filter streams by name or status...")
        self.triage_search_box.textChanged.connect(self.filter_triage_table)
        triage_search_layout.addWidget(self.triage_search_box)
        triage_layout.addLayout(triage_search_layout)
        
        self.triage_table = QTableWidget()
        self.triage_table.setColumnCount(6)
        self.triage_table.setHorizontalHeaderLabels(["Stream Name", "Status", "Strikes (7d)", "Last Pulse", "Penalty Wait", "Select"])
        self.triage_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.triage_table.setSortingEnabled(True)

        # --- TRIAGE CONTEXT MENU BINDING ---
        self.triage_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.triage_table.customContextMenuRequested.connect(self.triage_context_menu)

        triage_layout.addWidget(self.triage_table)
        
        triage_btn_layout = QHBoxLayout()
        
        self.btn_amnesty_selected = QPushButton("✨ Amnesty Selected (Clear Penalties)")
        self.btn_amnesty_selected.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
        self.btn_amnesty_selected.clicked.connect(self.amnesty_selected)
        
        self.btn_disable_selected = QPushButton("Disable & Tag Selected")
        self.btn_disable_selected.setStyleSheet("background-color: #ff5252; color: white; font-weight: bold;")
        self.btn_disable_selected.clicked.connect(self.disable_selected)
        
        triage_btn_layout.addStretch()
        triage_btn_layout.addWidget(self.btn_amnesty_selected)
        triage_btn_layout.addWidget(self.btn_disable_selected)
        triage_layout.addLayout(triage_btn_layout)
        
        self.tabs.addTab(self.tab_triage, "🏥 Triage Ward")

        # --- TAB 3: GRAVEYARD ---
        self.tab_grave = QWidget()
        grave_layout = QVBoxLayout(self.tab_grave)
        
        grave_toolbar_layout = QHBoxLayout()
        grave_toolbar_layout.addWidget(QLabel("<b>Smart Select:</b>"))
        
        g_btn_all = QPushButton("[Select All]"); g_btn_all.clicked.connect(lambda: self.select_all(self.grave_table, 2))
        
        g_btn_fatal = QPushButton("[FATAL]"); g_btn_fatal.setStyleSheet("background-color: #B71C1C; color: white;")
        g_btn_fatal.clicked.connect(lambda: self.select_by_type(self.grave_table, "FATAL", 1, 2))
        
        g_btn_silent = QPushButton("[SILENT]"); g_btn_silent.setStyleSheet("background-color: #cfd8dc; color: black;")
        g_btn_silent.clicked.connect(lambda: self.select_by_type(self.grave_table, "SILENT", 1, 2))
        
        g_btn_loop = QPushButton("[LOOP]"); g_btn_loop.setStyleSheet("background-color: #e1bee7; color: black;")
        g_btn_loop.clicked.connect(lambda: self.select_by_type(self.grave_table, "LOOP", 1, 2))
        
        g_btn_none = QPushButton("[Deselect All]"); g_btn_none.clicked.connect(lambda: self.deselect_all(self.grave_table, 2))
        
        grave_toolbar_layout.addWidget(g_btn_all); grave_toolbar_layout.addWidget(g_btn_fatal)
        grave_toolbar_layout.addWidget(g_btn_silent); grave_toolbar_layout.addWidget(g_btn_loop)
        grave_toolbar_layout.addWidget(g_btn_none); grave_toolbar_layout.addStretch()
        
        grave_layout.addLayout(grave_toolbar_layout)
        
        self.grave_table = QTableWidget()
        self.grave_table.setColumnCount(3)
        self.grave_table.setHorizontalHeaderLabels(["Stream Name (Tagged)", "Original Reason", "Select"])
        self.grave_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.grave_table.setSortingEnabled(True)
        grave_layout.addWidget(self.grave_table)
        
        grave_btn_layout = QHBoxLayout()
        self.btn_revive = QPushButton("Revive Selected")
        self.btn_revive.setStyleSheet("background-color: #66bb6a; color: white; font-weight: bold;")
        self.btn_revive.clicked.connect(self.revive_selected)
        grave_btn_layout.addStretch()
        grave_btn_layout.addWidget(self.btn_revive)
        grave_layout.addLayout(grave_btn_layout)

        self.tabs.addTab(self.tab_grave, "🪦 The Graveyard")

        
        # --- TAB 4: DISCOVERY RADAR ---
        self.tab_radar = QWidget()
        radar_layout = QVBoxLayout(self.tab_radar)
        
        radar_info = QLabel("<b>Discovery Radar:</b> Search YouTube for keywords (e.g., 'live bird feeder', 'african watering hole live'). The radar highlights streams you already have and lets you quickly test and add new ones.")
        radar_info.setWordWrap(True)
        radar_layout.addWidget(radar_info)
        
        radar_controls = QHBoxLayout()
        self.edit_search = QLineEdit()
        self.edit_search.setPlaceholderText("Enter keywords...")
        self.edit_search.returnPressed.connect(self.start_discovery)
        
        self.btn_search = QPushButton("📡 Run Radar")
        self.btn_search.setStyleSheet("background-color: #00897B; color: white; font-weight: bold; padding: 8px;")
        self.btn_search.clicked.connect(self.start_discovery)
        
        self.lbl_radar_status = QLabel("Ready.")
        
        radar_controls.addWidget(self.edit_search, 1)
        radar_controls.addWidget(self.btn_search)
        radar_controls.addWidget(self.lbl_radar_status)
        radar_layout.addLayout(radar_controls)
        
        self.table_radar = QTableWidget()
        self.table_radar.setColumnCount(5)
        self.table_radar.setHorizontalHeaderLabels(["Mark", "Status", "Stream Title", "Channel/Uploader", "Links", "Action"])
        self.table_radar.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table_radar.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table_radar.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table_radar.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.table_radar.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table_radar.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        
        self.table_radar.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_radar.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_radar.horizontalHeader().sortIndicatorChanged.connect(self.save_radar_sort_state)
        radar_layout.addWidget(self.table_radar)
        
        self.tabs.addTab(self.tab_radar, "📡 Discovery Radar")
        
        self.layout.addWidget(self.tabs)
        
        self.btn_close = QPushButton("Close Window (Data will persist in background)")
        self.btn_close.clicked.connect(self.accept)
        self.layout.addWidget(self.btn_close)

        self.radar_worker = None
        self.sync_worker = None

        self.load_maintenance_data()

        if self.state['radar_results']:
            self.populate_radar_table(self.state['radar_results'], from_cache=True)
            
        if self.state['sync_proposals']:
            self.populate_sync_tree(self.state['sync_proposals'])

        self.populate_roster_table()

    # --- TRIAGE CONTEXT MENU ---
    def triage_context_menu(self, pos):
        item = self.triage_table.itemAt(pos)
        if not item: return
        row = item.row()
        name_item = self.triage_table.item(row, 0)
        if name_item:
            url = name_item.data(Qt.ItemDataRole.UserRole)
            if url:
                menu = QMenu()
                action = QAction("🌐 Open Stream URL", self)
                action.triggered.connect(lambda: webbrowser.open(url))
                menu.addAction(action)
                menu.exec(self.triage_table.viewport().mapToGlobal(pos))

    # --- TRIAGE SEARCH FILTER ---
    def filter_triage_table(self):
        text = self.triage_search_box.text().strip().lower()
        for i in range(self.triage_table.rowCount()):
            match = False
            for j in range(2): 
                item = self.triage_table.item(i, j)
                if item and text in item.text().lower():
                    match = True
                    break
            self.triage_table.setRowHidden(i, not match)

    # --- THE BLACKLIST MANAGER ---
    def open_blacklist_manager(self):
        d = BlacklistManagerDialog(self)
        d.exec()

    # --- THE BULK IGNORE PATCH ---
    def bulk_reject_and_ignore(self, proposals_to_ignore, skip_confirm=False):
        if not proposals_to_ignore: return
        
        if not skip_confirm:
            msg = f"You are about to Reject and permanently Blacklist {len(proposals_to_ignore)} new streams.\n\nThey will be hidden from future syncs unless their titles change significantly.\nProceed?"
            if QMessageBox.question(self, "Confirm Bulk Ignore", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
                
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            if "sync_engine_settings" not in cfg: cfg["sync_engine_settings"] = {}
            if "ignored_proposals" not in cfg["sync_engine_settings"]: cfg["sync_engine_settings"]["ignored_proposals"] = {}
            
            ignored_dict = cfg["sync_engine_settings"]["ignored_proposals"]
            
            for p in proposals_to_ignore:
                url = p.get('new_url')
                title = p.get('new_title', '')
                if url:
                    clean_url = re.sub(r'[\?&]variant=\d+', '', url).strip()
                    ignored_dict[clean_url] = title
                    
            self.safe_config_write(cfg)
            
            for p in proposals_to_ignore:
                self._remove_proposal(p)
                
            if not skip_confirm:
                QMessageBox.information(self, "Success", f"Successfully ignored {len(proposals_to_ignore)} streams.")
        except Exception as e:
            if not skip_confirm:
                QMessageBox.critical(self, "Error", f"Failed to bulk ignore: {e}")
            else:
                logging.error(f"Failed to ignore proposal: {e}")

    def roster_context_menu(self, pos):
        item = self.roster_table.itemAt(pos)
        if not item: return
        row = item.row()
        channel_name = self.roster_table.item(row, 0).text()
        
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            channel_url = cfg.get("channels", {}).get(channel_name, "")
            if channel_url:
                menu = QMenu()
                action = QAction("🌐 Open Channel URL", self)
                action.triggered.connect(lambda: webbrowser.open(channel_url))
                menu.addAction(action)
                menu.exec(self.roster_table.viewport().mapToGlobal(pos))
        except Exception as e:
            logging.error(f"Context menu error: {e}")

    def sync_tree_context_menu(self, pos):
        menu = QMenu()
        selected_items = self.tree_sync.selectedItems()
        
        # --- THE MULTI-SELECT BULK IGNORE PATCH ---
        if len(selected_items) > 1:
            case_n_items =[i for i in selected_items if i.data(0, Qt.ItemDataRole.UserRole) and i.data(0, Qt.ItemDataRole.UserRole).get('case') == 'N']
            if case_n_items:
                act_bulk_ignore = QAction(f"🚫 Reject & Ignore Selected ({len(case_n_items)} New Streams)", self)
                act_bulk_ignore.triggered.connect(lambda: self.bulk_reject_and_ignore([i.data(0, Qt.ItemDataRole.UserRole) for i in case_n_items]))
                menu.addAction(act_bulk_ignore)
        else:
            item = self.tree_sync.itemAt(pos)
            if not item: return
            p = item.data(0, Qt.ItemDataRole.UserRole)
            
            if p:
                if p.get('case') == 'N':
                    act_ignore = QAction("🚫 Reject & Ignore this New Stream", self)
                    act_ignore.triggered.connect(lambda: self.bulk_reject_and_ignore([p]))
                    menu.addAction(act_ignore)
                    
                old_url = p.get('old_url')
                new_url = p.get('new_url')
                
                if old_url:
                    act_old = QAction("🎬 Open Original Stream URL", self)
                    act_old.triggered.connect(lambda: webbrowser.open(old_url))
                    menu.addAction(act_old)
                if new_url:
                    act_new = QAction("✨ Open Suggested Stream URL", self)
                    act_new.triggered.connect(lambda: webbrowser.open(new_url))
                    menu.addAction(act_new)
                    
                old_chan = p.get('old_channel')
                if old_chan:
                    act_c_old = QAction("🏠 Open Original Channel URL", self)
                    act_c_old.triggered.connect(lambda: webbrowser.open(old_chan))
                    menu.addAction(act_c_old)
                    
            else:
                if item.childCount() > 0:
                    case_n_children =[]
                    for i in range(item.childCount()):
                        child_p = item.child(i).data(0, Qt.ItemDataRole.UserRole)
                        if child_p and child_p.get('case') == 'N':
                            case_n_children.append(child_p)
                            
                    if case_n_children:
                        act_ignore_all = QAction(f"🚫 Reject & Ignore ALL New Streams ({len(case_n_children)}) in this Channel", self)
                        act_ignore_all.triggered.connect(lambda: self.bulk_reject_and_ignore(case_n_children))
                        menu.addAction(act_ignore_all)
                        
                    first_child = item.child(0)
                    p_child = first_child.data(0, Qt.ItemDataRole.UserRole)
                    if p_child:
                        c_url = p_child.get('old_channel') or p_child.get('new_channel')
                        if c_url:
                            act_chan = QAction("🌐 Open Channel URL", self)
                            act_chan.triggered.connect(lambda: webbrowser.open(c_url))
                            menu.addAction(act_chan)
                            
        if menu.actions():
            menu.exec(self.tree_sync.viewport().mapToGlobal(pos))

    def open_legend(self):
        d = LegendDialog(self)
        d.exec()

    # --- SYNC SETTINGS ---
    def open_sync_settings(self):
        d = SyncSettingsDialog(self)
        if d.exec():
            vals = d.get_values()
            try:
                cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                cfg["sync_engine_settings"] = vals
                self.safe_config_write(cfg)
                QMessageBox.information(self, "Saved", "Sync settings updated.")
                self.populate_roster_table()
            except Exception as e:
                logging.error(f"Failed to save sync settings: {e}")

    # --- ROSTER LOGIC ---
    def filter_roster_table(self):
        text = self.roster_search_box.text().strip().lower()
        for i in range(self.roster_table.rowCount()):
            match = False
            for j in range(3): 
                item = self.roster_table.item(i, j)
                if item and text in item.text().lower():
                    match = True
                    break
            self.roster_table.setRowHidden(i, not match)

    def populate_roster_table(self):
        self.roster_table.setSortingEnabled(False)
        self.roster_table.setRowCount(0)
        
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        except:
            cfg = {}
            
        channels = cfg.get("channels", {})
        channel_rules = cfg.get("channel_sync_rules", {})
        ignored_channels = set(cfg.get("ignored_channels",[]))
        
        configured_counts = Counter()
        for s in cfg.get("streams",[]):
            cn = s.get("channel_name", "").strip()
            if cn:
                configured_counts[cn] += 1
        
        live_counts = {}
        try:
            if METADATA_CACHE_FILE.exists():
                cache = json.loads(METADATA_CACHE_FILE.read_text(encoding='utf-8'))
                live_counts = cache.get("channel_live_counts", {})
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
                    
        self.roster_table.setRowCount(len(channels))
        row = 0
        for c_name, c_url in sorted(channels.items(), key=lambda x: x[0].lower()):
            item_name = QTableWidgetItem(c_name)
            item_name.setToolTip(c_url)
            item_name.setFlags(item_name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.roster_table.setItem(row, 0, item_name)
            
            conf_count = configured_counts.get(c_name, 0)
            item_conf_count = NumericItem(str(conf_count))
            item_conf_count.setFlags(item_conf_count.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.roster_table.setItem(row, 1, item_conf_count)
            
            count = live_counts.get(c_url, "Unknown")
            item_count = NumericItem(str(count))
            item_count.setFlags(item_count.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.roster_table.setItem(row, 2, item_count)
            
            combo = QComboBox()
            combo.addItems(["Auto", "Always Scan", "Never Scan"])
            current_rule = channel_rules.get(c_url, "Auto")
            
            if current_rule == "Auto" and c_url in ignored_channels:
                combo.setItemText(0, "Auto (Ignored)")
                
            combo.setCurrentText(current_rule if current_rule != "Auto" else combo.itemText(0))
            combo.currentIndexChanged.connect(lambda idx, url=c_url, cb=combo: self.update_channel_strategy(url, cb.currentText()))
            self.roster_table.setCellWidget(row, 3, combo)
            
            btn_scan = QPushButton("▶ Force Scan")
            btn_scan.setStyleSheet("background-color: #00897B; color: white; font-weight: bold;")
            btn_scan.clicked.connect(lambda chk, u=c_url, n=c_name: self.force_scan_channel(n, u))
            self.roster_table.setCellWidget(row, 4, btn_scan)
            
            row += 1
            
        self.roster_table.setSortingEnabled(True)

    def update_channel_strategy(self, url, strategy_text):
        strategy = strategy_text.replace(" (Ignored)", "")
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            if "channel_sync_rules" not in cfg:
                cfg["channel_sync_rules"] = {}
            cfg["channel_sync_rules"][url] = strategy
            self.safe_config_write(cfg)
        except Exception as e:
            logging.error(f"Failed to update channel strategy: {e}")

    def force_scan_channel(self, channel_name, url):
        if not url: return
            
        self.progress_dialog = QProgressDialog(f"Surgically Scanning '{channel_name}'...", "Cancel", 0, 100, self)
        self.progress_dialog.setWindowTitle("Force Scan")
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setAutoClose(True)
        
        self.scan_worker = ChannelSyncWorker(url)
        self.scan_worker.progress_update.connect(self._update_scan_progress)
        self.scan_worker.finished.connect(self._on_channel_scan_finished)
        self.scan_worker.start()
        self.progress_dialog.exec()
        
    def _update_scan_progress(self, c, t, msg):
        if hasattr(self, 'progress_dialog') and self.progress_dialog.wasCanceled():
            if hasattr(self, 'scan_worker'): self.scan_worker.terminate()
            if hasattr(self, 'sync_worker'): self.sync_worker.terminate()
            return
            
        if hasattr(self, 'progress_dialog') and self.progress_dialog.isVisible():
            self.progress_dialog.setMaximum(t)
            self.progress_dialog.setValue(c)
            self.progress_dialog.setLabelText(msg)
            
        self.sync_progress_bar.setMaximum(t)
        self.sync_progress_bar.setValue(c)
        self.lbl_sync_status.setText(msg)

    def _on_channel_scan_finished(self, proposals):
        if hasattr(self, 'progress_dialog'):
            self.progress_dialog.accept()
        try:
            existing =[]
            if SYNC_PROPOSALS_FILE.exists():
                existing = json.loads(SYNC_PROPOSALS_FILE.read_text(encoding='utf-8'))
            
            target_url = self.scan_worker.target_url
            filtered =[p for p in existing if p.get('old_channel') != target_url and p.get('new_channel') != target_url]
            filtered.extend(proposals)
            
            SYNC_PROPOSALS_FILE.write_text(json.dumps(filtered, indent=2), encoding='utf-8')
            
            QMessageBox.information(self, "Scan Complete", f"Force Scan completed.\nFound {len(proposals)} updates/streams for this channel.\nReview them in the Resolution Center below.")
            
            self.state['sync_proposals'] = filtered
            self.populate_sync_tree(filtered)
            self.populate_roster_table() 
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save proposals: {e}")

    # --- MAINTENANCE DATA LOADERS ---
    def load_maintenance_data(self):
        self.streams =[self.editor_ref.stream_list_widget.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.editor_ref.stream_list_widget.count())]
        if CONFIG_FILE.exists():
            self.config = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        else:
            self.config = {}

        self.triage_table.setSortingEnabled(False)
        self.grave_table.setSortingEnabled(False)
        self.triage_table.setRowCount(0)
        self.grave_table.setRowCount(0)
        
        thresh_intermittent = int(self.config.get('intermittent_threshold', 2))
        thresh_unresponsive = int(self.config.get('unresponsive_threshold', 5))
        
        candidates = db_connector.get_maintenance_candidates()
        url_map = {s['page_url']: s for s in self.streams if s.get('enabled', True)}
        
        triage_rows =[]
        for url, status, next_ts, strikes, last_success_ts in candidates:
            if url in url_map:
                display_status = status
                if status == 'HICCUP': display_status = "GLITCH"
                elif status == 'FAILURE':
                    if strikes >= thresh_unresponsive: display_status = "UNRESPONSIVE"
                    elif strikes >= thresh_intermittent: display_status = "INTERMITTENT"
                elif status == 'SILENT_VISUAL':
                    display_status = "MIC DEAD (Vision Active)"
                elif status == 'TERMINAL':
                    display_status = "FATAL"
                
                wait_min = int((next_ts - time.time()) / 60)
                wait_str = f"{wait_min} min" if wait_min > 0 else "Ready"
                # Store URL into the tuple for the context menu
                triage_rows.append((url_map[url]['name'], display_status, strikes, wait_str, last_success_ts, url))
        
        self.triage_table.setRowCount(len(triage_rows))
        for i, (name, status, strikes, wait, last_ts, url) in enumerate(triage_rows):
            name_item = QTableWidgetItem(name)
            name_item.setData(Qt.ItemDataRole.UserRole, url) # Embedded for Context Menu
            self.triage_table.setItem(i, 0, name_item)
            
            s_item = QTableWidgetItem(status)
            if 'FATAL' in status:
                s_item.setBackground(QColor('#B71C1C')); s_item.setForeground(QBrush(QColorConstants.White))
            elif 'SUSPENDED' in status:
                s_item.setBackground(QColor('#AB47BC')); s_item.setForeground(QBrush(QColorConstants.White))
            elif 'UNRESPONSIVE' in status:
                s_item.setBackground(QColor('#EF5350')); s_item.setForeground(QBrush(QColorConstants.White))
            elif 'INTERMITTENT' in status:
                s_item.setBackground(QColor('#FFB74D')); s_item.setForeground(QBrush(QColorConstants.Black))
            elif 'GLITCH' in status:
                s_item.setBackground(QColor('#80DEEA')); s_item.setForeground(QBrush(QColorConstants.Black))
            elif 'SILENT' in status and 'MIC DEAD' not in status: s_item.setBackground(QColor('#cfd8dc'))
            elif 'MIC DEAD (Vision Active)' in status:
                s_item.setBackground(QColor('#00897B')); s_item.setForeground(QBrush(QColorConstants.White))
            elif 'LOOP' in status: s_item.setBackground(QColor('#e1bee7'))
            else: s_item.setBackground(QColor('#ffe0b2'))
            
            self.triage_table.setItem(i, 1, s_item)
            self.triage_table.setItem(i, 2, QTableWidgetItem(str(strikes)))
            
            pulse_text = "Never"
            pulse_color = QColor('red')
            
            if last_ts:
                diff = time.time() - last_ts
                if diff < 86400: 
                    pulse_text = f"{int(diff/3600)}h ago"
                    pulse_color = QColor('green')
                elif diff < 604800: 
                    pulse_text = f"{int(diff/86400)}d ago"
                    pulse_color = QColor('#FFA726') 
                else:
                    pulse_text = f"{int(diff/86400)}d ago"
            
            pulse_item = QTableWidgetItem(pulse_text)
            pulse_item.setForeground(QBrush(pulse_color))
            pulse_item.setData(Qt.ItemDataRole.UserRole, last_ts or 0)
            self.triage_table.setItem(i, 3, pulse_item)
            self.triage_table.setItem(i, 4, QTableWidgetItem(wait))
            
            chk = QCheckBox()
            cell_widget = QWidget()
            layout = QHBoxLayout(cell_widget); layout.addWidget(chk); layout.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.setContentsMargins(0,0,0,0)
            self.triage_table.setCellWidget(i, 5, cell_widget)
            
        grave_rows =[]
        for s in self.streams:
            if not s.get('enabled', True):
                reason = s.get('disable_reason')
                if not reason: 
                    match = re.search(r'\[(.*?)\]', s['name'])
                    if match: reason = match.group(1)
                if reason: 
                    if reason == 'TERMINAL': reason = 'FATAL'
                    grave_rows.append((s, reason))
                
        self.grave_table.setRowCount(len(grave_rows))
        for i, (s, reason) in enumerate(grave_rows):
            self.grave_table.setItem(i, 0, QTableWidgetItem(s['name']))
            self.grave_table.setItem(i, 1, QTableWidgetItem(reason))
            
            chk = QCheckBox()
            cell_widget = QWidget()
            layout = QHBoxLayout(cell_widget); layout.addWidget(chk); layout.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.setContentsMargins(0,0,0,0)
            self.grave_table.setCellWidget(i, 2, cell_widget)

        self.triage_table.setSortingEnabled(True)
        self.grave_table.setSortingEnabled(True)

    def select_by_type(self, table, type_str, col_status, col_chk):
        for i in range(table.rowCount()):
            status_item = table.item(i, col_status)
            widget = table.cellWidget(i, col_chk)
            chk = widget.layout().itemAt(0).widget()
            if type_str == status_item.text():
                chk.setChecked(True)
    
    def select_all(self, table, col_chk):
        for i in range(table.rowCount()):
            widget = table.cellWidget(i, col_chk)
            chk = widget.layout().itemAt(0).widget()
            chk.setChecked(True)

    def deselect_all(self, table, col_chk):
        for i in range(table.rowCount()):
            widget = table.cellWidget(i, col_chk)
            chk = widget.layout().itemAt(0).widget()
            chk.setChecked(False)

    def amnesty_selected(self):
        names_to_amnesty =[]
        for i in range(self.triage_table.rowCount()):
            widget = self.triage_table.cellWidget(i, 5) 
            chk = widget.layout().itemAt(0).widget()
            if chk.isChecked():
                name = self.triage_table.item(i, 0).text()
                names_to_amnesty.append(name)

        if names_to_amnesty:
            cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            urls_to_amnesty = []
            for s in cfg.get('streams',[]):
                if s['name'] in names_to_amnesty:
                    urls_to_amnesty.append(s.get('page_url'))
            
            if urls_to_amnesty:
                try:
                    with db_connector.get_db_connection(force_local=True) as con:
                        placeholders = ','.join(['?'] * len(urls_to_amnesty))
                        con.execute(f"UPDATE stream_queue SET check_count = 0, next_eligible_ts = 0, status_note = NULL WHERE url IN ({placeholders})", urls_to_amnesty)
                        
                        con.execute(f"DELETE FROM stream_health_events WHERE stream_url IN ({placeholders}) AND status != 'SUCCESS'", urls_to_amnesty)
                        
                    QMessageBox.information(self, "Success", f"Amnesty granted to {len(urls_to_amnesty)} streams.\nPenalties and recent strikes cleared. They will be checked immediately.")
                    self.load_maintenance_data()
                except Exception as e:
                    QMessageBox.critical(self, "Error", f"Failed to apply amnesty: {e}")

    def disable_selected(self):
        count = 0
        names_to_disable = {}
        for i in range(self.triage_table.rowCount()):
            widget = self.triage_table.cellWidget(i, 5) 
            chk = widget.layout().itemAt(0).widget()
            if chk.isChecked():
                name = self.triage_table.item(i, 0).text()
                status = self.triage_table.item(i, 1).text()
                names_to_disable[name] = status

        if names_to_disable:
            cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            updated = False
            for s in cfg.get('streams',[]):
                if s['name'] in names_to_disable:
                    s['enabled'] = False
                    s['disable_reason'] = names_to_disable[s['name']]
                    if f"[{names_to_disable[s['name']]}]" not in s['name']:
                        s['name'] = f"[{names_to_disable[s['name']]}] {s['name']}"
                    updated = True
                    count += 1
            if updated:
                self.safe_config_write(cfg)
                QMessageBox.information(self, "Success", f"Disabled {count} streams.\nThey have moved to the Graveyard.")
                self.load_maintenance_data()

    def revive_selected(self):
        count = 0
        names_to_revive =[]
        for i in range(self.grave_table.rowCount()):
            widget = self.grave_table.cellWidget(i, 2)
            chk = widget.layout().itemAt(0).widget()
            if chk.isChecked():
                name = self.grave_table.item(i, 0).text()
                names_to_revive.append(name)

        if names_to_revive:
            cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            updated = False
            for s in cfg.get('streams', []):
                if s['name'] in names_to_revive:
                    s['enabled'] = True
                    if 'disable_reason' in s: del s['disable_reason']
                    s['name'] = re.sub(r'\s*\[.*?\]', '', s['name'])
                    updated = True
                    count += 1
            if updated:
                self.safe_config_write(cfg)
                QMessageBox.information(self, "Success", f"Revived {count} streams.\nThey are back in the active list.")
                self.load_maintenance_data()

    # --- SYNC ENGINE LOGIC ---
    def start_sync_engine(self):
        self.combo_sync_filter.hide()
        self.sync_search_box.hide()
        self.lbl_proposal_count.hide()
        self.btn_run_sync.setEnabled(False)
        self.btn_run_sync.setText("⏳ Syncing...")
        
        self.sync_progress_bar.setFormat("%p% [%v / %m]")
        self.sync_progress_bar.setVisible(True)
        self.lbl_sync_status.setVisible(True)
        self.lbl_sync_status.setText("Starting Sync Engine...")
        
        # Capture scroll position before clear (though unlikely to matter on full run)
        self.tree_sync.clear()
        
        self.sync_worker = SyncEngineWorker()
        self.sync_worker.progress_update.connect(self._update_scan_progress)
        self.sync_worker.finished.connect(self.on_full_sync_finished)
        self.sync_worker.start()

    def filter_sync_tree(self):
        mode = self.combo_sync_filter.currentText()
        search_text = self.sync_search_box.text().strip().lower()
        visible_proposals = 0
        visible_channels = 0
        
        for i in range(self.tree_sync.topLevelItemCount()):
            parent = self.tree_sync.topLevelItem(i)
            visible_children = 0
            for j in range(parent.childCount()):
                child = parent.child(j)
                p = child.data(0, Qt.ItemDataRole.UserRole)
                if not p: continue
                is_auto = p.get('auto_heal_eligible', False)
                case_id = p.get('case', '')
                
                # --- UNIVERSAL SEARCH PATCH ---
                match_text = True
                if search_text:
                    combined_text = f"{p.get('friendly_name','')} {p.get('old_url','')} {p.get('new_url','')} {p.get('old_channel_name','')} {p.get('new_channel_name','')}".lower()
                    if search_text not in combined_text:
                        match_text = False
                
                if not match_text:
                    child.setHidden(True)
                elif mode == "Auto-Heal Eligible (⚡) Only" and not is_auto:
                    child.setHidden(True)
                elif mode == "Manual Review Required (✋) Only" and is_auto:
                    child.setHidden(True)
                elif mode == "New Streams (Case N) Only" and case_id != "N":
                    child.setHidden(True)
                elif mode == "Modifications (Cases A-H, R, M) Only" and case_id == "N":
                    child.setHidden(True)
                else:
                    child.setHidden(False)
                    visible_children += 1
                    visible_proposals += 1
                    
            if visible_children == 0:
                parent.setHidden(True)
            else:
                parent.setHidden(False)
                visible_channels += 1
                
        self.lbl_proposal_count.setText(f"Proposals: {visible_proposals} remaining across {visible_channels} channels")

    def on_full_sync_finished(self, proposals):
        fresh_proposals =[]
        if SYNC_PROPOSALS_FILE.exists():
            try:
                fresh_proposals = json.loads(SYNC_PROPOSALS_FILE.read_text(encoding='utf-8'))
            except Exception as e:
                logging.error(f"Failed to read fresh proposals after full sync: {e}")
                fresh_proposals = proposals 
        else:
            fresh_proposals = proposals
            
        self.combo_sync_filter.show()
        self.sync_search_box.show()
        self.lbl_proposal_count.show()
        self.sync_progress_bar.hide()
        self.lbl_sync_status.hide()
        
        self.btn_run_sync.setEnabled(True)
        self.btn_run_sync.setText("▶ Run Full Sync")
        
        QApplication.processEvents()
        
        self.populate_roster_table()
        self.populate_sync_tree(fresh_proposals)
        
        self.tree_sync.repaint()
        QApplication.processEvents()

    def populate_sync_tree(self, proposals):
        # --- PRESERVE SCROLL STATE PATCH ---
        v_scroll = self.tree_sync.verticalScrollBar().value()
        
        self.state['sync_proposals'] = proposals
        self.tree_sync.clear()
        
        if not proposals:
            self.lbl_proposal_count.setText("Proposals: 0 remaining")
            return

        channel_name_map = {}
        channel_url_map = {}
        if CONFIG_FILE.exists():
            try:
                cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                for name, url in cfg.get("channels", {}).items():
                    channel_name_map[url] = name
                    channel_url_map[name] = url
            except:
                pass

        # --- THE ACCURATE HEADER COUNTS FIX ---
        configured_counts_by_url = Counter()
        try:
            for s in cfg.get('streams',[]):
                c_name = s.get('channel_name', '').strip()
                if c_name:
                    c_url = channel_url_map.get(c_name)
                    if c_url:
                        configured_counts_by_url[c_url] += 1
                    else:
                        configured_counts_by_url[c_name] += 1 
        except: pass

        grouped = {}
        for p in proposals:
            c_url = p.get('old_channel') or p.get('new_channel') or "Unknown Channel"
            c_name = channel_name_map.get(c_url, c_url)
            if c_name not in grouped: 
                grouped[c_name] =[]
            grouped[c_name].append(p)
            
        for c_name in sorted(grouped.keys(), key=lambda x: x.lower()):
            items = grouped[c_name]
            items.sort(key=lambda x: x.get('friendly_name', '').lower())
            
            parent = QTreeWidgetItem(self.tree_sync)
            
            # --- THE CONTEXTUAL COUNTS PATCH (ANGLE D) ---
            c_url = items[0].get('new_channel') or items[0].get('old_channel')
            live_cnt = items[0].get('channel_live_count', '?') if items else '?'
            
            conf_cnt = configured_counts_by_url.get(c_url, configured_counts_by_url.get(c_name, 0))
            
            parent.setText(0, f"📺 {c_name} ({len(items)} updates | {conf_cnt} Configured | {live_cnt} Live)")
            parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            
            for p in items:
                child = QTreeWidgetItem(parent)
                
                case_id = p['case']
                is_auto = p.get('auto_heal_eligible', False)
                heal_icon = "⚡" if is_auto else "✋"
                
                if case_id == "A":
                    icon = "💀"
                    color = QColor("#EF5350")
                    action_text = "View & Disable"
                elif case_id in["B", "D", "E", "F", "G", "H"]:
                    icon = "🔄"
                    color = QColor("#FFB74D")
                    action_text = "View & Migrate"
                elif case_id == "C":
                    if p.get('old_channel_name') != p.get('new_channel_name'):
                        icon = "🏢"
                        color = QColor("#AB47BC")
                        action_text = "View & Rename"
                    else:
                        icon = "✏️"
                        color = QColor("#00E676")
                        action_text = "View & Rename"
                elif case_id == "N":
                    icon = "✨"
                    color = QColor("#00E5FF")
                    action_text = "Review & Add"
                elif case_id == "R":
                    icon = "🧟"
                    color = QColor("#64DD17")
                    action_text = "View & Resurrect"
                elif case_id == "M":
                    icon = "🪄"
                    color = QColor("#FFB74D")
                    action_text = "Review Edits"
                else:
                    icon = "❓"
                    color = QColor("white")
                    action_text = "View"

                child.setText(0, f"{heal_icon} {icon} {p['friendly_name']}")
                child.setForeground(0, QBrush(color))
                
                child.setText(1, f"Case {case_id}")
                
                conf = p['confidence'] * 100
                if conf > 0:
                    child.setText(2, f"{conf:.1f}%")
                else:
                    child.setText(2, "N/A")
                    
                btn = QPushButton(action_text)
                btn.clicked.connect(lambda chk, prop=p: self.open_diff_viewer(prop))
                self.tree_sync.setItemWidget(child, 3, btn)
                
                child.setData(0, Qt.ItemDataRole.UserRole, p)
                
            parent.setExpanded(True)
            QApplication.processEvents() 
            
        self.filter_sync_tree()
        
        # --- RESTORE SCROLL POSITION ---
        self.tree_sync.verticalScrollBar().setValue(v_scroll)

    def handle_sync_click(self, item, column):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data:
            self.open_diff_viewer(data)

    def open_diff_viewer(self, proposal):
        next_proposal = None
        found_current = False
        
        for i in range(self.tree_sync.topLevelItemCount()):
            parent = self.tree_sync.topLevelItem(i)
            if parent.isHidden(): continue
            for j in range(parent.childCount()):
                child = parent.child(j)
                if child.isHidden(): continue
                p = child.data(0, Qt.ItemDataRole.UserRole)
                if p == proposal:
                    found_current = True
                elif found_current and not next_proposal:
                    next_proposal = p
                    break
            if next_proposal: break
            
        dialog = DiffViewerDialog(proposal, self)
        if dialog.exec():
            action = getattr(dialog, 'result_action', 'cancel')
            
            if action in ('discard', 'discard_next', 'ignore'):
                if action == 'ignore':
                    self.bulk_reject_and_ignore([proposal], skip_confirm=True)
                else:
                    self._remove_proposal(proposal)
                    
                if action == 'discard_next' and next_proposal:
                    QTimer.singleShot(100, lambda: self.open_diff_viewer(next_proposal))
                    
            elif action in ('accept', 'accept_next'):
                self.execute_sync_proposal(proposal, dialog)
                if action == 'accept_next' and next_proposal:
                    QTimer.singleShot(100, lambda: self.open_diff_viewer(next_proposal))

    def _remove_proposal(self, proposal):
        target_name = proposal.get('friendly_name')
        
        filtered_list = []
        for p in self.state['sync_proposals']:
            if p.get('friendly_name') == target_name:
                if p.get('old_url') and p.get('old_url') == proposal.get('old_url'):
                    continue
                if not p.get('old_url') and p.get('new_url') == proposal.get('new_url'):
                    continue
            filtered_list.append(p)
            
        self.state['sync_proposals'] = filtered_list
        try:
            SYNC_PROPOSALS_FILE.write_text(json.dumps(self.state['sync_proposals'], indent=2), encoding='utf-8')
        except Exception as e:
            logging.error(f"Failed to write sync proposals to disk: {e}")
            
        self.populate_sync_tree(self.state['sync_proposals'])

    def execute_sync_proposal(self, proposal, dialog):
        case_id = proposal['case']
        old_url = proposal.get('old_url')
        old_name = proposal.get('friendly_name')
        
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            updated = False
            
            target_stream = None
            if case_id != "N":
                for s in cfg.get('streams',[]):
                    clean_url = re.sub(r'[\?&]variant=\d+', '', s.get('page_url', ''))
                    if clean_url == old_url or s.get('name') == old_name:
                        target_stream = s
                        break
                        
                if not target_stream:
                    QMessageBox.warning(self, "Obsolete Proposal", "This stream could not be found in your active configuration.\n\nIt was likely already deleted or its URL was changed manually in the main window.\n\nThis obsolete proposal will now be removed from the queue.")
                    self._remove_proposal(proposal)
                    self.load_maintenance_data()
                    return
                    
            if case_id == "A":
                target_stream['enabled'] = False
                target_stream['disable_reason'] = "Dead (Case A Sync)"
                if '[DEAD]' not in target_stream['name']:
                    target_stream['name'] = f"[DEAD] {target_stream['name']}"
                updated = True
                
            elif case_id in["B", "C", "D", "E", "F", "G", "H", "R", "M"]:
                new_url = proposal['new_url']
                target_stream['page_url'] = new_url
                target_stream['original_yt_title'] = proposal.get('original_yt_title', proposal.get('new_title', ''))
                target_stream['enabled'] = True
                target_stream['updated_at'] = time.time()
                target_stream.pop('disable_reason', None)
                target_stream.pop('status_reason', None)
                
                if 'stream_type' in proposal:
                    target_stream['stream_type'] = proposal['stream_type']
                
                if case_id == "R":
                    target_stream['name'] = re.sub(r'\s*\[.*?\]', '', target_stream['name'])
                    
                if proposal.get('new_channel_name'):
                    target_stream['channel_name'] = proposal['new_channel_name']
                    if 'channels' not in cfg: cfg['channels'] = {}
                    if proposal['new_channel_name'] not in cfg['channels']:
                        cfg['channels'][proposal['new_channel_name']] = proposal['new_channel']
                updated = True
                
                success, log_msg = stream_migrator.migrate_stream_data(old_url, new_url)
                if not success:
                    logging.warning(f"DB migration warning: {log_msg}")
                    
            elif case_id == "N":
                try:
                    lat = float(dialog.edit_lat.text().strip())
                    lon = float(dialog.edit_lon.text().strip())
                except ValueError:
                    lat, lon = 0.0, 0.0
                    
                new_stream = {
                    "name": dialog.edit_friendly_name.text().strip(),
                    "original_yt_title": proposal.get('original_yt_title', proposal.get('new_title', '')),
                    "page_url": proposal['new_url'],
                    "lat": lat,
                    "lon": lon,
                    "enabled": True,
                    "created_at": time.time(),
                    "updated_at": time.time()
                }
                
                if 'stream_type' in proposal:
                    new_stream['stream_type'] = proposal['stream_type']
                    
                if proposal.get('new_channel_name'):
                    new_stream['channel_name'] = proposal['new_channel_name']
                    if 'channels' not in cfg: cfg['channels'] = {}
                    if proposal['new_channel_name'] not in cfg['channels']:
                        cfg['channels'][proposal['new_channel_name']] = proposal['new_channel']
                        
                if "streams" not in cfg: cfg["streams"] = []
                cfg["streams"].append(new_stream)
                updated = True
                
            if updated:
                self.safe_config_write(cfg)
                
                try:
                    if METADATA_CACHE_FILE.exists():
                        cache_data = json.loads(METADATA_CACHE_FILE.read_text(encoding='utf-8'))
                        
                        if case_id == "N":
                            clean_new_url = re.sub(r'[\?&]variant=\d+', '', proposal['new_url']).strip()
                            cache_data.setdefault("streams", {})[clean_new_url] = {
                                "friendly_name": dialog.edit_friendly_name.text().strip(),
                                "url": proposal['new_url'],
                                "title": proposal['new_title'],
                                "channel_url": proposal['new_channel'],
                                "channel_name": proposal['new_channel_name'],
                                "last_checked": time.time(),
                                "status": "alive"
                            }
                        else:
                            clean_old_url = re.sub(r'[\?&]variant=\d+', '', old_url).strip()
                            if clean_old_url in cache_data.get("streams", {}):
                                stream_cache = cache_data["streams"].pop(clean_old_url)
                                if case_id != "A":
                                    clean_new_url = re.sub(r'[\?&]variant=\d+', '', proposal['new_url']).strip()
                                    stream_cache['title'] = proposal['new_title']
                                    stream_cache['channel_url'] = proposal.get('new_channel', stream_cache.get('channel_url'))
                                    if proposal.get('new_channel_name'):
                                        stream_cache['channel_name'] = proposal['new_channel_name']
                                    stream_cache['last_checked'] = time.time()
                                    stream_cache['status'] = "alive"
                                    cache_data["streams"][clean_new_url] = stream_cache
                                else:
                                    stream_cache['status'] = "dead"
                                    stream_cache['last_checked'] = time.time()
                                    cache_data["streams"][clean_old_url] = stream_cache
                            
                            METADATA_CACHE_FILE.write_text(json.dumps(cache_data, indent=2), encoding='utf-8')
                except Exception as e:
                    logging.error(f"Failed to update metadata cache: {e}")

                self._remove_proposal(proposal)
                self.load_maintenance_data()
                
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to execute proposal: {e}")

    # --- STATE SAVING HELPERS ---
    def save_radar_sort_state(self, logical_index, order):
        self.state['radar_sort_col'] = logical_index
        self.state['radar_sort_order'] = order

    # --- SAFETY HELPER ---
    def safe_config_write(self, cfg_dict):
        try:
            tmp_file = CONFIG_FILE.with_suffix('.tmp')
            with open(tmp_file, 'w', encoding='utf-8') as f:
                json.dump(cfg_dict, f, indent=2)
            os.replace(tmp_file, CONFIG_FILE)
            if self.editor_ref and hasattr(self.editor_ref, 'load_config'):
                self.editor_ref.load_config()
            return True
        except Exception as e:
            logging.error(f"Failed atomic config write: {e}")
            raise e

    def apply_row_styling(self, table, row, is_dim, is_healed_or_added=False):
        bg_color = QBrush(QColor("#1B5E20")) if is_healed_or_added else QBrush(QColor("transparent"))
        text_color = QBrush(QColor("#555555") if is_dim else QColor("#FFFFFF"))
            
        for col in range(1, table.columnCount() - 1):
            item = table.item(row, col)
            if item:
                item.setBackground(bg_color)
                if not is_dim and not is_healed_or_added:
                    if col == 1 and isinstance(item, NumericTableWidgetItem):
                        pass
                    else:
                        item.setForeground(text_color)
                else:
                    item.setForeground(text_color)

    def get_all_existing_urls(self):
        if not CONFIG_FILE.exists(): return[]
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            urls =[]
            for s in cfg.get('streams',[]):
                u = s.get('page_url', '')
                if u: 
                    u = re.sub(r'[\?&]variant=\d+', '', u)
                    urls.append(u)
            return urls
        except:
            return[]

    def open_map(self, lat, lon):
        try: webbrowser.open(f"https://www.google.com/maps/search/?api=1&query={lat},{lon}")
        except: pass
        
    def open_both_urls(self, url1, url2):
        try:
            webbrowser.open(url1)
            time.sleep(0.5)
            webbrowser.open(url2)
        except Exception as e:
            logging.error(f"Failed to open both URLs: {e}")

    # --- DISCOVERY RADAR LOGIC ---
    def start_discovery(self):
        term = self.edit_search.text().strip()
        if not term: return
        
        self.btn_search.setEnabled(False)
        self.table_radar.setSortingEnabled(False)
        self.table_radar.setRowCount(0)
        self.lbl_radar_status.setText("Scanning...")
        
        self.radar_worker = DiscoveryWorker(term)
        self.radar_worker.progress_msg.connect(self.lbl_radar_status.setText)
        self.radar_worker.result_ready.connect(self.populate_radar_table)
        self.radar_worker.start()

    def populate_radar_table(self, results, from_cache=False):
        if not from_cache:
            self.state['radar_results'] = results
            self.state['added_urls'] = set()
            
        existing_urls = self.get_all_existing_urls()
        
        self.table_radar.setSortingEnabled(False)
        self.table_radar.setRowCount(len(results))
        for i, res in enumerate(results):
            new_url = res['url']
            is_added = new_url in self.state['added_urls']
            is_dim = new_url in self.state['marked_radar']
            
            chk = QCheckBox()
            chk.setChecked(is_dim)
            chk.setToolTip("Mark as 'Done' to ignore this row.")
            w_chk = QWidget(); l_chk = QHBoxLayout(w_chk); l_chk.addWidget(chk); l_chk.setAlignment(Qt.AlignmentFlag.AlignCenter); l_chk.setContentsMargins(0,0,0,0)
            self.table_radar.setCellWidget(i, 0, w_chk)
            
            chk.stateChanged.connect(lambda state, w=w_chk, key=new_url: self.mark_done(self.table_radar, w, state, key, 'radar'))
            
            is_dup = False
            clean_new_url = re.sub(r'[\?&]variant=\d+', '', new_url)
            
            for ext in existing_urls:
                if clean_new_url in ext or ext in clean_new_url:
                    is_dup = True
                    break
                    
            if is_dup:
                status_item = QTableWidgetItem("✔ Already Configured")
                status_item.setForeground(QBrush(QColor("#888888")))
            else:
                status_item = QTableWidgetItem("✨ New")
                status_item.setForeground(QBrush(QColor("#00E5FF")))
                
            status_item.setToolTip(status_item.text())
            self.table_radar.setItem(i, 1, status_item)
            
            item_title = QTableWidgetItem(res['title'])
            item_title.setToolTip(res['title'])
            self.table_radar.setItem(i, 2, item_title)
            
            item_up = QTableWidgetItem(res['uploader'])
            item_up.setToolTip(res['uploader'])
            self.table_radar.setItem(i, 3, item_up)
            
            link_widget = QWidget()
            link_layout = QHBoxLayout(link_widget)
            link_layout.setContentsMargins(2, 2, 2, 2)
            
            btn_vid = QPushButton("🌐 Vid")
            btn_vid.setToolTip("Open URL in Browser")
            btn_vid.clicked.connect(lambda chk, url=new_url: webbrowser.open(url))
            link_layout.addWidget(btn_vid)
            self.table_radar.setCellWidget(i, 4, link_widget)
            
            if is_dup:
                btn = QPushButton("In System")
                btn.setEnabled(False)
            elif is_added:
                btn = QPushButton("✔ ADDED")
                btn.setEnabled(False)
                btn.setStyleSheet("background-color: #1B5E20; color: white; font-weight: bold;")
            else:
                btn = QPushButton("+ Add to Hub")
                btn.setStyleSheet("background-color: #0078d7; color: white; font-weight: bold;")
                btn.clicked.connect(lambda chk, r=res, w=w_chk: self.add_discovered_stream(r, w))
                
            self.table_radar.setCellWidget(i, 5, btn)
            self.apply_row_styling(self.table_radar, i, is_dim, is_added)
            
        self.table_radar.setSortingEnabled(True)
        self.table_radar.sortItems(self.state['radar_sort_col'], self.state['radar_sort_order'])
        self.btn_search.setEnabled(True)

    def mark_done(self, table, w_chk, state, key, state_type):
        row_index = table.indexAt(w_chk.pos()).row()
        is_checked = state == Qt.CheckState.Checked.value
        
        target_set = self.state['marked_radar']
            
        if is_checked: target_set.add(key)
        elif key in target_set: target_set.remove(key)
            
        self.apply_row_styling(table, row_index, is_checked)

    def add_discovered_stream(self, res_dict, w_chk):
        row_index = self.table_radar.indexAt(w_chk.pos()).row()
        d = QuickAddStreamDialog(res_dict['title'], res_dict['url'], self)
        
        if d.exec():
            new_stream_data = d.get_stream_data()
            try:
                cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                if "streams" not in cfg: cfg["streams"] = []
                cfg["streams"].append(new_stream_data)
                
                self.safe_config_write(cfg)
                self.state['added_urls'].add(res_dict['url'])
                
                btn = self.table_radar.cellWidget(row_index, 5)
                if btn:
                    btn.setText("✔ ADDED")
                    btn.setEnabled(False)
                    btn.setStyleSheet("background-color: #1B5E20; color: white; font-weight: bold;")
                    
                chk = w_chk.layout().itemAt(0).widget()
                self.apply_row_styling(self.table_radar, row_index, chk.isChecked(), True)
                    
                QMessageBox.information(self, "Added", f"'{new_stream_data['name']}' has been added to your configuration!")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to add stream: {e}")