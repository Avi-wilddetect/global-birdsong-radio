# FILE: init_cloud_db.py
# PURPOSE: Connects to Cloud DB (via db_connector), ensures tables exist, and migrates data.
# VERSION: 4.2 - "The Cloud Explicit Patch"
# COMMANDS: MIGRATE, IMAGES, RECENT, NUKE_SYNC, INIT
# UPDATED: Explicitly requests force_local=False to bypass the new Local-only safety switch.

import sqlite3
import logging
from pathlib import Path
import db_connector

# --- Configuration ---
ROOT = Path(__file__).resolve().parent
SCHEMA_FILE = ROOT / "schema_postgres.sql"
LOCAL_DB_FILE = ROOT / "detections.db"
LOCAL_IMAGE_DB_FILE = ROOT / "image_database.db"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [DB] - %(message)s')

def apply_schema(conn):
    """Reads schema_postgres.sql and applies it to the connected DB."""
    logging.info("Reading schema file...")
    try:
        with open(SCHEMA_FILE, 'r', encoding='utf-8') as f:
            schema_sql = f.read()
        
        logging.info("Applying schema to database...")
        cur = conn.cursor()
        try:
            cur.execute(schema_sql)
            conn.commit()
            logging.info("Schema applied successfully.")
        except Exception as e:
            logging.error(f"Failed to execute schema SQL: {e}")
            conn.rollback()
            raise e
        finally:
            cur.close()
    except Exception as e:
        logging.error(f"Failed to apply schema: {e}")

def migrate_images(cloud_conn):
    """Migrates images from local SQLite to Cloud Postgres."""
    if not LOCAL_IMAGE_DB_FILE.exists():
        logging.warning("No local image database found.")
        return

    logging.info("Migrating Species Images...")
    local_conn = sqlite3.connect(LOCAL_IMAGE_DB_FILE)
    local_conn.row_factory = sqlite3.Row
    local_cur = local_conn.cursor()
    cloud_cur = cloud_conn.cursor()
    
    try:
        cloud_cur.execute("""
            CREATE TABLE IF NOT EXISTS species_images (
                species_name TEXT PRIMARY KEY,
                image_data BYTEA,
                source_url TEXT,
                last_updated TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING'
            );
        """)
        cloud_conn.commit()

        local_cur.execute("SELECT species_name, image_data, source_url, last_updated, status FROM species_images")
        images = local_cur.fetchall()
        
        if not images:
            logging.info("No images found in local database.")
            return

        logging.info(f"Found {len(images)} images to check/migrate...")
        
        count = 0
        for img in images:
            cloud_cur.execute(
                """INSERT INTO species_images (species_name, image_data, source_url, last_updated, status)
                   VALUES (%s, %s, %s, %s, %s) 
                   ON CONFLICT (species_name) DO UPDATE SET
                   image_data = EXCLUDED.image_data,
                   source_url = EXCLUDED.source_url,
                   last_updated = EXCLUDED.last_updated,
                   status = EXCLUDED.status
                """,
                (img['species_name'], img['image_data'], img['source_url'], img['last_updated'], img['status'])
            )
            count += 1
            if count % 50 == 0: logging.info(f"Processed {count} images...")
        
        cloud_conn.commit()
        logging.info(f"Successfully migrated {count} images.")
        
    except Exception as e:
        logging.error(f"Image Migration Failed: {e}")
        cloud_conn.rollback()
    finally:
        local_conn.close()
        cloud_cur.close()

