# 🌍 Global Birdsong Radio (GBR)

> **An Asynchronous Presence Engine & Distributed Biological Sensor Network**

Global Birdsong Radio (GBR) is a distributed monitoring platform that actively listens to and watches ~400 curated live nature streams (YouTube, HLS, IP Cams) across the globe. By utilizing a multi-layered AI pipeline, GBR filters out chaotic background noise and dynamically triggers alerts when it detects specific biological events—ranging from a rare bird singing to a Lion roaring on the African savanna.

Detections are plotted in real-time onto a live, public Web Map: https://wilddetection.net/

---

### ⚠️ Educational Use & Liability Disclaimer
**Please Read Before Use:**
The "Hydra Network" (SIM rotation) and "4-Tier Stream Resolver" modules contained in this codebase are provided strictly as conceptual research tools demonstrating network resiliency and dynamic traffic routing. Interacting with third-party platforms (such as YouTube) using automated scraping tools or headless browsers may violate their respective Terms of Service (ToS). 

End-users are 100% responsible for how they deploy this software. The creator of this project accepts **zero legal liability** for API bans, account terminations, or ToS violations incurred by using this code. 

### 🤖 Authorship & Transparency
This project was entirely conceptualized, architected, and directed by **Avi** (`birddetect@gmail.com`). However, I am not a software engineer. **100% of the underlying Python, C++, SQL, and HTML/JS code in this repository was written by advanced Large Language Models (LLMs)** acting under my direction. This project stands as a testament to what is possible when human architectural vision is paired with AI programming capabilities.

---

## 🧠 The "Split-Brain" Architecture

To handle massive bandwidth throughput and evade severe anti-bot IP blocking, GBR utilizes a hybrid Edge/Cloud architecture.

### 1. The Edge Node ("The Ears & The Hydra")
Designed to run on a local, high-performance Windows PC, the Edge Node does the heavy lifting:
* **The Hydra Network:** Dynamically binds listener processes to rotating 4G/5G SIM card IPs. It calculates "Network Heat" based on hourly speed caps and monthly GB quotas, initiating a 95% Soft-Lockout to prevent data depletion and OS-fallback leaks.
* **4-Tier Stream Resolver:** Defeats bot-protection by falling back through 4 extraction tiers: 1. yt-dlp (Anonymous Proxy) -> 2. Cookie-Auth Proxy -> 3. Direct Connection -> 4. Headless Selenium/Chrome Network Sniffing.
* **The GUI Ecosystem:** A massive PyQt6 application suite for the Admin, including a Real-Time Dashboard, an Intelligence Hub (for AI prompting), a Discovery Radar (for auto-healing dead links), and a Curation Studio (for ML dataset exporting).

### 2. The Cloud Node ("The Brain")
Designed for a lightweight Linux VPS (e.g., DigitalOcean) running Docker.
* **Infrastructure:** Nginx + Gunicorn + Flask API, backed by a PostgreSQL database.
* **The Web Map:** Serves a dynamic Leaflet.js map (`map_template.html`), managing API routes, dynamically balancing map slots between Audio and Vision detections, and acting as a secure HLS reverse proxy. The Cloud Node *never* scrapes YouTube directly.

---

## 🦉 The AI & Multimodal Intelligence

GBR doesn't just blindly pass streams to an AI; it uses a highly engineered, cross-engine filtering pipeline:

* **Audio Engine (BirdNET):** Analyzes audio chunks and applies Adaptive SNR Distance Estimation. It compares current audio levels against a stream's historical "Golden Anchor" baseline to mathematically determine if an animal is "Point Blank" or in the "Deep Background."
* **Vision Engine (Google Gemini 2.5 Flash):** Captures video frames, runs local pixel-variance motion detection (to save API quota), and queries Gemini. It enforces an "AND-Gate" physical size rule (2D Frame Size vs 3D Depth) and applies specific systemic prompts (e.g., Night/Low-Vis mode overrides).
* **The Predator Reflex:** A cross-engine bounty system. If the Audio Engine hears a high-confidence acoustic target (e.g., a Lion's roar), it instantly bypasses the Vision Engine's dormancy state, forcing an immediate, highly-focused visual scan for that specific animal. If both confirm, it merges into a **"Multimodal (🔥)"** detection.

---

## 🛠️ Technology Stack
* **Languages:** Python 3.11+, HTML5, JS, CSS
* **Databases:** PostgreSQL 15 (Cloud), SQLite (Local WAL mode)
* **Cloud Backend:** Flask, Waitress, Gunicorn, Nginx, Docker Compose
* **Edge Frontend:** PyQt6
* **Media & Automation:** FFmpeg, yt-dlp, Selenium, pydub, Pillow
* **AI Models:** BirdNET-Analyzer, Google `google-genai` SDK

---

## 🚀 Deployment Overview

*(Detailed installation documentation is currently pending. Below is a high-level overview.)*

**Cloud Node (Linux/VPS):**
1. Install Docker and Docker Compose.
2. Clone the repository and configure your generic placeholders in `birdnet_config.json`.
3. Run `docker-compose up -d --build`.

**Edge Node (Windows):**
1. Install Python 3.11+.
2. Install required dependencies: `pip install -r requirements.txt`.
3. Download and place `ffmpeg.exe` and `ffprobe.exe` into an `ffmpeg/bin/` subdirectory.
4. Launch the GUI: `python config_editor_gui.py`.

---

## 📬 Contact
For inquiries regarding the live Web Map, community discussions, or the architecture of this project, please contact **Avi** at `birddetect@gmail.com`.
