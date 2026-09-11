# FILE: curation_studio_gui.py
# VERSION: 40.21 - "The Workflow & Tooltips Patch"
# RESPONSIBILITY: Side-by-side review, AI correction, ML exports, and batch processing.
# UPDATED: Implemented "Proceed to Confirm/Reject & Next" button to wizard for faster workflow. Added AI reasoning hover tooltips to batch review image thumbnails.

import sys
import os
import json
import sqlite3
import shutil
import logging
import traceback
import requests
import time
import re
import webbrowser
import html
from datetime import datetime, timezone, timedelta
from pathlib import Path
import unicodedata
from urllib.parse import quote_plus
from io import BytesIO

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QPushButton, QTableWidget, 
                             QTableWidgetItem, QHeaderView, QComboBox, QGroupBox, 
                             QSplitter, QMessageBox, QAbstractItemView, QLineEdit, QDialog,
                             QFormLayout, QScrollArea, QMenu, QGridLayout, QSizePolicy,
                             QRadioButton, QButtonGroup, QFrame)
from PyQt6.QtCore import Qt, QUrl, pyqtSignal, QTimer
from PyQt6.QtGui import QPixmap, QColor, QBrush, QImage, QAction

try:
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
    MULTIMEDIA_AVAILABLE = True
except ImportError:
    MULTIMEDIA_AVAILABLE = False
    print("WARNING: PyQt6-Multimedia not found. Audio will open in external default player.")

# --- CONFIGURATION ---
ROOT = Path(__file__).resolve().parent
DATABASE_PATH = ROOT / "detections.db"
IMAGE_DB_PATH = ROOT / "image_database.db"
BASELINE_CLIPS_DIR = ROOT / "baseline_clips"
ML_EXPORT_DIR = ROOT / "ml_exports"
CONFIG_FILE = ROOT / "birdnet_config.json"
VISION_TARGETS_FILE = ROOT / "vision_targets.json"
SETTINGS_FILE = ROOT / "curation_studio_settings.json"

FRAME_LEVELS =["Massive", "Huge", "Large", "Medium", "Small", "Tiny", "Speck"]
DEPTH_LEVELS =["Point Blank", "Very Near", "Near", "Mid-ground", "Background", "Deep Background", "Horizon"]

sys.path.append(str(ROOT))
import db_connector

ML_EXPORT_DIR.mkdir(exist_ok=True)

# --- TRAFFIC LIGHT REASON PARSER ---
def color_code_reason_string(reason_str):
    if not reason_str: return ""
    
    FRAME_MAP = {k.upper(): 7-i for i, k in enumerate(FRAME_LEVELS)}
    DEPTH_MAP = {k.upper(): 7-i for i, k in enumerate(DEPTH_LEVELS)}
    
    def replacer(match):
        metric = match.group(1) # "Frame" or "Depth"
        val_obs = match.group(2).strip()
        val_req = match.group(3).strip()
        
        passed = False
        if metric == "Frame":
            passed = FRAME_MAP.get(val_obs.upper(), 0) >= FRAME_MAP.get(val_req.upper(), 0)
        else:
            passed = DEPTH_MAP.get(val_obs.upper(), 0) >= DEPTH_MAP.get(val_req.upper(), 0)
        
        color = "#00E676" if passed else "#EF5350"
        status_text = "Pass" if passed else "Fail"
        
        return f"{metric}: <span style='color:{color}; font-weight:bold;'>{val_obs} ({status_text})</span> vs Req {val_req}"
    
    # Matches anything up to a comma or closing parenthesis to capture multi-word strings
    return re.sub(r"(Frame|Depth):\s*([^,)]+?)\s*vs Req\s*([^,)]+)", replacer, reason_str)


# --- CUSTOM WIDGETS ---
class ClickableImageLabel(QLabel):
    clicked = pyqtSignal()
    doubleClicked = pyqtSignal()
    
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)
        
    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit()
        try:
            super().mouseDoubleClickEvent(event)
        except RuntimeError:
            pass

class ImageViewerDialog(QDialog):
    # Class-level signals for the carousel patch
    index_changed = pyqtSignal(int)
    exclusion_toggled = pyqtSignal(int)

    def __init__(self, pixmap=None, items_data=None, initial_index=0, parent=None):
        super().__init__(parent)
        self.items_data = items_data
        self.current_index = initial_index
        self.single_pixmap = pixmap
        
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background-color: #000;")
        
        self.layout.addWidget(self.image_label)
        self.btn_close = QPushButton("CLOSE VIEWER")
        self.btn_close.setFocusPolicy(Qt.FocusPolicy.NoFocus) # Prevents stealing keyboard arrows
        self.btn_close.setStyleSheet("background-color: #333; color: white; font-weight: bold; font-size: 16px; padding: 15px; border: none;")
        self.btn_close.clicked.connect(self.accept)
        self.layout.addWidget(self.btn_close)
        
        try:
            if SETTINGS_FILE.exists():
                s = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
                geom = s.get('viewer_geometry')
                if geom: self.setGeometry(geom['x'], geom['y'], geom['width'], geom['height'])
                else: self.resize(800, 600)
            else: self.resize(800, 600)
        except: self.resize(800, 600)
        
        self._update_ui()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFocus()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_ui()

    def _update_ui(self):
        if self.items_data is not None:
            # --- CAROUSEL MODE ---
            item = self.items_data[self.current_index]
            pm = item['pixmap']
            
            # Emit signal to sync background focus instantly
            self.index_changed.emit(self.current_index)
            
            # Safely check parent status for title update
            is_included = True
            if self.parent() and hasattr(self.parent(), 'accepted_rows'):
                is_included = item['row_data'] in self.parent().accepted_rows
                
            # Update title with dynamic status
            status = "[✔ INCLUDED]" if is_included else "[❌ EXCLUDED]"
            self.setWindowTitle(f"Image Viewer - {self.current_index + 1} of {len(self.items_data)} {status}")
        else:
            # --- SINGLE IMAGE MODE ---
            pm = self.single_pixmap
            self.setWindowTitle("Image Viewer")

        if pm and not pm.isNull():
            scaled = pm.scaled(self.image_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.image_label.setPixmap(scaled)
        else:
            self.image_label.clear()
            self.image_label.setText("No Visual Data")

    def keyPressEvent(self, event):
        if self.items_data is not None:
            if event.key() == Qt.Key.Key_Right:
                self.current_index = min(len(self.items_data) - 1, self.current_index + 1)
                self._update_ui()
                event.accept()
            elif event.key() == Qt.Key.Key_Left:
                self.current_index = max(0, self.current_index - 1)
                self._update_ui()
                event.accept()
            elif event.key() == Qt.Key.Key_Space:
                self.exclusion_toggled.emit(self.current_index)
                self._update_ui()
                event.accept()
            elif event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Enter, Qt.Key.Key_Return):
                self.accept()
            else:
                super().keyPressEvent(event)
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        try:
            s = json.loads(SETTINGS_FILE.read_text(encoding='utf-8')) if SETTINGS_FILE.exists() else {}
            geom = self.geometry()
            s['viewer_geometry'] = {'x': geom.x(), 'y': geom.y(), 'width': geom.width(), 'height': geom.height()}
            SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding='utf-8')
        except: pass
        super().closeEvent(event)

