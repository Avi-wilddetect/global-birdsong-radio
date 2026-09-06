# FILE: audio_capture.py
# VERSION: 8.13 - "The Client Spoofing Restoration Patch"
#
# CHANGELOG:
# [2026-09-04 20:34] - v8.13: Reverted the Web Client Restoration Patch to allow yt-dlp to use 'tv', 'ios', and 'mweb' clients, bypassing YouTube's strict blocking of the 'web' client.
# [2026-09-04 12:08] - v8.12: Implemented Pure Python Downloader via 'requests' to bypass FFmpeg's TCP stack for MP3s, preventing Returncode 3419392776 crashes. Downgraded Selenium Sniffer failures from TERMINATED to AUTO_RESOLVER_FAILED to prevent 24-hour permanent bans on temporary proxy hiccups.
# [2026-09-03 13:45] - v8.10: Bypassed the proxy entirely for Native Audio (MP3) streams to prevent aggressive SO_LINGER socket destruction from crashing FFmpeg with returncode 3419392776.
# [2026-09-03 13:00] - v8.9: Implemented Web Client Restoration Patch to force 'web' client for yt-dlp, fixing YouTube audio extraction.
# [2026-09-03 03:20] - v8.8: Fixed false-positive FATAL/SUSPENDED flags by routing "Private/Unavailable" YouTube blocks and IP Camera token expirations directly to the Selenium Auto-Healer.
# [2026-09-03 03:00] - v8.7: Upgraded FFmpeg segfault regex to catch 4294957242 and all other 10-digit memory access violations.

import os
import time
import re
import random
import subprocess
import json
import logging
import tempfile
import requests
from urllib.parse import urljoin
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent
# We dynamically append ROOT to sys.path to allow imports
import sys
sys.path.append(str(ROOT))

import network_manager
try:
    import stream_resolver
except ImportError:
    stream_resolver = None

CFG_PATH = ROOT / "birdnet_config.json"
_proxy_rate_limited_until: dict[str, float] = {}

_DEFAULT_JITTER_MIN_SEC   = 5.0   
_DEFAULT_JITTER_MAX_SEC   = 12.0  
_DEFAULT_BACKOFF_SEC      = 900   

def _load_pacing_config() -> dict:
    try:
        if CFG_PATH.exists():
            data = json.loads(CFG_PATH.read_text(encoding='utf-8'))
            return data.get('extraction_strategy', {})
    except Exception: pass
    return {}

def _get_jitter_range() -> tuple[float, float]:
    cfg = _load_pacing_config()
    lo = float(cfg.get('jitter_min_sec', _DEFAULT_JITTER_MIN_SEC))
    hi = float(cfg.get('jitter_max_sec', _DEFAULT_JITTER_MAX_SEC))
    lo = max(3.0, lo)
    hi = max(lo + 2.0, hi)
    return lo, hi

def _get_backoff_sec() -> float:
    cfg = _load_pacing_config()
    return float(cfg.get('rate_limit_backoff_sec', _DEFAULT_BACKOFF_SEC))

def _human_jitter_sleep(listener_id: str, proxy_url: str):
    lo, hi = _get_jitter_range()
    delay = random.uniform(lo, hi)
    logging.info(f"[{listener_id}] 🕐 Human Jitter: sleeping {delay:.1f}s before yt-dlp call (proxy: {proxy_url or 'Direct'}) to avoid 429 rate-limiting.")
    time.sleep(delay)

def _mark_proxy_rate_limited(proxy_url: str):
    key = proxy_url or "direct"
    ban_until = time.time() + _get_backoff_sec()
    _proxy_rate_limited_until[key] = ban_until
    minutes = _get_backoff_sec() / 60
    logging.warning(f"[PACING] 🚫 Proxy {key} marked as RATE-LIMITED for {minutes:.0f} min.")

def _is_proxy_rate_limited(proxy_url: str) -> bool:
    key = proxy_url or "direct"
    ban_until = _proxy_rate_limited_until.get(key, 0)
    if time.time() < ban_until:
        remaining = ban_until - time.time()
        logging.warning(f"[PACING] ⏳ Proxy {key} is RATE-LIMITED. {remaining/60:.1f} min remaining. Skipping.")
        return True
    if key in _proxy_rate_limited_until:
        del _proxy_rate_limited_until[key]
        logging.info(f"[PACING] ✅ Proxy {key} rate-limit window expired. Reinstating.")
    return False

def _is_429_error(error_str: str) -> bool:
    markers =["429", "too many requests", "rate-limited", "rate limited", "ratelimit", "quota", "http error 429"]
    s = error_str.lower()
    return any(m in s for m in markers)

