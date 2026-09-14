# FILE: scheduler.py
# VERSION: 14.3 - "The In-Memory Tag-Along Patch"
# RESPONSIBILITY: Manages Proxies, Launches Workers, Sends Reports, Syncs Config, Cleans Logs, Enforces SIM Data Limits, Auto-Heals Dead Links, and Manages the Financial/Hit-Rate Cruise Control.
# CHANGELOG:
# [2026-09-14 11:15] - v14.3: Injected the URL Tag-Along patch directly into the AutoSyncThread's memory dictionary to prevent race conditions with stream_migrator.
# [2026-09-12 02:45] - v14.2: EconomicCruiseControlThread now dynamically reads 'tuning_interval_mins' from the config to control its sleep cycle.

import sys
import json
import subprocess
import time
import math
import logging
import signal
import sqlite3
import random
import threading
import requests
import re
import os
import shutil
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

# --- IMPORT MODULES ---
import network_manager
import proxy_manager
import db_connector

try:
    import yt_sync_engine
    import stream_migrator
except ImportError:
    yt_sync_engine = None
    stream_migrator = None

# --- Centralized Logging ---
log_file = Path(__file__).resolve().parent / "monitor_debug.txt"
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s -[%(filename)s:%(lineno)d] - %(message)s',
                    handlers=[logging.FileHandler(log_file, mode='a', encoding='utf-8'), logging.StreamHandler(sys.stdout)])

# --- File Paths ---
ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / "birdnet_config.json"
DATABASE_PATH = ROOT / "detections.db"
ENGINE_SCRIPT = ROOT / "stream_to_alert_birdnet.py"
LOG_DIR = ROOT / "app_logs"
PROXY_LOG_FILE = ROOT / "proxy_debug.txt"
BASELINE_CLIPS_DIR = ROOT / "baseline_clips"
HYDRA_STATE_FILE = ROOT / "hydra_heat_state.json"
SYNC_PROPOSALS_FILE = ROOT / "sync_proposals.json"
PROXY_MAP_FILE = ROOT / "proxy_map.json"

# --- Process Management ---
child_processes = set()