def sync_recent_history(cloud_conn, limit=1000):
    """
    Forcefully synchronizes the most recent detections from Local to Cloud.
    Uses Timestamp + URL to find and patch ghost records, ignoring broken IDs.
    """
    if not LOCAL_DB_FILE.exists():
        logging.warning("No local detections database found.")
        return

    logging.info(f"Force-syncing the last {limit} detections (Healing Ghost Records & Syncing Curation)...")
    
    local_conn = sqlite3.connect(LOCAL_DB_FILE)
    local_conn.row_factory = sqlite3.Row 
    local_cur = local_conn.cursor()
    cloud_cur = cloud_conn.cursor()

    try:
        has_full_schema = True
        try:
            local_cur.execute(f"SELECT id, timestamp, channel_url, species, latitude, longitude, distance_category, snr, alert_sent, listener_id, network_interface, detection_method, vision_path, human_verified FROM detections ORDER BY timestamp DESC LIMIT {limit}")
        except sqlite3.OperationalError:
            has_full_schema = False
            local_cur.execute(f"SELECT id, timestamp, channel_url, species, latitude, longitude, distance_category, snr, alert_sent, listener_id, network_interface FROM detections ORDER BY timestamp DESC LIMIT {limit}")

        rows = local_cur.fetchall()
        logging.info(f"Found {len(rows)} recent local detections. Patching Cloud DB...")

        update_query = """
            UPDATE detections SET 
                species = %s, distance_category = %s, snr = %s, alert_sent = %s, 
                detection_method = %s, vision_path = %s, human_verified = %s
            WHERE timestamp = %s AND channel_url = %s
        """
        
        insert_query = """
            INSERT INTO detections (timestamp, channel_url, species, latitude, longitude, distance_category, snr, alert_sent, listener_id, network_interface, detection_method, vision_path, human_verified)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        count_updated = 0
        count_inserted = 0
        
        for row in rows:
            alert_val = True if row['alert_sent'] else False
            det_method = row['detection_method'] if has_full_schema else 'audio'
            v_path = row['vision_path'] if has_full_schema else None
            h_verified = row['human_verified'] if has_full_schema else 'pending'
            
            cloud_cur.execute(update_query, (
                row['species'], row['distance_category'], row['snr'], alert_val, 
                det_method, v_path, h_verified, 
                row['timestamp'], row['channel_url']
            ))
            
            if cloud_cur.rowcount == 0:
                cloud_cur.execute(insert_query, (
                    row['timestamp'], row['channel_url'], row['species'], 
                    row['latitude'], row['longitude'], row['distance_category'], 
                    row['snr'], alert_val, row['listener_id'], row['network_interface'], 
                    det_method, v_path, h_verified
                ))
                count_inserted += 1
            else:
                count_updated += 1

        cloud_conn.commit()
        logging.info(f"✔ Quick Patch Complete: {count_updated} Ghost Records Healed | {count_inserted} Missing Records Inserted.")

    except Exception as e:
        logging.error(f"Recent Sync Failed: {e}")
        cloud_conn.rollback()
    finally:
        local_conn.close()
        cloud_cur.close()

def nuke_and_sync(cloud_conn):
    """Wipes the Cloud detections table completely, then pushes a fresh clone from local."""
    logging.info("☢️ INITIATING NUKE PROTOCOL: Erasing Cloud Detections Table...")
    try:
        cloud_cur = cloud_conn.cursor()
        cloud_cur.execute("TRUNCATE TABLE detections RESTART IDENTITY CASCADE;")
        cloud_conn.commit()
        logging.info("✔ Cloud Detections Table has been wiped clean. Ghost records destroyed.")
    except Exception as e:
        logging.error(f"Failed to nuke table: {e}")
        cloud_conn.rollback()
        return
    finally:
        cloud_cur.close()
    
    logging.info("Initiating fresh clone from Local Database...")
    migrate_detections(cloud_conn)

def migrate_detections(cloud_conn):
    """Migrates the main detection history from local SQLite to Cloud Postgres (Bulk/Fast)."""
    if not LOCAL_DB_FILE.exists():
        logging.warning("No local detections database found.")
        return

    logging.info("Migrating Detection History (Bulk mode - this may take a moment)...")
    
    local_conn = sqlite3.connect(LOCAL_DB_FILE)
    local_conn.row_factory = sqlite3.Row 
    local_cur = local_conn.cursor()
    cloud_cur = cloud_conn.cursor()

    try:
        has_full_schema = True
        try:
            local_cur.execute("SELECT id, timestamp, channel_url, species, latitude, longitude, distance_category, snr, alert_sent, listener_id, network_interface, detection_method, vision_path, human_verified FROM detections")
        except sqlite3.OperationalError:
            has_full_schema = False
            local_cur.execute("SELECT id, timestamp, channel_url, species, latitude, longitude, distance_category, snr, alert_sent, listener_id, network_interface FROM detections")

        rows = local_cur.fetchall()
        logging.info(f"Found {len(rows)} local detections. Syncing to Cloud...")

        count = 0
        batch_size = 500
        buffer =[]
        
        query = """
            INSERT INTO detections (id, timestamp, channel_url, species, latitude, longitude, distance_category, snr, alert_sent, listener_id, network_interface, detection_method, vision_path, human_verified)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        """

        for row in rows:
            alert_val = True if row['alert_sent'] else False
            det_method = row['detection_method'] if has_full_schema else 'audio'
            v_path = row['vision_path'] if has_full_schema else None
            h_verified = row['human_verified'] if has_full_schema else 'pending'
            
            data = (
                row['id'], row['timestamp'], row['channel_url'], row['species'], 
                row['latitude'], row['longitude'], row['distance_category'], row['snr'], 
                alert_val, row['listener_id'], row['network_interface'], 
                det_method, v_path, h_verified
            )
            buffer.append(data)

            if len(buffer) >= batch_size:
                cloud_cur.executemany(query, buffer)
                cloud_conn.commit()
                count += len(buffer)
                logging.info(f"Synced {count}/{len(rows)} detections...")
                buffer =[]

        if buffer:
            cloud_cur.executemany(query, buffer)
            cloud_conn.commit()
            count += len(buffer)

        logging.info(f"Detection History Sync Complete. Total processed: {count}")

        logging.info("Resetting Cloud ID Sequence...")
        cloud_cur.execute("SELECT setval('detections_id_seq', (SELECT MAX(id) FROM detections))")
        cloud_conn.commit()
        logging.info("Sequence reset successfully.")

    except Exception as e:
        logging.error(f"Detection Migration Failed: {e}")
        cloud_conn.rollback()
    finally:
        local_conn.close()
        cloud_cur.close()

def migrate_users_and_traits(cloud_conn):
    local_conn = sqlite3.connect(LOCAL_DB_FILE)
    local_conn.row_factory = sqlite3.Row
    local_cur = local_conn.cursor()
    cloud_cur = cloud_conn.cursor()

    try:
        local_cur.execute("SELECT email, password_hash FROM users")
        for u in local_cur.fetchall():
            cloud_cur.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s) ON CONFLICT (email) DO NOTHING",
                (u['email'], u['password_hash'])
            )
        
        local_cur.execute("SELECT * FROM species_traits")
        for t in local_cur.fetchall():
            cloud_cur.execute(
                """INSERT INTO species_traits (species_name, family_group, size_class, beak_type, color_primary, silhouette, call_pattern)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (species_name) DO NOTHING""",
                (t['species_name'], t['family_group'], t['size_class'], t['beak_type'], t['color_primary'], t['silhouette'], t['call_pattern'])
            )

        cloud_conn.commit()
        logging.info("Users and Traits synced.")

    except Exception as e:
        logging.error(f"MetaData Migration Failed: {e}")
        cloud_conn.rollback()
    finally:
        local_conn.close()
        cloud_cur.close()

if __name__ == "__main__":
    print("=====================================================")
    print("--- GBR CLOUD DATABASE SYNC TOOL (v4.2) ---")
    print("=====================================================\n")
    
    # CRITICAL FIX: Explicitly bypass the local-only safety switch to hit the Cloud.
    conn = db_connector.get_db_connection(force_local=False)
    
    if getattr(conn, 'db_type', 'sqlite') != 'postgres':
        print("❌ ERROR: Not connected to Cloud DB. Check birdnet_config.json.")
        exit(1)
        
    print(f"✔ Connected to Cloud DB: {conn.db_type}\n")
    
    print("Available Commands:")
    print("  [MIGRATE]   - Full Sync (Slow, safe for bulk uploading history).")
    print("  [IMAGES]    - Image Sync Only (Fast, uploads new Image Curator pictures).")
    print("  [RECENT]    - Quick Patch (Fast, force-updates the last 1000 detections).")
    print("[NUKE_SYNC] - ☢️ Wipe Cloud History & Resync (Fixes ghost records).")
    print("  [INIT]      - Schema Only (Creates missing tables in the cloud).\n")
    
    action = input("Type a command and press Enter: ").strip().upper()
    
    if action == 'MIGRATE':
        migrate_images(conn)
        migrate_users_and_traits(conn)
        migrate_detections(conn)
    elif action == 'IMAGES':
        migrate_images(conn)
    elif action == 'RECENT':
        sync_recent_history(conn)
    elif action == 'NUKE_SYNC':
        nuke_and_sync(conn)
    elif action == 'INIT':
        apply_schema(conn)
    else:
        print("Invalid command. Exiting.")
        
    conn.close()
    print("\nDone.")