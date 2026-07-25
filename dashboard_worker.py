# FILE: dashboard_worker.py
# VERSION: 1.3 - "The Verified Cycles Patch"
# RESPONSIBILITY: Offloads heavy SQLite queries and data aggregation to a background thread.
# UPDATED: Added queries for Verified Audio/Vision Cycles, and dynamic SQL injection for Vision/Multimodal dashboard filters.

import json
import logging
import time
import sqlite3
from collections import Counter, defaultdict
from PyQt6.QtCore import QThread, pyqtSignal

# --- IMPORT MODULES ---
from dashboard_utils import get_db_connection, ROOT, CONFIG_FILE

TARGETS_FILE = ROOT / "bioacoustic_targets.json"

# --- MASTER NOISE EXCLUSION LIST ---
BIRDNET_NOISE_CLASSES =[
    "Siren", "Dog", "Motor vehicle (road)", "Car alarm", "Human voice", 
    "Human narrator", "Human whistling", "Human vocal", "Human footstep", 
    "Engine", "Wind", "Rain", "Gunshot, gunfire", "Fireworks", "Noise", "Car"
]

def get_target_filter_sql(mode, col_name="species"):
    """
    Constructs the dynamic WHERE clause for Noise Exclusion and Master Data Toggles.
    Returns: (sql_string, params_list)
    """
    sql_parts =[f"{col_name} NOT IN ({','.join(['?']*len(BIRDNET_NOISE_CLASSES))})"]
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
                for val in raw.values():
                    if isinstance(val, list):
                        for v in val:
                            bio_targets.add(v['display'].strip() if isinstance(v, dict) else v.strip())
                    elif isinstance(val, str):
                        bio_targets.add(val.strip())
        except Exception as e: 
            logging.error(f"Error parsing targets file for UI filter: {e}")
        
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