def cleanup_fleet():
    """
    Guarantees all child processes are forcefully terminated to prevent 
    zombie processes from eating up RAM and Virtual Memory.
    """
    global child_processes
    if not child_processes:
        return
        
    logging.info(f"Cleaning up {len(child_processes)} orphaned worker processes...")
    for p in list(child_processes):
        try:
            if p.poll() is None:
                if sys.platform == "win32":
                    subprocess.run(f"TASKKILL /F /T /PID {p.pid}", check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    p.terminate()
        except: pass
    child_processes.clear()

def terminate_handler(signum, frame):
    logging.info(f"Termination signal received. Shutting down...")
    cleanup_fleet()
    try:
        proxy_manager.stop_all_proxies()
    except: pass
    sys.exit(0)


# --- ECONOMIC CRUISE CONTROL (BUDGET AUTOTUNER) ---
class EconomicCruiseControlThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True

    def run(self):
        logging.info("EconomicCruiseControlThread started. Will dynamically wake up to tune AI budgets.")
        time.sleep(120) # Startup delay
        
        while True:
            try:
                if not CFG_PATH.exists():
                    time.sleep(60); continue
                    
                cfg = json.loads(CFG_PATH.read_text(encoding='utf-8'))
                eco = cfg.get("economic_control", {})
                
                if not eco.get("enabled", False):
                    time.sleep(600); continue
                    
                # 1. Read Inputs
                target_30m = float(eco.get("target_detections_30m", 15))
                max_budget_day = float(eco.get("max_daily_budget_credits", 5.0))
                cost_per_1000 = float(eco.get("cost_per_1000_images_credits", 0.75))
                
                vision_cfg = cfg.get("vision_ai", {})
                active_vision_streams = len(vision_cfg.get("enabled_streams", []))
                
                if active_vision_streams == 0:
                    time.sleep(3600); continue
                    
                curr_interval = float(vision_cfg.get("cycle_interval_seconds", 60))
                if curr_interval <= 0: curr_interval = 60
                
                # 2. Calculate Hit Rate
                cutoff_12h = time.time() - (12 * 3600)
                with db_connector.get_db_connection(force_local=True) as con:
                    cur = con.cursor()
                    cur.execute("SELECT COUNT(*) FROM detections WHERE timestamp > ? AND detection_method IN ('vision', 'multimodal')", (cutoff_12h,))
                    d_12h = cur.fetchone()[0]
                    
                d_30m_avg = max(d_12h / 24.0, 0.1) # Floor at 0.1 to avoid div zero
                scans_30m = (1800.0 / curr_interval) * active_vision_streams
                hit_rate = d_30m_avg / scans_30m if scans_30m > 0 else 0.001
                if hit_rate <= 0: hit_rate = 0.001
                
                # 3. Calculate Required Scans
                req_scans_30m = target_30m / hit_rate
                
                # 4. Budget Constraint
                cost_per_scan = cost_per_1000 / 1000.0
                max_scans_day = max_budget_day / cost_per_scan if cost_per_scan > 0 else 999999
                max_scans_30m = max_scans_day / 48.0
                
                reason = "None"
                if req_scans_30m > max_scans_30m:
                    req_scans_30m = max_scans_30m
                    reason = "Budget Cap Active"
                    
                # 5. SIM Data Limitation & Assignment
                # Vision ~ 8MB/scan = 0.0078 GB. Audio ~ 1MB/min = 0.06 GB/hr.
                total_audio_streams = len([s for s in cfg.get('streams', []) if s.get('enabled', True) and not s.get('mute_audio', False)])
                audio_gb_hr = total_audio_streams * 0.06
                vision_gb_hr = (req_scans_30m * 2) * 0.0078
                total_gb_hr_needed = (audio_gb_hr + vision_gb_hr) * 1.2 # 20% headroom
                
                network_map = cfg.get("network_map", {})
                sims = list(set(network_map.values()) - {"Default / OS"})
                num_sims = len(sims)
                
                if num_sims > 0:
                    per_sim_limit = round(total_gb_hr_needed / num_sims, 2)
                    with db_connector.get_db_connection(force_local=True) as con:
                        for sim in sims:
                            # Upsert max_gb_per_hour without breaking existing monthly limits
                            con.execute("""
                                INSERT INTO network_quotas (interface_name, max_gb_per_hour) 
                                VALUES (?, ?) 
                                ON CONFLICT(interface_name) 
                                DO UPDATE SET max_gb_per_hour = excluded.max_gb_per_hour
                            """, (sim, per_sim_limit))
                
                # 6. Apply New Interval
                if req_scans_30m > 0:
                    new_interval = 1800.0 / (req_scans_30m / active_vision_streams)
                else:
                    new_interval = 3600
                    
                # Clamp safely between 20s and 3600s
                new_interval = max(20, min(3600, int(new_interval)))
                
                # 7. Write Back to Config
                cfg_update = json.loads(CFG_PATH.read_text(encoding='utf-8'))
                cfg_update.setdefault("vision_ai", {})["cycle_interval_seconds"] = new_interval
                eco_update = cfg_update.setdefault("economic_control", {})
                eco_update["last_calculated_hit_rate"] = hit_rate
                eco_update["last_calculated_cycle_s"] = new_interval
                eco_update["last_throttle_reason"] = reason
                
                tmp_path = CFG_PATH.with_suffix('.tmp')
                tmp_path.write_text(json.dumps(cfg_update, indent=2), encoding='utf-8')
                os.replace(tmp_path, CFG_PATH)
                
                logging.info(f"[ECO CRUISE] Tuned! Hit Rate: {hit_rate*100:.2f}% | Target Scans/30m: {req_scans_30m:.0f} | New Interval: {new_interval}s | Reason: {reason}")
                
                # THE FREQUENCY DIAL PATCH: Read user's interval preference
                tuning_interval_mins = float(eco.get("tuning_interval_mins", 30))
                sleep_secs = max(60, int(tuning_interval_mins * 60))
                
            except Exception as e:
                logging.error(f"[ECO CRUISE] Error in background tuning: {e}")
                sleep_secs = 1800 # Fallback 30 mins on error
                
            time.sleep(sleep_secs)


# --- AUTO-SYNC THREAD (DRIP-FEED CHANNEL SCANNER) ---
class AutoSyncThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True
        
    def run(self):
        logging.info("AutoSyncThread started. Will run background channel syncs (Drip-Feed mode).")
        time.sleep(120)  # Initial startup delay so it doesn't block immediate listening
        
        while True:
            try:
                if not CFG_PATH.exists() or not yt_sync_engine:
                    time.sleep(60)
                    continue
                    
                cfg = json.loads(CFG_PATH.read_text(encoding='utf-8'))
                sync_settings = cfg.get("sync_engine_settings", {})
                interval_hours = float(sync_settings.get("scan_interval_hours", 24.0))
                auto_heal = sync_settings.get("auto_heal_enabled", False)
                
                channels_dict = cfg.get("channels", {})
                channel_urls = list(set(channels_dict.values()))
                
                if not channel_urls:
                    time.sleep(3600)
                    continue
                    
                logging.info(f"Starting Drip-Feed Sync across {len(channel_urls)} channels...")
                all_proposals =[]
                
                for c_url in channel_urls:
                    try:
                        engine = yt_sync_engine.YouTubeSyncEngine()
                        engine.run_sync(target_channels=[c_url])
                        all_proposals.extend(engine.proposals)
                    except Exception as e:
                        logging.error(f"Error during drip-feed sync for {c_url}: {e}")
                        
                    sleep_time = random.uniform(15.0, 30.0)
                    time.sleep(sleep_time)
                    
                manual_proposals =[]
                healed_count = 0
                
                cfg_to_update = json.loads(CFG_PATH.read_text(encoding='utf-8'))
                config_updated = False
                
                for p in all_proposals:
                    if auto_heal and p.get("auto_heal_eligible"):
                        old_url = p.get('old_url')
                        old_name = p.get('friendly_name')
                        
                        target_stream = None
                        for s in cfg_to_update.get('streams',[]):
                            clean_url = re.sub(r'[\?&]variant=\d+', '', s.get('page_url', ''))
                            if clean_url == old_url or s.get('name') == old_name:
                                target_stream = s
                                break
                                
                        if target_stream:
                            new_url = p['new_url']
                            target_stream['page_url'] = new_url
                            target_stream['updated_at'] = time.time()
                            target_stream.pop('disable_reason', None)
                            target_stream.pop('status_reason', None)
                            
                            # --- THE UNKNOWN CHANNEL PRESERVATION PATCH ---
                            new_cname = str(p.get('new_channel_name', '')).strip()
                            old_cname = str(target_stream.get('channel_name', '')).strip()
                            bad_names = ["Unknown Channel", "Unknown", ""]
                            
                            if new_cname and new_cname not in bad_names:
                                target_stream['channel_name'] = new_cname
                                if 'channels' not in cfg_to_update: cfg_to_update['channels'] = {}
                                if new_cname not in cfg_to_update['channels']:
                                    cfg_to_update['channels'][new_cname] = p.get('new_channel', '')
                            elif old_cname and old_cname not in bad_names:
                                target_stream['channel_name'] = old_cname
                            else:
                                target_stream['channel_name'] = "Unknown Channel"
                            # ----------------------------------------------
                                    
                            # --- THE URL TAG-ALONG PATCH (IN-MEMORY) ---
                            if "vision_ai" in cfg_to_update and "enabled_streams" in cfg_to_update["vision_ai"]:
                                vision_enabled = cfg_to_update["vision_ai"]["enabled_streams"]
                                for i, u in enumerate(vision_enabled):
                                    if u == old_url:
                                        vision_enabled[i] = new_url
                                        logging.info(f"Auto-Healer successfully transferred Vision Checkbox state to new URL: {new_url}")
                                        break
                            # -------------------------------------------
                            
                            config_updated = True
                            if stream_migrator:
                                try:
                                    stream_migrator.migrate_stream_data(old_url, new_url)
                                except Exception as me:
                                    logging.error(f"Auto-Heal DB Migration error: {me}")
                            healed_count += 1
                            logging.info(f"Auto-Healed: {old_name} -> {new_url}")
                    else:
                        manual_proposals.append(p)
                        
                if config_updated:
                    tmp_path = CFG_PATH.with_suffix('.tmp')
                    tmp_path.write_text(json.dumps(cfg_to_update, indent=2), encoding='utf-8')
                    os.replace(tmp_path, CFG_PATH)
                    
                existing_proposals =[]
                if SYNC_PROPOSALS_FILE.exists():
                    try:
                        existing_proposals = json.loads(SYNC_PROPOSALS_FILE.read_text(encoding='utf-8'))
                    except: pass
                    
                filtered_existing =[ep for ep in existing_proposals if ep.get('old_channel') not in channel_urls and ep.get('new_channel') not in channel_urls]
                filtered_existing.extend(manual_proposals)
                
                SYNC_PROPOSALS_FILE.write_text(json.dumps(filtered_existing, indent=2), encoding='utf-8')
                logging.info(f"Auto-Sync Complete. Healed: {healed_count}. Manual proposals queued: {len(manual_proposals)}.")
                
                interval_seconds = max(3600, int(interval_hours * 3600))
                time.sleep(interval_seconds)
                
            except Exception as e:
                logging.error(f"AutoSyncThread Error: {e}", exc_info=True)
                time.sleep(300)


# --- DATA QUOTA TRACKER & EMA SMOOTHING THREAD ---
class HardwareTelemetryThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True
        self.ema_heat_map = {}
        self.two_throttled_start_ts = 0.0
        
    def run(self):
        logging.info("HardwareTelemetryThread started. Tracking SIM data usage and calculating EMA every 60s...")
        time.sleep(5) 
        while True:
            try:
                network_manager.take_hardware_snapshot()
                
                if CFG_PATH.exists():
                    cfg = json.loads(CFG_PATH.read_text(encoding='utf-8'))
                    network_map = cfg.get("network_map", {})
                    expected_interfaces = list(set(network_map.values()) - {"Default / OS"})
                    
                    pid_settings = cfg.get("hydra_pid_settings", {})
                    alpha = float(pid_settings.get("ema_alpha", 0.3))
                    soft_lockout_thresh = float(pid_settings.get("soft_lockout_pct", 90)) / 100.0
                    global_brake_thresh = float(pid_settings.get("global_brake_pct", 95)) / 100.0
                    throttling_enabled = pid_settings.get("throttling_enabled", True)
                    
                    active_ifaces_list = network_manager.get_active_interfaces()
                    active_iface_names = {i['name'] for i in active_ifaces_list}
                    
                    current_state = {}
                    
                    if expected_interfaces:
                        throttled_count = 0
                        hot_count = 0
                        
                        # 1. Calculate Individual Interface Heats FIRST
                        for iface in expected_interfaces:
                            if iface not in active_iface_names:
                                ema_heat = 0.0
                                i_arrow = "❌ OFFLINE (Disconnected)"
                                self.ema_heat_map[iface] = ema_heat
                                current_state[iface] = {"heat": ema_heat, "arrow": i_arrow}
                                continue
                                
                            used_bytes, limit_bytes, is_over = network_manager.get_interface_quota_status(iface)
                            is_monthly_dead = (limit_bytes > 0 and used_bytes >= limit_bytes)
                            is_banned = network_manager.is_ip_banned(iface)
                                
                            raw_heat = network_manager.get_interface_heat(iface)
                            prev_heat = self.ema_heat_map.get(iface, raw_heat)
                            ema_heat = (raw_heat * alpha) + (prev_heat * (1.0 - alpha))
                            
                            # Force heat to 1.0 internally if banned, hard speed limit hit, or quota dead 
                            # so the Load Balancer instantly aligns with the Hard Limiter.
                            if is_banned or is_over or is_monthly_dead:
                                ema_heat = 1.0
                                
                            self.ema_heat_map[iface] = ema_heat
                            
                            i_arrow = "➖ Stable"
                            if is_banned: 
                                i_arrow = "🔥 IP BANNED (Needs Replug)"
                            elif is_monthly_dead: 
                                i_arrow = "⛔ DATA DEPLETED (Monthly Cap)"
                            elif ema_heat >= 1.0: 
                                i_arrow = "🛑 LOCAL SPEED BLOCKED (100% Heat)" if throttling_enabled else "⚠️ MAX SPEED EXCEEDED (Bypassed)"
                            elif ema_heat >= soft_lockout_thresh: 
                                i_arrow = "🟠 THROTTLED MAX (Soft-Lockout)" if throttling_enabled else "⚠️ RUNNING HOT (Bypassed)"
                            elif ema_heat > prev_heat + 0.001: 
                                i_arrow = "🔺 Heating Up"
                            elif ema_heat < prev_heat - 0.001: 
                                i_arrow = "🔽 Cooling Down"
                            
                            current_state[iface] = {"heat": ema_heat, "arrow": i_arrow}
                            
                            if ema_heat >= soft_lockout_thresh: throttled_count += 1
                            elif ema_heat >= (soft_lockout_thresh - 0.10): hot_count += 1

                        # 2. Evaluate Global Coordination Rules (Emergency Brake)
                        engage_brake = False
                        brake_reason = ""
                        
                        if throttled_count >= 2:
                            if self.two_throttled_start_ts == 0.0:
                                self.two_throttled_start_ts = time.time()
                            elapsed_two = time.time() - self.two_throttled_start_ts
                        else:
                            self.two_throttled_start_ts = 0.0
                            elapsed_two = 0.0
                            
                        # Never let 3 routers hit the soft lockout limit.
                        if throttled_count >= 3:
                            engage_brake = True
                            brake_reason = "3+ SIMs Throttled"
                        elif throttled_count == 2 and hot_count >= 1:
                            engage_brake = True
                            brake_reason = "Preventing 3rd SIM Throttle"
                        elif throttled_count == 2 and elapsed_two >= 240: 
                            engage_brake = True
                            brake_reason = "Enforcing 5m Max for 2 SIMs"

                        if not throttling_enabled:
                            engage_brake = False

                        # 3. Calculate Global Heat & Apply Overrides
                        raw_global = network_manager.get_global_network_heat(expected_interfaces)
                        prev_global = self.ema_heat_map.get("GLOBAL", raw_global)
                        ema_global = (raw_global * alpha) + (prev_global * (1.0 - alpha))
                        
                        g_arrow = "➖ Stable"
                        
                        if engage_brake:
                            ema_global = global_brake_thresh - 0.001  
                            g_arrow = f"🚨 GLOBAL EMERGENCY BRAKE ({brake_reason})"
                        else:
                            if not throttling_enabled: g_arrow = "⚠️ THROTTLING DISABLED"
                            elif ema_global >= 1.0: g_arrow = "🛑 GLOBAL SPEED BLOCKED (100% Heat)"
                            elif ema_global >= global_brake_thresh: g_arrow = "🟠 GLOBAL THROTTLED MAX"
                            elif ema_global > prev_global + 0.001: g_arrow = "🔺 Heating Up"
                            elif ema_global < prev_global - 0.001: g_arrow = "🔽 Cooling Down"
                            
                        self.ema_heat_map["GLOBAL"] = ema_global
                        current_state["GLOBAL"] = {"heat": ema_global, "arrow": g_arrow}
                        
                    HYDRA_STATE_FILE.write_text(json.dumps(current_state, indent=2), encoding='utf-8')
                    
            except Exception as e:
                logging.error(f"Telemetry Snapshot/EMA Error: {e}")
            time.sleep(60)

# --- NETWORK MONITOR THREAD (WI-FI & QUOTA DROPS) ---
class NetworkMonitorThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True
        self.missing_alert_timestamps = {}
        self.monthly_alert_timestamps = {}
        self.hourly_alert_timestamps = {}
        self.banned_alert_timestamps = {}
        self.monthly_dropped = set()
        self.hourly_dropped = set()

    def check_specific_quotas(self, interface_name):
        try:
            with db_connector.get_db_connection(force_local=True) as con:
                cur = con.cursor()
                try:
                    cur.execute("SELECT limit_gb, reset_day, max_gb_per_hour FROM network_quotas WHERE interface_name = ?", (interface_name,))
                    row = cur.fetchone()
                except sqlite3.OperationalError:
                    return False, False
                    
                if not row: return False, False
                limit_gb = float(row[0] or 0.0)
                reset_day = int(row[1] or 1)
                max_gb_per_hour = float(row[2] or 0.0)
                
                is_monthly_over = False
                is_hourly_over = False
                
                if limit_gb > 0:
                    limit_bytes = limit_gb * (1024**3)
                    cycle_start = network_manager.get_billing_cycle_start(reset_day)
                    cur.execute("SELECT SUM(bytes_sent + bytes_recv) FROM network_hardware_logs WHERE interface_name = ? AND timestamp >= ?", (interface_name, cycle_start))
                    row_m = cur.fetchone()
                    used_monthly = row_m[0] if row_m and row_m[0] else 0
                    if used_monthly >= limit_bytes:
                        is_monthly_over = True
                        
                if max_gb_per_hour > 0:
                    cutoff_1h = time.time() - 3600
                    cur.execute("SELECT SUM(bytes_sent + bytes_recv) FROM network_hardware_logs WHERE interface_name = ? AND timestamp >= ?", (interface_name, cutoff_1h))
                    row_h = cur.fetchone()
                    used_hourly = row_h[0] if row_h and row_h[0] else 0
                    limit_h_bytes = max_gb_per_hour * (1024**3)
                    if used_hourly >= limit_h_bytes:
                        is_hourly_over = True
                        
                return is_monthly_over, is_hourly_over
        except Exception as e:
            return False, False
            
    def run(self):
        logging.info("NetworkMonitorThread started. Watching for mapped Wi-Fi/SIM drops and Quota Limits...")
        time.sleep(15) 
        
        while True:
            try:
                if not CFG_PATH.exists():
                    time.sleep(10); continue
                    
                cfg = json.loads(CFG_PATH.read_text(encoding='utf-8'))
                network_map = cfg.get("network_map", {})
                expected_interfaces = set(network_map.values()) - {"Default / OS"}
                
                if not expected_interfaces:
                    time.sleep(10); continue
                    
                current_interfaces = network_manager.get_active_interfaces()
                current_names = {iface['name'] for iface in current_interfaces}
                
                report_cfg = cfg.get('periodic_report', {})
                alert_hourly = report_cfg.get('alert_hourly_speed', True)
                alert_monthly = report_cfg.get('alert_monthly_quota', True)
                
                pid_settings = cfg.get("hydra_pid_settings", {})
                throttling_enabled = pid_settings.get("throttling_enabled", True)
                
                for iface in expected_interfaces:
                    # 1. OS-Level Disconnect Check
                    if iface not in current_names:
                        last_alert_time = self.missing_alert_timestamps.get(iface, 0)
                        if time.time() - last_alert_time >= 600:
                            msg = f"❌ <b>CRITICAL NETWORK DROP</b> ❌\nInterface <b>{iface}</b> is physically disconnected from the OS!\nListeners mapped to this interface will fail."
                            self.send_alert(cfg, msg)
                            self.missing_alert_timestamps[iface] = time.time()
                        
                        self.monthly_dropped.discard(iface)
                        self.hourly_dropped.discard(iface)
                        self.monthly_alert_timestamps.pop(iface, None)
                        self.hourly_alert_timestamps.pop(iface, None)
                        self.banned_alert_timestamps.pop(iface, None)
                    else:
                        if iface in self.missing_alert_timestamps:
                            msg = f"✅ <b>NETWORK RECOVERED</b> ✅\nInterface <b>{iface}</b> has reconnected to the OS."
                            self.send_alert(cfg, msg)
                            del self.missing_alert_timestamps[iface]
                            
                        # 2. IP Ban (403) Check
                        is_banned = network_manager.is_ip_banned(iface)
                        if is_banned:
                            last_ban_alert = self.banned_alert_timestamps.get(iface, 0)
                            if time.time() - last_ban_alert >= 900: # Alert every 15 mins
                                msg = f"🔥 <b>IP BANNED (403)</b> 🔥\nInterface <b>{iface}</b> has been blocked by YouTube. Traffic is dynamically rerouting to other SIMs.\n\n<i>Consider physically replugging the dongle to acquire a new IP address!</i>"
                                self.send_alert(cfg, msg)
                                self.banned_alert_timestamps[iface] = time.time()
                        else:
                            if iface in self.banned_alert_timestamps:
                                msg = f"✅ <b>IP BAN LIFTED</b> ✅\nInterface <b>{iface}</b> cooldown expired. Re-integrating into active pool."
                                self.send_alert(cfg, msg)
                                del self.banned_alert_timestamps[iface]

                        # 3. Quota & Speed Limit Checks
                        is_monthly_over, is_hourly_over = self.check_specific_quotas(iface)
                        
                        if is_monthly_over:
                            self.monthly_dropped.add(iface)
                            last_m_alert = self.monthly_alert_timestamps.get(iface, 0)
                            if time.time() - last_m_alert >= 600:
                                if alert_monthly:
                                    msg = f"⛔ <b>MONTHLY DATA DEPLETED</b> ⛔\nInterface <b>{iface}</b> has hit its absolute monthly GB limit. Traffic is blocked until the next billing cycle."
                                    self.send_alert(cfg, msg)
                                self.monthly_alert_timestamps[iface] = time.time()
                        else:
                            if iface in self.monthly_dropped:
                                self.monthly_dropped.discard(iface)
                                self.monthly_alert_timestamps.pop(iface, None)
                                if alert_monthly:
                                    msg = f"🔄 <b>MONTHLY QUOTA RESET</b> 🔄\nInterface <b>{iface}</b> has started a new billing cycle. Re-integrating into active pool."
                                    self.send_alert(cfg, msg)
                                    
                        if is_hourly_over and not is_monthly_over:
                            self.hourly_dropped.add(iface)
                            last_h_alert = self.hourly_alert_timestamps.get(iface, 0)
                            if time.time() - last_h_alert >= 600:
                                if alert_hourly and throttling_enabled:
                                    msg = f"🛑 <b>LOCAL SPEED BLOCKED (100% Heat)</b> 🛑\nInterface <b>{iface}</b> exceeded its safe hourly bandwidth. Temporarily locked out until it cools down."
                                    self.send_alert(cfg, msg)
                                self.hourly_alert_timestamps[iface] = time.time()
                        elif not is_hourly_over:
                            if iface in self.hourly_dropped:
                                self.hourly_dropped.discard(iface)
                                self.hourly_alert_timestamps.pop(iface, None)
                                if alert_hourly and not is_monthly_over and throttling_enabled:
                                    msg = f"✅ <b>LOCAL SPEED LIMIT RECOVERED (Below 100%)</b> ✅\nInterface <b>{iface}</b> has dropped below the hard cap.\n\n<i>Note: It may still be in a 95% Soft-Lockout cooling phase.</i>"
                                    self.send_alert(cfg, msg)
                                    
            except Exception as e:
                pass
            
            time.sleep(10)
            
    def send_alert(self, cfg, msg):
        try:
            bot_token = cfg.get('bot_token')
            chat_id = cfg.get('chat_id')
            if bot_token and chat_id:
                requests.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                    timeout=10
                )
        except Exception as e: 
            logging.error(f"Failed to send network alert: {e}")

