# FILE: vision_capture.py
# VERSION: 16.19 - "The Client Spoofing Restoration Patch"
# 
# CHANGELOG:
# [2026-09-04 20:40] - v16.19: Reverted the Web Client Restoration Patch to allow yt-dlp to use 'tv', 'ios', and 'mweb' clients, bypassing YouTube's strict blocking of the 'web' client.
# [2026-09-03 13:01] - v16.18: Implemented Web Client Restoration Patch to force 'web' client for yt-dlp, synchronizing with audio_capture.py to prevent unnecessary Selenium fallbacks.
# [2026-09-02 15:09] - v16.17: Increased Selenium timeout to 45s and enhanced JS payload to forcefully nuke new YouTube UI overlays/cookie dialogs.

import sys
import os
import time
import re
import subprocess
import tempfile
import logging
import json
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent
VISION_LOG_FILE = ROOT / "vision_debug.txt"
CONFIG_FILE = ROOT / "birdnet_config.json"

v_logger = logging.getLogger("VisionEngine")
v_logger.setLevel(logging.INFO)
v_logger.propagate = False 

if not v_logger.handlers:
    formatter = logging.Formatter('%(asctime)s - [VISION] - %(message)s')
    file_handler = logging.FileHandler(VISION_LOG_FILE, mode='a', encoding='utf-8')
    file_handler.setFormatter(formatter)
    v_logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    v_logger.addHandler(stream_handler)

def extract_stream_url(raw_url, cookies_path=None, proxy_url=None, resolution="720", strict_proxy=False):
    clean_url = raw_url.replace(r"\u0026", "&").replace(r"\/", "/")
    clean_url = clean_url.replace("hhttps://", "https://")  
    clean_url = re.sub(r'[\?&]variant=\d+', '', clean_url)
    
    if ".m3u8" in clean_url or "manifest.googlevideo.com" in clean_url: 
        return clean_url, True,[]

    # --- EXTRACTION STRATEGY LOAD ---
    ext_strat = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            ext_strat = cfg.get("extraction_strategy", {})
        except: pass
        
    # --- CLIENT SPOOFING RESTORATION PATCH ---
    # Allow the original config list to flow directly to yt-dlp without stripping mobile clients
    clients = [c.strip() for c in ext_strat.get('player_client', 'tv, mweb, ios').split(',') if c.strip()]
    if not clients:
        clients = ['tv', 'mweb', 'ios', 'web'] # Robust fallback

    def try_extract(use_proxy, use_cookies):
        ydl_opts = {
            # --- FORMAT RELAX PATCH ---
            'format': f'bestvideo[height<={resolution}][protocol^=m3u8]/best[height<={resolution}][protocol^=m3u8]/bestvideo[height<={resolution}]/best[height<={resolution}]/best/bv/b',
            'quiet': True, 
            'socket_timeout': 15,
            'nocheckcertificate': True,
            'extractor_args': {'youtube': {'player_client': clients}},
            'js_runtimes': {'node': {}},
        }
        if use_proxy and proxy_url and proxy_url != "None": 
            ydl_opts['proxy'] = proxy_url
        if use_cookies and cookies_path and os.path.exists(cookies_path): 
            ydl_opts['cookiefile'] = cookies_path
            
        with yt_dlp.YoutubeDL(ydl_opts) as ydl: 
            info = ydl.extract_info(clean_url, download=False)
            
            if info and info.get('extractor', '').lower().startswith('youtube'):
                is_live = info.get('is_live')
                live_status = info.get('live_status')
                if is_live is False or live_status in['was_live', 'not_live', 'post_live']:
                    raise RuntimeError(f"VOD_REJECTED: Stream explicitly flagged as ended or VOD. Status: {live_status}")
                    
            extracted = info.get('url', clean_url).replace(r"\u0026", "&").replace(r"\/", "/")
            headers_dict = info.get('http_headers', {})
            header_list =[f"{k}: {v}" for k, v in headers_dict.items()]
            return extracted, header_list

    # ios client does not support cookies - skip cookies if ios is the primary client
    ios_is_primary = clients[0].strip().lower() == 'ios' if clients else False
    try:
        url, hdrs = try_extract(use_proxy=True, use_cookies=not ios_is_primary)
        return url, True, hdrs
    except Exception as e1:
        e1_str = str(e1).lower()
        if "vod_rejected" in e1_str: return None, False,[]
        
        # --- THE SNIFFER FALLBACK PATCH ---
        # Intercept YouTube Blocks - gracefully skip to Selenium Fallback instead of crashing
        if "no video formats found" in e1_str or "sign in to confirm you" in e1_str or "bot" in e1_str or "requested format is not available" in e1_str:
            v_logger.warning(f"YOUTUBE_BLOCK: yt-dlp hit a wall. Forcing direct Selenium capture...")
            return clean_url, True, []
            
        v_logger.warning(f"TIER 1 (Proxy+Cookies) failed: {e1_str[:150]}")
        
        try:
            v_logger.info("TIER 1.5: Retrying with Proxy, No Cookies...")
            url, hdrs = try_extract(use_proxy=True, use_cookies=False)
            return url, True, hdrs
        except Exception as e1_5:
            e1_5_str = str(e1_5).lower()
            if "vod_rejected" in e1_5_str: return None, False,[]
            v_logger.warning(f"TIER 1.5 (Proxy+No Cookies) failed: {e1_5_str[:150]}")
            
            if strict_proxy:
                v_logger.warning("Strict Proxy is ENABLED. Bypassing Tiers 2/3. Forcing Auto-Healer Fallback.")
                return clean_url, True,[]
            
            try:
                v_logger.info("TIER 2: Retrying WITHOUT Proxy (Anonymous)...")
                url, hdrs = try_extract(use_proxy=False, use_cookies=False)
                return url, False, hdrs
            except Exception as e2:
                e2_str = str(e2).lower()
                if "vod_rejected" in e2_str: return None, False,[]
                v_logger.warning(f"TIER 2 (No Proxy+Anonymous) failed: {e2_str[:150]}")
                try:
                    v_logger.info("TIER 3: Retrying WITHOUT Proxy AND WITH Cookies...")
                    url, hdrs = try_extract(use_proxy=False, use_cookies=True)
                    return url, False, hdrs
                except Exception as e3:
                    e3_str = str(e3).lower()
                    if "vod_rejected" in e3_str: return None, False,[]
                    v_logger.warning(f"TIER 3 Failed. Bypassing Selenium Intercept for YouTube...")
                    return clean_url, False,[]