class DataFetchWorker(QThread):
    data_ready = pyqtSignal(dict)
    
    def __init__(self, cycle_agg_mode, timeframe, url_map, target_filter_mode):
        super().__init__()
        self.cycle_aggregation_mode = cycle_agg_mode
        self.current_health_timeframe = timeframe
        self.url_to_name_map = url_map
        self.target_filter_mode = target_filter_mode
        
    def _calculate_estimated_cycle_time(self):
        try:
            if not CONFIG_FILE.exists(): return 1800
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return int(cfg.get("interval_seconds", 300))
        except: return 1800

    def run(self):
        payload = {}
        con = None
        try:
            con = get_db_connection()
            cur = con.cursor()
            
            cond_sql, cond_params = get_target_filter_sql(self.target_filter_mode, "species")
            
            # --- THE NEW VISION/MULTIMODAL FILTER INJECTION ---
            method_sql = ""
            t1_method_sql = ""
            
            if "Visual AI" in self.target_filter_mode:
                method_sql = " AND detection_method = 'vision'"
                t1_method_sql = " AND t1.detection_method = 'vision'"
            elif "Multimodal" in self.target_filter_mode:
                method_sql = " AND detection_method = 'multimodal'"
                t1_method_sql = " AND t1.detection_method = 'multimodal'"
            
            # 1. Total Counts (Filtered)
            cur.execute(f"SELECT COUNT(*) FROM detections WHERE {cond_sql} {method_sql}", cond_params)
            payload['total_detections'] = cur.fetchone()[0]
            
            cur.execute(f"SELECT COUNT(*) FROM detections WHERE alert_sent = 1 AND {cond_sql} {method_sql}", cond_params)
            payload['total_alerts'] = cur.fetchone()[0]
            
            cur.execute(f"SELECT COUNT(DISTINCT species) FROM detections WHERE {cond_sql} {method_sql}", cond_params)
            payload['unique_species'] = cur.fetchone()[0]
            
            cur.execute(f"SELECT COUNT(DISTINCT channel_url) FROM detections WHERE timestamp > ? AND {cond_sql} {method_sql}",[time.time() - 86400] + cond_params)
            payload['active_24h'] = cur.fetchone()[0] or 0

            # 2. System Pulse (Filtered)
            pulse_cutoff = time.time() - 1800 
            cur.execute(f"SELECT COUNT(*) FROM detections WHERE alert_sent = 1 AND timestamp > ? AND {cond_sql} {method_sql}",[pulse_cutoff] + cond_params)
            payload['alerts_30m'] = cur.fetchone()[0]

            # 3. Top Lists (Filtered)
            cur.execute(f"SELECT species, COUNT(*) as c FROM detections WHERE {cond_sql} {method_sql} GROUP BY species ORDER BY c DESC LIMIT 200", cond_params)
            payload['species_counts'] = cur.fetchall()
            
            cur.execute(f"SELECT channel_url, COUNT(*) as c FROM detections WHERE {cond_sql} {method_sql} GROUP BY channel_url", cond_params)
            payload['channel_counts_raw'] = cur.fetchall()

            # 4. Breakdowns (Filtered)
            cur.execute(f"SELECT species, channel_url, COUNT(*) FROM detections WHERE {cond_sql} {method_sql} GROUP BY species, channel_url", cond_params)
            payload['breakdown_rows'] = cur.fetchall()
            
            # 4.5. Last 25 Detections (Chronological) with V40.0 Failsafe
            recent_25_map = defaultdict(list)
            try:
                recent_25_query = f"""
                    SELECT channel_url, species, timestamp, detection_method, distance_category, frame_size, filter_reason
                    FROM (
                        SELECT channel_url, species, timestamp, detection_method, distance_category, frame_size, filter_reason,
                               ROW_NUMBER() OVER(PARTITION BY channel_url ORDER BY timestamp DESC) as rn
                        FROM detections
                        WHERE {cond_sql} {method_sql}
                    )
                    WHERE rn <= 25
                """
                cur.execute(recent_25_query, cond_params)
                recent_25_raw = cur.fetchall()
                for row in recent_25_raw:
                    c_name = self.url_to_name_map.get(row[0], row[0])
                    recent_25_map[c_name].append((row[1], row[2], row[3], row[4], row[5], row[6]))
            except sqlite3.OperationalError:
                # Fallback for old schemas
                recent_25_query = f"""
                    SELECT channel_url, species, timestamp, detection_method, distance_category
                    FROM (
                        SELECT channel_url, species, timestamp, detection_method, distance_category,
                               ROW_NUMBER() OVER(PARTITION BY channel_url ORDER BY timestamp DESC) as rn
                        FROM detections
                        WHERE {cond_sql} {method_sql}
                    )
                    WHERE rn <= 25
                """
                cur.execute(recent_25_query, cond_params)
                recent_25_raw = cur.fetchall()
                for row in recent_25_raw:
                    c_name = self.url_to_name_map.get(row[0], row[0])
                    recent_25_map[c_name].append((row[1], row[2], row[3], row[4], 'N/A', ''))
            
            payload['recent_25_map'] = recent_25_map

            # 5. Distance Data (Filtered)
            cur.execute(f"SELECT channel_url, distance_category, COUNT(*) FROM detections WHERE distance_category IS NOT NULL AND {cond_sql} {method_sql} GROUP BY channel_url, distance_category", cond_params)
            payload['dist_rows'] = cur.fetchall()
            
            cur.execute(f"SELECT channel_url, distance_category, species, COUNT(*) FROM detections WHERE distance_category IS NOT NULL AND {cond_sql} {method_sql} GROUP BY channel_url, distance_category, species", cond_params)
            payload['dist_spec_rows'] = cur.fetchall()

            # 6. Health Data Prep
            estimated_cycle_sec = self._calculate_estimated_cycle_time()
            timeframe_map = {
                "Last 30 Minutes": 1800, "Last 1 Hour": 3600, "Last 3 Hours": 10800,
                "Last 6 Hours": 21600, "Last 12 Hours": 43200, "Last 24 Hours": 86400, 
                "Last 7 Days": 604800, "Last 30 Days": 2592000 
            }
            
            is_cycle_view = self.current_health_timeframe.startswith("Last Full Cycle")
            is_3_cycle_view = self.current_health_timeframe.startswith("Last 3 Full Cycles")
            is_all_time_view = self.current_health_timeframe == "All-Time"
            
            if is_cycle_view: view_cutoff = time.time() - estimated_cycle_sec
            elif is_3_cycle_view: view_cutoff = time.time() - (estimated_cycle_sec * 3)
            else: view_cutoff = 0 if is_all_time_view else time.time() - timeframe_map.get(self.current_health_timeframe, 0)
            
            status_cutoff = min(view_cutoff, time.time() - 1800) if (is_cycle_view or is_3_cycle_view) else view_cutoff
            
            payload['view_cutoff'] = view_cutoff

            cur.execute("SELECT stream_url, average_noise_dbfs FROM stream_noise_profiles")
            payload['stream_noise_profiles_map'] = dict(cur.fetchall())
            
            # Channel Detections in Timeframe
            query = f"SELECT channel_url, COUNT(*) FROM detections WHERE {cond_sql} {method_sql}"
            params = list(cond_params)
            if view_cutoff > 0: 
                query += " AND timestamp > ?"
                params.append(view_cutoff)
            query += " GROUP BY channel_url"
            cur.execute(query, params)
            payload['detections_in_timeframe_map'] = dict(cur.fetchall())

            join_cond_sql = cond_sql.replace('species', 't1.species')
            
            query_last_det = f"""
                SELECT t1.channel_url, t1.species, t1.timestamp 
                FROM detections t1 
                JOIN (SELECT channel_url, MAX(timestamp) AS max_ts FROM detections WHERE {cond_sql} {method_sql} GROUP BY channel_url) t2 
                ON t1.channel_url = t2.channel_url AND t1.timestamp = t2.max_ts
                WHERE {join_cond_sql} {t1_method_sql}
            """
            cur.execute(query_last_det, cond_params + cond_params)
            payload['last_detected_map'] = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
            
            query_last_alarm = f"""
                SELECT t1.channel_url, t1.species, t1.timestamp 
                FROM detections t1 
                JOIN (SELECT channel_url, MAX(timestamp) AS max_ts FROM detections WHERE alert_sent = 1 AND {cond_sql} {method_sql} GROUP BY channel_url) t2 
                ON t1.channel_url = t2.channel_url AND t1.timestamp = t2.max_ts
                WHERE {join_cond_sql} {t1_method_sql}
            """
            cur.execute(query_last_alarm, cond_params + cond_params)
            payload['last_alarmed_map'] = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

            cur.execute("SELECT url, status_note FROM stream_queue WHERE status_note IS NOT NULL")
            payload['queue_status_map'] = dict(cur.fetchall())

            # --- THE NEW VERIFIED CYCLES & LAST SEEN LOGIC ---
            
            # Fetch Verified Cycles (Audio)
            q_audio_cycles = "SELECT stream_url, COUNT(*) FROM stream_health_events WHERE status = 'SUCCESS'"
            p_audio_cycles =[]
            if view_cutoff > 0:
                q_audio_cycles += " AND timestamp > ?"
                p_audio_cycles.append(view_cutoff)
            q_audio_cycles += " GROUP BY stream_url"
            cur.execute(q_audio_cycles, p_audio_cycles)
            payload['audio_cycles_map'] = dict(cur.fetchall())

            # Fetch Verified Cycles (Vision)
            q_vision_cycles = "SELECT stream_url, COUNT(*) FROM stream_health_events WHERE status IN ('VISION_SUCCESS', 'VISION_NO_MOTION', 'VISION_DARK')"
            p_vision_cycles =[]
            if view_cutoff > 0:
                q_vision_cycles += " AND timestamp > ?"
                p_vision_cycles.append(view_cutoff)
            q_vision_cycles += " GROUP BY stream_url"
            cur.execute(q_vision_cycles, p_vision_cycles)
            payload['vision_cycles_map'] = dict(cur.fetchall())
            
            # Fetch the absolute LAST VERIFIED timestamps for the Connectivity Audit button
            cur.execute("SELECT stream_url, MAX(timestamp) FROM stream_health_events WHERE status = 'SUCCESS' GROUP BY stream_url")
            payload['last_audio_verify_map'] = dict(cur.fetchall())
            
            cur.execute("SELECT stream_url, MAX(timestamp) FROM stream_health_events WHERE status IN ('VISION_SUCCESS', 'VISION_NO_MOTION', 'VISION_DARK') GROUP BY stream_url")
            payload['last_vision_verify_map'] = dict(cur.fetchall())
            
            # ------------------------------------------------

            if self.cycle_aggregation_mode == 0:
                query, params = "SELECT stream_url, COUNT(*) FROM stream_health_events WHERE status = 'FAILURE'",[]
                if status_cutoff > 0: query += " AND timestamp > ?"; params.append(status_cutoff)
                query += " GROUP BY stream_url"; cur.execute(query, params)
                payload['recent_failures_count'] = dict(cur.fetchall())
            else:
                query, params = "SELECT stream_url, status, timestamp FROM stream_health_events",[]
                if status_cutoff > 0: query += " WHERE timestamp > ?"; params.append(status_cutoff)
                cur.execute(query, params)
                latest_status_per_stream = {}
                for url, status, ts in cur.fetchall():
                    if url not in latest_status_per_stream or ts > latest_status_per_stream[url][1]: latest_status_per_stream[url] = (status, ts)
                rc = {}
                for url, (status, ts) in latest_status_per_stream.items():
                    rc[url] = 1 if status == 'FAILURE' else 0
                payload['recent_failures_count'] = rc

            cond_sql_p, cond_params_p = get_target_filter_sql(self.target_filter_mode, "p.species_name")
            # Count total profiles for display
            cur.execute(f"SELECT COUNT(*) FROM species_stream_profiles p WHERE {cond_sql_p}", cond_params_p)
            payload['total_profiles_count'] = cur.fetchone()[0]
            # Fetch only the 500 most recently updated profiles to prevent UI freeze
            # with large databases (400+ streams x many species = thousands of rows)
            cur.execute(f"SELECT p.stream_url, p.species_name, p.max_snr_observed, p.sample_count, p.last_updated, p.baseline_detection_id, n.average_noise_dbfs FROM species_stream_profiles p LEFT JOIN stream_noise_profiles n ON p.stream_url = n.stream_url WHERE {cond_sql_p} ORDER BY p.last_updated DESC LIMIT 500", cond_params_p)
            payload['all_profiles'] = cur.fetchall()
            
            # Fetch system log events in the background thread to avoid UI thread DB blocking
            cur.execute("SELECT timestamp, event_type, status, message, details FROM system_events ORDER BY timestamp DESC LIMIT 200")
            payload['system_log_events'] = cur.fetchall()

            con.close()
            self.data_ready.emit(payload)
            
        except Exception as e:
            logging.error(f"Worker Error: {e}", exc_info=True)
            payload['error'] = str(e)
            self.data_ready.emit(payload)
            if con: con.close()