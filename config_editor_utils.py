# FILE: gui_utils.py
# Contains helper functions and static maintenance classes extracted from config_editor_gui.py

import math
import requests
import winreg
import zipfile
import io
from pathlib import Path

# --- Configuration Paths ---
ROOT = Path(__file__).resolve().parent

# --- GEOGRAPHY HELPER ---
def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = math.sin(dLat/2) * math.sin(dLat/2) + \
        math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * \
        math.sin(dLon/2) * math.sin(dLon/2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

# --- MAINTENANCE HELPER CLASS ---
class ChromeMaintenance:
    @staticmethod
    def get_installed_chrome_version():
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon")
            version, _ = winreg.QueryValueEx(key, "version")
            return version
        except:
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Google Chrome")
                version, _ = winreg.QueryValueEx(key, "DisplayVersion")
                return version
            except:
                return None

    @staticmethod
    def download_driver(version):
        try:
            api_url = "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"
            r = requests.get(api_url)
            data = r.json()
            target_major = version.split('.')[0]
            best_match = None
            for entry in reversed(data['versions']):
                if entry['version'].startswith(target_major):
                    best_match = entry
                    break
            if not best_match: return False, f"No driver found for Chrome {version}"
            driver_url = None
            for d in best_match['downloads'].get('chromedriver', []):
                if d['platform'] == 'win64':
                    driver_url = d['url']
                    break
            if not driver_url: return False, "No Windows driver URL found."
            r_zip = requests.get(driver_url)
            with zipfile.ZipFile(io.BytesIO(r_zip.content)) as z:
                for filename in z.namelist():
                    if filename.endswith("chromedriver.exe"):
                        with z.open(filename) as source, open(ROOT / "chromedriver.exe", "wb") as target:
                            target.write(source.read())
                        return True, f"Updated to {best_match['version']}"
            return False, "Failed to extract driver."
        except Exception as e: return False, str(e)