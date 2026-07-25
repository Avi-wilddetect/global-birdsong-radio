# FILE: gui_threads.py
# Contains background worker threads extracted from config_editor_gui.py

import os
import requests
import yt_dlp
from PyQt6.QtCore import QThread, pyqtSignal

class StreamCheckThread(QThread):
    result_ready = pyqtSignal(str, str) 
    def __init__(self, url, cookies_path):
        super().__init__()
        self.url = url
        self.cookies_path = cookies_path

    def run(self):
        try:
            if "youtube.com" in self.url or "youtu.be" in self.url:
                ydl_opts = {'format': 'bestaudio/best', 'quiet': True}
                if self.cookies_path and os.path.exists(self.cookies_path):
                    ydl_opts['cookiefile'] = self.cookies_path
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(self.url, download=False)
                    if info and 'url' in info:
                        self.result_ready.emit(f"SUCCESS: YouTube stream is valid.\nTitle: {info.get('title', 'N/A')}", "SUCCESS")
                    else:
                        self.result_ready.emit("ERROR: Could not get a valid stream URL from yt-dlp.", "GENERIC_ERROR")
            else:
                r = requests.head(self.url, timeout=10, allow_redirects=True, headers={'User-Agent': 'Mozilla/5.0'})
                r.raise_for_status()
                ct = r.headers.get('content-type', '').lower()
                if 'audio' in ct or 'video' in ct or 'octet-stream' in ct:
                    self.result_ready.emit(f"SUCCESS: Direct stream is valid.\nContent-Type: {ct}", "SUCCESS")
                else:
                    self.result_ready.emit(f"WARNING: URL is valid but Content-Type is '{ct}'. May not be a listenable stream.", "SUCCESS")
        except Exception as e:
            error_str = str(e).lower()
            if 'rate-limited' in error_str or '429' in error_str:
                msg = "Status: YouTube Rate Limit Exceeded."
                self.result_ready.emit(msg, "TEMPORARY_ERROR")
            elif 'video unavailable' in error_str or 'private video' in error_str:
                msg = "Status: ERROR - Stream Unavailable."
                self.result_ready.emit(msg, "PERMANENT_ERROR")
            else:
                self.result_ready.emit(f"ERROR: An unexpected error occurred.\nDetails: {str(e)}", "GENERIC_ERROR")