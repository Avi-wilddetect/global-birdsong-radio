# FILE: launch_visible_browser.py
# A simple helper script to launch a VISIBLE Chrome browser for manual YouTube login.
# VERSION 4: Reverted to standard Selenium to bypass security/patching issues.

import sys
import json
import logging
import time
from pathlib import Path
import traceback

try:
    # --- MODIFIED: Switch from undetected_chromedriver to standard selenium ---
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.common.exceptions import WebDriverException
except ImportError:
    print("FATAL: selenium is not installed. Please run 'pip install selenium' in your venv.")
    input("--- PRESS ENTER TO EXIT ---")
    sys.exit(1)

# --- Configuration ---
ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "birdnet_config.json"
log_file = ROOT / "monitor_debug.txt"

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
                    handlers=[logging.FileHandler(log_file, mode='a'), logging.StreamHandler()])

def launch_browser():
    """
    Reads the main configuration, validates browser automation settings,
    and launches a visible Chrome browser instance for the user to interact with.
    """
    logging.info("[Visible Browser] Launch requested by user.")
    print("--- LAUNCHING VISIBLE BROWSER FOR YOUTUBE LOGIN ---")

    if not CONFIG_FILE.exists():
        logging.error("[Visible Browser] Configuration file not found.")
        print(f"ERROR: Configuration file not found at {CONFIG_FILE}")
        return

    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        browser_cfg = config.get("browser_automation", {})
    except Exception as e:
        logging.error(f"[Visible Browser] Failed to read or parse config file: {e}")
        print(f"ERROR: Could not read configuration file: {e}")
        return

    if not browser_cfg.get("enabled"):
        logging.warning("[Visible Browser] Automation is disabled in the configuration. Aborting.")
        print("INFO: Browser automation is currently disabled in your configuration. Nothing to launch.")
        return

    profile_path = browser_cfg.get("chrome_profile_path")
    # --- MODIFIED: Reinstate the driver_path, it's now required by standard Selenium ---
    driver_path = browser_cfg.get("webdriver_path")

    if not profile_path or not driver_path:
        logging.error("[Visible Browser] Chrome Profile Path or WebDriver Path is missing.")
        print("ERROR: Your configuration is missing the path to the Chrome Profile or the WebDriver.")
        return

    if not Path(profile_path).exists() or not Path(driver_path).exists():
        logging.error(f"[Visible Browser] A required path does not exist. Profile: '{profile_path}', Driver: '{driver_path}'")
        print("ERROR: The specified Chrome Profile Path or WebDriver Path does not exist. Please check your settings.")
        return

    driver = None
    try:
        print("\n1. A new Chrome window will open using your specified profile.")
        print("2. Please log in to your YouTube/Google account as you normally would.")
        print("3. IMPORTANT: If you see a 'Turn on sync?' prompt, click 'Yes, I'm in' or similar to confirm.")
        print("4. Once you are successfully logged in, you can simply close the Chrome window.")
        print("\nLaunching...")

        # --- MODIFIED: Use standard Selenium setup ---
        service = Service(executable_path=driver_path)
        options = webdriver.ChromeOptions()
        options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
        options.add_argument(f"--user-data-dir={profile_path}")
        # Add experimental option to detach the browser, which can help with stability
        options.add_experimental_option("detach", True)

        driver = webdriver.Chrome(service=service, options=options)

        driver.get("https://www.youtube.com")

        logging.info("[Visible Browser] Browser launched. The user will now log in and close it manually.")
        print("\n--- BROWSER IS NOW OPEN. ---")
        print("After you log in and close the browser window, please press Enter in THIS console window.")

    except WebDriverException as e:
        # --- MODIFIED: Catch the specific Selenium exception for better error messages ---
        logging.error(f"[Visible Browser] A WebDriver error occurred: {e}", exc_info=True)
        print("\n" + "="*50)
        print("### FATAL ERROR: COULD NOT LAUNCH THE BROWSER ###")
        print("="*50)

        # Check for the common version mismatch error
        if "This version of ChromeDriver only supports Chrome version" in str(e):
            print("ERROR: CHROME DRIVER VERSION MISMATCH.\n\n"
                  "Your installed Google Chrome browser has been updated, and your current\n"
                  "`chromedriver.exe` is now incompatible.\n\n"
                  "Please download the correct chromedriver.exe for your Chrome version and\n"
                  "replace the one in your project folder.")
        else:
            print("The browser failed to start. Please check the detailed error message below:\n")
            traceback.print_exc()
        print("\n" + "="*50)

    except Exception as e:
        logging.error(f"[Visible Browser] A critical error occurred: {e}", exc_info=True)
        print("\n" + "="*50)
        print("### FATAL ERROR: AN UNEXPECTED ERROR OCCURRED ###")
        print("="*50)
        traceback.print_exc()
        print("\n" + "="*50)
    finally:
        # We no longer need to quit the driver, as the "detach" option leaves it running
        # for the user to close manually.
        pass

if __name__ == "__main__":
    launch_browser()
    print("\n--- The script has finished. Press Enter to close this window. ---")
    input()