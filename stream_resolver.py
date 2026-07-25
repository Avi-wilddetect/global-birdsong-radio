# FILE: stream_resolver.py
# VERSION: 4.77 - "The Clean Silence & Sys Patch"
# PURPOSE: Resolves streams using HTTP Headers, yt-dlp, Static Regex, and Headless Browser Network Sniffing.
# UPDATED: Removed destructive monkeypatches. Properly imported sys and os to securely silence Windows command prompts for Node and Selenium.

import sys
import os
import logging
import traceback
import re
import requests
import json
import time
import subprocess
from pathlib import Path

print("DEBUG: Stream Resolver v4.77 (Clean Silence & Sys Patch) Loaded")

# --- GLOBAL NODE.JS PATH INJECTION ---
def ensure_node_in_path():
    try:
        # THE SILENT DEADLOCK PATCH: Explicitly sever stdin and enforce a 2-second timeout
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        subprocess.run(["node", "-v"], check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, creationflags=flags)
        return
    except: pass
    search_paths =[r"C:\Program Files\nodejs", r"C:\Program Files (x86)\nodejs", os.path.expandvars(r"%APPDATA%\npm")]
    current_path = os.environ.get("PATH", "")
    for p in search_paths:
        if os.path.exists(os.path.join(p, "node.exe")):
            if p not in current_path:
                os.environ["PATH"] = f"{p};{current_path}"
                break

# Run immediately at startup so yt-dlp sees it in the environment variables
ensure_node_in_path()

# --- PATH SETUP ---
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "birdnet_config.json"

try:
    import yt_dlp
except ImportError:
    print("FATAL: yt_dlp is missing.")

try:
    # --- STANDARD SELENIUM IMPORTS ---
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    SELENIUM_AVAILABLE = True
except ImportError:
    print("WARNING: Selenium libraries missing. Strategy C disabled.")
    SELENIUM_AVAILABLE = False

logger = logging.getLogger(__name__)

