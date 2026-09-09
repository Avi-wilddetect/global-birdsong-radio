# FILE: yt_sync_engine.py
# VERSION: 6.14 - "The Scheduled Stream Blockade Patch"
# PURPOSE: Compares local YouTube stream metadata against live channel data to detect URL changes, Title/Description changes, new streams, and resurrect dead streams.
# CHANGELOG:
# [2026-09-10 00:55] - v6.14: Bulletproofed scheduled stream rejection by filtering out all non-'is_live' states in Tier 1 and hooking directly into the language-agnostic 'UPCOMING' and 'PREMIERE' style badges in Tier 2.
# [2026-09-10 00:04] - v6.13: Patched channel scraper to explicitly reject 'is_upcoming' / 'UPCOMING' scheduled streams in both yt-dlp and HTML tiers, preventing future broadcasts from inflating live counts and clogging the queue.
# [2026-09-09 23:25] - v6.12: Added _get_safe_cname helper to aggressively preserve existing channel names if YouTube scraping temporarily returns 'Unknown Channel'.

import json
import logging
import time
import difflib
import re
import traceback
import requests
from pathlib import Path
from collections import Counter

try:
    import yt_dlp
except ImportError:
    pass

# --- CONFIGURATION ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s -[SYNC ENGINE] - %(message)s')

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "birdnet_config.json"
METADATA_CACHE_FILE = ROOT / "youtube_metadata_cache.json"
SYNC_PROPOSALS_FILE = ROOT / "sync_proposals.json"
TRACEBACK_LOG_FILE = ROOT / "sync_engine_tracebacks.log"
DEBUG_DUMP_FILE = ROOT / "sync_debug_dump.txt"