def grab_video_frame(stream_url, output_path, headers_list=None, proxy_url=None):
    os.environ['NO_PROXY'] = 'localhost,127.0.0.1,::1'
    target_url = stream_url
    
    # --- EXTRACTION STRATEGY LOAD ---
    ext_strat = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            ext_strat = cfg.get("extraction_strategy", {})
        except: pass
        
    ua = ext_strat.get('ffmpeg_user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
    frame_quality = str(ext_strat.get('frame_quality', 2))
    
    # Only convert googlevideo manifest URLs back to watch URLs (for Selenium).
    # Regular .m3u8 HLS URLs must go straight to FFmpeg - do NOT convert them.
    if "manifest.googlevideo.com" in target_url:
        yt_match = re.search(r'/id/([^/.]+)', target_url)
        if yt_match:
            target_url = f"https://www.youtube.com/watch?v={yt_match.group(1)}"

    if "youtube.com" not in target_url and "youtu.be" not in target_url:
        try:
            ff_cmd =[
                str(ROOT / "ffmpeg" / "bin" / "ffmpeg.exe"), "-y", "-hide_banner", "-loglevel", "error",
                "-reconnect", "1", 
                "-reconnect_streamed", "1", 
                "-reconnect_delay_max", "5"
            ]
            if proxy_url and proxy_url != "None": ff_cmd.extend(["-http_proxy", proxy_url])
            
            header_str = f"User-Agent: {ua}\r\n"
            if headers_list:
                for h in headers_list:
                    if not h.lower().startswith("user-agent:"): header_str += f"{h}\r\n"
            ff_cmd.extend(["-headers", header_str])
            
            ff_cmd.extend(["-i", target_url, "-vframes", "1", "-q:v", frame_quality, "-t", "5", str(output_path)])
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            ff_proc = subprocess.Popen(ff_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=creation_flags)
            ff_proc.communicate(timeout=15)
            return output_path.exists()
        except: return False

    driver = None
    temp_profile_dir = None
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        
        cfg = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        driver_path = cfg.get("browser_automation", {}).get("webdriver_path")
        
        if not driver_path or not os.path.exists(driver_path):
            v_logger.error("Screenshot Engine: WebDriver path is not configured or missing.")
            return False
            
        # CRITICAL FIX: Use mkdtemp (directory) instead of mkstemp (file tuple)
        temp_profile_dir = tempfile.mkdtemp(prefix=f"vision_chrome_tmp_")
        
        opts = webdriver.ChromeOptions()
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--mute-audio")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-software-rasterizer")
        opts.add_argument("--autoplay-policy=no-user-gesture-required")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--remote-debugging-port=0") 
        opts.add_argument(f"--user-data-dir={temp_profile_dir}")
        opts.add_experimental_option("excludeSwitches",["enable-automation", "enable-logging"])
        opts.add_experimental_option('useAutomationExtension', False)
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument(f"--user-agent={ua}")
        
        if proxy_url and proxy_url != "None":
            clean_proxy = proxy_url.replace("http://", "").replace("https://", "")
            opts.add_argument(f'--proxy-server={clean_proxy}')
            opts.add_argument('--proxy-bypass-list=<-loopback>,127.0.0.1,localhost')
            
        service = Service(executable_path=driver_path)
        if os.name == 'nt':
            service.creation_flags = subprocess.CREATE_NO_WINDOW
        driver = webdriver.Chrome(service=service, options=opts)
        
        # --- THE COOKIE WALL BYPASS PATCH ---
        try:
            driver.execute_cdp_cmd('Network.enable', {})
            driver.execute_cdp_cmd('Network.setCookie', {'domain': '.youtube.com', 'name': 'CONSENT', 'value': 'YES+cb', 'path': '/'})
            driver.execute_cdp_cmd('Network.setCookie', {'domain': '.youtube.com', 'name': 'SOCS', 'value': 'CAI', 'path': '/'})
        except Exception as e:
            v_logger.warning(f"Could not inject CDP bypass cookies: {e}")
            
        # INCREASED TIMEOUT: From 30s to 45s for slower cellular proxies
        driver.set_page_load_timeout(45)
        try: driver.get(target_url)
        except: pass
        
        # --- THE AUTO-PLAY OVERRIDE PATCH & COOKIE BANNER FIX ---
        js_payload = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        
        // Aggressively hide all UI overlays that might block the play button or dirty the screenshot
        let style = document.createElement('style');
        style.textContent = `
            body, html { background: black !important; overflow: hidden !important; }
            .ytp-chrome-top, .ytp-chrome-bottom, .ytp-watermark, .ytp-show-cards-title, .ytp-ce-element, .ytp-ad-module, .ytp-pause-overlay, .ytp-progress-bar-container, .ytp-progress-bar, .ytp-play-progress, .ytp-load-progress, .ytp-scrubber-container, .ytp-chapters-container, .ytp-heat-map-container, .ytp-error { display: none !important; opacity: 0 !important; pointer-events: none !important; }
            /* Hide modal backdrops and popups entirely */
            ytd-popup-container, tp-yt-iron-overlay-backdrop, tp-yt-paper-dialog, ytd-consent-bump-v2-lightbox, iron-overlay-backdrop, ytd-action-companion-ad-renderer, ytd-promoted-sparkles-web-renderer { display: none !important; opacity: 0 !important; pointer-events: none !important; }
            video { 
                position: fixed !important; 
                top: 0 !important; 
                left: 0 !important; 
                width: 100vw !important; 
                height: 100vh !important; 
                z-index: 2147483647 !important; 
                object-fit: contain !important;
                background: black !important; 
                margin: 0 !important;
                padding: 0 !important;
                transform: none !important;
            }
        `;
        document.documentElement.appendChild(style);

        setInterval(function() {
            // Nuke dialogs aggressively from DOM
            document.querySelectorAll('ytd-popup-container, tp-yt-iron-overlay-backdrop, tp-yt-paper-dialog, ytd-consent-bump-v2-lightbox, iron-overlay-backdrop, div[role="dialog"], .ytp-ad-overlay-container, .yt-spec-button-shape-next--filled').forEach(el => el.remove());
            
            let v = document.querySelector('video');
            if (v && v.paused) {
                v.muted = true;
                v.play().catch(e=>{});
            }
            
            let largePlayBtn = document.querySelector('.ytp-large-play-button');
            if (largePlayBtn) largePlayBtn.click();
            
            let playBtn = document.querySelector('button.ytp-play-button');
            if (playBtn) playBtn.click();
            
            let clickToPlay = document.querySelector('.ytp-click-to-play');
            if (clickToPlay) clickToPlay.click();
            
            // AGGRESSIVE COOKIE BANNER DISMISSAL (Buttons, Links, and Spans)
            let clickables = document.querySelectorAll('button, a, span, [role="button"]');
            clickables.forEach(b => {
                let txt = (b.innerText || '').toLowerCase();
                if(txt.includes('accept') || txt.includes('agree') || txt.includes('reject') || txt.includes('got it') || txt.includes('skip ad')) {
                    b.click();
                }
            });
        }, 300);
        """
        driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': js_payload})

        import time
        video_element = None
        # INCREASED POLLING ATTEMPTS: From 15 to 25 to allow video to buffer and start
        for attempt in range(25):
            try:
                # Actively force play on every poll attempt via execute_script
                driver.execute_script("""
                    let v = document.querySelector('video');
                    if (v) {
                        v.muted = true;
                        v.play().catch(e=>{});
                    }
                """)
            except: pass
            
            try:
                # STRICT PROOF OF PLAY: Must be actively advancing frames
                is_playing = driver.execute_script("let v = document.querySelector('video'); return v && v.readyState >= 2 && v.currentTime > 0;")
                if is_playing:
                    time.sleep(1.0)
                    video_element = driver.find_element(By.CSS_SELECTOR, "video")
                    break
            except: pass
            time.sleep(1.0)
        
        if video_element:
            video_element.screenshot(str(output_path))
            return output_path.exists()
        else:
            v_logger.warning("Screenshot Engine: Video never started (Stuck on Cover Page/Thumbnail). Aborting capture to prevent AI hallucination.")
            return False
            
    except Exception as e:
        v_logger.error(f"Screenshot Engine crash: {e}")
        return False
    finally:
        if driver:
            try: driver.quit()
            except: pass
        if temp_profile_dir:
            try:
                import shutil
                shutil.rmtree(temp_profile_dir, ignore_errors=True)
            except: pass