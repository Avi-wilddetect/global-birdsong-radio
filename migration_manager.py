# FILE: migration_manager.py
# VERSION: 1.1 - "Fixed Init"
# LOGIC: Lazy-loads migration data from JSON to SQLite when a bird is detected.

import sqlite3
import json
import logging
from pathlib import Path

# --- Configuration ---
ROOT = Path(__file__).resolve().parent
DATABASE_PATH = ROOT / "detections.db"
REFERENCE_FILE = ROOT / "migration_reference.json"

logging.basicConfig(level=logging.INFO)

def get_db_connection():
    con = sqlite3.connect(DATABASE_PATH, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    return con

def init_migration_table():
    """Creates the table if it doesn't exist."""
    try:
        con = get_db_connection()
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS species_geography (
                species_name TEXT PRIMARY KEY,
                breeding_lat REAL,
                breeding_lon REAL,
                wintering_lat REAL,
                wintering_lon REAL,
                source TEXT
            )
        """)
        con.commit()
        con.close()
    except Exception as e:
        logging.error(f"Migration DB Init Error: {e}")

def get_family_for_species(species_name):
    """Helper to find the family group of a species from species_traits."""
    try:
        con = get_db_connection()
        cur = con.cursor()
        cur.execute("SELECT family_group FROM species_traits WHERE species_name = ?", (species_name,))
        row = cur.fetchone()
        con.close()
        # Map common names to scientific family keys used in JSON
        if row:
            fam = row[0]
            if "Warbler" in fam: return "Parulidae"
            if "Thrush" in fam: return "Turdidae"
            if "Finch" in fam: return "Fringillidae"
            if "Duck" in fam or "Goose" in fam: return "Anatidae"
            if "Hawk" in fam or "Eagle" in fam: return "Accipitridae"
            if "Swallow" in fam: return "Hirundinidae"
            if "Crow" in fam or "Jay" in fam: return "Corvidae"
            if "Gull" in fam or "Tern" in fam: return "Laridae"
        return None
    except: return None

def check_and_update_migration(species_name):
    """
    The Core Function.
    1. Checks DB. If exists, return.
    2. If not, reads JSON.
    3. Finds match (Species -> Family).
    4. Writes to DB.
    """
    # 1. Check DB
    try:
        con = get_db_connection()
        cur = con.cursor()
        # Ensure table exists first
        init_migration_table()
        
        cur.execute("SELECT 1 FROM species_geography WHERE species_name = ?", (species_name,))
        if cur.fetchone():
            con.close()
            return # Data already exists
    except Exception as e:
        logging.error(f"DB Check Failed: {e}")
        return
    
    # 2. Load Library
    if not REFERENCE_FILE.exists():
        logging.warning("Migration Reference JSON missing. Run create_migration_reference.py first.")
        return

    try:
        with open(REFERENCE_FILE, 'r', encoding='utf-8') as f:
            lib = json.load(f)
        
        breed = None
        winter = None
        source = "Unknown"

        # 3. Lookup Logic
        if species_name in lib["species"]:
            # Exact Match
            data = lib["species"][species_name]
            breed = data["breed"]
            winter = data["winter"]
            source = "Exact"
        else:
            # Fallback to Family
            fam_key = get_family_for_species(species_name)
            if fam_key and fam_key in lib["families"]:
                data = lib["families"][fam_key]
                breed = data["breed"]
                winter = data["winter"]
                source = f"Family ({fam_key})"
        
        # 4. Insert into DB
        if breed and winter:
            con = get_db_connection()
            con.execute("""
                INSERT OR REPLACE INTO species_geography 
                (species_name, breeding_lat, breeding_lon, wintering_lat, wintering_lon, source)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (species_name, breed[0], breed[1], winter[0], winter[1], source))
            con.commit()
            con.close()
            logging.info(f"Migration Data Lazy-Loaded for '{species_name}' via {source}.")
            
    except Exception as e:
        logging.error(f"Migration Lazy-Load Failed: {e}")

if __name__ == "__main__":
    # Self-test
    init_migration_table()
    print("Migration Manager Loaded.")