class YouTubeSyncEngine:
    def __init__(self, cookies_path=None):
        self.config = self._load_json(CONFIG_FILE)
        self.cache = self._load_json(METADATA_CACHE_FILE)
        self.cookies_path = cookies_path or self.config.get("youtube_cookies_file", "")
        self.known_channels = self.config.get("channels", {})
        self.live_channel_data = {}  
        self.proposals = []

    def _debug_log(self, msg):
        try:
            with open(DEBUG_DUMP_FILE, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except: pass

    def _dump_traceback(self, context_msg):
        try:
            with open(TRACEBACK_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {context_msg}\n")
        except: pass

    def _load_json(self, filepath):
        if not filepath.exists(): return {}
        with open(filepath, 'r', encoding='utf-8') as f: return json.load(f)

    def _save_cache(self):
        try:
            with open(METADATA_CACHE_FILE, 'w', encoding='utf-8') as f: json.dump(self.cache, f, indent=2)
        except Exception as e:
            self._dump_traceback(f"Failed to save metadata cache: {e}")

    def _add_to_ignored_channels(self, channel_url):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f: cfg = json.load(f)
            if "ignored_channels" not in cfg: cfg["ignored_channels"] = []
            if channel_url not in cfg["ignored_channels"]:
                cfg["ignored_channels"].append(channel_url)
                tmp_file = CONFIG_FILE.with_suffix('.tmp')
                with open(tmp_file, 'w', encoding='utf-8') as f: json.dump(cfg, f, indent=2)
                tmp_file.replace(CONFIG_FILE)
                self.config = cfg 
        except Exception as e:
            self._dump_traceback(f"Failed to auto-ignore channel {channel_url}: {e}")

    def _update_ignored_proposals(self, new_ignored_dict):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f: cfg = json.load(f)
            if "sync_engine_settings" not in cfg: cfg["sync_engine_settings"] = {}
            cfg["sync_engine_settings"]["ignored_proposals"] = new_ignored_dict
            tmp_file = CONFIG_FILE.with_suffix('.tmp')
            with open(tmp_file, 'w', encoding='utf-8') as f: json.dump(cfg, f, indent=2)
            tmp_file.replace(CONFIG_FILE)
            self.config = cfg 
        except Exception as e:
            self._dump_traceback(f"Failed to update ignored proposals: {e}")

    def _calculate_similarity(self, text1, text2):
        if not text1 or not text2: return 0.0
        def extract_dna(t):
            t = t.lower()
            t = re.sub(r'https?://\S+', '', t)
            t = re.sub(r'\b\d{2,4}[-/]\d{2}[-/]\d{2,4}\b', '', t)
            t = re.sub(r'\b\d{1,2}:\d{2}(:\d{2})?\b', '', t)
            noise_words = ['🔴', 'live', 'stream', '24/7', 'broadcast', 'welcome', 'subscribe', 'chat', 'official']
            for w in noise_words: t = t.replace(w, '')
            t = re.sub(r'[^\w\s]', ' ', t)
            return re.sub(r'\s+', ' ', t).strip()

        dna1 = extract_dna(text1)
        dna2 = extract_dna(text2)
        if not dna1 and not dna2: return 0.0
        return difflib.SequenceMatcher(None, dna1, dna2).ratio()

    def _get_ydl_opts(self, extract_flat=False):
        ext_strat = self.config.get("extraction_strategy", {})
        clients = [c.strip() for c in ext_strat.get('player_client', 'web').split(',') if c.strip()]
        if not clients: clients = ['web']
        ua = ext_strat.get('ffmpeg_user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
        ydl_opts = {
            'quiet': True, 'no_warnings': True, 'skip_download': True,
            'socket_timeout': 15, 'user_agent': ua,
            'compat_opts': ['allow-un-sandboxed-javascript'],
            'extractor_args': {'youtube': {'player_client': clients}},
            'js_runtimes': {'node': {}}
        }
        if extract_flat: ydl_opts['extract_flat'] = True
        if self.cookies_path and Path(self.cookies_path).exists(): 
            ios_is_primary = clients[0].lower() == 'ios' if clients else False
            if not ios_is_primary: ydl_opts['cookiefile'] = self.cookies_path
        return ydl_opts

    def _fetch_actual_title(self, url):
        try:
            import yt_dlp
            ydl_opts = self._get_ydl_opts()
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info and 'title' in info: return info['title']
        except Exception as e:
            self._dump_traceback(f"Failed to fetch actual title for {url}: {str(e)[:100]}")
        return None

    def _check_if_actually_live(self, url):
        try:
            import yt_dlp
            ydl_opts = self._get_ydl_opts()
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    is_live = info.get('is_live')
                    live_status = info.get('live_status')
                    if is_live is True or live_status == 'is_live': return True, "Alive"
                    return False, f"Dead (is_live={is_live}, status={live_status})"
                return False, "Dead (No info returned)"
        except Exception as e:
            err = str(e)[:100]
            self._dump_traceback(f"Failsafe ping error for {url}: {err}")
            return False, f"Error: {err}"

    def _scrape_channel_live_tab(self, channel_url):
        if not channel_url: return []
        
        # --- ROBUST URL SANITIZATION PATCH ---
        # 1. Extract only the first line if the URL contains accidental line-breaks
        clean_channel_url = channel_url.split('\n')[0].replace('\r', '').strip()
        
        # 2. If the URL was accidentally pasted twice (e.g., http...http...), isolate the first one
        if clean_channel_url.count("http") > 1:
            parts = clean_channel_url.split("http")
            clean_channel_url = "http" + parts[1]
            
        # 3. Strip ?si= or any other tracking parameters before appending /streams
        clean_channel_url = clean_channel_url.split('?')[0].strip()
        
        target_url = clean_channel_url if clean_channel_url.endswith("/streams") else clean_channel_url.rstrip("/") + "/streams"
        live_streams = []
        seen_urls = set()
        
        # --- TIER 1: YT-DLP EXTRACTION (Bulletproof VOD Filtering) ---
        try:
            import yt_dlp
            ydl_opts = self._get_ydl_opts(extract_flat=True)
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(target_url, download=False)
                if info and 'entries' in info:
                    for entry in info['entries']:
                        if not entry: continue
                        
                        # VOD CHECK: Live streams typically have NO duration in flat extraction.
                        # If duration is populated, it is almost certainly a VOD. Skip it.
                        if entry.get('duration') is not None:
                            continue
                            
                        # Strict live check to kill 'is_upcoming' / scheduled streams
                        live_status = entry.get('live_status')
                        if live_status and live_status != 'is_live': 
                            continue
                            
                        vid_id = entry.get('id')
                        if not vid_id: continue
                        
                        url = f"https://www.youtube.com/watch?v={vid_id}"
                        clean_url = re.sub(r'[\?&]variant=\d+', '', url).strip()
                        
                        if clean_url in seen_urls: continue
                        seen_urls.add(clean_url)
                        
                        title = entry.get('title', '')
                        desc = entry.get('description', '')
                        uploader = entry.get('uploader') or entry.get('channel', 'Unknown Channel')
                        
                        live_streams.append({
                            'url': url, 'title': title, 'description': desc,
                            'channel_url': channel_url, 'channel_name': uploader
                        })
                        
            if live_streams:
                self._debug_log(f"Tier 1 (yt-dlp) found {len(live_streams)} live streams on {channel_url}")
                return live_streams
            else:
                self._debug_log(f"Tier 1 (yt-dlp) completed but found 0 live streams. Falling back to Tier 2 HTML.")
        except Exception as e:
            self._dump_traceback(f"Tier 1 yt-dlp extraction failed for {target_url}: {e}")

        # --- TIER 2: HTML REGEX FALLBACK (Smashes Cookie Wall) ---
        self._debug_log(f"Executing Tier 2 HTML scraping for {channel_url}")
        
        # Use a full session and inject Google consent cookies to bypass the EU wall
        session = requests.Session()
        session.cookies.set('CONSENT', 'YES+cb', domain='.youtube.com')
        session.cookies.set('SOCS', 'CAI', domain='.youtube.com')
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36', 
            'Accept-Language': 'en-US,en;q=0.9'
        }
        
        try:
            res = session.get(target_url, headers=headers, timeout=15)
            res.raise_for_status()
            html = res.text

            # Bulletproof single-line JSON extractor
            data = None
            for line in html.splitlines():
                if 'ytInitialData' in line and ('window["ytInitialData"] =' in line or 'var ytInitialData =' in line):
                    idx = line.find('ytInitialData')
                    start = line.find('{', idx)
                    end = line.rfind('}') + 1
                    if start != -1 and end > start:
                        try:
                            data = json.loads(line[start:end])
                            break
                        except: pass
            
            if not data:
                self._dump_traceback(f"Could not parse ytInitialData from HTML for {target_url}. Google may be serving a bot-challenge page.")
                return []

            def find_videos(obj):
                videos = []
                if isinstance(obj, dict):
                    if 'videoRenderer' in obj: videos.append(obj['videoRenderer'])
                    for k, v in obj.items(): videos.extend(find_videos(v))
                elif isinstance(obj, list):
                    for item in obj: videos.extend(find_videos(item))
                return videos

            tabs = data.get('contents', {}).get('twoColumnBrowseResultsRenderer', {}).get('tabs', [])
            target_tab_content = None
            for tab in tabs:
                tab_renderer = tab.get('tabRenderer', {})
                if tab_renderer.get('selected', False) or 'streams' in tab_renderer.get('endpoint', {}).get('commandMetadata', {}).get('webCommandMetadata', {}).get('url', ''):
                    target_tab_content = tab_renderer.get('content')
                    break
            
            if not target_tab_content: target_tab_content = data 
                
            video_renderers = find_videos(target_tab_content)
            
            header_node = data.get('header', {})
            channel_name_fallback = 'Unknown Channel'
            if 'c4TabbedHeaderRenderer' in header_node:
                channel_name_fallback = header_node['c4TabbedHeaderRenderer'].get('title', 'Unknown Channel')
            elif 'pageHeaderRenderer' in header_node:
                channel_name_fallback = header_node['pageHeaderRenderer'].get('pageTitle', 'Unknown Channel')
            
            if channel_name_fallback == 'Unknown Channel':
                metadata_node = data.get('metadata', {}).get('channelMetadataRenderer', {})
                channel_name_fallback = metadata_node.get('title', 'Unknown Channel')

            for video in video_renderers:
                is_live = False
                is_upcoming = False
                
                # Check for explicit language-agnostic style badges
                for overlay in video.get('thumbnailOverlays', []):
                    style = overlay.get('thumbnailOverlayTimeStatusRenderer', {}).get('style', '')
                    if style in ['UPCOMING', 'PREMIERE']:
                        is_upcoming = True
                    elif style == 'LIVE':
                        is_live = True
                        
                for badge in video.get('badges', []):
                    style = badge.get('metadataBadgeRenderer', {}).get('style', '')
                    label = badge.get('metadataBadgeRenderer', {}).get('label', '')
                    if style == 'BADGE_STYLE_TYPE_LIVE_NOW' or label == 'LIVE' or label == 'Live':
                        is_live = True
                    elif label == 'UPCOMING' or label == 'Upcoming' or 'UPCOMING' in style:
                        is_upcoming = True

                view_str = str(video.get('viewCountText', '')).lower()
                pub_str = str(video.get('publishedTimeText', '')).lower()
                
                # Check for textual indicators like "Scheduled for..." or "Premieres in..."
                if "views" in view_str or "waiting" in view_str or "streamed" in view_str or "streamed" in pub_str or "scheduled" in pub_str or "premieres" in pub_str:
                    is_live = False 
                    
                if is_upcoming:
                    is_live = False
                
                if not is_live: continue

                vid_id = video.get('videoId')
                if not vid_id: continue
                url = f"https://www.youtube.com/watch?v={vid_id}"
                
                clean_url = re.sub(r'[\?&]variant=\d+', '', url).strip()
                if clean_url in seen_urls: continue
                seen_urls.add(clean_url)

                title = ""
                title_runs = video.get('title', {}).get('runs', [])
                if title_runs: title = "".join([run.get('text', '') for run in title_runs])

                desc = ""
                desc_runs = video.get('descriptionSnippet', {}).get('runs', [])
                if desc_runs: desc = "".join([run.get('text', '') for run in desc_runs])
                    
                uploader = video.get('ownerText', {}).get('runs', [{}])[0].get('text')
                if not uploader: uploader = channel_name_fallback

                live_streams.append({
                    'url': url, 'title': title, 'description': desc,
                    'channel_url': channel_url, 'channel_name': uploader
                })
                
            self._debug_log(f"Tier 2 (HTML) found {len(live_streams)} live streams on {channel_url}")
        except Exception as e:
            self._dump_traceback(f"Failed to scrape HTML for channel {target_url}: {e}")
            
        return live_streams

    def _fugitive_search(self, title_query):
        try: import yt_dlp
        except ImportError: return [] 
            
        ydl_opts = self._get_ydl_opts(extract_flat=True)
        clean_query = re.sub(r'🔴|\[.*?\]|\(.*?\)', '', title_query).strip()
        search_target = f"ytsearch5:{clean_query} live"
        results, seen_urls = [], set()
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search_target, download=False)
                if info and 'entries' in info:
                    for entry in info['entries']:
                        if not entry: continue
                        live_status = entry.get('live_status')
                        if live_status and live_status != 'is_live': continue
                        url = entry.get('url', '')
                        if not url.startswith('http'): url = f"https://www.youtube.com/watch?v={url}"
                        clean_url = re.sub(r'[\?&]variant=\d+', '', url).strip()
                        if clean_url in seen_urls: continue
                        seen_urls.add(clean_url)
                        results.append({ 'url': url, 'title': entry.get('title', ''), 'description': entry.get('description', ''), 'channel_name': entry.get('uploader', 'Unknown Channel'), 'channel_url': entry.get('uploader_url', '') })
        except Exception as e:
            self._dump_traceback(f"Fugitive search failed for '{clean_query}': {e}")
        return results

    def run_sync(self, progress_callback=None, target_channels=None):
        try:
            with open(DEBUG_DUMP_FILE, "w", encoding="utf-8") as f:
                f.write("--- SYNC ENGINE DEBUG DUMP ---\n")
        except: pass
        
        try:
            self._run_sync_internal(progress_callback, target_channels)
        except Exception as e:
            err_trace = traceback.format_exc()
            self._debug_log(f"\nCRASH TRACEBACK:\n{err_trace}")
            logging.error(f"Sync Engine crashed: {e}")
            raise e  

    def _run_sync_internal(self, progress_callback, target_channels):
        
        # --- THE UNKNOWN CHANNEL PRESERVATION PATCH ---
        def _get_safe_cname(new_name, old_name):
            bad_names = ["Unknown Channel", "Unknown", "", None]
            n = str(new_name).strip() if new_name else ""
            o = str(old_name).strip() if old_name else ""
            if n in bad_names:
                if o not in bad_names:
                    return o
            return n if n else "Unknown Channel"
        # ----------------------------------------------

        self._debug_log(f"--- ENGINE STARTING _run_sync_internal ---")
        self._debug_log(f"Explicit target_channels passed: {target_channels}")
        
        if not self.config:
            msg = "Missing config."
            self._debug_log(f"ERROR: {msg}")
            logging.error(msg)
            if progress_callback: progress_callback(0, 1, msg)
            return
            
        if not isinstance(self.cache, dict): self.cache = {}

        sync_settings = self.config.get("sync_engine_settings", {})
        max_channel_streams = sync_settings.get("max_channel_streams", 15)
        channel_rules = self.config.get("channel_sync_rules", {})
        ignored_channels = set(self.config.get("ignored_channels", []))
        
        ignored_proposals = sync_settings.get("ignored_proposals", {})
        ignored_proposals_changed = False

        channels_to_scan = {}
        if target_channels:
            # Force inclusion of all target_channels even if not in known_channels yet
            for t_url in target_channels:
                found_name = "Unknown Channel"
                for c_name, c_url in self.known_channels.items():
                    if c_url == t_url:
                        found_name = c_name
                        break
                channels_to_scan[found_name] = t_url
        else:
            channels_to_scan = self.known_channels.copy()
            
        self._debug_log(f"Channels determined for scanning: {channels_to_scan}")

        streams_to_evaluate = self.cache.get("streams", {}).copy()
        for s in self.config.get("streams", []):
            s_url = s.get("page_url", "")
            if not s_url: continue
            
            s_type = s.get("stream_type", "youtube")
            is_yt = s_type == "youtube" or "youtube.com" in s_url or "youtu.be" in s_url
            if not is_yt: continue
            
            if s_url not in streams_to_evaluate:
                c_name = s.get("channel_name", "Unknown Channel")
                c_url = self.known_channels.get(c_name, "")
                
                streams_to_evaluate[s_url] = {
                    "channel_url": c_url,
                    "friendly_name": s.get("name", "Unknown"),
                    "title": s.get("name", "Unknown"),
                    "channel_name": c_name
                }

        total_channels = len(channels_to_scan)
        if target_channels:
            total_streams = sum(1 for s in streams_to_evaluate.values() if s.get('channel_url') in target_channels)
        else:
            total_streams = len(streams_to_evaluate)
            
        total_steps = total_channels + total_streams

        if total_steps == 0:
            msg = "No valid channels or streams to sync."
            self._debug_log(f"ABORTING: {msg}")
            if progress_callback: progress_callback(1, 1, msg)
            return

        start_time = time.time()
        current_step = 0

        def emit_progress(msg):
            nonlocal current_step
            if not progress_callback:
                logging.info(msg)
                return
            elapsed = time.time() - start_time
            if current_step > 0:
                avg_time = elapsed / current_step
                remaining_steps = max(1, total_steps - current_step)
                eta_seconds = avg_time * remaining_steps
                eta_m = int(eta_seconds // 60)
                eta_s = int(eta_seconds % 60)
                eta_str = f"{eta_m}m {eta_s}s" if eta_m > 0 else f"{eta_s}s"
            else:
                eta_str = "Calculating..."
            progress_callback(current_step, total_steps, f"{msg} (ETA: {eta_str})")

        skipped_channels = set()
        
        for c_name, c_url in channels_to_scan.items():
            current_step += 1
            if not c_url: continue
            rule = channel_rules.get(c_url, "Auto")

            if not target_channels:
                if rule == "Never Scan":
                    skipped_channels.add(c_url)
                    emit_progress(f"Skipping (Rule: Never Scan): {c_name}")
                    continue
                if rule == "Auto" and c_url in ignored_channels:
                    skipped_channels.add(c_url)
                    emit_progress(f"Skipping (Auto-Ignored): {c_name}")
                    continue

            emit_progress(f"Scanning channel: {c_name}")
            live_streams = self._scrape_channel_live_tab(c_url)
            
            if "channel_live_counts" not in self.cache: self.cache["channel_live_counts"] = {}
            self.cache["channel_live_counts"][c_url] = len(live_streams)

            if not target_channels and rule == "Auto" and len(live_streams) > max_channel_streams:
                skipped_channels.add(c_url)
                emit_progress(f"Auto-Ignoring {c_name} (Streams: {len(live_streams)} > {max_channel_streams})")
                if c_url not in ignored_channels: self._add_to_ignored_channels(c_url)
                continue

            self.live_channel_data[c_url] = live_streams
            time.sleep(0.5) 
            
        self._save_cache()

        config_stream_by_url = {}
        config_stream_by_name = {}
        global_known_urls = set()
        
        for s in self.config.get("streams", []):
            u = s.get("page_url", "")
            if u:
                clean_u = re.sub(r'[\?&]variant=\d+', '', u).strip()
                config_stream_by_url[clean_u] = s
                global_known_urls.add(clean_u)
                
            n = s.get("name", "")
            if n:
                config_stream_by_name[n] = s

        for db_url, db_meta in streams_to_evaluate.items():
            old_channel_url = db_meta.get("channel_url", "")
            friendly_name = db_meta.get("friendly_name", "Unknown")
            
            if target_channels and old_channel_url not in target_channels: 
                continue
                
            current_step += 1
            
            self._debug_log(f"\n--- EVALUATING: {friendly_name} ---")
            self._debug_log(f"DB URL: {db_url}")
            self._debug_log(f"Channel URL: {old_channel_url}")
            
            if not target_channels and old_channel_url in skipped_channels: 
                self._debug_log("SKIPPED: Channel is in skipped_channels list.")
                continue
            
            clean_db_url = re.sub(r'[\?&]variant=\d+', '', db_url).strip()
            config_stream = config_stream_by_url.get(clean_db_url) or config_stream_by_name.get(friendly_name, {})
            
            old_channel_name = config_stream.get("channel_name")
            if not old_channel_name: old_channel_name = db_meta.get("channel_name", "Unknown Channel")
            
            old_title = config_stream.get("original_yt_title")
            if old_title:
                self._debug_log(f"Config Original Title: '{old_title}'")
            else:
                old_title = db_meta.get("title", "")
                self._debug_log(f"Cached Title: '{old_title}'")
            
            emit_progress(f"Cross-referencing: {friendly_name[:40]}...")
            
            if not old_title or old_title == friendly_name or old_title == "Unknown":
                emit_progress(f"Restoring missing cache title: {friendly_name[:30]}...")
                actual_title = self._fetch_actual_title(db_url)
                if actual_title:
                    old_title = actual_title
                    db_meta["title"] = actual_title
                    if "streams" not in self.cache: self.cache["streams"] = {}
                    self.cache["streams"][db_url] = db_meta
                    self._save_cache() 
                    self._debug_log(f"RESTORED TITLE: '{old_title}'")
                else:
                    self._debug_log("RESTORE FAILED: yt-dlp returned None (Video deleted/private)")
                    
            if not old_title or old_title == friendly_name or old_title == "Unknown":
                self._debug_log("FALLBACK: Attempting to use cleaned friendly_name for discovery.")
                clean_name = re.sub(r'\[.*?\]', '', friendly_name)
                clean_name = re.sub(r'\(.*?\)', '', clean_name)
                clean_name = re.sub(r'\s+', ' ', clean_name).strip()
                
                if clean_name and clean_name != "Unknown":
                    old_title = clean_name
                    self._debug_log(f"FALLBACK TITLE GENERATED: '{old_title}'")
            
            if not old_title or old_title == friendly_name or old_title == "Unknown": 
                self._debug_log("WARNING: Proceeding without a valid title. Bypassing lexical matching (Cases B-H).")
            else:
                expected_channel_live_streams = self.live_channel_data.get(old_channel_url, [])
                self._debug_log(f"Live Channel Streams Found: {len(expected_channel_live_streams)}")
                
                found_match = False
                for live_s in expected_channel_live_streams:
                    if clean_db_url == live_s['url']:
                        sim = self._calculate_similarity(old_title, live_s['title'])
                        self._debug_log(f"Exact URL Match Found! Similarity to old title: {sim:.2f}")
                        
                        new_chan_name = _get_safe_cname(live_s.get('channel_name'), old_channel_name)
                        chan_changed = False
                        
                        if old_channel_name and new_chan_name and str(old_channel_name).lower() != str(new_chan_name).lower():
                            if new_chan_name not in ["Unknown Channel", "Unknown"]: chan_changed = True
                        
                        if sim < 0.95 or chan_changed:
                            case_desc = "Same Channel, New Title/Desc" + (" (Channel Renamed)" if chan_changed else "")
                            self.proposals.append({
                                "case": "C", "case_desc": case_desc, "friendly_name": friendly_name,
                                "old_url": db_url, "new_url": live_s['url'], "old_title": old_title, "new_title": live_s['title'],
                                "old_channel": old_channel_url, "new_channel": live_s['channel_url'],
                                "old_channel_name": old_channel_name, "new_channel_name": new_chan_name,
                                "confidence": sim, "auto_heal_eligible": True 
                            })
                            self._debug_log("APPENDED: Case C (Title/Channel Name Changed)")
                        else:
                            self._debug_log("No changes detected. Stream is completely healthy.")
                            
                        found_match = True
                        break
                
                if found_match: 
                    if not config_stream.get('enabled', True):
                        self.proposals.append({
                            "case": "R", 
                            "case_desc": "Resurrected Stream (Same URL)", 
                            "friendly_name": friendly_name,
                            "old_url": db_url, 
                            "new_url": db_url, 
                            "old_title": friendly_name, 
                            "new_title": old_title,
                            "old_channel": old_channel_url, 
                            "new_channel": old_channel_url,
                            "old_channel_name": old_channel_name, 
                            "new_channel_name": _get_safe_cname(old_channel_name, old_channel_name),
                            "confidence": 1.0, 
                            "auto_heal_eligible": True 
                        })
                        self._debug_log("APPENDED: Case R (Same URL Resurrection)")
                    continue
                
                self._debug_log("URL not found in channel live tab. Scanning for migrations...")
                
                best_sim, best_candidate = 0.0, None
                for live_s in expected_channel_live_streams:
                    sim = self._calculate_similarity(old_title, live_s['title'])
                    if sim > best_sim: best_sim = sim; best_candidate = live_s
                        
                if best_candidate and best_sim >= 0.75:
                    case_id = "B" if best_sim > 0.95 else "D"
                    case_desc = "Same Channel, New URL" if case_id == "B" else "Same Channel, New URL & Meta"
                    self.proposals.append({
                        "case": case_id, "case_desc": case_desc, "friendly_name": friendly_name,
                        "old_url": db_url, "new_url": best_candidate['url'], "old_title": old_title, "new_title": best_candidate['title'],
                        "old_channel": old_channel_url, "new_channel": best_candidate['channel_url'],
                        "old_channel_name": old_channel_name, "new_channel_name": _get_safe_cname(best_candidate.get('channel_name'), old_channel_name),
                        "confidence": best_sim, "auto_heal_eligible": (case_id == "B") 
                    })
                    self._debug_log(f"APPENDED: Case {case_id} (Same Channel Migration - Confidence {best_sim:.2f})")
                    continue
                    
                best_sim, best_candidate = 0.0, None
                for c_url, live_streams in self.live_channel_data.items():
                    if c_url == old_channel_url: continue 
                    for live_s in live_streams:
                        sim = self._calculate_similarity(old_title, live_s['title'])
                        if sim > best_sim: best_sim = sim; best_candidate = live_s
                            
                if best_candidate and best_sim >= 0.85: 
                    case_id = "E" if best_sim > 0.95 else "F"
                    case_desc = "Known Channel, New URL" if case_id == "E" else "Known Channel, New URL & Meta"
                    self.proposals.append({
                        "case": case_id, "case_desc": case_desc, "friendly_name": friendly_name,
                        "old_url": db_url, "new_url": best_candidate['url'], "old_title": old_title, "new_title": best_candidate['title'],
                        "old_channel": old_channel_url, "new_channel": best_candidate['channel_url'],
                        "old_channel_name": old_channel_name, "new_channel_name": _get_safe_cname(best_candidate.get('channel_name'), old_channel_name),
                        "confidence": best_sim, "auto_heal_eligible": False 
                    })
                    self._debug_log(f"APPENDED: Case {case_id} (Cross-Channel Migration - Confidence {best_sim:.2f})")
                    continue
                    
                emit_progress(f"Fugitive Search (Deep Scan): {friendly_name[:40]}...")
                fugitive_results = self._fugitive_search(old_title)
                
                best_sim, best_candidate = 0.0, None
                for f_res in fugitive_results:
                    sim = self._calculate_similarity(old_title, f_res['title'])
                    if sim > best_sim: best_sim = sim; best_candidate = f_res
                        
                if best_candidate and best_sim >= 0.80:
                    clean_new_url = re.sub(r'[\?&]variant=\d+', '', best_candidate['url']).strip()
                    
                    if clean_db_url != clean_new_url:
                        case_id = "G" if best_sim > 0.95 else "H"
                        case_desc = "New Channel, New URL" if case_id == "G" else "New Channel, New URL & Meta"
                        self.proposals.append({
                            "case": case_id, "case_desc": case_desc, "friendly_name": friendly_name,
                            "old_url": db_url, "new_url": best_candidate['url'], "old_title": old_title, "new_title": best_candidate['title'],
                            "old_channel": old_channel_url, "new_channel": best_candidate['channel_url'],
                            "old_channel_name": old_channel_name, "new_channel_name": _get_safe_cname(best_candidate.get('channel_name'), old_channel_name),
                            "confidence": best_sim, "auto_heal_eligible": False 
                        })
                        self._debug_log(f"APPENDED: Case {case_id} (Fugitive Migration - Confidence {best_sim:.2f})")
                        continue
                    else:
                        self._debug_log("Fugitive Search found the EXACT SAME URL. Ignoring to allow Failsafe Ping to run.")
                
            emit_progress(f"Direct Failsafe Ping: {friendly_name[:40]}...")
            
            is_live, ping_reason = self._check_if_actually_live(clean_db_url)
            self._debug_log(f"Direct Failsafe Ping Result: {is_live} ({ping_reason})")
            
            if is_live: 
                self._debug_log("Stream is actually ALIVE on YouTube.")
                if not config_stream.get('enabled', True):
                    self.proposals.append({
                        "case": "R", 
                        "case_desc": "Resurrected Stream (Same URL)", 
                        "friendly_name": friendly_name,
                        "old_url": db_url, 
                        "new_url": db_url, 
                        "old_title": friendly_name, 
                        "new_title": old_title,
                        "old_channel": old_channel_url, 
                        "new_channel": old_channel_url,
                        "old_channel_name": old_channel_name, 
                        "new_channel_name": _get_safe_cname(old_channel_name, old_channel_name),
                        "confidence": 1.0, 
                        "auto_heal_eligible": True 
                    })
                    self._debug_log("APPENDED: Case R (Same URL Resurrection)")
                continue 
                
            is_enabled = config_stream.get('enabled', True)
            self._debug_log(f"Config 'enabled' status: {is_enabled}")
            
            if not is_enabled:
                self._debug_log("SKIPPING CASE A: Stream is already disabled in the config.")
                continue
                
            self.proposals.append({
                "case": "A", "case_desc": "Stream Dead / Not Found", "friendly_name": friendly_name,
                "old_url": db_url, "new_url": None, "old_title": old_title, "new_title": None,
                "old_channel": old_channel_url, "new_channel": None, "old_channel_name": old_channel_name, "new_channel_name": None,
                "confidence": 0.0, "auto_heal_eligible": False
            })
            self._debug_log("APPENDED: Case A (Stream Dead)")

        emit_progress("Finalizing: Searching for brand new streams and graveyard resurrections...")
        self._debug_log("--- ENTERING FINALIZING BLOCK ---")
        
        proposed_new_urls = set() 
        channel_ignored_counts = Counter() 
        
        graveyard_streams = [s for s in self.config.get('streams', []) if not s.get('enabled', True)]
        
        for c_url, live_streams in self.live_channel_data.items():
            if not live_streams: continue 
            
            # Use reverse lookup to find real channel name for Case N safety
            real_c_name = "Unknown Channel"
            for kname, kurl in self.known_channels.items():
                if kurl == c_url:
                    real_c_name = kname
                    break
                    
            c_name_check = _get_safe_cname(live_streams[0].get('channel_name'), real_c_name)
            
            channel_siblings = [s for s in self.config.get('streams', []) if (s.get('channel_name') or '') == c_name_check and (s.get('lat', 0.0) != 0.0 or s.get('lon', 0.0) != 0.0)]
            
            for live_s in live_streams:
                clean_live = re.sub(r'[\?&]variant=\d+', '', live_s.get('url', ''))
                
                if clean_live in ignored_proposals:
                    saved_title = ignored_proposals[clean_live]
                    sim = self._calculate_similarity(saved_title, live_s.get('title', ''))
                    if sim > 0.80:
                        channel_ignored_counts[c_url] += 1 
                        continue
                    else:
                        del ignored_proposals[clean_live]
                        ignored_proposals_changed = True
                        logging.info(f"Blacklist Update: Title changed for {clean_live}. Removing from Ignore List.")
                
                if clean_live not in global_known_urls and clean_live not in proposed_new_urls:
                    is_migration_target = any(p.get('new_url') == live_s.get('url') for p in self.proposals)
                    if not is_migration_target:
                        
                        best_r_sim = 0.0
                        best_r_stream = None
                        
                        for dead_s in graveyard_streams:
                            sim = self._calculate_similarity(dead_s.get('name', ''), live_s.get('title', ''))
                            if sim > best_r_sim:
                                best_r_sim = sim
                                best_r_stream = dead_s
                                
                        if best_r_stream and best_r_sim >= 0.80:
                            auto_heal = best_r_sim >= 0.95
                            proposed_new_urls.add(clean_live)
                            self.proposals.append({
                                "case": "R", 
                                "case_desc": "Resurrected Stream (Graveyard Match)", 
                                "friendly_name": best_r_stream.get('name'),
                                "old_url": best_r_stream.get('page_url'), 
                                "new_url": live_s.get('url'), 
                                "old_title": best_r_stream.get('name'), 
                                "new_title": live_s.get('title'),
                                "old_channel": None, 
                                "new_channel": live_s.get('channel_url'),
                                "old_channel_name": best_r_stream.get('channel_name', 'Unknown Channel'), 
                                "new_channel_name": _get_safe_cname(live_s.get('channel_name'), best_r_stream.get('channel_name')),
                                "confidence": best_r_sim, 
                                "auto_heal_eligible": auto_heal
                            })
                            continue 

                        suggested_lat, suggested_lon, match_name, siblings_list = 0.0, 0.0, "", []
                        if channel_siblings:
                            best_sim, best_sib = 0.0, None
                            new_text = f"{live_s.get('title', '')} {live_s.get('description', '')}".lower()
                            for sib in channel_siblings:
                                sim = self._calculate_similarity(new_text, sib.get('name', '').lower())
                                if sim > best_sim: best_sim = sim; best_sib = sib
                            if best_sim > 0.3 and best_sib:
                                suggested_lat, suggested_lon, match_name = best_sib.get('lat', 0.0), best_sib.get('lon', 0.0), best_sib.get('name', '')
                                for s in self.config.get('streams', []):
                                    if s.get('lat') == suggested_lat and s.get('lon') == suggested_lon: siblings_list.append(s.get('name', 'Unknown Stream'))
                        
                        proposed_new_urls.add(clean_live)
                        self.proposals.append({
                            "case": "N", "case_desc": "New Stream Discovered", "friendly_name": live_s.get('title'),
                            "old_url": None, "new_url": live_s.get('url'), "old_title": None, "new_title": live_s.get('title'),
                            "old_channel": None, "new_channel": live_s.get('channel_url'),
                            "old_channel_name": None, "new_channel_name": _get_safe_cname(live_s.get('channel_name'), real_c_name),
                            "confidence": 1.0, "auto_heal_eligible": False,
                            "suggested_lat": suggested_lat, "suggested_lon": suggested_lon, "suggested_match_name": match_name, "sibling_streams": siblings_list
                        })

        if ignored_proposals_changed:
            self._update_ignored_proposals(ignored_proposals)

        self._debug_log("--- COUNTING CONFIG METADATA ---")
        config_active_counts = Counter()
        config_dead_counts = Counter()
        
        for s in self.config.get('streams', []):
            c_name = s.get('channel_name', '')
            if c_name is not None:
                c_name = str(c_name).strip()
            else:
                c_name = ""
                
            if c_name:
                if s.get('enabled', True):
                    config_active_counts[c_name] += 1
                else:
                    config_dead_counts[c_name] += 1

        self._debug_log("--- UPDATING PROPOSALS WITH METADATA COUNTS ---")
        for p in self.proposals:
            c_url = p.get('new_channel') or p.get('old_channel') or ""
            c_name = p.get('new_channel_name') or p.get('old_channel_name') or 'Unknown Channel'
            
            p['channel_live_count'] = self.cache.get("channel_live_counts", {}).get(c_url, 0)
            p['channel_active_count'] = config_active_counts.get(c_name, 0)
            p['channel_dead_count'] = config_dead_counts.get(c_name, 0)
            p['channel_ignored_count'] = channel_ignored_counts.get(c_url, 0)

        self._debug_log("--- SAVING TO DISK ---")
        try:
            with open(SYNC_PROPOSALS_FILE, 'w', encoding='utf-8') as f: json.dump(self.proposals, f, indent=2)
            self._debug_log(f"--- SYNC COMPLETE. GENERATED {len(self.proposals)} PROPOSALS. ---")
            if progress_callback: progress_callback(total_steps, total_steps, "Scan Complete.")
        except Exception as e:
            self._dump_traceback(f"Failed to save proposals: {e}")

if __name__ == "__main__":
    engine = YouTubeSyncEngine()
    engine.run_sync()