# --- Automated Cloud Config Syncer ---
class ConfigSyncThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True
        self.last_mtime = 0
        
    def run(self):
        if CFG_PATH.exists():
            self.last_mtime = CFG_PATH.stat().st_mtime

        while True:
            try:
                if CFG_PATH.exists():
                    current_mtime = CFG_PATH.stat().st_mtime
                    if self.last_mtime != 0 and current_mtime > self.last_mtime:
                        time.sleep(2.0) 
                        self.sync_config()
                        self.last_mtime = CFG_PATH.stat().st_mtime
            except Exception as e:
                pass
            time.sleep(5)

    def sync_config(self):
        try:
            cfg_content = CFG_PATH.read_text(encoding='utf-8')
            cfg = json.loads(cfg_content)
            
            all_streams = cfg.get("streams",[])
            db_connector.init_stream_queue(all_streams)

            bot_token = cfg.get('bot_token', '')
            if not bot_token: return
            
            sync_url = "https://wilddetection.com/api/upload_config"
            files = {'config_file': ('birdnet_config.json', cfg_content.encode('utf-8'), 'application/json')}
            data = {'secret_token': bot_token}
            
            requests.post(sync_url, files=files, data=data, timeout=20, verify=True)
        except Exception as e:
            pass

# --- Periodic Reporter Thread ---
class ReporterThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True
        self.start_time = time.time()
        self.last_report_time = time.time()
        self.startup_report_sent = False
        
    def run(self):
        while True:
            try:
                if not CFG_PATH.exists():
                    time.sleep(60); continue
                    
                cfg = json.loads(CFG_PATH.read_text(encoding='utf-8'))
                
                report_cfg = cfg.get('periodic_report', {})
                enabled = report_cfg.get('enabled', True)
                interval_hours = float(report_cfg.get('interval_hours', 0.5))
                
                if not enabled:
                    time.sleep(300); continue

                now = time.time()
                
                if not self.startup_report_sent:
                    if (now - self.start_time) >= 300: 
                        self.send_report(cfg, 300, is_startup=True)
                        self.startup_report_sent = True
                        self.last_report_time = now 
                else:
                    interval_seconds = interval_hours * 3600
                    time_since_last = now - self.last_report_time
                    
                    if time_since_last >= interval_seconds:
                        self.send_report(cfg, time_since_last, is_startup=False)
                        self.last_report_time = now
                    
                time.sleep(60)
            except Exception as e:
                time.sleep(60)

    def send_report(self, cfg, period_seconds, is_startup=False):
        try:
            bot_token = cfg.get('bot_token')
            chat_id = cfg.get('chat_id')
            if not bot_token or not chat_id: return

            active_streams =[s for s in cfg.get('streams', []) if s.get('enabled', True)]
            active_urls =[s.get('page_url') for s in active_streams if s.get('page_url')]
            active_streams_count = len(active_urls)
            
            with db_connector.get_db_connection(force_local=True) as con:
                cur = con.cursor()
                cutoff = time.time() - period_seconds
                try:
                    cur.execute("SELECT COUNT(*) FROM detections WHERE alert_sent = ? AND timestamp > ?", (True, cutoff))
                except:
                    cur.execute("SELECT COUNT(*) FROM detections WHERE alert_sent = ? AND timestamp > ?", (1, cutoff))
                alerts_sent = cur.fetchone()[0]
                
                status_counts = {"Healthy": 0, "Minor Glitches": 0, "Mic Dead (Vision Active)": 0, "Silent (No A/V)": 0, "Quarantined Loop": 0, "Offline/Dead": 0}
                
                if active_urls:
                    placeholders = ','.join(['?'] * len(active_urls))
                    cur.execute(f"SELECT url, status_note FROM stream_queue WHERE url IN ({placeholders})", active_urls)
                    queue_status = {row[0]: row[1] for row in cur.fetchall()}
                    
                    for url in active_urls:
                        note = queue_status.get(url, "SUCCESS")
                        if not note or note == "SUCCESS" or note == "CHECKING": status_counts["Healthy"] += 1
                        elif note == "SILENT_VISUAL": status_counts["Mic Dead (Vision Active)"] += 1
                        elif note == "SILENT": status_counts["Silent (No A/V)"] += 1
                        elif note in["INTERMITTENT", "FAILURE", "HICCUP"]: status_counts["Minor Glitches"] += 1
                        elif note in["UNRESPONSIVE", "SUSPENDED", "FATAL", "TERMINAL"]: status_counts["Offline/Dead"] += 1
                        elif note == "LOOP": status_counts["Quarantined Loop"] += 1
                        else: status_counts["Healthy"] += 1
            
            healthy_total = status_counts["Healthy"] + status_counts["Mic Dead (Vision Active)"]
            health_pct = int((healthy_total / active_streams_count) * 100) if active_streams_count > 0 else 0
            
            title = "🚀 <b>Startup Health Check</b>" if is_startup else "📋 <b>Periodic Status Report</b>"
            time_str = f"{int(period_seconds/60)}m" if period_seconds < 3600 else f"{period_seconds/3600:.1f}h"

            msg = (f"{title}\n"
                   f"• <b>Period:</b> Last {time_str}\n"
                   f"• <b>Overall Health:</b> {health_pct}% ({healthy_total}/{active_streams_count} Nominal)\n"
                   f"• <b>Alerts Sent:</b> {alerts_sent}\n\n"
                   f"📊 <b>Detailed Stream States:</b>\n"
                   f"✅ Healthy: {status_counts['Healthy']}\n"
                   f"👁️ Mic Dead (Vision Active): {status_counts['Mic Dead (Vision Active)']}\n"
                   f"⚠️ Minor Glitches: {status_counts['Minor Glitches']}\n"
                   f"🔇 Silent (No A/V): {status_counts['Silent (No A/V)']}\n"
                   f"🔁 Quarantined Loop: {status_counts['Quarantined Loop']}\n"
                   f"💀 Offline/Dead: {status_counts['Offline/Dead']}")

            try:
                if HYDRA_STATE_FILE.exists():
                    heat_state = json.loads(HYDRA_STATE_FILE.read_text(encoding='utf-8'))
                    if heat_state:
                        msg += f"\n\n🔥 <b>Hydra Network Pacing (Heat)</b>\n"
                        g_data = heat_state.get("GLOBAL")
                        if g_data:
                            msg += f"• <b>Main Pipe (Global Throttle):</b> {g_data['heat']*100:.1f}% [{g_data['arrow']}]\n"
                        
                        network_map = cfg.get("network_map", {})
                        expected_interfaces = list(set(network_map.values()) - {"Default / OS"})
                        
                        for iface in expected_interfaces:
                            i_data = heat_state.get(iface)
                            if i_data:
                                msg += f"  - {iface}: {i_data['heat']*100:.1f}%[{i_data['arrow']}]\n"
            except Exception as e:
                pass

            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=20
            )
        except Exception as e:
            pass