def ensure_node_in_path():
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
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

ensure_node_in_path()

def update_config_with_new_url(stream_name, new_url):
    for attempt in range(5):
        try:
            if not CFG_PATH.exists(): return
            data = json.loads(CFG_PATH.read_text(encoding='utf-8'))
            updated = False
            for s in data.get('streams',[]):
                if s.get('name') == stream_name and s.get('page_url') != new_url:
                    # Preserve original URL for Auto-Healer sniffer
                    if not s.get('original_url') and ("youtube.com" in s.get('page_url', '') or "youtu.be" in s.get('page_url', '')):
                        s['original_url'] = s['page_url']
                        
                    s['page_url'] = new_url
                    s['updated_at'] = time.time()
                    updated = True
                    break
            if updated:
                tmp_path = CFG_PATH.with_suffix('.tmp')
                tmp_path.write_text(json.dumps(data, indent=2), encoding='utf-8')
                try: tmp_path.replace(CFG_PATH)
                except OSError: time.sleep(0.2); continue
            return
        except: time.sleep(0.2)

class AudioCaptureEngine:
    def __init__(self, listener_id, db_manager, global_config):
        self.lid = listener_id
        self.db = db_manager
        self.g_cfg = global_config

    def _grab_pure_audio_stream(self, url, capture_seconds, headers_list, proxy_url, interface_name):
        """
        Bypasses FFmpeg's TCP stack entirely to read MP3/Icecast streams directly via 'requests'.
        Prevents FFmpeg returncode 3419392776 crashes caused by poor HTTP stream handling.
        """
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url and proxy_url != "None" else None
        
        headers = {}
        for h in headers_list:
            if ":" in h:
                k, v = h.split(":", 1)
                headers[k.strip()] = v.strip()
        
        if "User-Agent" not in headers:
            ext_strat = self.g_cfg.get('extraction_strategy', {})
            headers["User-Agent"] = ext_strat.get('ffmpeg_user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
            
        logging.info(f"[{self.lid}] Engaging Native Python HTTP downloader for audio stream...")
        
        try:
            r = requests.get(url, headers=headers, proxies=proxies, stream=True, timeout=10)
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Native Python Downloader request failed: {e}")
        
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tf:
            temp_path = tf.name
            start_time = time.time()
            try:
                for chunk in r.iter_content(chunk_size=16384):
                    if chunk:
                        tf.write(chunk)
                    if time.time() - start_time >= capture_seconds:
                        break
            except Exception as e:
                logging.warning(f"[{self.lid}] Stream read interrupted (normal for live streams): {e}")
                
        # Now convert downloaded file to WAV via FFmpeg locally (no networking required)
        ffmpeg_exe = str(ROOT / "ffmpeg" / "bin" / "ffmpeg.exe")
        ff_cmd =[
            ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
            "-i", temp_path
        ]
        
        if self.g_cfg.get('extraction_strategy', {}).get('audio_normalization', True):
            ff_cmd.extend(["-af", "dynaudnorm"])
            
        ff_cmd.extend(["-ac", "1", "-ar", "48000", "-f", "wav", "pipe:1"])
        
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        
        try:
            ff_proc = subprocess.Popen(ff_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=creation_flags)
            out, _ = ff_proc.communicate(timeout=15)
            
            if ff_proc.returncode != 0:
                raise RuntimeError(f"FFmpeg decoding failed with returncode {ff_proc.returncode}")
                
            self.db.log_health_event(url, "SUCCESS", "Captured via Native Python Downloader")
                
            if interface_name and interface_name != "Default / OS":
                try: 
                    network_manager.log_app_usage(interface_name, 'audio', int(capture_seconds * 128 * 1024))
                except Exception: pass
                
            return out, headers_list
        finally:
            try: os.unlink(temp_path)
            except: pass

    def _grab_audio_via_native_python(self, base_youtube_url, capture_seconds, proxy_url, interface_name, is_youtube=True, original_url=None):
        if is_youtube:
            if _is_proxy_rate_limited(proxy_url):
                raise RuntimeError(f"429 RATE-LIMITED: Proxy {proxy_url} is in cooldown. Rotate interface.")
            _human_jitter_sleep(self.lid, proxy_url)

        m3u8_url = None
        headers_dict = {}

        ext_strat = self.g_cfg.get('extraction_strategy', {})
        
        # --- CLIENT SPOOFING RESTORATION PATCH ---
        # Allow the original config list to flow directly to yt-dlp without stripping mobile clients
        clients = [c.strip() for c in ext_strat.get('player_client', 'tv, mweb, ios').split(',') if c.strip()]
        if not clients:
            clients = ['tv', 'mweb', 'ios', 'web'] # Robust fallback

        if is_youtube:
            ydl_opts = {
                'format': 'bestaudio/best/bv*+ba/b',
                'quiet': True,
                'nocheckcertificate': True,
                'compat_opts':['allow-un-sandboxed-javascript'],
                'socket_timeout': 20,
                'extractor_args': {'youtube': {'player_client': clients}},
                'js_runtimes': {'node': {}},
            }
            if proxy_url and proxy_url != "None":
                ydl_opts['proxy'] = proxy_url

            cookies_path = self.g_cfg.get('youtube_cookies_file')
            ios_only = all(c.strip().lower() == 'ios' for c in clients)
            if cookies_path and os.path.exists(cookies_path) and not ios_only:
                ydl_opts['cookiefile'] = cookies_path

            _ydl_result = [None]
            _ydl_error  =[None]

            def _run_ydl():
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        _ydl_result[0] = ydl.extract_info(base_youtube_url, download=False)
                except Exception as ex:
                    _ydl_error[0] = ex

            _ydl_thread = __import__('threading').Thread(target=_run_ydl, daemon=True)
            _ydl_thread.start()
            _ydl_thread.join(timeout=45)

            if _ydl_thread.is_alive():
                raise RuntimeError("YT-DLP TIMEOUT: extract_info exceeded 45s wall-clock limit. Releasing listener thread to next stream.")

            try:
                if _ydl_error[0] is not None:
                    raise _ydl_error[0]

                info = _ydl_result[0]
                
                if info:
                    m3u8_url = info.get('url')
                    headers_dict = info.get('http_headers', {})

                    if m3u8_url and ("youtube.com/watch" in m3u8_url or "youtu.be/" in m3u8_url):
                        real_url = None
                        for fmt in (info.get('requested_formats') or info.get('formats') or []):
                            fmt_url = fmt.get('url', '')
                            if fmt_url and "youtube.com/watch" not in fmt_url and "youtu.be/" not in fmt_url:
                                real_url = fmt_url
                                headers_dict = fmt.get('http_headers', headers_dict)
                                break
                        if real_url:
                            logging.info(f"[{self.lid}] yt-dlp returned page URL — resolved real media URL from formats list.")
                            m3u8_url = real_url
                        else:
                            logging.warning(f"[{self.lid}] yt-dlp returned a YouTube page URL and no usable format found. Triggering sniffer.")
                            raise RuntimeError("YOUTUBE_BLOCK: yt-dlp resolved page URL only, no media formats available.")

                    is_live = info.get('is_live')
                    live_status = info.get('live_status')
                    if is_live is False or live_status in['was_live', 'not_live', 'post_live']:
                        raise RuntimeError(f"VOD_REJECTED: Stream explicitly flagged as ended or VOD. Status: {live_status}")

            except Exception as e:
                e_str = str(e)

                if "vod_rejected" in e_str.lower():
                    raise e 
                
                # --- THE BOT WALL BYPASS PATCH ---
                # Explicitly map YouTube's new bot-wall phrases to YOUTUBE_BLOCK
                # so the Engine triggers Selenium instead of killing the stream.
                block_markers = [
                    "no video formats found", "sign in to confirm you", "bot", 
                    "requested format is not available", "only images are available",
                    "is not available", "private video", "video is unavailable",
                    "this live stream recording"
                ]
                if any(x in e_str.lower() for x in block_markers):
                    raise RuntimeError(f"YOUTUBE_BLOCK: {e_str}")

                if _is_429_error(e_str):
                    _mark_proxy_rate_limited(proxy_url)
                    raise RuntimeError(f"429 RATE-LIMITED: YouTube banned proxy {proxy_url}. Rotating to next interface. Original error: {e_str[:120]}")

                raise RuntimeError(f"Extraction failed. {e_str}")
        else:
            m3u8_url = base_youtube_url

        if not m3u8_url:
            raise RuntimeError("Extraction failed: No media URL resolved. YouTube proxy IP blocked or stream offline.")

        logging.info(f"[{self.lid}] Routing extracted media URL directly to FFmpeg...")
        
        if original_url and "Referer" not in headers_dict:
            headers_dict["Referer"] = original_url
            
        header_list = [f"{k}: {v}" for k, v in headers_dict.items()]
        
        return self._execute_ffmpeg(
            m3u8_url, capture_seconds, header_list, 'direct_stream',
            use_proxy=(proxy_url is not None and proxy_url != "None"),
            proxy_url=proxy_url, interface_name=interface_name
        )

    def grab_audio(self, raw_url, original_url, stream_name, stream_config, interface_name, proxy_url):
        clean_url = raw_url.replace(r"\u0026", "&").replace(r"\/", "/")
        clean_url = clean_url.replace("hhttps://", "https://")
        clean_url = re.sub(r'[\?&]variant=\d+', '', clean_url)

        stream_type = stream_config.get('stream_type', 'unknown')
        if stream_type == 'unknown':
            if "manifest.googlevideo.com" in clean_url or ".m3u8" in clean_url: 
                stream_type = 'hls'
            elif "youtube" in clean_url or "youtu.be" in clean_url: 
                stream_type = 'youtube'

        capture_seconds = int(stream_config.get('capture_seconds_override', self.g_cfg.get('capture_seconds', 12)))

        is_yt_origin = original_url and ("youtube.com" in original_url or "youtu.be" in original_url)
        is_cached_manifest = "manifest.googlevideo.com" in clean_url or "googlevideo.com" in clean_url

        if stream_type == 'youtube' or is_cached_manifest or is_yt_origin:
            if is_cached_manifest:
                target = clean_url
                use_ytdlp = False
            else:
                target = original_url if original_url else clean_url
                if "manifest.googlevideo.com" in target or (not ("youtube.com" in target or "youtu.be" in target)):
                    yt_match = re.search(r'/id/([^/.]+)', target)
                    if yt_match: target = f"https://www.youtube.com/watch?v={yt_match.group(1)}"
                use_ytdlp = True

            try:
                return self._grab_audio_via_native_python(target, capture_seconds, proxy_url, interface_name, is_youtube=use_ytdlp, original_url=original_url)
            except RuntimeError as e:
                err_str = str(e).lower()
                
                cached_manifest_dead = not use_ytdlp and any(x in err_str for x in [
                    "403", "404", "410", "forbidden", "not found", "gone", "unauthorized",
                    "returncode", "connection reset", "wsaeconnreset", "invalid data", "0 bytes",
                    "no media segments", "native downloader request failed"
                ])
                
                ffmpeg_crashed = bool(re.search(r'[1-4]\d{9}', err_str))
                
                if "youtube_block" in err_str or cached_manifest_dead or ffmpeg_crashed:
                    reason = "FFmpeg crashed" if ffmpeg_crashed else ("YT-DLP blocked" if "youtube_block" in err_str else "Cached link expired")
                    logging.warning(f"[{self.lid}] {reason}. Launching Selenium Sniffer for {stream_name}...")
                    if not stream_resolver:
                        raise RuntimeError("AUTO_RESOLVER_FAILED: stream_resolver missing, cannot sniff.")
                    
                    base_yt = original_url if original_url else (clean_url if "youtube" in clean_url else None)
                    if not base_yt:
                        raise RuntimeError("AUTO_RESOLVER_FAILED: No base YouTube URL to sniff.")
                        
                    links, stype, msg = stream_resolver.resolve_stream_url(base_yt, proxy_url=proxy_url, fast_mode=False)
                    if links:
                        new_url = links[0].replace(r"\u0026", "&").replace(r"\/", "/")
                        update_config_with_new_url(stream_name, new_url)
                        logging.info(f"[{self.lid}] Successfully sniffed and cached raw .m3u8 link!")
                        try:
                            return self._grab_audio_via_native_python(new_url, capture_seconds, proxy_url, interface_name, is_youtube=False, original_url=base_yt)
                        except Exception as inner_e:
                            raise RuntimeError(f"AUTO_RESOLVER_FAILED: Auto-healed link failed immediately: {inner_e}")
                    else:
                        raise RuntimeError(f"AUTO_RESOLVER_FAILED: Selenium Sniffer failed to find stream: {msg}")
                raise e

        try:
            headers_list =[]
            if original_url: headers_list.append(f"Referer: {original_url}")
            
            # --- THE NATIVE AUDIO PROXY BYPASS PATCH ---
            if stream_type == 'audio':
                return self._grab_pure_audio_stream(clean_url, capture_seconds, headers_list, proxy_url, interface_name)
                
            return self._execute_ffmpeg(
                clean_url, capture_seconds, headers_list, stream_type,
                use_proxy=True, proxy_url=proxy_url, interface_name=interface_name
            )
        except RuntimeError as e:
            err_str = str(e).lower()
            
            # --- IP CAMERA HEALING PATCH ---
            is_crash_or_block = any(x in err_str for x in [
                "403 forbidden", "404 not found", "410 gone", "invalid data found", 
                "stream ends prematurely", "end of file", "timed out", "-10054", "10054", 
                "connection reset", "ffmpeg failed with returncode", "server returned 403", 
                "server returned 404", "server returned 410", "native downloader request failed"
            ]) or bool(re.search(r'[1-4]\d{9}', err_str))
            
            if is_crash_or_block:
                if original_url and original_url != clean_url and not original_url.endswith('.m3u8'):
                    logging.warning(f"[{self.lid}] IP Camera token expired or FFmpeg crashed. Launching Selenium Sniffer for {stream_name}...")
                    if not stream_resolver:
                        raise RuntimeError("AUTO_RESOLVER_FAILED: stream_resolver missing, cannot sniff.")
                    
                    links, stype, msg = stream_resolver.resolve_stream_url(original_url, proxy_url=proxy_url, fast_mode=False)
                    if links:
                        new_url = links[0].replace(r"\u0026", "&").replace(r"\/", "/")
                        update_config_with_new_url(stream_name, new_url)
                        logging.info(f"[{self.lid}] Successfully sniffed and cached fresh IP Camera link!")
                        try:
                            return self._grab_audio_via_native_python(new_url, capture_seconds, proxy_url, interface_name, is_youtube=False, original_url=original_url)
                        except Exception as inner_e:
                            raise RuntimeError(f"AUTO_RESOLVER_FAILED: Auto-healed IP Camera link failed immediately: {inner_e}")
                    else:
                        raise RuntimeError(f"AUTO_RESOLVER_FAILED: Selenium Sniffer failed to find IP Camera stream: {msg}")
                else:
                    logging.info(f"[{self.lid}] TLS Block or FFmpeg crash detected on non-YouTube stream. Engaging Native Python Downloader for {clean_url[:60]}...")
                    try: 
                        return self._grab_pure_audio_stream(clean_url, capture_seconds, headers_list, proxy_url, interface_name)
                    except Exception as ex: 
                        raise RuntimeError(f"Native Downloader failed: {ex}")
            raise e

    def _execute_ffmpeg(self, url, capture_seconds, headers_list, stream_type, use_proxy, proxy_url, interface_name):
        ffmpeg_exe = str(ROOT / "ffmpeg" / "bin" / "ffmpeg.exe")
        
        ff_cmd =[
            ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
            "-reconnect", "1", 
            "-reconnect_streamed", "1", 
            "-reconnect_delay_max", "5"
        ]

        if use_proxy and proxy_url and proxy_url != "None":
            ff_cmd.extend(["-http_proxy", proxy_url])

        ext_strat = self.g_cfg.get('extraction_strategy', {})
        ua = ext_strat.get('ffmpeg_user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
        
        header_str = f"User-Agent: {ua}\r\n"
        
        if "youtube.com" in url or "youtu.be" in url or "googlevideo.com" in url:
            header_str += "Cookie: CONSENT=YES+cb; SOCS=CAI;\r\n"
        
        if headers_list:
            for h in headers_list:
                if not h.lower().startswith("user-agent:") and not h.lower().startswith("cookie:"): 
                    header_str += f"{h}\r\n"

        ff_cmd.extend(["-headers", header_str])
        ff_cmd.extend(["-i", url])
        ff_cmd.extend(["-t", str(capture_seconds)])

        normalize = bool(ext_strat.get('audio_normalization', True))
        if normalize:
            ff_cmd.extend(["-af", "dynaudnorm"])

        ff_cmd.extend(["-ac", "1", "-ar", "48000", "-f", "wav", "pipe:1"])

        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

        ff_proc = subprocess.Popen(
            ff_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags
        )

        try:
            out, _ = ff_proc.communicate(timeout=capture_seconds + 20)

            if ff_proc.returncode != 0:
                raise RuntimeError(f"FFmpeg failed with returncode {ff_proc.returncode}")

            self.db.log_health_event(url, "SUCCESS", "Captured via Direct FFmpeg")

            if interface_name and interface_name != "Default / OS":
                try: 
                    network_manager.log_app_usage(interface_name, 'audio', int(capture_seconds * 128 * 1024))
                except Exception: pass

            return out, headers_list

        except subprocess.TimeoutExpired:
            logging.error(f"[{self.lid}] Capture TIMEOUT on {url}. Brutally killing zombie processes.")
            raise RuntimeError("FFmpeg process timed out and was killed.")
        except RuntimeError as re: raise re
        except Exception as e:
            logging.error(f"[{self.lid}] Capture crash: {e}")
            raise RuntimeError(f"Capture general crash: {e}")
        finally:
            try:
                if ff_proc: ff_proc.kill()
            except: pass
            if os.name == 'nt' and ff_proc:
                subprocess.run(f"TASKKILL /F /T /PID {ff_proc.pid}", capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)