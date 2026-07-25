# FILE: network_manager.py
# VERSION: 3.4 - "The Proportional Workload Patch"
# PURPOSE: Scans active interfaces, tracks raw hardware data usage, calculates Dual-Sensor Heat, and acts as the Dynamic Dispatcher (Ticket Booth) for routing.
# UPDATED: When the master throttling switch is disabled, it bypasses the 0.95 soft-lockout and proportionally distributes load across all online interfaces based on their monthly GB plan.

import psutil
import socket
import logging
import time
import json
import calendar
import random
from datetime import datetime
from pathlib import Path

import db_connector

# Configure simple logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [NET] - %(message)s')

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "birdnet_config.json"
STATE_FILE = ROOT / "network_counters.json"
BANNED_IPS_FILE = ROOT / "banned_ips.json"
API_PACING_FILE = ROOT / "api_pacing.json"

def get_active_interfaces():
    """
    Returns a list of dictionaries containing interface details.
    Only returns interfaces that have a valid IPv4 address and are not Loopback.
    """
    interfaces =[]
    
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        
        for name, snics in addrs.items():
            if name in stats and not stats[name].isup:
                continue
                
            for snic in snics:
                if snic.family == socket.AF_INET:
                    if snic.address == '127.0.0.1':
                        continue
                    if snic.address.startswith('169.254'):
                        continue

                    interfaces.append({
                        'name': name,
                        'ip': snic.address,
                        'netmask': snic.netmask
                    })
                    
    except Exception as e:
        logging.error(f"[NETWORK] Failed to scan network interfaces: {e}")
        return[]

    interfaces.sort(key=lambda x: x['name'])
    return interfaces