# --- HOUSEKEEPING ---
class HousekeepingThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True
        self.last_run_time = 0

    def run(self):
        time.sleep(15) 
        while True:
            try:
                if CFG_PATH.exists():
                    cfg = json.loads(CFG_PATH.read_text(encoding='utf-8'))
                    hk_cfg = cfg.get("housekeeping", {})
                    
                    janitor_interval = float(hk_cfg.get("janitor_interval_hours", 1.0))
                    log_retention = float(hk_cfg.get("legacy_log_retention_hours", 72.0))
                    temp_retention = float(hk_cfg.get("temp_retention_hours", 24.0))
                    auto_wipe_py = hk_cfg.get("auto_wipe_python_caches", True)
                    auto_clips = hk_cfg.get("auto_wipe_orphaned_clips", True)
                    
                    now = time.time()
                    if self.last_run_time == 0 or (now - self.last_run_time) >= (janitor_interval * 3600):
                        self.clean_log_file(log_file, log_retention)
                        self.clean_log_file(PROXY_LOG_FILE, log_retention)
                        if LOG_DIR.exists():
                            for lf in LOG_DIR.glob("*.log"):
                                self.clean_log_file(lf, log_retention)
                        self.clean_temp_directory(max_age_hours=temp_retention)
                        if auto_wipe_py: self.clean_python_caches()
                        if auto_clips: self.clean_orphaned_clips(retention_hours=log_retention)
                        self.last_run_time = time.time()
            except Exception as e:
                pass
            time.sleep(60)
            
    def clean_temp_directory(self, max_age_hours=24):
        try:
            temp_dir = Path(tempfile.gettempdir())
            if not temp_dir.exists(): return
            cutoff_time = time.time() - (max_age_hours * 3600)
            for item in temp_dir.iterdir():
                try:
                    is_target = item.name.startswith(('scoped_dir', 'uc_', 'yt-dlp')) or (item.name.startswith('tmp') and item.name.endswith('.wav'))
                    if is_target and item.stat().st_mtime < cutoff_time:
                        if item.is_dir(): shutil.rmtree(item, ignore_errors=True)
                        else: item.unlink(missing_ok=True)
                except Exception: pass
        except Exception: pass

    def clean_python_caches(self):
        try:
            for p in ROOT.rglob("__pycache__"):
                if p.is_dir():
                    try: shutil.rmtree(p, ignore_errors=True)
                    except: pass
            for p in ROOT.rglob("*.pyc"):
                if p.is_file():
                    try: p.unlink(missing_ok=True)
                    except: pass
            try:
                flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                subprocess.run([sys.executable, "-m", "pip", "cache", "purge"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
            except: pass
        except Exception: pass

    def clean_orphaned_clips(self, retention_hours):
        if not BASELINE_CLIPS_DIR.exists(): return
        try:
            cutoff_time = time.time() - (retention_hours * 3600)
            with db_connector.get_db_connection(force_local=True) as con:
                cur = con.cursor()
                cur.execute("SELECT DISTINCT baseline_detection_id FROM species_stream_profiles WHERE baseline_detection_id IS NOT NULL")
                anchor_ids = {str(r[0]) for r in cur.fetchall()}
            for f in BASELINE_CLIPS_DIR.glob("detection_*.*"):
                if f.suffix in ['.mp3', '.wav']:
                    try:
                        clip_id = f.stem.split('_')[1].split('.')[0]
                        if clip_id not in anchor_ids and f.stat().st_mtime < cutoff_time:
                            f.unlink()
                    except: pass
        except Exception: pass 
            
    def clean_log_file(self, filepath, retention_hours):
        if not filepath.exists(): return
        try:
            cutoff_time = datetime.now() - timedelta(hours=retention_hours)
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f: lines = f.readlines()
            if not lines: return
            kept_lines =[]
            date_pattern = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
            proxy_pattern = re.compile(r'^\[(\d{2}:\d{2}:\d{2})\]') 
            keep_current_block = True
            for line in lines:
                match = date_pattern.match(line)
                proxy_match = proxy_pattern.match(line)
                if match:
                    try: keep_current_block = datetime.strptime(match.group(1), '%Y-%m-%d %H:%M:%S') >= cutoff_time
                    except ValueError: pass
                elif proxy_match:
                    try:
                        t_obj = datetime.strptime(proxy_match.group(1), '%H:%M:%S')
                        now = datetime.now()
                        log_time = now.replace(hour=t_obj.hour, minute=t_obj.minute, second=t_obj.second)
                        if log_time > now + timedelta(minutes=5): log_time -= timedelta(days=1)
                        keep_current_block = log_time >= cutoff_time
                    except Exception: pass
                if keep_current_block: kept_lines.append(line)
            if len(kept_lines) < len(lines):
                tmp_path = filepath.with_suffix('.tmp')
                with open(tmp_path, 'w', encoding='utf-8') as f: f.writelines(kept_lines)
                try: os.replace(tmp_path, filepath)
                except Exception:
                    if tmp_path.exists(): tmp_path.unlink()
        except Exception: pass


# --- Main Scheduler Logic ---

def main(show_windows_flag: str):
    show_windows = show_windows_flag.lower() == 'true'
    logging.info(f"Scheduler started. Windows Visible: {show_windows}")

    try:
        with db_connector.get_db_connection(force_local=True) as con:
            con.execute("DROP TABLE IF EXISTS scheduler_status")
            con.execute('''
                CREATE TABLE scheduler_status (
                    listener_id TEXT PRIMARY KEY,
                    cycle_start_time REAL,
                    total_cycle_seconds REAL,
                    managed_streams_json TEXT,
                    last_updated REAL,
                    cycle_count INTEGER DEFAULT 0
                )
            ''')
    except Exception as e:
        logging.error(f"Failed to reset status table: {e}")

    LOG_DIR.mkdir(exist_ok=True)
    
    # --- START THREADS ---
    ReporterThread().start()
    ConfigSyncThread().start()
    HousekeepingThread().start()
    HardwareTelemetryThread().start()
    NetworkMonitorThread().start()
    AutoSyncThread().start()
    EconomicCruiseControlThread().start()
    
    global child_processes
    
    while True:
        child_processes.clear()
        
        if not CFG_PATH.exists():
            time.sleep(60); continue
            
        try:
            cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
            
            num_listeners = int(cfg.get("parallel_listeners", 4))
            target_cycle_s = int(cfg.get("interval_seconds", 300))
            
            hydra_state = {}
            if HYDRA_STATE_FILE.exists():
                try: hydra_state = json.loads(HYDRA_STATE_FILE.read_text(encoding='utf-8'))
                except: pass
                
            network_map = cfg.get("network_map", {})
            expected_interfaces = list(set(network_map.values()) - {"Default / OS"})
            
            pid_settings = cfg.get("hydra_pid_settings", {})
            global_brake_thresh = float(pid_settings.get("global_brake_pct", 95)) / 100.0
            throttling_enabled = pid_settings.get("throttling_enabled", True)
            
            global_heat = 0.0
            engage_brake = False
            
            if expected_interfaces:
                g_data = hydra_state.get("GLOBAL")
                if g_data and isinstance(g_data, dict):
                    global_heat = g_data.get('heat', 0.0)
                    if "🚨" in g_data.get('arrow', ''):
                        engage_brake = True
            
            # --- THE EMERGENCY BRAKE BLOCK ---
            if throttling_enabled and (engage_brake or global_heat >= (global_brake_thresh - 0.001)):
                logging.warning("🚨 GLOBAL EMERGENCY BRAKE IS ACTIVE. Halting worker launches for 60 seconds...")
                time.sleep(60)
                continue
            
            if throttling_enabled:
                capped_heat = min(global_brake_thresh - 0.01, global_heat) 
                exp_multiplier = 1.0 / (1.0 - capped_heat)
                padded_cycle_s = int(target_cycle_s * exp_multiplier)
                
                if expected_interfaces:
                    logging.info(f"[HYDRA-PID] Audio Main Pipe Heat: {global_heat*100:.1f}%. Stretching Cycle from {target_cycle_s}s to {padded_cycle_s}s.")
            else:
                exp_multiplier = 1.0
                padded_cycle_s = target_cycle_s
                if expected_interfaces:
                    logging.info(f"[HYDRA-PID] ⚠️ THROTTLING DISABLED. Running at max capacity (Cycle: {target_cycle_s}s).")
            
            all_streams = cfg.get("streams",[])
            db_connector.init_stream_queue(all_streams)
            
            # --- TICKET BOOTH PROXY GENERATOR ---
            available_proxies = {}
            base_port = 8081
            
            sorted_interfaces = sorted(list(expected_interfaces))
            for proxy_index, iface_name in enumerate(sorted_interfaces):
                port = base_port + proxy_index
                proxy_manager.start_proxy_for_interface(iface_name, port)
                available_proxies[iface_name] = f"http://127.0.0.1:{port}"

            try:
                PROXY_MAP_FILE.write_text(json.dumps(available_proxies, indent=2), encoding='utf-8')
            except Exception as e:
                logging.error(f"Failed to write proxy map: {e}")

            try:
                with db_connector.get_db_connection() as con:
                    init_data =[]
                    for i in range(num_listeners):
                        lid = f"L{i+1}"
                        init_data.append((lid, time.time(), padded_cycle_s, json.dumps(["Waiting for work..."]), time.time(), 0))
                    con.executemany("REPLACE INTO scheduler_status VALUES (?, ?, ?, ?, ?, ?)", init_data)
            except Exception as e: logging.error(f"Pre-pop failed: {e}")

            executable = sys.executable.replace("pythonw.exe", "python.exe")
            
            phase_delay = int(padded_cycle_s / num_listeners)
            phase_delay = max(5, min(int(120 * exp_multiplier), phase_delay)) 
            
            logging.info(f"Target Cycle={padded_cycle_s}s. Startup Stagger={phase_delay}s.")

            for i in range(num_listeners):
                lid = f"L{i+1}"

                popen_kwargs = {}
                if sys.platform == "win32" and show_windows:
                    popen_kwargs['creationflags'] = subprocess.CREATE_NEW_CONSOLE
                else:
                    log_path = LOG_DIR / f"listener_{lid}.log"
                    log_file_handle = open(log_path, 'a', encoding='utf-8') 
                    popen_kwargs['stdout'] = log_file_handle
                    popen_kwargs['stderr'] = log_file_handle
                    if sys.platform == "win32": popen_kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

                launch_config = json.dumps({"mode": "dynamic", "padded_cycle_s": padded_cycle_s})
                
                cmd =[executable, str(ENGINE_SCRIPT), launch_config, lid, json.dumps(available_proxies)]
                
                proc = subprocess.Popen(cmd, **popen_kwargs)
                child_processes.add(proc)
                
                if i < num_listeners - 1:
                    time.sleep(phase_delay)

            running = True
            while running:
                time.sleep(5)
                
                dead = set()
                for p in child_processes:
                    if p.poll() is not None: dead.add(p)
                
                if dead:
                    logging.info("A Listener process finished/died. Restarting fleet to re-sync...")
                    running = False 
                    break
                    
                # Mid-flight check for Global Emergency Brake
                if throttling_enabled and HYDRA_STATE_FILE.exists():
                    try:
                        current_hydra = json.loads(HYDRA_STATE_FILE.read_text(encoding='utf-8'))
                        g_data = current_hydra.get("GLOBAL")
                        if g_data and isinstance(g_data, dict):
                            if g_data.get('heat', 0.0) >= (global_brake_thresh - 0.001) or "🚨" in g_data.get('arrow', ''):
                                logging.warning("🚨 GLOBAL EMERGENCY BRAKE ENGAGED MID-FLIGHT! Restarting fleet to enforce deep sleep.")
                                running = False
                                break
                    except: pass
                
            # THE FIX: Put the cleanup here, OUTSIDE the while running loop
            cleanup_fleet()
            time.sleep(5) 

        except Exception as e:
            logging.error(f"Scheduler Error: {e}", exc_info=True)
            cleanup_fleet()
            time.sleep(30)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate_handler)
    signal.signal(signal.SIGINT, terminate_handler)
    try:
        if len(sys.argv) >= 2: main(sys.argv[-1])
        else: main('false')
    except KeyboardInterrupt:
        terminate_handler(None, None)