# FILE: expert_rules.py
# The "Expert in the Cloud" logic for matching detections to user interests.
# VERSION 2.0: Supports Granular "Sniper" and "Entity" Alerts.

import sqlite3
import logging
from pathlib import Path
from collections import defaultdict

# --- Configuration ---
ROOT = Path(__file__).resolve().parent
DATABASE_PATH = ROOT / "detections.db"

# Configure logging if not already configured
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_db_connection():
    """Establishes a read-only connection to the database."""
    try:
        if not DATABASE_PATH.exists():
            return None
        # Use URI for read-only mode to prevent accidental writes
        db_uri = f"file:{DATABASE_PATH}?mode=ro"
        con = sqlite3.connect(db_uri, uri=True)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.OperationalError as e:
        logging.error(f"EXPERT_RULES DB ERROR: Could not connect to {DATABASE_PATH}. Error: {e}")
        return None

def load_user_profiles():
    """
    Loads all user interests from the database and organizes them into profiles.
    Returns a dictionary where keys are user_ids and values are their interest profiles.
    """
    profiles = defaultdict(lambda: {
        'continents': set(),
        'habitats': set(),
        'bird_groups': set(),
        'species': set(),           # TYPE: Entity (The Collector)
        'stream_alerts': set(),     # TYPE: Context (The Local)
        'sniper_matches': set()     # TYPE: Sniper (The Match) -> Set of (species, stream_url) tuples
    })
    
    con = get_db_connection()
    if not con:
        return {}
        
    try:
        cur = con.cursor()
        # Fetch all columns including the new 'specific_context'
        # We wrap in try/except because specific_context might not exist during migration
        try:
            cur.execute("SELECT user_id, interest_type, interest_value, specific_context FROM user_interests")
        except sqlite3.OperationalError:
            # Fallback for old schema if migration hasn't run yet
            cur.execute("SELECT user_id, interest_type, interest_value, NULL as specific_context FROM user_interests")
        
        for row in cur.fetchall():
            user_id = row['user_id']
            itype = row['interest_type']
            ivalue = row['interest_value']
            icontext = row['specific_context'] # Only used for sniper
            
            # --- BROAD FILTERS ---
            if itype == 'continent':
                profiles[user_id]['continents'].add(ivalue)
            elif itype == 'habitat':
                profiles[user_id]['habitats'].add(ivalue)
            elif itype == 'bird_group':
                profiles[user_id]['bird_groups'].add(ivalue)
            
            # --- GRANULAR ALERTS (New) ---
            elif itype == 'species':
                # User wants this bird anywhere
                profiles[user_id]['species'].add(ivalue)
            
            elif itype == 'stream_alert':
                # User watches this stream specifically
                profiles[user_id]['stream_alerts'].add(ivalue)
            
            elif itype == 'sniper_match':
                # User wants THIS bird on THIS stream
                if icontext:
                    profiles[user_id]['sniper_matches'].add((ivalue, icontext))

        con.close()
        return dict(profiles)
    except Exception as e:
        logging.error(f"EXPERT_RULES ERROR: Could not load user profiles: {e}", exc_info=True)
        if con: con.close()
        return {}

def get_species_for_bird_groups(bird_groups):
    """
    Translates a set of user-friendly bird group names (e.g., {"Owls"})
    into a set of specific eBird common names.
    """
    if not bird_groups:
        return set()
        
    species = set()
    con = get_db_connection()
    if not con:
        return species
        
    try:
        cur = con.cursor()
        # Query joins custom groups to eBird families
        # Using parameter substitution correctly
        placeholders = ','.join('?' for _ in bird_groups)
        query = f"""
            SELECT DISTINCT es.comName
            FROM ebird_species es
            JOIN bird_groups bg ON es.familyComName = bg.ebirdFamilyComName
            WHERE bg.groupName IN ({placeholders})
        """
        
        cur.execute(query, tuple(bird_groups))
        for row in cur.fetchall():
            species.add(row['comName'])
        con.close()
        return species
    except Exception as e:
        logging.error(f"EXPERT_RULES ERROR: Could not get species for bird groups: {e}")
        if con: con.close()
        return species

# --- CLI Test Block ---
if __name__ == '__main__':
    print("Testing expert_rules v2.0...")
    
    # Mock Data Test
    try:
        profiles = load_user_profiles()
        print(f"Loaded {len(profiles)} profiles from DB.")
    except Exception as e:
        print(f"DB Load failed (expected if DB missing): {e}")