def get_billing_cycle_start(reset_day):
    """Calculates the UNIX timestamp of the start of the current billing cycle."""
    now = datetime.now()
    month = now.month
    year = now.year
    
    if now.day >= reset_day:
        # We are currently past the reset day in the current month
        try:
            dt = now.replace(day=reset_day, hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            # Handles edge cases (e.g. reset day 31, but month only has 30 days)
            last_day = calendar.monthrange(year, month)[1]
            dt = now.replace(day=min(reset_day, last_day), hour=0, minute=0, second=0, microsecond=0)
    else:
        # The reset day hasn't happened yet this month, so cycle started last month
        month -= 1
        if month == 0:
            month = 12
            year -= 1
        last_day = calendar.monthrange(year, month)[1]
        dt = now.replace(year=year, month=month, day=min(reset_day, last_day), hour=0, minute=0, second=0, microsecond=0)
        
    return dt.timestamp()

def take_hardware_snapshot():
    """
    Reads OS-level hardware counters, calculates the delta since the last check,
    and logs the exact bytes uploaded/downloaded to the SQLite database.
    """
    try:
        import psutil
        import json
        import time
        import db_connector
        
        counters = psutil.net_io_counters(pernic=True)
        active_ifaces = [i['name'] for i in get_active_interfaces()]
        
        # Load previous state to calculate delta
        prev_state = {}
        if STATE_FILE.exists():
            try:
                prev_state = json.loads(STATE_FILE.read_text(encoding='utf-8'))
            except:
                pass
        
        new_state = {}
        logs_to_insert = []
        now_ts = time.time()
        
        for nic_name, stats in counters.items():
            if nic_name not in active_ifaces:
                continue
                
            current_sent = stats.bytes_sent
            current_recv = stats.bytes_recv
            
            new_state[nic_name] = {"bytes_sent": current_sent, "bytes_recv": current_recv}
            
            # THE FIX: Only calculate delta if we have a valid previous baseline
            if nic_name in prev_state:
                prev_sent = prev_state[nic_name].get("bytes_sent", current_sent)
                prev_recv = prev_state[nic_name].get("bytes_recv", current_recv)
                
                delta_sent = current_sent - prev_sent
                delta_recv = current_recv - prev_recv
                
                # THE FIX: System reboot protection (Counters wrap to 0). Do NOT log absolute values.
                if delta_sent < 0 or delta_recv < 0:
                    continue
                    
                # THE FIX: Sanity cap. If the delta is somehow > 10GB in 60 seconds, it's a glitch. Ignore it.
                if delta_sent > 10 * (1024**3) or delta_recv > 10 * (1024**3):
                    continue
                
                if delta_sent > 0 or delta_recv > 0:
                    logs_to_insert.append((nic_name, now_ts, delta_sent, delta_recv))
        
        STATE_FILE.write_text(json.dumps(new_state, indent=2), encoding='utf-8')
        
        if logs_to_insert:
            with db_connector.get_db_connection(force_local=True) as con:
                con.executemany(
                    "INSERT INTO network_hardware_logs (interface_name, timestamp, bytes_sent, bytes_recv) VALUES (?, ?, ?, ?)",
                    logs_to_insert
                )
    except Exception as e:
        logging.error(f"[NETWORK] Hardware snapshot failed: {e}")

def log_app_usage(interface_name, engine_type, bytes_used):
    """
    Records estimated bytes consumed by the Audio Engine vs Vision Engine.
    Used exclusively to calculate the split-ratio pie charts.
    """
    if not interface_name or bytes_used <= 0: 
        return
    try:
        with db_connector.get_db_connection(force_local=True) as con:
            con.execute(
                "INSERT INTO network_app_logs (interface_name, engine_type, timestamp, bytes_used) VALUES (?, ?, ?, ?)",
                (interface_name, engine_type, time.time(), bytes_used)
            )
    except Exception:
        pass 

def get_interface_quota_status(interface_name):
    """
    Checks if a specific SIM/Router has exceeded its monthly GB limit OR its hourly speed limit.
    Returns: (used_bytes, limit_bytes, is_over_quota)
    """
    try:
        with db_connector.get_db_connection(force_local=True) as con:
            cur = con.cursor()
            
            try:
                cur.execute("SELECT limit_gb, reset_day, max_gb_per_hour FROM network_quotas WHERE interface_name = ?", (interface_name,))
                row = cur.fetchone()
                if not row:
                    return 0, 0, False 
                
                limit_gb = float(row[0] or 0.0)
                reset_day = int(row[1] or 1)
                max_gb_per_hour = float(row[2] or 0.0)
                
            except sqlite3.OperationalError:
                # Fallback for old schema
                cur.execute("SELECT limit_gb, reset_day FROM network_quotas WHERE interface_name = ?", (interface_name,))
                row = cur.fetchone()
                if not row:
                    return 0, 0, False
                limit_gb = float(row[0] or 0.0)
                reset_day = int(row[1] or 1)
                max_gb_per_hour = 0.0
            
            # 1. Check Monthly Quota
            limit_bytes = limit_gb * 1024 * 1024 * 1024
            cycle_start = get_billing_cycle_start(reset_day)
            
            cur.execute("SELECT SUM(bytes_sent + bytes_recv) FROM network_hardware_logs WHERE interface_name = ? AND timestamp >= ?", (interface_name, cycle_start))
            used_row = cur.fetchone()
            used_bytes = used_row[0] if used_row and used_row[0] else 0
            
            is_over_monthly = (limit_bytes > 0) and (used_bytes >= limit_bytes)
            
            # 2. Check Hourly Speed Faucet
            is_over_hourly = False
            if max_gb_per_hour > 0:
                cutoff_1h = time.time() - 3600
                cur.execute("SELECT SUM(bytes_sent + bytes_recv) FROM network_hardware_logs WHERE interface_name = ? AND timestamp >= ?", (interface_name, cutoff_1h))
                used_1h_row = cur.fetchone()
                used_1h_bytes = used_1h_row[0] if used_1h_row and used_1h_row[0] else 0
                
                hourly_limit_bytes = max_gb_per_hour * 1024 * 1024 * 1024
                if used_1h_bytes >= hourly_limit_bytes:
                    is_over_hourly = True
                    
            return used_bytes, limit_bytes, (is_over_monthly or is_over_hourly)
            
    except Exception as e:
        logging.error(f"[NETWORK] Quota check failed for {interface_name}: {e}")
        return 0, 0, False

def get_interface_heat(interface_name):
    """[DUAL-SENSOR PID METRIC]
    Calculates the Effective Heat (0.0 to 1.0) of a specific interface.
    Heat = max(Hourly_Utilization, Monthly_Utilization).
    """
    try:
        with db_connector.get_db_connection(force_local=True) as con:
            cur = con.cursor()
            
            try:
                cur.execute("SELECT limit_gb, reset_day, max_gb_per_hour FROM network_quotas WHERE interface_name = ?", (interface_name,))
                row = cur.fetchone()
                if not row:
                    return 0.0 # No limits set = stone cold
                
                limit_gb = float(row[0] or 0.0)
                reset_day = int(row[1] or 1)
                max_gb_per_hour = float(row[2] or 0.0)
                
            except sqlite3.OperationalError:
                # Fallback for old schema
                return 0.0
            
            monthly_heat = 0.0
            hourly_heat = 0.0
            
            # 1. Calculate Monthly Heat (The Fuel Gauge)
            if limit_gb > 0:
                limit_bytes = limit_gb * (1024**3)
                cycle_start = get_billing_cycle_start(reset_day)
                
                cur.execute("SELECT SUM(bytes_sent + bytes_recv) FROM network_hardware_logs WHERE interface_name = ? AND timestamp >= ?", (interface_name, cycle_start))
                used_row = cur.fetchone()
                used_bytes = used_row[0] if used_row and used_row[0] else 0
                
                monthly_heat = min(1.0, used_bytes / limit_bytes)
            
            # 2. Calculate Hourly Heat (The Speedometer)
            if max_gb_per_hour > 0:
                cutoff_1h = time.time() - 3600
                cur.execute("SELECT SUM(bytes_sent + bytes_recv) FROM network_hardware_logs WHERE interface_name = ? AND timestamp >= ?", (interface_name, cutoff_1h))
                used_1h_row = cur.fetchone()
                used_1h_bytes = used_1h_row[0] if used_1h_row and used_1h_row[0] else 0
                
                hourly_limit_bytes = max_gb_per_hour * (1024**3)
                hourly_heat = min(1.0, used_1h_bytes / hourly_limit_bytes)
                
            return max(monthly_heat, hourly_heat)
            
    except Exception as e:
        logging.error(f"[HYDRA-PID] Heat calculation failed for {interface_name}: {e}")
        return 0.0

def get_global_network_heat(interface_names):
    """[DUAL-SENSOR PID METRIC]
    Calculates the aggregate "Main Pipe" heat across the provided active routers.
    Returns a float between 0.0 and 1.0.
    """
    if not interface_names:
        return 0.0
        
    total_heat = 0.0
    valid_count = 0
    
    for iface in interface_names:
        if iface and iface != "Default / OS":
            heat = get_interface_heat(iface)
            total_heat += heat
            valid_count += 1
            
    if valid_count == 0:
        return 0.0
        
    return total_heat / valid_count

# ==============================================================================
# PACING & BURNED IP TRACKING ("THE TICKET BOOTH")
# ==============================================================================

def mark_ip_banned(interface_name, duration_minutes=15):
    """
    DISABLED. YouTube 403s are too common and transient when running multiple concurrent workers.
    Issuing a 15-minute lockout causes the Emergency Brake to trigger unnecessarily.
    """
    pass

def is_ip_banned(interface_name):
    """
    DISABLED. Always returns False to prevent the Emergency Brake from freezing the system.
    """
    return False

def get_api_token(interface_name, cooldown_seconds=5):
    """Token bucket to prevent API stampeding on a single IP."""
    try:
        pacing = {}
        if API_PACING_FILE.exists():
            try:
                pacing = json.loads(API_PACING_FILE.read_text(encoding='utf-8'))
            except json.JSONDecodeError:
                pass
        
        last_used = pacing.get(interface_name, 0)
        now = time.time()
        
        # --- THE CHRONO CLAMP PATCH ---
        # Uses abs() to prevent infinite deadlocks caused by Windows DST hardware clock drift
        if abs(now - last_used) < cooldown_seconds:
            return False # Must wait
            
        pacing[interface_name] = now
        API_PACING_FILE.write_text(json.dumps(pacing, indent=2), encoding='utf-8')
        return True
    except Exception:
        return True

def get_best_available_interface(expected_interfaces):
    """
    Dynamic Dispatcher: Evaluates all expected interfaces and returns the best one.
    Criteria: Not disconnected, Not banned, Heat < 0.95 (Unless Throttling Disabled).
    Sorts by lowest heat to smoothly distribute the load (Unless Throttling Disabled).
    If Throttling Disabled, distributes load proportionally based on monthly GB plan.
    Returns: (interface_name, ip_address) or (None, None) if all are exhausted.
    """
    active_ifaces = {i['name']: i['ip'] for i in get_active_interfaces()}
    
    # --- THE UNSHACKLED PATCH: Check if throttling is enabled ---
    throttling_enabled = True
    try:
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            throttling_enabled = cfg.get("hydra_pid_settings", {}).get("throttling_enabled", True)
    except Exception:
        pass

    candidates = []
    for iface in expected_interfaces:
        if iface == "Default / OS":
            continue
            
        if iface not in active_ifaces:
            continue # Offline (Physically disconnected)
            
        if is_ip_banned(iface):
            continue # 403 Banned (Currently Disabled)
            
        heat = get_interface_heat(iface)
        
        # If throttling is enabled, enforce the 0.95 soft-lockout
        if throttling_enabled and heat >= 0.95:
            continue # Soft-lockout or Monthly Data depleted
            
        used_bytes, limit_bytes, is_over = get_interface_quota_status(iface)
        
        candidates.append({
            'name': iface,
            'ip': active_ifaces[iface],
            'heat': heat,
            'limit_bytes': limit_bytes
        })
        
    if not candidates:
        return None, None
        
    if throttling_enabled:
        # Sort by heat ascending, so the "coldest" router gets the next request
        candidates.sort(key=lambda x: x['heat'])
        best = candidates[0]
    else:
        # Throttling disabled: proportionally distribute load based on GB limit
        weights = []
        for c in candidates:
            w = c['limit_bytes']
            if w <= 0:
                w = 1000 * 1024**3 # 1000 GB fallback for unlimited plans
            weights.append(w)
        
        best = random.choices(candidates, weights=weights, k=1)[0]
    
    return best['name'], best['ip']

if __name__ == "__main__":
    print("-" * 60)
    print("NETWORK MANAGER DIAGNOSTIC (V3.4 - Proportional Workload Patch)")
    print("-" * 60)
    print("Scanning active interfaces...")
    found = get_active_interfaces()
    if not found:
        print("WARNING: No active non-local IPv4 interfaces found!")
    else:
        for i, iface in enumerate(found):
            name = iface['name']
            heat = get_interface_heat(name)
            banned = "YES" if is_ip_banned(name) else "NO"
            print(f"  {i+1}. NAME: {name} | IP: {iface['ip']} | HEAT: {heat*100:.1f}% | BANNED: {banned}")
            
        global_heat = get_global_network_heat([f['name'] for f in found])
        print(f"\n  [MAIN PIPE] AGGREGATE HEAT: {global_heat*100:.1f}%")
        
        best_name, best_ip = get_best_available_interface([f['name'] for f in found])
        print(f"[DISPATCHER] Best available interface right now: {best_name} ({best_ip})")
            
    print("\nTesting hardware snapshot logic...")
    take_hardware_snapshot()
    print("Snapshot complete. Run again in 10 seconds to generate Delta records.")
    input("\nPress Enter to exit...")