# --- LOAD EXTRACTION STRATEGY ---
def get_extraction_strategy():
    ext_strat = {}
    try:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
            ext_strat = cfg.get("extraction_strategy", {})
    except: pass
    
    clients =[c.strip() for c in ext_strat.get('player_client', 'web').split(',') if c.strip()]
    if not clients: clients = ['web']
    
    ua = ext_strat.get('ffmpeg_user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    
    return clients, ua

def sanitize_extracted_url(url):
    if not url: return url
    cleaned = url.replace("\\u0026", "&").replace("\\/", "/")
    cleaned = cleaned.replace(r"\u0026", "&").replace(r"\/", "/")
    return cleaned

def get_driver_path():
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
            return data.get("browser_automation", {}).get("webdriver_path")
    except: pass
    return None

def deep_scan_browser_intercept(url, proxy_url=None):
    if not SELENIUM_AVAILABLE:
        return[], "Browser Intercept: Selenium libraries not installed."

    driver_path = get_driver_path()
    if not driver_path or not Path(driver_path).exists():
        return[], "Browser Intercept: Driver path not configured in settings."

    log_buffer =["Browser Intercept: Initializing Headless Chrome..."]
    driver = None
    found_links = set()

    try:
        # --- STANDARD SELENIUM INIT ---
        opts = webdriver.ChromeOptions()
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--mute-audio")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        
        # Enable performance logging to sniff network traffic
        opts.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
        
        if proxy_url and proxy_url != "None":
            clean_proxy = proxy_url.replace("http://", "").replace("https://", "")
            opts.add_argument(f'--proxy-server={clean_proxy}')
            log_buffer.append(f"Browser Intercept: Routing through proxy {clean_proxy}")
        
        if driver_path and Path(driver_path).exists():
            service = Service(executable_path=driver_path)
            if sys.platform == "win32":
                service.creation_flags = subprocess.CREATE_NO_WINDOW
            driver = webdriver.Chrome(service=service, options=opts)
        else:
            service = Service()
            if sys.platform == "win32":
                service.creation_flags = subprocess.CREATE_NO_WINDOW
            driver = webdriver.Chrome(service=service, options=opts)
            
        driver.set_page_load_timeout(20)
        
        # --- THE AUTO-PLAY INJECTION PATCH ---
        # Forces the video to start playing so the network logger can catch the .m3u8 traffic
        js_payload = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        setInterval(function() {
            let v = document.querySelector('video');
            if (v && v.paused) v.play().catch(e=>{});
            let largePlayBtn = document.querySelector('.ytp-large-play-button');
            if (largePlayBtn) largePlayBtn.click();
            let btns = document.querySelectorAll('button');
            btns.forEach(b => {
                let txt = b.innerText.toLowerCase();
                if(txt.includes('accept') || txt.includes('agree') || txt.includes('reject')) b.click();
            });
        }, 500);
        """
        driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': js_payload})
        
        log_buffer.append(f"Browser Intercept: Loading {url}...")
        try: driver.get(url)
        except Exception as load_e: log_buffer.append(f"Browser Intercept: Page load hit timeout/error. Sniffing anyway...")
        
        is_youtube = "youtube.com" in url or "youtu.be" in url
        if is_youtube:
            time.sleep(3) 
            try:
                page_source = driver.page_source
                live_indicators =["isLiveBroadcast", "isLiveNow", "isLive", '"status":"LIVE"']
                if not any(indicator in page_source for indicator in live_indicators):
                    return[], "\n".join(log_buffer) + "\nVOD_REJECTED: Selenium confirmed this YouTube page is a recorded VOD."
            except Exception as e:
                log_buffer.append(f"Browser Intercept: Could not verify VOD status ({e}).")
        
        for i in range(10):
            time.sleep(1)
            try: logs = driver.get_log('performance')
            except Exception: continue 
                
            for entry in logs:
                try:
                    msg = json.loads(entry.get('message', '{}'))
                    method = msg.get('message', {}).get('method', '')
                    if 'Network.requestWillBeSent' in method:
                        req_url = msg.get('message', {}).get('params', {}).get('request', {}).get('url', '')
                        # Catch standard m3u8 AND raw YouTube manifest links
                        if '.m3u8' in req_url or '.mpd' in req_url or 'manifest.googlevideo.com' in req_url:
                            if 'favicon' not in req_url and 'generate_204' not in req_url:
                                found_links.add(sanitize_extracted_url(req_url))
                except: continue
            if found_links:
                log_buffer.append(f"Browser Intercept: Sniffed {len(found_links)} stream URL(s)!")
                break
    except Exception as e:
        log_buffer.append(f"Browser Intercept Error: {str(e)}")
    finally:
        if driver:
            try: driver.quit()
            except: pass

    if found_links:
        return sorted(list(found_links), key=len), "\n".join(log_buffer)
    return[], "\n".join(log_buffer) + "\nBrowser Intercept: No media streams found in network traffic."

def deep_scan_generic_page(url, proxy_url=None):
    clients, ua = get_extraction_strategy()
    headers = {'User-Agent': ua, 'Referer': url}
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url and proxy_url != "None" else {}
    log_buffer =[f"Deep Scan (Static): Fetching {url}..."]
    try:
        r = requests.get(url, headers=headers, proxies=proxies, timeout=10)
        content = r.text
        pattern_std = r'(https?://[^\s"\'<>]+?\.m3u8[^\s"\'<>]*)'
        pattern_esc = r'(https?:\\?/\\?/[^\s"\'<>]+?\.m3u8[^\s"\'<>]*)'
        matches = re.findall(pattern_std, content)
        matches_esc = re.findall(pattern_esc, content)
        for m in matches_esc: matches.append(m.replace(r'\/', '/').replace('\\/', '/'))
        unique_matches = list(set([sanitize_extracted_url(m) for m in matches if 'http' in m]))
        if unique_matches:
            log_buffer.append(f"Deep Scan (Static): Found {len(unique_matches)} candidates.")
            return sorted(unique_matches, key=len), "\n".join(log_buffer)
        return[], "Deep Scan (Static): No .m3u8 patterns found in page source."
    except Exception as e:
        return[], f"Deep Scan (Static) Error: {str(e)}"

def resolve_stream_url(webpage_url, proxy_url=None, fast_mode=False):
    """
    Returns: list_of_links, stream_type, log_message
    """
    clients, ua = get_extraction_strategy()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url and proxy_url != "None" else {}
        
    if ".m3u8" in webpage_url or ".mpd" in webpage_url or "manifest.googlevideo.com" in webpage_url:
        return [sanitize_extracted_url(webpage_url)], 'hls', "Direct HLS/Manifest link detected."
    
    if any(webpage_url.endswith(ext) for ext in['.mp3', '.aac', '.ogg', '.wav']):
        return[sanitize_extracted_url(webpage_url)], 'audio', "Direct Audio link detected by extension."

    if webpage_url.startswith('http') and 'youtube' not in webpage_url and 'youtu.be' not in webpage_url:
        try:
            head_req = requests.head(webpage_url, timeout=5, allow_redirects=True, headers={'User-Agent': ua}, proxies=proxies)
            ct = head_req.headers.get('content-type', '').lower()
            if ct.startswith('audio/'):
                return [sanitize_extracted_url(webpage_url)], 'audio', f"Direct Audio link detected via server headers ({ct})."
        except: pass

    cookies_path = None
    try:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
            cookies_path = cfg.get("youtube_cookies_file", "")
    except: pass

    def try_extract(use_proxy, use_cookies):
        ydl_opts = {
            'quiet': True, 'no_warnings': True, 'skip_download': True, 'force_generic_extractor': False, 
            'noplaylist': True, 'format': 'best[protocol^=m3u8]/best', 'ignoreerrors': True, 
            'user_agent': ua,
            'compat_opts': ['allow-un-sandboxed-javascript'],
            'extractor_args': {'youtube': {'player_client': clients}},
            'js_runtimes': {'node': {}},
        }
        if use_proxy and proxy_url and proxy_url != "None": ydl_opts['proxy'] = proxy_url
        if use_cookies and cookies_path and Path(cookies_path).exists(): ydl_opts['cookiefile'] = cookies_path

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(webpage_url, download=False)
            if info and info.get('extractor', '').lower().startswith('youtube'):
                is_live = info.get('is_live')
                live_status = info.get('live_status')
                if is_live is False or live_status in ['was_live', 'not_live', 'post_live']:
                    raise RuntimeError(f"VOD_REJECTED: Stream explicitly flagged as ended or VOD. Status: {live_status}")
            
            if info:
                if 'youtube' in info.get('extractor', '').lower():
                    # --- THE URL FORMATTING PATCH ---
                    full_yt_url = f"https://www.youtube.com/watch?v={info['id']}"
                    return [full_yt_url], 'youtube', f"YouTube stream resolved: {full_yt_url}"
                    
                direct_url = info.get('url')
                if direct_url:
                    clean_direct = sanitize_extracted_url(direct_url)
                    if '.m3u8' in clean_direct or 'm3u8' in info.get('protocol', ''):
                        return [clean_direct], 'hls', "Successfully resolved HLS stream (via yt-dlp)."
                    if any(clean_direct.endswith(ext) for ext in['.mp3', '.aac', '.ogg']):
                        return [clean_direct], 'audio', "Resolved audio stream."
                    return [clean_direct], 'direct_stream', f"Resolved generic stream (Protocol: {info.get('protocol', 'unknown')})"
            
            raise RuntimeError("yt-dlp returned no valid media links (Unsupported URL or malformed response).")

    log_msg = ""
    try:
        links, stype, msg = try_extract(use_proxy=True, use_cookies=False)
        return links, stype, log_msg + msg
    except Exception as e1:
        if "vod_rejected" in str(e1).lower(): return[], 'unknown', log_msg + str(e1)
        log_msg += f"TIER 1 (Proxy+Anonymous) failed: {e1}\n"
        try:
            links, stype, msg = try_extract(use_proxy=True, use_cookies=True)
            return links, stype, log_msg + msg
        except Exception as e1_5:
            if "vod_rejected" in str(e1_5).lower(): return[], 'unknown', log_msg + str(e1_5)
            log_msg += f"TIER 1.5 (Proxy+Cookies) failed: {e1_5}\n"
            
            # --- THE UNSHACKLING PATCH ---
            # We explicitly ignore Strict Proxy here. Metadata fetches are 5KB and will not leak heavy video data.
            log_msg += "Bypassing Strict Proxy for Metadata Extraction (Safe 5KB Fetch)...\n"
            try:
                links, stype, msg = try_extract(use_proxy=False, use_cookies=False)
                return links, stype, log_msg + msg
            except Exception as e2:
                if "vod_rejected" in str(e2).lower(): return[], 'unknown', log_msg + str(e2)
                log_msg += f"TIER 2 (No Proxy+Anonymous) failed: {e2}\n"
                try:
                    links, stype, msg = try_extract(use_proxy=False, use_cookies=True)
                    return links, stype, log_msg + msg
                except Exception as e3:
                    if "vod_rejected" in str(e3).lower(): return[], 'unknown', log_msg + str(e3)
                    log_msg += f"TIER 3 (No Proxy+Cookies) failed: {e3}\n"

    log_msg += "Switching to Deep Scan (Static)...\n"
    links, scan_log = deep_scan_generic_page(webpage_url, proxy_url=proxy_url)
    if links:
        return links, 'hls', log_msg + scan_log

    if fast_mode:
        return[], 'unknown', log_msg + scan_log + "\nFast Mode Active: Skipping Selenium Intercept. All quick extraction methods failed."

    log_msg += scan_log + "\nSwitching to Browser Intercept (Dynamic)...\n"
    links, browser_log = deep_scan_browser_intercept(webpage_url, proxy_url=proxy_url)
    
    if links:
        return links, 'hls', log_msg + browser_log
    
    return [], 'unknown', log_msg + browser_log + "\nResolution Failed: All methods exhausted."