class BatchReviewDialog(QDialog):
    def __init__(self, similar_rows, filter_reason, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Wizard: Review {len(similar_rows)} Similar Detections")
        self.resize(950, 700)
        self.similar_rows = similar_rows
        self.accepted_rows = list(similar_rows)
        
        self.items_data =[]
        self.current_focus_index = 0
        self.col_count = 4
        
        self.layout = QVBoxLayout(self)

        reason_html = f"<br><b>Grouped By Core Reason:</b> <span style='color: #EF5350;'>{filter_reason}</span>" if filter_reason else ""
        info = QLabel(f"<span style='font-size:14px;'><b>Batch Thumbnail Review:</b> Double-click or press <b>Enter</b> to view full size carousel. Click 'Exclude' or press <b>Spacebar</b> to remove bad detections. Use <b>Arrow Keys</b> to navigate.</span>{reason_html}")
        info.setWordWrap(True)
        self.layout.addWidget(info)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus) # Don't steal arrow keys
        self.grid_widget = QWidget()
        self.grid_layout = QGridLayout(self.grid_widget)
        self.scroll.setWidget(self.grid_widget)
        self.layout.addWidget(self.scroll)

        self.populate_grid()

        # --- NEW BULK ACTION BUTTONS ---
        btn_layout = QHBoxLayout()
        
        self.btn_cancel = QPushButton("Cancel Review")
        self.btn_cancel.setStyleSheet("background-color: #555; font-weight: bold; padding: 10px; font-size: 14px;")
        self.btn_cancel.clicked.connect(self.reject)
        
        self.btn_exc_purged = QPushButton("Exclude All Purged")
        self.btn_exc_purged.setStyleSheet("background-color: #F57C00; font-weight: bold; padding: 10px; font-size: 14px;")
        self.btn_exc_purged.clicked.connect(self.exclude_purged)
        
        self.btn_exc_all = QPushButton("Exclude All")
        self.btn_exc_all.setStyleSheet("background-color: #D32F2F; font-weight: bold; padding: 10px; font-size: 14px;")
        self.btn_exc_all.clicked.connect(self.exclude_all)
        
        self.btn_done = QPushButton("Done Reviewing")
        self.btn_done.setStyleSheet("background-color: #0078d7; font-weight: bold; padding: 10px; font-size: 14px;")
        self.btn_done.clicked.connect(self.accept)
        
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_exc_purged)
        btn_layout.addWidget(self.btn_exc_all)
        btn_layout.addWidget(self.btn_done)
        self.layout.addLayout(btn_layout)

        # Set initial focus for keyboard
        self.setFocus()
        if self.items_data:
            self.set_focus(0)

    def exclude_all(self):
        for item in self.items_data:
            if item['row_data'] in self.accepted_rows:
                self.toggle_exclude(item['row_data'], item['btn'], item['img_lbl'], item['time_lbl'])

    def exclude_purged(self):
        for item in self.items_data:
            if item['row_data'] in self.accepted_rows:
                v_path = item['row_data'].get('vision_path')
                if not v_path or not os.path.exists(v_path):
                    self.toggle_exclude(item['row_data'], item['btn'], item['img_lbl'], item['time_lbl'])

    def reject(self):
        # Override to completely clear the batch memory if X or ESC is pressed
        self.exclude_all()
        super().reject()

    def play_cell_audio(self, did):
        main_win = self.parent().parent() # SmartTuningDialog -> CurationStudio
        if hasattr(main_win, 'play_audio_by_id'):
            main_win.play_audio_by_id(did)

    def populate_grid(self):
        for i, row_data in enumerate(self.similar_rows):
            cell_frame = QFrame()
            cell_frame.setObjectName("CellFrame")
            cell_frame.setStyleSheet("QFrame#CellFrame { border: 2px solid transparent; border-radius: 6px; padding: 2px; }")
            cell_lay = QVBoxLayout(cell_frame)
            cell_lay.setContentsMargins(4, 4, 4, 4)
            
            img_lbl = ClickableImageLabel()
            img_lbl.setFixedSize(180, 180)
            img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            img_lbl.setStyleSheet("background-color: #222; border: 1px solid #555;")
            
            v_path = row_data.get('vision_path')
            pixmap = QPixmap()
            if v_path and os.path.exists(v_path):
                pixmap = QPixmap(v_path)
                img_lbl.setPixmap(pixmap.scaled(180, 180, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                
                sp_safe = html.escape(str(row_data.get('species') or 'Unknown'))
                notes_safe = html.escape(str(row_data.get('ai_notes') or 'None'))
                tt_html = f"<div style='background-color: #1e1e1e; color: #eee; padding: 4px; max-width: 300px; word-wrap: break-word;'><b>Target:</b> {sp_safe}<br><b>AI Reasoning:</b> {notes_safe}</div>"
                img_lbl.setToolTip(tt_html)
            else:
                img_lbl.setTextFormat(Qt.TextFormat.RichText)
                det_method = str(row_data.get('detection_method') or '').lower()
                if det_method == 'audio':
                    img_lbl.setText("<div style='text-align: center; font-size: 40px; margin-bottom: 10px;'>🎵</div><div style='font-size: 14px; font-weight: bold; color: #81D4FA;'>AUDIO ONLY</div>")
                else:
                    img_lbl.setText("<div style='text-align: center; font-size: 40px; margin-bottom: 10px;'>🗑️</div><div style='font-size: 14px; font-weight: bold; color: #EF5350;'>IMAGE PURGED</div>")
            
            # Interactions
            img_lbl.clicked.connect(lambda idx=i: self.set_focus(idx))
            img_lbl.doubleClicked.connect(lambda p=pixmap, idx=i: self.open_large(p, idx))
            
            time_str = datetime.fromtimestamp(row_data['timestamp']).strftime('%m-%d %H:%M')
            lbl_time = QLabel(time_str)
            lbl_time.setAlignment(Qt.AlignmentFlag.AlignCenter)
            
            # --- IN-CELL AUDIO & ACTION WIDGETS ---
            action_lay = QHBoxLayout()
            action_lay.setContentsMargins(0, 0, 0, 0)
            action_lay.setSpacing(4)
            
            btn_exc = QPushButton("Exclude")
            btn_exc.setFocusPolicy(Qt.FocusPolicy.NoFocus) # Prevents spacebar from sticking to button
            btn_exc.setStyleSheet("background-color: #D32F2F;")
            btn_exc.clicked.connect(lambda chk, r=row_data, b=btn_exc, i_lbl=img_lbl, t_lbl=lbl_time: self.toggle_exclude(r, b, i_lbl, t_lbl))
            action_lay.addWidget(btn_exc)
            
            # Check for actual audio file
            did = row_data['id']
            p_mp3 = BASELINE_CLIPS_DIR / f"detection_{did}.mp3"
            p_wav = BASELINE_CLIPS_DIR / f"detection_{did}.wav"
            if p_mp3.exists() or p_wav.exists():
                btn_play = QPushButton("▶")
                btn_play.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                btn_play.setFixedWidth(30)
                btn_play.setStyleSheet("background-color: #0078d7; font-weight: bold;")
                btn_play.clicked.connect(lambda chk, d=did: self.play_cell_audio(d))
                action_lay.addWidget(btn_play)
            
            cell_lay.addWidget(img_lbl)
            cell_lay.addWidget(lbl_time)
            cell_lay.addLayout(action_lay)
            
            row = i // self.col_count
            col = i % self.col_count
            self.grid_layout.addWidget(cell_frame, row, col)
            
            self.items_data.append({
                'frame': cell_frame,
                'img_lbl': img_lbl,
                'btn': btn_exc,
                'time_lbl': lbl_time,
                'row_data': row_data,
                'pixmap': pixmap
            })

    def handle_viewer_exclusion(self, index):
        if not self.items_data or index < 0 or index >= len(self.items_data):
            return
        item = self.items_data[index]
        self.toggle_exclude(item['row_data'], item['btn'], item['img_lbl'], item['time_lbl'])

    def set_focus(self, index):
        if not self.items_data or index < 0 or index >= len(self.items_data): 
            return
            
        # Clear old focus
        old_frame = self.items_data[self.current_focus_index]['frame']
        old_frame.setStyleSheet("QFrame#CellFrame { border: 2px solid transparent; border-radius: 6px; padding: 2px; }")
        
        self.current_focus_index = index
        
        # Set new focus
        new_frame = self.items_data[self.current_focus_index]['frame']
        new_frame.setStyleSheet("QFrame#CellFrame { border: 2px solid #00E5FF; border-radius: 6px; padding: 2px; background-color: rgba(0, 229, 255, 0.05); }")
        
        # Scroll to view automatically
        self.scroll.ensureWidgetVisible(new_frame, 10, 10)
        
        # Force UI repaint immediately
        QApplication.processEvents()

    def keyPressEvent(self, event):
        if not self.items_data:
            super().keyPressEvent(event)
            return

        idx = self.current_focus_index
        key = event.key()

        if key == Qt.Key.Key_Right:
            self.set_focus(min(len(self.items_data) - 1, idx + 1))
        elif key == Qt.Key.Key_Left:
            self.set_focus(max(0, idx - 1))
        elif key == Qt.Key.Key_Down:
            self.set_focus(min(len(self.items_data) - 1, idx + self.col_count))
        elif key == Qt.Key.Key_Up:
            self.set_focus(max(0, idx - self.col_count))
        elif key == Qt.Key.Key_Space:
            if not self.btn_done.hasFocus() and not self.btn_cancel.hasFocus() and not self.btn_exc_all.hasFocus() and not self.btn_exc_purged.hasFocus():
                item = self.items_data[idx]
                self.toggle_exclude(item['row_data'], item['btn'], item['img_lbl'], item['time_lbl'])
                event.accept()
            else:
                super().keyPressEvent(event)
        elif key in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            if not self.btn_done.hasFocus() and not self.btn_cancel.hasFocus() and not self.btn_exc_all.hasFocus() and not self.btn_exc_purged.hasFocus():
                item = self.items_data[idx]
                self.open_large(item['pixmap'], idx)
                event.accept()
            else:
                super().keyPressEvent(event)
        else:
            super().keyPressEvent(event)

    def open_large(self, pixmap, index=None):
        if index is not None:
            self.set_focus(index)
            viewer = ImageViewerDialog(items_data=self.items_data, initial_index=index, parent=self)
            viewer.index_changed.connect(self.set_focus)
            viewer.exclusion_toggled.connect(self.handle_viewer_exclusion)
            viewer.exec()
            # AFTER DIALOG CLOSES, RE-ASSERT FOCUS ON THE LAST VIEWED ITEM
            self.set_focus(viewer.current_index)
        else:
            viewer = ImageViewerDialog(pixmap=pixmap, parent=self)
            viewer.exec()

    def toggle_exclude(self, row_data, btn, img_lbl, t_lbl):
        if row_data in self.accepted_rows:
            self.accepted_rows.remove(row_data)
            btn.setText("Include")
            btn.setStyleSheet("background-color: #388E3C;")
            img_lbl.setStyleSheet("background-color: #111; border: 2px solid #D32F2F; opacity: 0.4;")
            t_lbl.setStyleSheet("color: #666;")
        else:
            self.accepted_rows.append(row_data)
            btn.setText("Exclude")
            btn.setStyleSheet("background-color: #D32F2F;")
            img_lbl.setStyleSheet("background-color: #222; border: 1px solid #555; opacity: 1.0;")
            t_lbl.setStyleSheet("color: #eee;")

    def get_accepted(self):
        return self.accepted_rows

class SmartTuningDialog(QDialog):
    def __init__(self, action_name, context_dict, parent=None):
        super().__init__(parent)
        self.is_batch = context_dict.get('is_batch', False)
        self.count = context_dict.get('count', 1)
        self.unique_combos = context_dict.get('unique_combos',[])
        self.extra_batch_items =[]
        self.context_dict = context_dict
        self.go_next = False
        
        # --- DETERMINE NIGHT MODE PRESENCE ---
        self.is_night_mode = False
        for d in context_dict.get('all_selected_data',[]):
            notes = d.get('ai_notes') or ''
            if '🌙' in notes or '[SOLAR NIGHT]' in notes or '[LOW-VISIBILITY]' in notes:
                self.is_night_mode = True
                break
        
        title_sp = "Batch Detections" if self.is_batch else context_dict['data']['species']
        self.setWindowTitle(f"{action_name} & Tune: {title_sp}")
        self.setMinimumWidth(650)
        self.pixmap = context_dict.get('pixmap')
        self.layout = QVBoxLayout(self)

        action_color = "#388E3C" if action_name == "Confirm" else "#D32F2F"
        self.header_lbl = QLabel(f"<h2 style='color: {action_color}; margin: 0;'>{action_name.upper()}ING {self.count} RECORD(S)</h2>")
        self.layout.addWidget(self.header_lbl)
        
        # --- THE SMART WIZARD BATCH FINDER (WITH FUZZY LOGIC & DB RETRY) ---
        try:
            ref_data = context_dict['data']
            sp = ref_data['species']
            
            def get_fuzzy_reason(reason_str):
                if not reason_str: return ""
                if "Failed Size/Depth" in reason_str: return "Failed Size/Depth constraints"
                if "Taxonomy" in reason_str: return "Taxonomy behavior/privacy"
                return reason_str.strip()
                
            self.f_reason_fuzzy = get_fuzzy_reason(ref_data.get('filter_reason'))
            
            stream_name = context_dict['stream_name']
            selected_ids = [d['id'] for d in context_dict.get('all_selected_data',[])]
            
            self.similar_rows =[]
            for attempt in range(3):
                try:
                    with db_connector.get_db_connection(force_local=True) as con:
                        con.conn.row_factory = sqlite3.Row 
                        cur = con.cursor()
                        
                        cur.execute("SELECT * FROM detections WHERE species = ? AND (human_verified = 'pending' OR human_verified IS NULL)", (sp,))
                        all_pending =[dict(r) for r in cur.fetchall()]
                        
                        for r in all_pending:
                            if r['id'] in selected_ids:
                                continue
                            
                            r_stream_name = parent.get_stream_name(r['channel_url'])
                            if r_stream_name != stream_name:
                                continue
                                
                            r_reason_fuzzy = get_fuzzy_reason(r.get('filter_reason'))
                            
                            if r_reason_fuzzy == self.f_reason_fuzzy:
                                self.similar_rows.append(r)
                                
                    break # Success, exit retry loop
                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower():
                        time.sleep(0.5)
                        continue
                    else:
                        break
                        
        except Exception as e:
            self.similar_rows =[]
            logging.error(f"Error finding similar items for batch: {e}")

        if self.similar_rows:
            reason_snippet = f"blocked for the same core reason" if self.f_reason_fuzzy else "with the same Map status"
            self.btn_review_sim = QPushButton(f"🪄 WIZARD: Found {len(self.similar_rows)} more pending '{sp}' images {reason_snippet}. Click to review & add to batch.")
            self.btn_review_sim.setStyleSheet("background-color: #00897B; color: white; font-weight: bold; margin-bottom: 10px; padding: 12px; font-size: 13px; border-radius: 4px; border: 1px solid #00BFA5;")
            self.btn_review_sim.clicked.connect(self.open_batch_review)
            self.layout.addWidget(self.btn_review_sim)

        # --- METADATA ---
        if not self.is_batch:
            d = context_dict['data']
            stream_name = context_dict['stream_name']
            self.layout.addWidget(QLabel(f"<b>Stream:</b> {stream_name} | <b>Species:</b> {d['species']}"))
            
            meta_group = QGroupBox("Gemini Diagnostics & Rules")
            meta_group.setStyleSheet("QGroupBox { border: 1px solid #555; margin-top: 10px; } QGroupBox::title { color: #aaa; }")
            meta_layout = QVBoxLayout(meta_group)
            
            alert_val = d.get('alert_sent')
            is_alert = (alert_val == 1 or str(alert_val).lower() == 'true')
            alert_str = "<span style='color: #00E676;'>Yes</span>" if is_alert else "<span style='color: #EF5350;'>No (Filtered)</span>"
            meta_layout.addWidget(QLabel(f"<b>Alert Sent:</b> {alert_str}"))
            
            f_reason = d.get('filter_reason')
            if f_reason:
                parsed_reason = color_code_reason_string(f_reason)
                meta_layout.addWidget(QLabel(f"<b>Block Reason:</b> <span style='color: #bbb;'>{parsed_reason}</span>"))
            
            # --- STRICT NONETYPE FALLBACKS ---
            raw_dist = str(d.get('distance_category') or 'Unknown')
            clean_dist = raw_dist.replace("(Flock/Herd)", "").strip()
            is_flock = "(Flock/Herd)" in raw_dist
            group_str = "Flock/Herd" if is_flock else "Single"
            
            frame_str = str(d.get('frame_size') or 'N/A')
            
            dist_color = "#FFA726" if raw_dist.startswith("Curate:") else "#00E676"
            meta_layout.addWidget(QLabel(f"<b>Type:</b> {group_str} | <b>Depth:</b> <span style='color: {dist_color};'>{clean_dist}</span> | <b>Frame Size:</b> {frame_str}"))
            
            notes_lbl = QLabel(f"<b>AI Reasoning:</b><br><i>{d.get('ai_notes') or 'N/A'}</i>")
            notes_lbl.setWordWrap(True)
            notes_lbl.setStyleSheet("color: #b39ddb; padding: 10px; background: #311b92; border-radius: 4px; margin-top: 5px;")
            meta_layout.addWidget(notes_lbl)
            
            rules_html = parent.get_active_rules_html(d['channel_url'], d['species'])
            rules_lbl = QLabel(rules_html)
            rules_lbl.setWordWrap(True)
            meta_layout.addWidget(rules_lbl)
            
            self.layout.addWidget(meta_group)
        else:
            summary = f"<b>Applying rules to {len(self.unique_combos)} unique Stream/Species combinations.</b><br><br>"
            for url, sp in self.unique_combos[:5]:
                summary += f"- {sp} on {parent.get_stream_name(url)}<br>"
            if len(self.unique_combos) > 5: summary += "...and more."
            self.layout.addWidget(QLabel(summary))

        # --- SMART GROUP CORRECTION (Only if Confirming) ---
        self.group_correction = "Keep"
        if action_name == "Confirm":
            self.group_box = QGroupBox("Smart Group Correction (Fixes AI Miscounts)")
            g_lay = QHBoxLayout(self.group_box)
            self.rb_keep = QRadioButton("Keep AI Decision")
            self.rb_single = QRadioButton("Force Single")
            self.rb_flock = QRadioButton("Force Flock/Herd")
            self.rb_keep.setChecked(True)
            
            self.btn_group = QButtonGroup()
            self.btn_group.addButton(self.rb_keep)
            self.btn_group.addButton(self.rb_single)
            self.btn_group.addButton(self.rb_flock)
            
            g_lay.addWidget(self.rb_keep)
            g_lay.addWidget(self.rb_single)
            g_lay.addWidget(self.rb_flock)
            self.layout.addWidget(self.group_box)

        # --- TUNING FORM (2x2 Matrix) ---
        
        # NIGHT WARNING INDICATOR
        if self.is_night_mode:
            night_lbl = QLabel("🌙 <b>Night/Low-Vis Mode Detected:</b> Your tuning changes will be saved to the dedicated Night Rules matrix and will not affect daytime scanning.")
            night_lbl.setStyleSheet("color: #b39ddb; background-color: #311b92; padding: 8px; border-radius: 4px;")
            night_lbl.setWordWrap(True)
            self.layout.addWidget(night_lbl)
            
        self.layout.addWidget(QLabel("<b>Optional Tuning:</b> Set a surgical blind-spot or 2D/3D size requirement for the selected animal(s) on their specific cameras:"))

        # NEW SHORTCUT TOOLBAR
        shortcut_layout = QHBoxLayout()
        
        self.btn_match = QPushButton("🎯 Match Observed (Allow this)")
        self.btn_match.setStyleSheet("background-color: #388E3C; font-weight: bold; color: white;")
        self.btn_match.clicked.connect(self.auto_tune_to_observed)
        
        self.btn_tighten = QPushButton("🛑 Tighten 1 Notch (Block this)")
        self.btn_tighten.setStyleSheet("background-color: #D32F2F; font-weight: bold; color: white;")
        self.btn_tighten.clicked.connect(self.auto_tune_tighten)
        
        self.btn_explain = QPushButton("❓ How does this work?")
        self.btn_explain.clicked.connect(self.show_explanation)
        
        shortcut_layout.addWidget(self.btn_match)
        shortcut_layout.addWidget(self.btn_tighten)
        shortcut_layout.addWidget(self.btn_explain)
        
        self.layout.addLayout(shortcut_layout)

        form = QFormLayout()
        
        self.combo_f_s = QComboBox()
        self.combo_f_s.addItem("Don't change (Use Global Defaults)", "Don't change (Use Global Defaults)")
        for i, f in enumerate(FRAME_LEVELS):
            tag = " (Strictest)" if i == 0 else (" (Loosest)" if i == len(FRAME_LEVELS)-1 else "")
            self.combo_f_s.addItem(f"{7-i} - {f}{tag}", f)
            
        self.combo_d_s = QComboBox()
        self.combo_d_s.addItem("Don't change (Use Global Defaults)", "Don't change (Use Global Defaults)")
        for i, d in enumerate(DEPTH_LEVELS):
            tag = " (Closest/Strictest)" if i == 0 else (" (Farthest/Loosest)" if i == len(DEPTH_LEVELS)-1 else "")
            self.combo_d_s.addItem(f"{7-i} - {d}{tag}", d)
            
        self.combo_f_f = QComboBox()
        self.combo_f_f.addItem("Don't change (Use Global Defaults)", "Don't change (Use Global Defaults)")
        for i, f in enumerate(FRAME_LEVELS):
            tag = " (Strictest)" if i == 0 else (" (Loosest)" if i == len(FRAME_LEVELS)-1 else "")
            self.combo_f_f.addItem(f"{7-i} - {f}{tag}", f)
            
        self.combo_d_f = QComboBox()
        self.combo_d_f.addItem("Don't change (Use Global Defaults)", "Don't change (Use Global Defaults)")
        for i, d in enumerate(DEPTH_LEVELS):
            tag = " (Closest/Strictest)" if i == 0 else (" (Farthest/Loosest)" if i == len(DEPTH_LEVELS)-1 else "")
            self.combo_d_f.addItem(f"{7-i} - {d}{tag}", d)
        
        prompt_layout = QHBoxLayout()
        self.edit_prompt = QLineEdit()
        self.edit_prompt.setPlaceholderText("e.g. 'Ignore the raccoon-shaped rock on the right.'")
        prompt_layout.addWidget(self.edit_prompt)
        
        if not self.is_batch:
            self.btn_view_image = QPushButton("🖼️ View Image")
            self.btn_view_image.setStyleSheet("background-color: #555; font-weight: bold;")
            self.btn_view_image.clicked.connect(self.view_image)
            if not self.pixmap or self.pixmap.isNull(): self.btn_view_image.setEnabled(False)
            prompt_layout.addWidget(self.btn_view_image)
        
        form.addRow("Min Frame Size (Single):", self.combo_f_s)
        form.addRow("Min Depth (Single):", self.combo_d_s)
        form.addRow("Min Frame Size (Flock):", self.combo_f_f)
        form.addRow("Min Depth (Flock):", self.combo_d_f)
        form.addRow("Custom Negative Prompt:", prompt_layout)
        
        self.layout.addLayout(form)
        
        # --- BUTTONS ---
        btn_layout = QHBoxLayout()
        self.btn_cancel = QPushButton("Cancel Operation")
        self.btn_cancel.clicked.connect(self.reject)
        
        self.btn_save = QPushButton(f"Proceed to {action_name}")
        self.btn_save.setStyleSheet(f"background-color: {action_color}; color: white; font-weight: bold; padding: 8px 15px;")
        self.btn_save.clicked.connect(self.accept)
        
        self.btn_save_next = QPushButton(f"Proceed to {action_name} & Next")
        self.btn_save_next.setStyleSheet(f"background-color: {action_color}; color: white; font-weight: bold; padding: 8px 15px;")
        self.btn_save_next.clicked.connect(self._accept_and_next)
        
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_save)
        btn_layout.addWidget(self.btn_save_next)
        
        self.layout.addSpacing(10)
        self.layout.addLayout(btn_layout)

    def _accept_and_next(self):
        self.go_next = True
        self.accept()

    # --- AUTO TUNING METHODS ---
    def _get_observed_metrics(self):
        d = self.context_dict.get('data', {})
        # STRICT NONETYPE FALLBACK
        obs_f = str(d.get('frame_size') or 'NONE').upper()
        raw_dist = str(d.get('distance_category') or 'NONE')
        is_flock = "(Flock/Herd)" in raw_dist
        obs_d = raw_dist.replace("(Flock/Herd)", "").replace("Curate:", "").strip().upper()
        return obs_f, obs_d, is_flock

    def set_combo_by_data(self, combo, target_data):
        if not target_data: return False
        target_str = str(target_data).upper()
        for i in range(combo.count()):
            c_data = combo.itemData(i)
            if c_data and str(c_data).upper() == target_str:
                combo.setCurrentIndex(i)
                return True
        return False
        
    def flash_combo(self, combos, color_hex):
        original_styles =[c.styleSheet() for c in combos]
        for c in combos:
            c.setStyleSheet(f"background-color: {color_hex}; color: black; font-weight: bold;")
        def restore():
            for c, s in zip(combos, original_styles):
                c.setStyleSheet(s)
        QTimer.singleShot(600, restore)

    def auto_tune_to_observed(self):
        obs_f, obs_d, is_flock = self._get_observed_metrics()
        combos_to_flash =[]
        if is_flock:
            if self.set_combo_by_data(self.combo_f_f, obs_f): combos_to_flash.append(self.combo_f_f)
            if self.set_combo_by_data(self.combo_d_f, obs_d): combos_to_flash.append(self.combo_d_f)
        else:
            if self.set_combo_by_data(self.combo_f_s, obs_f): combos_to_flash.append(self.combo_f_s)
            if self.set_combo_by_data(self.combo_d_s, obs_d): combos_to_flash.append(self.combo_d_s)
            
        if combos_to_flash:
            self.flash_combo(combos_to_flash, "#00E676")

    def auto_tune_tighten(self):
        obs_f, obs_d, is_flock = self._get_observed_metrics()
        
        FRAME_MAP = {k.upper(): 7-i for i, k in enumerate(FRAME_LEVELS)}
        DEPTH_MAP = {k.upper(): 7-i for i, k in enumerate(DEPTH_LEVELS)}
        REV_F = {v: k for k, v in FRAME_MAP.items()}
        REV_D = {v: k for k, v in DEPTH_MAP.items()}
        
        val_f = FRAME_MAP.get(obs_f)
        val_d = DEPTH_MAP.get(obs_d)
        
        req_f = obs_f
        req_d = obs_d
        
        if val_f is not None and val_f < 7: req_f = REV_F[val_f + 1]
        if val_d is not None and val_d < 7: req_d = REV_D[val_d + 1]
        
        combos_to_flash =[]
        if is_flock:
            if self.set_combo_by_data(self.combo_f_f, req_f): combos_to_flash.append(self.combo_f_f)
            if self.set_combo_by_data(self.combo_d_f, req_d): combos_to_flash.append(self.combo_d_f)
        else:
            if self.set_combo_by_data(self.combo_f_s, req_f): combos_to_flash.append(self.combo_f_s)
            if self.set_combo_by_data(self.combo_d_s, req_d): combos_to_flash.append(self.combo_d_s)
            
        if combos_to_flash:
            self.flash_combo(combos_to_flash, "#EF5350")
            
    def show_explanation(self):
        msg = (
            "<h3>How 2D Frame & 3D Depth Work (The AND-Gate)</h3>"
            "<p>For an animal to pass the filter and appear on the web map, it must satisfy <b>BOTH</b> your Frame Size rule <b>AND</b> your Depth rule. If either one fails, the animal is blocked.</p>"
            "<hr>"
            "<p><b>1. Frame Size (2D Pixel Footprint)</b><br>"
            "How much of the screen does the animal take up? A small bird sitting right on the camera lens might be 'Massive' in the frame, while an elephant a mile away might be a 'Speck'.</p>"
            "<p><b>2. Depth (3D Physical Distance)</b><br>"
            "How far away is the animal in the real world? The AI estimates this regardless of camera zoom. That elephant is in the 'Background', even if the camera is zoomed in so it looks 'Large' in the frame.</p>"
            "<hr>"
            "<b>How to use the numbers:</b><br>"
            "The scale is 7 (Strictest/Largest/Closest) to 1 (Loosest/Smallest/Farthest).<br>"
            "If you set the requirement to <b>4 - Medium</b>, then anything 4, 5, 6, or 7 will pass. Anything 3, 2, or 1 will fail."
        )
        QMessageBox.information(self, "Logic Explanation", msg)

    def open_batch_review(self):
        dialog = BatchReviewDialog(self.similar_rows, getattr(self, 'f_reason_fuzzy', ''), self)
        dialog.exec()
        self.extra_batch_items = dialog.get_accepted()
        
        total_count = self.count + len(self.extra_batch_items)
        action_name = self.windowTitle().split(" ")[0]
        
        self.btn_review_sim.setText(f"✅ Included {len(self.extra_batch_items)} additional items to batch.")
        self.btn_review_sim.setStyleSheet("background-color: #388E3C; font-weight: bold; margin-bottom: 10px; padding: 10px;")
        self.header_lbl.setText(f"<h2 style='color: {'#388E3C' if 'Confirm' in action_name else '#D32F2F'}; margin: 0;'>{action_name.upper()}ING {total_count} RECORD(S)</h2>")

    def view_image(self):
        if self.pixmap and not self.pixmap.isNull():
            viewer = ImageViewerDialog(pixmap=self.pixmap, parent=self)
            viewer.exec()

    def get_tuning(self):
        return (
            self.combo_f_s.currentData(),
            self.combo_d_s.currentData(),
            self.combo_f_f.currentData(),
            self.combo_d_f.currentData(),
            self.edit_prompt.text().strip(),
            self.is_night_mode # Pass the night mode flag up
        )
        
    def get_group_correction(self):
        if not hasattr(self, 'rb_single'): return "Keep"
        if self.rb_single.isChecked(): return "Single"
        if self.rb_flock.isChecked(): return "Flock"
        return "Keep"
        
    def get_extra_batch(self):
        return self.extra_batch_items

    def accept(self):
        total_to_process = self.count + len(self.extra_batch_items)
        if total_to_process > 1:
            msg = f"⚠️ WARNING: You are about to batch process {total_to_process} images simultaneously.\n\nAre you ABSOLUTELY sure there are no AI misidentifications hidden in this batch?"
            if QMessageBox.warning(self, "Confirm Batch Curation", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        super().accept()

class CurationStudio(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Multimodal Curation & AI Retraining Studio (V40.21)")
        self.resize(1400, 850)
        self.setStyleSheet("""
            QMainWindow, QWidget { background-color: #1e1e1e; color: #e0e0e0; font-family: 'Segoe UI', sans-serif; }
            QGroupBox { border: 1px solid #444; margin-top: 15px; font-weight: bold; color: #00E676; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QTableWidget { background-color: #2b2b2b; gridline-color: #444; border: none; }
            QTableWidget::item:selected { background-color: #0078d7; color: white; }
            QHeaderView::section { background-color: #333; color: white; padding: 4px; border: 1px solid #444; }
            QPushButton { background-color: #333; color: white; padding: 8px; border: 1px solid #555; border-radius: 4px; font-weight: bold; }
            QPushButton:hover { background-color: #444; }
            QComboBox, QLineEdit { background-color: #333; color: white; padding: 6px; border: 1px solid #555; border-radius: 4px; }
        """)

        # Media Player Setup
        self.player = None
        self.audio_output = None
        if MULTIMEDIA_AVAILABLE:
            self.player = QMediaPlayer()
            self.audio_output = QAudioOutput()
            self.player.setAudioOutput(self.audio_output)
            self.audio_output.setVolume(1.0)

        self.current_pixmap = QPixmap()
        self.stream_name_map = self._load_stream_names()

        # The Debounce Timer for smart SQL searching
        self.search_debounce_timer = QTimer(self)
        self.search_debounce_timer.setSingleShot(True)
        self.search_debounce_timer.setInterval(500)
        self.search_debounce_timer.timeout.connect(self.manual_refresh)

        self.init_ui()
        self.load_settings()
        db_connector.check_and_migrate_schema()
        self.load_database()

    def _load_stream_names(self):
        m = {}
        if CONFIG_FILE.exists():
            try:
                cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                for s in cfg.get('streams',[]):
                    m[s['page_url']] = s['name']
                    if s.get('original_url'):
                        m[s['original_url']] = s['name']
            except: pass
        return m

    def get_stream_name(self, url):
        """Robust stream name resolution bypassing variant tags."""
        if not url: return "Unknown"
        if url in self.stream_name_map: return self.stream_name_map[url]
        clean_url = re.sub(r'[\?&]variant=\d+', '', url)
        if clean_url in self.stream_name_map: return self.stream_name_map[clean_url]
        base_url = clean_url.split('?')[0]
        if base_url in self.stream_name_map: return self.stream_name_map[base_url]
        for mapped_url, name in self.stream_name_map.items():
            if clean_url in mapped_url or base_url in mapped_url:
                return name
        return url

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)

        # --- TOP TOOLBAR ---
        toolbar = QHBoxLayout()
        
        self.filter_method = QComboBox()
        self.filter_method.addItems(["All Methods", "Multimodal Only", "Vision Only", "Audio Only"])
        self.filter_method.currentIndexChanged.connect(self.manual_refresh)
        
        self.filter_status = QComboBox()
        self.filter_status.addItems(["Pending Curation", "All Statuses", "Confirmed", "Rejected"])
        self.filter_status.currentIndexChanged.connect(self.manual_refresh)
        
        self.filter_alert = QComboBox()
        self.filter_alert.addItems(["All Map Statuses", "Sent to Web Map", "Filtered / Blocked"])
        self.filter_alert.currentIndexChanged.connect(self.manual_refresh)
        
        self.filter_group = QComboBox()
        self.filter_group.addItems(["All Group Types", "Singles Only", "Flocks/Herds Only"])
        self.filter_group.currentIndexChanged.connect(self.manual_refresh)
        
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search species or stream across entire DB history...")
        self.search_box.textChanged.connect(self.search_debounce_timer.start) 

        self.btn_select_all = QPushButton("☑ Select All")
        self.btn_select_all.clicked.connect(self.select_all_rows)
        self.btn_select_all.setStyleSheet("background-color: #0078d7; font-weight: bold;")

        self.btn_refresh = QPushButton("🔄 Refresh Data")
        self.btn_refresh.clicked.connect(self.manual_refresh)

        toolbar.addWidget(QLabel("<b>Method:</b>"))
        toolbar.addWidget(self.filter_method)
        toolbar.addSpacing(10)
        toolbar.addWidget(QLabel("<b>Status:</b>"))
        toolbar.addWidget(self.filter_status)
        toolbar.addSpacing(10)
        toolbar.addWidget(QLabel("<b>Map:</b>"))
        toolbar.addWidget(self.filter_alert)
        toolbar.addSpacing(10)
        toolbar.addWidget(QLabel("<b>Group:</b>"))
        toolbar.addWidget(self.filter_group)
        toolbar.addSpacing(10)
        toolbar.addWidget(QLabel("<b>Smart Search:</b>"))
        toolbar.addWidget(self.search_box, 1) 
        toolbar.addWidget(self.btn_select_all)
        toolbar.addSpacing(10)
        toolbar.addWidget(self.btn_refresh)
        layout.addLayout(toolbar)

        # --- SPLITTER ---
        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        # LEFT PANE: Detection List
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["ID", "Timestamp", "Method", "Species", "Stream"])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.itemSelectionChanged.connect(self.on_selection_changed)
        
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        
        self.splitter.addWidget(self.table)

        # RIGHT PANE: Review Studio
        right_pane = QWidget()
        right_layout = QVBoxLayout(right_pane)

        self.meta_scroll = QScrollArea()
        self.meta_scroll.setWidgetResizable(True)
        self.meta_scroll.setFixedHeight(220)
        self.meta_scroll.setStyleSheet("background: #222; border-radius: 5px; border: none;")
        
        self.meta_container = QWidget()
        self.meta_layout = QVBoxLayout(self.meta_container)
        self.meta_layout.setContentsMargins(10, 10, 10, 10)
        
        self.lbl_meta = QLabel("Select one or more detections from the list to begin curation.")
        self.lbl_meta.setStyleSheet("font-size: 14px; background: transparent;")
        self.lbl_meta.setWordWrap(True)
        self.lbl_meta.setOpenExternalLinks(True) # REQUIRED FOR GOOGLE SEARCH
        self.meta_layout.addWidget(self.lbl_meta)
        
        self.lbl_active_rules = QLabel("")
        self.lbl_active_rules.setWordWrap(True)
        self.lbl_active_rules.setStyleSheet("background: transparent;")
        self.meta_layout.addWidget(self.lbl_active_rules)
        
        self.meta_layout.addStretch()
        self.meta_scroll.setWidget(self.meta_container)
        right_layout.addWidget(self.meta_scroll)

        # Media Splitter
        media_layout = QHBoxLayout()
        self.lbl_image = ClickableImageLabel("No Visual Data")
        self.lbl_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_image.setStyleSheet("background: #000; border: 1px solid #555;")
        self.lbl_image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.lbl_image.doubleClicked.connect(self.open_image_viewer)
        self.lbl_image.setToolTip("Double-click to open large viewer.")
        media_layout.addWidget(self.lbl_image, 2)

        audio_group = QGroupBox("Acoustic Data")
        audio_layout = QVBoxLayout(audio_group)
        self.btn_play = QPushButton("▶ Play Audio")
        self.btn_play.setStyleSheet("background-color: #0078d7; font-size: 16px; padding: 15px;")
        self.btn_play.clicked.connect(self.play_audio)
        self.btn_play.setEnabled(False)
        audio_layout.addWidget(self.btn_play)
        audio_layout.addStretch()
        media_layout.addWidget(audio_group, 1)

        right_layout.addLayout(media_layout, 1)

        # --- AI CORRECTION & RETRAINING ---
        teach_group = QGroupBox("AI Correction & Retraining (Fix & Teach)")
        teach_group.setStyleSheet("QGroupBox { border: 1px solid #673AB7; margin-top: 15px; font-weight: bold; color: #b39ddb; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }")
        teach_layout = QVBoxLayout(teach_group)
        
        teach_desc = QLabel("Correct a misidentified animal. This exports the old mistake to ML, updates the local DB, renames the image, adds to registry, injects into AI watchlist, AND triggers a Pinpoint Sync.")
        teach_desc.setWordWrap(True)
        teach_desc.setStyleSheet("color: #bbb; font-size: 12px;")
        teach_layout.addWidget(teach_desc)
        
        t_row = QHBoxLayout()
        self.edit_correct_name = QLineEdit()
        self.edit_correct_name.setPlaceholderText("Enter exact true name (e.g. Polar Bear)...")
        
        self.btn_teach = QPushButton("✨ Rename & Teach AI")
        self.btn_teach.setStyleSheet("background-color: #673AB7; font-size: 14px; font-weight: bold;")
        self.btn_teach.clicked.connect(self.teach_ai)
        self.btn_teach.setEnabled(False)
        
        t_row.addWidget(self.edit_correct_name, 1)
        t_row.addWidget(self.btn_teach)
        teach_layout.addLayout(t_row)
        right_layout.addWidget(teach_group)

        # --- ML EXPORTER CONTROLS ---
        export_group = QGroupBox("Neural Network Dataset Exporter & Cloud Rollback")
        export_layout = QVBoxLayout(export_group)
        
        export_desc = QLabel("<b>NOTE:</b> If the AI correctly identified the animal, you MUST click <b>Confirm</b>. "
                             "Even if the system correctly blocked it from the web map due to size/depth rules, "
                             "it is still a True Positive for the AI. Only click Reject for AI hallucinations (e.g., a rock called a bear).")
        export_desc.setWordWrap(True)
        export_desc.setStyleSheet("color: #FF9800; font-size: 12px; margin-bottom: 5px;")
        export_layout.addWidget(export_desc)

        self.lbl_export_path = QLabel("Export Path: Pending selection...")
        self.lbl_export_path.setStyleSheet("color: #00E5FF; font-family: monospace; font-size: 11px; margin-bottom: 5px;")
        export_layout.addWidget(self.lbl_export_path)

        row2 = QHBoxLayout()
        self.btn_confirm = QPushButton("✅ CONFIRM (AI was Correct)")
        self.btn_confirm.setStyleSheet("background-color: #388E3C; font-size: 14px;")
        self.btn_confirm.clicked.connect(lambda: self.curate_detection('confirmed'))
        
        self.btn_reject = QPushButton("❌ REJECT (AI Hallucinated)")
        self.btn_reject.setStyleSheet("background-color: #D32F2F; font-size: 14px;")
        self.btn_reject.setToolTip("Exports to neg_ folder, Erases Local DB, and Deletes Cloud Record (Map Rollback).")
        self.btn_reject.clicked.connect(lambda: self.curate_detection('rejected'))

        self.btn_confirm.setEnabled(False)
        self.btn_reject.setEnabled(False)

        row2.addWidget(self.btn_confirm)
        row2.addWidget(self.btn_reject)
        export_layout.addLayout(row2)

        right_layout.addWidget(export_group)
        self.splitter.addWidget(right_pane)
        self.splitter.setSizes([500, 900])
        layout.addWidget(self.splitter)

    class NumericTableWidgetItem(QTableWidgetItem):
        def __lt__(self, other):
            try: return float(self.text()) < float(other.text())
            except: return super().__lt__(other)

    def select_all_rows(self):
        self.table.selectAll()

    def show_context_menu(self, pos):
        item = self.table.itemAt(pos)
        if not item: return
        row = item.row()
        data = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        
        menu = QMenu()
        verify_action = QAction(f"Verify '{data['species']}' on Wikipedia", self)
        verify_action.triggered.connect(lambda: webbrowser.open(f"https://en.wikipedia.org/wiki/{quote_plus(data['species'])}"))
        menu.addAction(verify_action)
        
        url_action = QAction(f"🌐 Open Stream URL", self)
        url_action.triggered.connect(lambda: webbrowser.open(data['channel_url']))
        menu.addAction(url_action)
        
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def manual_refresh(self):
        self.btn_refresh.setText("⏳ Refreshing...")
        self.btn_refresh.setEnabled(False)
        self.table.clearSelection()
        QApplication.processEvents()
        self.load_database()
        self.btn_refresh.setText("🔄 Refresh Data")
        self.btn_refresh.setEnabled(True)

    def load_database(self):
        if not DATABASE_PATH.exists(): return

        self.table.setSortingEnabled(False)
        self.table.clearContents()
        self.table.setRowCount(0)
        
        method_filter = self.filter_method.currentText()
        status_filter = self.filter_status.currentText()
        alert_filter = self.filter_alert.currentText()
        group_filter = self.filter_group.currentText()
        search_text = self.search_box.text().strip()

        query = "SELECT id, timestamp, channel_url, species, distance_category, snr, detection_method, vision_path, human_verified, alert_sent, ai_notes, frame_size, filter_reason FROM detections WHERE 1=1"
        params =[]

        if status_filter == "Pending Curation": query += " AND (human_verified = 'pending' OR human_verified IS NULL)"
        elif status_filter == "Confirmed": query += " AND human_verified = 'confirmed'"
        elif status_filter == "Rejected": query += " AND human_verified = 'rejected'"

        if method_filter == "Multimodal Only": query += " AND detection_method = 'multimodal'"
        elif method_filter == "Vision Only": query += " AND detection_method = 'vision'"
        elif method_filter == "Audio Only": query += " AND detection_method = 'audio'"
            
        if alert_filter == "Sent to Web Map": query += " AND (alert_sent = 1 OR alert_sent = 'true' OR alert_sent = 'True' OR alert_sent = TRUE)"
        elif alert_filter == "Filtered / Blocked": query += " AND (alert_sent = 0 OR alert_sent = 'false' OR alert_sent = 'False' OR alert_sent = FALSE OR alert_sent IS NULL)"
            
        if group_filter == "Singles Only": query += " AND distance_category NOT LIKE '%(Flock/Herd)%'"
        elif group_filter == "Flocks/Herds Only": query += " AND distance_category LIKE '%(Flock/Herd)%'"

        if search_text:
            matching_urls =[url for url, name in self.stream_name_map.items() if search_text.lower() in name.lower()]
            if matching_urls:
                url_placeholders = ','.join('?' * len(matching_urls))
                query += f" AND (species LIKE ? OR channel_url LIKE ? OR channel_url IN ({url_placeholders}))"
                params.extend([f"%{search_text}%", f"%{search_text}%"] + matching_urls)
            else:
                query += " AND (species LIKE ? OR channel_url LIKE ?)"
                params.extend([f"%{search_text}%", f"%{search_text}%"])

        query += " ORDER BY timestamp DESC LIMIT 10000"

        try:
            with db_connector.get_db_connection(force_local=True) as con:
                con.conn.row_factory = sqlite3.Row
                cur = con.cursor()
                try: cur.execute(query, params)
                except sqlite3.OperationalError:
                    query_fallback = query.replace(", frame_size, filter_reason", "")
                    cur.execute(query_fallback, params)
                    
                rows = cur.fetchall()

            self.table.setRowCount(len(rows))
            for i, r in enumerate(rows):
                rd = dict(r)
                if 'frame_size' not in rd: rd['frame_size'] = 'N/A'
                if 'filter_reason' not in rd: rd['filter_reason'] = ''
                
                self.table.setItem(i, 0, self.NumericTableWidgetItem(str(rd['id'])))
                ts_str = datetime.fromtimestamp(rd['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
                self.table.setItem(i, 1, QTableWidgetItem(ts_str))
                
                method = str(rd['detection_method']).upper()
                item_m = QTableWidgetItem(method)
                if method == 'MULTIMODAL': item_m.setForeground(QBrush(QColor("#FF9800")))
                elif method == 'VISION': item_m.setForeground(QBrush(QColor("#00E676")))
                self.table.setItem(i, 2, item_m)
                
                # --- SPECIES TOOLTIP UPGRADE ---
                sp_name_str = str(rd['species'])
                sp_item = QTableWidgetItem(sp_name_str)
                sp_item.setToolTip(sp_name_str) 
                self.table.setItem(i, 3, sp_item)
                
                stream_name = self.get_stream_name(rd['channel_url'])
                stream_item = QTableWidgetItem(stream_name)
                stream_item.setToolTip(stream_name) 
                self.table.setItem(i, 4, stream_item)
                
                self.table.item(i, 0).setData(Qt.ItemDataRole.UserRole, rd)

            self.table.setSortingEnabled(True)
            self.table.sortItems(1, Qt.SortOrder.DescendingOrder)

        except Exception as e:
            QMessageBox.critical(self, "Database Error", str(e))

    def get_active_rules_html(self, stream_url, species):
        targets = {"global_defaults": {}, "registry": {}, "assignments": {}, "stream_overrides": {}}
        if VISION_TARGETS_FILE.exists():
            try: targets = json.loads(VISION_TARGETS_FILE.read_text(encoding='utf-8'))
            except: pass
            
        rules =[]
        g_prompt = targets.get("registry", {}).get(species, {}).get("global_prompt", "")
        if g_prompt: rules.append(f"<li><b>Global Registry Prompt:</b> <span style='color:#FFF59D;'>'{g_prompt}'</span></li>")
            
        s_ov = targets.get("stream_overrides", {}).get(stream_url, {})
        s_prompt = s_ov.get("custom_prompt", "")
        if s_prompt: rules.append(f"<li><b>Stream Camera Prompt:</b> <span style='color:#81D4FA;'>'{s_prompt}'</span></li>")
            
        sp_ov = s_ov.get("species_rules", {}).get(species, {})
        sp_prompt = sp_ov.get("prompt", "")
        if sp_prompt: rules.append(f"<li><b>Local Surgical Prompt:</b> <span style='color:#FFAB91;'>'{sp_prompt}'</span></li>")
            
        # Also check night rules for display
        night_ov = s_ov.get("night_species_rules", {}).get(species, {})
        night_prompt = night_ov.get("prompt", "")
        if night_prompt: rules.append(f"<li>🌙 <b>Night/Low-Vis Prompt:</b> <span style='color:#b39ddb;'>'{night_prompt}'</span></li>")
            
        if not rules: return "<div style='color:#888; font-size:12px; margin-top:5px;'>No custom prompts or overrides active for this animal on this stream.</div>"
            
        return "<div style='margin-top:10px; font-size:12px; background:#111; border:1px solid #333; padding:5px; border-radius:4px;'><b>Active Prompts/Rules:</b><ul style='margin-top:5px; margin-bottom:5px; padding-left:20px;'>" + "".join(rules) + "</ul></div>"

    def on_selection_changed(self):
        selected = self.table.selectedItems()
        if not selected:
            self.btn_confirm.setEnabled(False)
            self.btn_reject.setEnabled(False)
            self.btn_play.setEnabled(False)
            self.btn_teach.setEnabled(False)
            self.edit_correct_name.clear()
            self.lbl_meta.setText("Select a detection from the list to begin curation.")
            self.lbl_active_rules.setText("")
            self.lbl_image.clear()
            self.lbl_image.setText("No Visual Data")
            self.lbl_export_path.setText("Export Path: Pending selection...")
            self.current_pixmap = QPixmap()
            return

        unique_rows = list(set(item.row() for item in selected))
        if len(unique_rows) > 1:
            self.lbl_meta.setText(f"<b>Batch Mode:</b> <span style='color:#FF9800;'>{len(unique_rows)} detections selected</span> for processing.")
            self.lbl_active_rules.setText("")
            self.lbl_image.clear()
            self.lbl_image.setText(f"[ {len(unique_rows)} Images Selected ]\nBatch Actions Enabled.\n\nWizard Will Apply Rules to Unique Camera+Species Pairs.")
            self.lbl_export_path.setText("Export Path: Dynamic batch execution.")
            self.current_pixmap = QPixmap()
            self.btn_play.setEnabled(False)
            self.btn_play.setText("Batch Audio Disabled")
            self.btn_play.setStyleSheet("background-color: #555; font-size: 16px; padding: 15px;")
        else:
            row = unique_rows[0]
            data = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)

            stream_name = self.get_stream_name(data['channel_url'])
            ts_str = datetime.fromtimestamp(data['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
            
            ai_notes = data.get('ai_notes') or "None."
            alert_val = data.get('alert_sent')
            is_alert = (alert_val == 1 or str(alert_val).lower() == 'true')
            alert_status = "<span style='color:#00E676; font-weight:bold;'>SENT TO MAP</span>" if is_alert else "<span style='color:#EF5350; font-weight:bold;'>FILTERED/BLOCKED</span>"
            
            f_reason = data.get('filter_reason')
            parsed_reason = color_code_reason_string(f_reason) if f_reason else ""
            reason_str = f"<br><b>Block Reason:</b> <span style='color:#bbb;'>{parsed_reason}</span>" if parsed_reason else ""
            
            # --- STRICT NONETYPE FALLBACK FOR RAW DATA ---
            raw_dist = str(data.get('distance_category') or 'Unknown')
            is_flock = "(Flock/Herd)" in raw_dist
            clean_dist = raw_dist.replace("(Flock/Herd)", "").strip()
            group_type = "Flock/Herd" if is_flock else "Single"
            
            # --- BUILD SEARCH LINK ---
            search_url = f"https://www.google.com/search?tbm=isch&q={quote_plus(str(data.get('species') or 'Unknown'))}"
            
            meta_html = f"""
            <h2 style="margin-bottom:5px;"><a href="{search_url}" style="color: #00E676; text-decoration: none;" target="_blank">{data.get('species')}</a></h2>
            <b>Time:</b> {ts_str} <br>
            <b>Stream:</b> {stream_name} <br>
            <b>Method:</b> <span style='color:#00E676;'>{str(data.get('detection_method') or 'VISION').upper()}</span> <br>
            <b>Type:</b> {group_type} | <b>Depth:</b> {clean_dist} | <b>Frame Size:</b> {str(data.get('frame_size') or 'N/A')} <br>
            <b>Alert Status:</b> {alert_status}{reason_str}
            <div style='color: #b39ddb; padding: 8px; background: #311b92; border-radius: 4px; margin-top: 5px;'>
                <b>AI Notes (Features & Location):</b><br>{ai_notes}
            </div>
            """
            self.lbl_meta.setText(meta_html)
            self.lbl_active_rules.setText(self.get_active_rules_html(data['channel_url'], data.get('species')))

            v_path = data.get('vision_path')
            if v_path and os.path.exists(v_path):
                self.current_pixmap = QPixmap(v_path)
                self.lbl_image.setPixmap(self.current_pixmap.scaled(self.lbl_image.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            else:
                self.current_pixmap = QPixmap()
                self.lbl_image.clear()
                self.lbl_image.setText("No Visual Data Attached")

            audio_path_mp3 = BASELINE_CLIPS_DIR / f"detection_{data['id']}.mp3"
            audio_path_wav = BASELINE_CLIPS_DIR / f"detection_{data['id']}.wav"
            
            if audio_path_mp3.exists() or audio_path_wav.exists():
                self.btn_play.setEnabled(True)
                self.btn_play.setText("▶ Play Audio")
                self.btn_play.setStyleSheet("background-color: #0078d7; font-size: 16px; padding: 15px;")
            else:
                self.btn_play.setEnabled(False)
                self.btn_play.setText("No Audio Saved")
                self.btn_play.setStyleSheet("background-color: #555; font-size: 16px; padding: 15px;")

            # --- DYNAMIC EXPORT PATH ---
            clean_sp = str(data.get('species') or 'Unknown').lower().replace(" ", "_").replace("/", "")
            group_str = "flock" if is_flock else "single"
            self.lbl_export_path.setText(f"Export Path: ml_exports/pos/{clean_sp}/{group_str}/")

        self.btn_confirm.setEnabled(True)
        self.btn_reject.setEnabled(True)
        self.btn_teach.setEnabled(True)
        self.edit_correct_name.clear()

    def open_image_viewer(self):
        if not self.current_pixmap.isNull():
            viewer = ImageViewerDialog(pixmap=self.current_pixmap, parent=self)
            viewer.exec()

    def _check_and_purge_orphaned_species(self, species_set):
        bot_token = ""
        if CONFIG_FILE.exists():
            try:
                cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                bot_token = cfg.get("bot_token", "")
            except: pass
            
        for sp in species_set:
            try:
                with db_connector.get_db_connection(force_local=True) as con:
                    cur = con.cursor()
                    cur.execute("SELECT COUNT(*) FROM detections WHERE species = ?", (sp,))
                    count = cur.fetchone()[0]
                    
                    if count == 0:
                        logging.info(f"Species '{sp}' eradicated from database. Purging images...")
                        try:
                            with sqlite3.connect(IMAGE_DB_PATH, timeout=10) as img_con:
                                img_con.execute("DELETE FROM species_images WHERE species_name = ?", (sp,))
                        except Exception as e:
                            logging.error(f"Failed to delete local image for {sp}: {e}")
                        
                        if bot_token and cfg.get('database_cloud', {}).get('enabled'):
                            try:
                                res = requests.post("https://wilddetection.com/api/delete_image", data={'secret_token': bot_token, 'species_name': sp}, timeout=10)
                                if res.status_code == 200:
                                    logging.info(f"Successfully deleted '{sp}' image from Cloud.")
                            except Exception as e:
                                logging.error(f"Cloud image purge failed for {sp}: {e}")
            except Exception as e:
                logging.error(f"Error checking orphan status for {sp}: {e}")

    def teach_ai(self):
        selected_items = self.table.selectedItems()
        if not selected_items: return
        
        unique_rows = list(set(item.row() for item in selected_items))
        targets_data =[self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in unique_rows]
        total = len(targets_data)
        
        # --- THE APOSTROPHE CURATION PATCH ---
        # Correctly capitalize names with apostrophes (e.g. "Grauer's Gorilla" instead of "Grauer'S Gorilla")
        new_name = self.edit_correct_name.text().strip().title().replace("'S", "'s")
        if not new_name:
            QMessageBox.warning(self, "Missing Name", "Please type the correct animal name first.")
            return

        cfg = {}
        if CONFIG_FILE.exists():
            try: cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            except: pass
            
        bot_token = cfg.get("bot_token")
        cloud_cfg = cfg.get('database_cloud', {})
        do_cloud_sync = cloud_cfg.get('enabled') and bot_token

        targets = {"global_defaults": {"ignore_general_animals": False}, "registry": {}, "assignments": {}}
        if VISION_TARGETS_FILE.exists():
            try: targets = json.loads(VISION_TARGETS_FILE.read_text(encoding='utf-8'))
            except: pass
        if "registry" not in targets: targets["registry"] = {}
        if "assignments" not in targets: targets["assignments"] = {}

        if new_name not in targets["registry"]:
            targets["registry"][new_name] = {
                "type": "Specific", "synonyms":[], "behavior": "Alert",
                "cooldown_minutes": 60, "show_on_map": True
            }

        old_species_set = set()
        success_count = 0
        cloud_fails = 0

        for i, d in enumerate(targets_data):
            self.btn_teach.setText(f"⏳ Processing {i+1} of {total}...")
            QApplication.processEvents()

            old_name = d.get('species') or 'Unknown'
            old_species_set.add(old_name)
            did = d['id']
            url = d['channel_url']
            v_path = d.get('vision_path')
            det_timestamp = d['timestamp']
            
            alert_val = d.get('alert_sent')
            is_alert = (alert_val == 1 or str(alert_val).lower() == 'true')
            
            # --- THE MISTAKE ML EXPORT PATCH (SANITIZED SNR) ---
            try:
                clean_old_sp = str(old_name).lower().replace(" ", "_").replace("/", "")
                mistake_folder = ML_EXPORT_DIR / "neg" / clean_old_sp / "mistake"
                mistake_folder.mkdir(parents=True, exist_ok=True)
                
                ts_str = datetime.fromtimestamp(det_timestamp).strftime('%Y%m%d_%H%M%S')
                clean_st = "".join(x for x in self.get_stream_name(url) if x.isalnum())[:15]
                
                snr_val = d.get('snr') or 0.0
                snr_str = f"{snr_val:.2f}"
                base_name = f"{ts_str}__{clean_old_sp}__{snr_str}__{clean_st}"
                
                if v_path and os.path.exists(v_path):
                    shutil.copy2(v_path, mistake_folder / f"{base_name}.jpg")
                    
                p_mp3 = BASELINE_CLIPS_DIR / f"detection_{did}.mp3"
                p_wav = BASELINE_CLIPS_DIR / f"detection_{did}.wav"
                if p_mp3.exists(): shutil.copy2(p_mp3, mistake_folder / f"{base_name}.mp3")
                elif p_wav.exists(): shutil.copy2(p_wav, mistake_folder / f"{base_name}.wav")
                
                meta = {
                    "timestamp": ts_str, "stream": self.get_stream_name(url), "url": url,
                    "classification": {"score": snr_val, "primary_guess": old_name, "method": str(d.get('detection_method') or 'vision')},
                    "verified": False, "corrected_to": new_name, "user_label": "mistake"
                }
                (mistake_folder / f"{base_name}.json").write_text(json.dumps(meta, indent=2))
            except Exception as e:
                logging.error(f"Failed to export Mistake ML data: {e}")
            # -----------------------------------

            new_v_path = v_path
            if v_path and os.path.exists(v_path):
                old_p = Path(v_path)
                parts = old_p.stem.split('__')
                if len(parts) >= 3:
                    clean_new_sp = "".join(x for x in new_name if x.isalnum() or x in " _-")
                    new_stem = f"{parts[0]}__{clean_new_sp}__{parts[2]}"
                    new_p = old_p.with_name(f"{new_stem}{old_p.suffix}")
                    try:
                        old_p.rename(new_p)
                        new_v_path = str(new_p)
                    except: pass
                else:
                    clean_new_sp = "".join(x for x in new_name if x.isalnum() or x in " _-")
                    new_p = old_p.with_name(f"{clean_new_sp}_{old_p.name}")
                    try:
                        old_p.rename(new_p)
                        new_v_path = str(new_p)
                    except: pass

            try:
                with db_connector.get_db_connection(force_local=True) as con:
                    con.execute("UPDATE detections SET species = ?, vision_path = ? WHERE id = ?", (new_name, new_v_path, did))
                success_count += 1
            except Exception as e:
                logging.error(f"DB Update failed for ID {did}: {e}")
                continue

            generic_terms =["unknown", "animal", "bird", "mammal", "reptile", "quadruped", "large"]
            is_generic = any(t in str(old_name).lower() for t in generic_terms)
            
            if not is_generic and str(old_name).lower() != new_name.lower():
                if str(old_name).lower() not in targets["registry"][new_name]["synonyms"]:
                    targets["registry"][new_name]["synonyms"].append(str(old_name).lower())
            
            if url not in targets["assignments"]: targets["assignments"][url] =[]
            targets["assignments"][url] = [t for t in targets["assignments"][url] if t.get("name") != old_name]
            
            if not any(t.get("name") == new_name for t in targets["assignments"][url]):
                targets["assignments"][url].append({"name": new_name, "rule": "Enforce Target"})

            if do_cloud_sync and is_alert:
                try:
                    payload = {'secret_token': bot_token, 'timestamp': str(det_timestamp), 'channel_url': url, 'new_species': new_name}
                    res = requests.post("https://wilddetection.com/api/curate_detection", data=payload, timeout=10, verify=True)
                    if res.status_code != 200: cloud_fails += 1
                except: cloud_fails += 1

        VISION_TARGETS_FILE.write_text(json.dumps(targets, indent=2), encoding='utf-8')
        
        self.btn_teach.setText("⏳ Cleaning orphaned data...")
        QApplication.processEvents()
        self._check_and_purge_orphaned_species(old_species_set)
        
        self.btn_teach.setText("✨ Rename & Teach AI")
        self.edit_correct_name.clear()
        
        self.load_database()
        
        msg = f"Successfully updated {success_count} records.\nAI Registry & Watchlists updated.\nMistakes safely exported to ML dataset."
        if do_cloud_sync: msg += f"\nCloud Sync Fails: {cloud_fails}"
        QMessageBox.information(self, "Batch Complete", msg)

    def play_audio_by_id(self, did):
        p_mp3 = BASELINE_CLIPS_DIR / f"detection_{did}.mp3"
        p_wav = BASELINE_CLIPS_DIR / f"detection_{did}.wav"
        
        target = p_mp3 if p_mp3.exists() else p_wav
        if not target.exists(): return

        if self.player:
            self.player.setSource(QUrl.fromLocalFile(str(target)))
            self.player.play()
            self.btn_play.setText("⏸ Playing...")
            self.btn_play.setStyleSheet("background-color: #FF9800; font-size: 16px; padding: 15px;")
        else:
            try: os.startfile(str(target))
            except Exception as e: QMessageBox.warning(self, "Audio Error", f"Could not play: {e}")

    def play_audio(self):
        selected_items = self.table.selectedItems()
        if not selected_items: return
        row = list(set(item.row() for item in selected_items))[0]
        data = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        self.play_audio_by_id(data['id'])

    def curate_detection(self, status):
        selected_items = self.table.selectedItems()
        if not selected_items: return
        
        unique_rows = sorted(list(set(item.row() for item in selected_items)), reverse=True)
        targets_data =[self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in unique_rows]
        total = len(targets_data)
        
        action_name = "Confirm" if status == 'confirmed' else "Reject"
        unique_combos = list(set((d['channel_url'], str(d.get('species') or 'Unknown')) for d in targets_data))
        
        context_dict = {
            'is_batch': total > 1,
            'count': total,
            'unique_combos': unique_combos,
            'data': targets_data[0],
            'all_selected_data': targets_data,
            'stream_name': self.get_stream_name(targets_data[0]['channel_url']),
            'pixmap': self.current_pixmap
        }
        
        dialog = SmartTuningDialog(action_name, context_dict, self)
        if not dialog.exec():
            return 
            
        fs, ds, ff, df, prompt_rule, is_night_mode = dialog.get_tuning()
        group_correction = dialog.get_group_correction()
        go_next = getattr(dialog, 'go_next', False)
        next_row_to_select = unique_rows[-1]
        
        extra_batch = dialog.get_extra_batch()
        if extra_batch:
            targets_data.extend(extra_batch)
            total = len(targets_data)

        cfg = {}
        if CONFIG_FILE.exists():
            try: cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            except: pass
        bot_token = cfg.get("bot_token")
        do_cloud_sync = cfg.get('database_cloud', {}).get('enabled') and bot_token

        old_species_set = set()
        success_count = 0
        cloud_fails = 0

        for i, d in enumerate(targets_data):
            btn_ref = self.btn_confirm if status == 'confirmed' else self.btn_reject
            btn_ref.setText(f"⏳ Processing {i+1} of {total}...")
            QApplication.processEvents()
            
            old_sp_name = str(d.get('species') or 'Unknown')
            old_species_set.add(old_sp_name)
            
            alert_val = d.get('alert_sent')
            is_alert = (alert_val == 1 or str(alert_val).lower() == 'true')

            # --- STRICT NONETYPE SANITIZATION ---
            raw_dist = str(d.get('distance_category') or 'Unknown')
            is_flock = "(Flock/Herd)" in raw_dist

            # Process AI Correction and Nested Path Generation
            if status == 'confirmed':
                if group_correction == "Single":
                    is_flock = False
                    raw_dist = raw_dist.replace("(Flock/Herd)", "").strip()
                elif group_correction == "Flock":
                    if not is_flock:
                        is_flock = True
                        raw_dist = raw_dist.strip() + " (Flock/Herd)"
                        
                folder_group = "flock" if is_flock else "single"
                clean_sp = old_sp_name.lower().replace(" ", "_").replace("/", "")
                export_folder = ML_EXPORT_DIR / "pos" / clean_sp / folder_group
            else:
                clean_sp = old_sp_name.lower().replace(" ", "_").replace("/", "")
                export_folder = ML_EXPORT_DIR / "neg" / clean_sp / "hallucination"
                
            export_folder.mkdir(parents=True, exist_ok=True)

            # Export Files
            dt = datetime.fromtimestamp(d['timestamp'])
            ts_str = dt.strftime('%Y%m%d_%H%M%S')
            clean_st = "".join(x for x in self.get_stream_name(d['channel_url']) if x.isalnum())[:15]
            
            snr_val = d.get('snr') or 0.0
            snr_str = f"{snr_val:.2f}"
            base_name = f"{ts_str}__{clean_sp}__{snr_str}__{clean_st}"

            p_mp3 = BASELINE_CLIPS_DIR / f"detection_{d['id']}.mp3"
            p_wav = BASELINE_CLIPS_DIR / f"detection_{d['id']}.wav"
            if p_mp3.exists(): shutil.copy2(p_mp3, export_folder / f"{base_name}.mp3")
            elif p_wav.exists(): shutil.copy2(p_wav, export_folder / f"{base_name}.wav")

            v_path = d.get('vision_path')
            if v_path and os.path.exists(v_path):
                shutil.copy2(v_path, export_folder / f"{base_name}.jpg")

            meta = {
                "timestamp": ts_str, "stream": self.get_stream_name(d['channel_url']),
                "url": d['channel_url'], "classification": {"score": snr_val, "primary_guess": old_sp_name, "method": str(d.get('detection_method') or 'vision')},
                "verified": status == 'confirmed', "user_label": export_folder.name
            }
            (export_folder / f"{base_name}.json").write_text(json.dumps(meta, indent=2))

            # Database Action
            try:
                with db_connector.get_db_connection(force_local=True) as con:
                    if status == 'rejected':
                        con.execute("DELETE FROM detections WHERE id = ?", (d['id'],))
                        if do_cloud_sync and is_alert:
                            try:
                                payload = {'secret_token': bot_token, 'timestamp': str(d['timestamp']), 'channel_url': d['channel_url']}
                                res = requests.post("https://wilddetection.com/api/delete_detection", data=payload, timeout=10, verify=True)
                                if res.status_code != 200: cloud_fails += 1
                            except: cloud_fails += 1
                    else:
                        con.execute("UPDATE detections SET human_verified = ?, distance_category = ? WHERE id = ?", (status, raw_dist, d['id']))
                success_count += 1
            except Exception as e:
                logging.error(f"Database error on batch export: {e}")
                
            if i < len(unique_rows):
                self.table.removeRow(unique_rows[i])

        self.btn_confirm.setText("✅ CONFIRM (AI was Correct)")
        self.btn_reject.setText("❌ REJECT (AI Hallucinated)")
        
        if status == 'rejected':
            self._check_and_purge_orphaned_species(old_species_set)
            
        has_rules = any(x != "Don't change (Use Global Defaults)" for x in[fs, ds, ff, df]) or prompt_rule
        
        if has_rules:
            try: targets = json.loads(VISION_TARGETS_FILE.read_text(encoding='utf-8'))
            except: targets = {"global_defaults": {}, "registry": {}, "assignments": {}, "stream_overrides": {}}
                
            if "stream_overrides" not in targets: targets["stream_overrides"] = {}
            
            for url, sp in unique_combos:
                stream_name_for_tune = self.get_stream_name(url)
                urls_to_tune =[u for u, n in self.stream_name_map.items() if n == stream_name_for_tune]
                if not urls_to_tune:
                    urls_to_tune = [url]
                    
                for u in urls_to_tune:
                    if u not in targets["stream_overrides"]: targets["stream_overrides"][u] = {}
                    
                    # --- THE NIGHT MATRIX ROUTING SPLIT ---
                    target_dict_name = "night_species_rules" if is_night_mode else "species_rules"
                    
                    if target_dict_name not in targets["stream_overrides"][u]: 
                        targets["stream_overrides"][u][target_dict_name] = {}
                    if sp not in targets["stream_overrides"][u][target_dict_name]: 
                        targets["stream_overrides"][u][target_dict_name][sp] = {}
                    
                    sp_rules = targets["stream_overrides"][u][target_dict_name][sp]
                    
                    if fs != "Don't change (Use Global Defaults)": sp_rules["min_frame_single"] = fs
                    if ds != "Don't change (Use Global Defaults)": sp_rules["min_depth_single"] = ds
                    if ff != "Don't change (Use Global Defaults)": sp_rules["min_frame_flock"] = ff
                    if df != "Don't change (Use Global Defaults)": sp_rules["min_depth_flock"] = df
                    
                    if prompt_rule:
                        sp_rules["prompt"] = prompt_rule
                    
            VISION_TARGETS_FILE.write_text(json.dumps(targets, indent=2), encoding='utf-8')
        
        self.load_database()
        
        msg = f"Exported {success_count} records to nested ML directory."
        if cloud_fails > 0: msg += f"\nCloud Sync Fails: {cloud_fails}"
        if has_rules: 
            rule_type = "NIGHT/LOW-VIS RULES" if is_night_mode else "DAYTIME RULES"
            msg += f"\n\n{rule_type} applied to {len(unique_combos)} specific Stream/Species pairs."
        
        if go_next:
            if self.table.rowCount() > 0:
                target_row = min(next_row_to_select, self.table.rowCount() - 1)
                self.table.selectRow(target_row)
                item = self.table.item(target_row, 0)
                if item:
                    self.table.scrollToItem(item)
        else:
            if total > 1:
                msg += "\n\nDo you want to open the destination folder to view the ML dataset?"
                if QMessageBox.question(self, "Batch Export Complete", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                    if sys.platform == "win32":
                        os.startfile(str(export_folder))
                    elif sys.platform == "darwin":
                        import subprocess
                        subprocess.call(["open", str(export_folder)])
                    else:
                        import subprocess
                        subprocess.call(["xdg-open", str(export_folder)])
            else:
                QMessageBox.information(self, "Export Complete", msg)

    def save_settings(self):
        try:
            settings = json.loads(SETTINGS_FILE.read_text(encoding='utf-8')) if SETTINGS_FILE.exists() else {}
            geom = self.geometry()
            s['main_window'] = {'x': geom.x(), 'y': geom.y(), 'width': geom.width(), 'height': geom.height()}
            s['splitter_sizes'] = self.splitter.sizes()
            s['table_widths'] =[self.table.columnWidth(i) for i in range(self.table.columnCount())]
            SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding='utf-8')
        except: pass

    def load_settings(self):
        try:
            if SETTINGS_FILE.exists():
                s = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
                if 'main_window' in s:
                    g = s['main_window']
                    self.setGeometry(g['x'], g['y'], g['width'], g['height'])
                if 'splitter_sizes' in s: self.splitter.setSizes(s['splitter_sizes'])
                if 'table_widths' in s:
                    for i, w in enumerate(s['table_widths']): self.table.setColumnWidth(i, w)
        except: pass

    def closeEvent(self, event):
        self.save_settings()
        super().closeEvent(event)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = CurationStudio()
    window.show()
    sys.exit(app.exec())