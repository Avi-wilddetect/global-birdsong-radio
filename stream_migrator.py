# FILE: stream_migrator.py
# VERSION: 2.0 - "The URL Tag-Along Patch"
# PURPOSE: Logic for safely moving history from an Old URL to a New URL.
# UPDATED: Added JSON patching logic. When a URL is migrated, it now explicitly checks the vision_ai 'enabled_streams' list in birdnet_config.json and updates the old URL to the new URL, preventing the Vision Engine from going blind to auto-healed streams.

import sqlite3
import logging
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATABASE_PATH = ROOT / "detections.db"
CONFIG_FILE = ROOT / "birdnet_config.json"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

def get_db_connection():
    return sqlite3.connect(DATABASE_PATH, timeout=30)

def check_history(stream_url):
    """Returns the number of detections associated with a URL."""
    if not DATABASE_PATH.exists(): return 0
    try:
        con = get_db_connection()
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM detections WHERE channel_url = ?", (stream_url,))
        count = cur.fetchone()[0]
        con.close()
        return count
    except Exception as e:
        logging.error(f"Migration Check Error: {e}")
        return 0

def migrate_stream_data(old_url, new_url):
    """
    Moves all data (Detections, Health, Profiles) from Old URL to New URL.
    Handles conflicts (if New URL already has data) by prioritizing the New URL.
    Also patches birdnet_config.json to ensure Vision Engine doesn't lose the stream.
    """
    if not DATABASE_PATH.exists(): return False, "Database not found."
    
    con = get_db_connection()
    cur = con.cursor()
    
    try:
        cur.execute("BEGIN TRANSACTION")

        # 1. MIGRATE DETECTIONS (Simple Rename)
        # detections table has no unique constraint on URL, so we just update all.
        cur.execute("UPDATE detections SET channel_url = ? WHERE channel_url = ?", (new_url, old_url))
        det_count = cur.rowcount

        # 2. MIGRATE HEALTH EVENTS (Simple Rename)
        cur.execute("UPDATE stream_health_events SET stream_url = ? WHERE stream_url = ?", (new_url, old_url))
        
        # 3. MIGRATE AUDIO HASHES (Simple Rename - PK is hash+url)
        # If hash exists for new url, ignore old.
        cur.execute("UPDATE OR IGNORE audio_hashes SET stream_url = ? WHERE stream_url = ?", (new_url, old_url))
        cur.execute("DELETE FROM audio_hashes WHERE stream_url = ?", (old_url,)) # Cleanup leftovers

        # 4. MIGRATE NOISE PROFILES (PK is stream_url)
        # If New URL already has a profile, keep it. Discard Old.
        cur.execute("UPDATE OR IGNORE stream_noise_profiles SET stream_url = ? WHERE stream_url = ?", (new_url, old_url))
        cur.execute("DELETE FROM stream_noise_profiles WHERE stream_url = ?", (old_url,))

        # 5. MIGRATE SPECIES PROFILES (Calibration Data)
        # Constraint: UNIQUE(stream_url, species_name)
        # Logic: If New URL already has a calibration for 'Robin', keep it. Discard Old 'Robin' data.
        #        If New URL has no 'Robin', move Old 'Robin' to New.
        cur.execute("UPDATE OR IGNORE species_stream_profiles SET stream_url = ? WHERE stream_url = ?", (new_url, old_url))
        cur.execute("DELETE FROM species_stream_profiles WHERE stream_url = ?", (old_url,))

        con.commit()
        
        # --- THE URL TAG-ALONG PATCH (JSON) ---
        json_msg = ""
        try:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                
                vision_enabled = cfg.get("vision_ai", {}).get("enabled_streams", [])
                updated_json = False
                
                for i, u in enumerate(vision_enabled):
                    if u == old_url:
                        vision_enabled[i] = new_url
                        updated_json = True
                        break # Found and replaced
                
                if updated_json:
                    cfg["vision_ai"]["enabled_streams"] = vision_enabled
                    tmp_file = CONFIG_FILE.with_suffix('.tmp')
                    with open(tmp_file, 'w', encoding='utf-8') as f:
                        json.dump(cfg, f, indent=2)
                    os.replace(tmp_file, CONFIG_FILE)
                    json_msg = " | Vision Engine checkbox list successfully updated."
        except Exception as e:
            logging.error(f"Failed to update vision config during migration: {e}")
            json_msg = f" | Warning: Vision config update failed: {e}"
        # ----------------------------------------

        msg = f"Successfully migrated {det_count} detections and associated data.{json_msg}"
        logging.info(f"MIGRATION SUCCESS: {old_url} -> {new_url}")
        return True, msg

    except Exception as e:
        con.rollback()
        logging.error(f"MIGRATION FAILED: {e}", exc_info=True)
        return False, f"Migration failed: {e}"
    finally:
        con.close()