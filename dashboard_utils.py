# FILE: dashboard_utils.py
# VERSION: 2.1 - "Local Speed Edition"
# RESPONSIBILITY: Shared utilities for the Dashboard GUI.
# UPDATED: Now strictly enforces local SQLite access for speed and stability.

import logging
from pathlib import Path
from PyQt6.QtWidgets import QTableWidgetItem, QWidget
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPalette, QBrush, QPen, QPainter

# --- IMPORT CONNECTOR ---
import db_connector

# --- CONSTANTS & PATHS ---
ROOT = Path(__file__).resolve().parent
DATABASE_FILE = ROOT / "detections.db"
CONFIG_FILE = ROOT / "birdnet_config.json"
BASELINE_CLIPS_DIR = ROOT / "baseline_clips"
DASHBOARD_SETTINGS_FILE = ROOT / "dashboard_settings.json"
DASHBOARD_STATE_FILE = ROOT / "dashboard_state.json"
SCHEDULER_SCRIPT_NAME = "scheduler.py" 
REFRESH_INTERVAL_MS = 10000

# --- LOGGING SETUP ---
log_file = ROOT / "monitor_debug.txt"
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
                    handlers=[logging.FileHandler(log_file, mode='a'), logging.StreamHandler()])

# --- DATABASE HELPER ---
def get_db_connection():
    """
    Wraps the central db_connector. 
    Forces local SQLite connection for maximum UI speed and to avoid Postgres locks.
    """
    return db_connector.get_db_connection(force_local=True)

def init_database():
    """
    Initializes tables if they don't exist.
    Uses the connector to work on Local SQLite.
    """
    try:
        # Use the connector's context manager
        with get_db_connection() as con:
            # We use direct execute() calls. 
            # The connector handles the translation of syntax if needed.
            
            con.execute('CREATE TABLE IF NOT EXISTS detections (id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, channel_url TEXT NOT NULL, species TEXT NOT NULL, latitude REAL, longitude REAL, distance_category TEXT, snr REAL, alert_sent BOOLEAN DEFAULT FALSE)')
            con.execute('CREATE TABLE IF NOT EXISTS species_stream_profiles (id INTEGER PRIMARY KEY, stream_url TEXT NOT NULL, species_name TEXT NOT NULL, max_snr_observed REAL DEFAULT 0, sample_count INTEGER DEFAULT 0, last_updated REAL NOT NULL, baseline_detection_id INTEGER, UNIQUE(stream_url, species_name))')
            con.execute('CREATE TABLE IF NOT EXISTS stream_noise_profiles (stream_url TEXT PRIMARY KEY, sample_count INTEGER DEFAULT 0, average_noise_dbfs REAL DEFAULT -90.0, last_updated REAL NOT NULL)')
            con.execute('CREATE TABLE IF NOT EXISTS stream_health_events (stream_url TEXT NOT NULL, timestamp REAL NOT NULL, status TEXT NOT NULL, message TEXT)')
            con.execute('CREATE TABLE IF NOT EXISTS audio_hashes (hash_text TEXT NOT NULL, stream_url TEXT NOT NULL, first_seen_timestamp REAL NOT NULL, PRIMARY KEY (hash_text, stream_url))')
            con.execute('CREATE TABLE IF NOT EXISTS system_events (id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, event_type TEXT NOT NULL, status TEXT NOT NULL, message TEXT, details TEXT)')
            
            # --- COLUMN CHECKS (Migration Helpers) ---
            # Note: PRAGMA table_info is SQLite specific. 
            try:
                # We check if we are dealing with the SQLite wrapper
                if hasattr(con, 'db_type') and con.db_type == 'sqlite':
                    cur = con.cursor()
                    def add_column_if_not_exists(table_name, column_name, column_def):
                        cur.execute(f"PRAGMA table_info({table_name})")
                        if column_name not in [col[1] for col in cur.fetchall()]: 
                            cur.execute(f'ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}')
                    
                    add_column_if_not_exists('detections', 'alert_sent', 'BOOLEAN DEFAULT FALSE')
                    add_column_if_not_exists('species_stream_profiles', 'baseline_detection_id', 'INTEGER')
                    cur.close()
            except Exception as e:
                logging.warning(f"Skipped column check (likely Cloud DB active): {e}")

        logging.info("Dashboard: Database initialization and verification complete.")
    except Exception as e:
        logging.error(f"FATAL: Dashboard failed to initialize database: {e}", exc_info=True)

# --- CUSTOM TABLE ITEMS (Unchanged) ---
class NumericTableWidgetItem(QTableWidgetItem):
    def __lt__(self, other):
        try:
            val_self = float(self.text()) if self.text() != 'N/A' else -1
            val_other = float(other.text()) if other.text() != 'N/A' else -1
            return val_self < val_other
        except (ValueError, TypeError):
            return super().__lt__(other)

class StatusTableWidgetItem(QTableWidgetItem):
    def __lt__(self, other):
        return self.data(Qt.ItemDataRole.UserRole) < other.data(Qt.ItemDataRole.UserRole)

class DateTimeTableWidgetItem(QTableWidgetItem):
    def __lt__(self, other):
        return self.data(Qt.ItemDataRole.UserRole) < other.data(Qt.ItemDataRole.UserRole)

class DistanceDistributionItem(QTableWidgetItem):
    def __init__(self, text, dist_counts):
        super().__init__(text)
        self.dist_order =["Very Near", "Near", "Mid-range", "Far", "Very Far"]
        self.sort_key = tuple(dist_counts.get(cat, 0) for cat in self.dist_order)

    def __lt__(self, other):
        return self.sort_key > other.sort_key

class ProgressPie(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0
        self.setFixedSize(22, 22)

    def setValue(self, value: int):
        self._value = max(0, min(100, value))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        
        pie_color = QColor("#0078d7")
        background_color = self.palette().color(QPalette.ColorRole.Base)
        border_color = self.palette().color(QPalette.ColorRole.Mid)

        painter.setBrush(QBrush(background_color))
        painter.setPen(QPen(border_color, 1))
        painter.drawEllipse(rect)

        if self._value > 0:
            painter.setBrush(QBrush(pie_color))
            painter.setPen(Qt.PenStyle.NoPen)
            span_angle = -int(self._value / 100.0 * 360 * 16)
            start_angle = 90 * 16
            painter.drawPie(rect, start_angle, span_angle)