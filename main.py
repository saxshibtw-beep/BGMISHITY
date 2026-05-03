#!/usr/bin/env python3
import cv2
import pytesseract
import requests
import json
import time
import threading
import subprocess
import os
from datetime import datetime, timedelta
from collections import defaultdict
import re
from pathlib import Path
import pickle
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.auth.oauthlib.flow import InstalledAppFlow
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

# CONFIG
SCOPES = ['https://www.googleapis.com/auth/youtube.readonly']
YOUTUBE_API_KEY = None  # Will use auth
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.pickle"
CHECK_INTERVAL = 0.8  # seconds
YOUTUBE_RECHECK = 10 * 60  # 10 mins
SPAM_COOLDOWN = 30 * 60  # 30 mins
BRAVE_PATH = "/usr/bin/brave-browser"  # VPS Brave path

# COMMENTARY STYLES
COMMENTARIES = [
    # Excited
    "YOOOO {name} is LIVE in your lobby! Get him before chat does 🔥",
    "{name} STREAMING LIVE - this is your moment bro, full send 💪",
    "ALERT: {name} live in lobby! Everyone's watching, show them who's boss 🎯",
    
    # Hindi excited
    "{name} bhai LIVE hai! Chat dekh raha hai, usko finish karo 🔥",
    "LIVE HOGYA! {name} YouTube pe broadcast kar raha hai, thok do bhai 💥",
    "{name} ko dekh rahe hain sab, iska kya scene hai iska to pata chal jayega 🎮",
    
    # Strategic
    "Heads up: {name} is streaming this match live. Play smart 🧠",
    "{name} going live - use this as motivation, clean fights only 🎯",
    "Plot twist: {name} is live. Make every shot count 🎪",
    
    # Hindi strategic
    "{name} live streaming hai, iska matlab pura match dekha jayega 📹",
    "Iska audience dekh raha hai, toh iska ka scene banao 🎬",
    "{name} broadcast kar raha hai, toh clutch moment aata hai samne 🏆",
    
    # Casual
    "Btw {name} is live on YouTube right now lol",
    "{name} streaming - small world innit 🌍",
    "Just noticed {name} is going live, quite the coincidence",
    
    # Hindi casual
    "{name} bhai YouTube pe live hai, suno na bhai 📺",
    "Dekho na {name} live aa gaya, kya timing hai 😅",
    "{name} ke saath same lobby - YouTube pe content banega 🎥",
]

class BGMIVPSTracker:
    def __init__(self):
        self.service = None
        self.youtube_cache = {}
        self.tracked_opponents = {}
        self.processed_kills = set()
        self.commentary_index = 0
        self.browser = None
        self.ign = None
        self.teammates = []
        self.opponent_recheck = {}  # {name: next_check_time}
        
    def setup_gmail_auth(self):
        """Authenticate with Gmail"""
        print("[🔐] Setting up Gmail authentication...")
        
        try:
            creds = None
            
            if os.path.exists(TOKEN_FILE):
                with open(TOKEN_FILE, 'rb') as token:
                    creds = pickle.load(token)
            
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    print("[⚠️] credentials.json not found.")
                    print("[ℹ️] Download from: https://console.cloud.google.com/")
                    print("[ℹ️] OAuth 2.0 Client ID (Desktop app)")
                    flow = InstalledAppFlow.from_client_secrets_file(
                        CREDENTIALS_FILE, SCOPES)
                    creds = flow.run_local_server(port=0)
                
                with open(TOKEN_FILE, 'wb') as token:
                    pickle.dump(creds, token)
            
            print("[✓] Gmail authenticated")
            return creds
        
        except Exception as e:
            print(f"[✗] Auth failed: {e}")
            return None
    
    def init_brave_browser(self):
        """Launch Brave browser headless"""
        print("[🌐] Initializing Brave browser...")
        
        try:
            options = Options()
            options.binary_location = BRAVE_PATH
            options.add_argument("--headless")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            options.add_argument("--disable-ads")
            options.add_argument("--disable-popup-blocking")
            
            self.browser = webdriver.Chrome(options=options)
            print("[✓] Brave browser ready")
            return True
        
        except Exception as e:
            print(f"[⚠️] Brave init failed: {e}. Will use API fallback.")
            return False
    
    def detect_ign_and_teammates(self):
        """Auto-detect IGN and teammates from game"""
        print("[👤] Detecting IGN and teammates...")
        
        try:
            frame = self.capture_screen_fast()
            if frame is None:
                print("[⚠️] Cannot capture screen. Manual input needed.")
                self.ign = input("Enter your IGN: ").strip()
                self.teammates = input("Enter teammate IGNs (comma-separated): ").split(",")
                self.teammates = [t.strip() for t in self.teammates]
                return
            
            # Extract from top-left player info area
            player_region = frame[0:100, 0:400]
            gray = cv2.cvtColor(player_region, cv2.COLOR_BGR2GRAY)
            
            try:
                text = pytesseract.image_to_string(gray, config='--psm 11')
                lines = [line.strip() for line in text.split('\n') if line.strip()]
                
                if lines:
                    self.ign = lines[0]
                    self.teammates = lines[1:4] if len(lines) > 1 else []
                    print(f"[✓] IGN: {self.ign}")
                    print(f"[✓] Teammates: {', '.join(self.teammates)}")
                else:
                    raise ValueError("No text detected")
            
            except Exception as e:
                print(f"[⚠️] Auto-detect failed: {e}. Fallback to manual input.")
                self.ign = input("Enter your IGN: ").strip()
                self.teammates = input("Enter teammate IGNs (comma-separated): ").split(",")
                self.teammates = [t.strip() for t in self.teammates]
        
        except Exception as e:
            print(f"[✗] Detection error: {e}")
            self.ign = input("Enter your IGN: ").strip()
    
    def check_youtube_live_via_api(self, player_name, creds):
        """Check live status via YouTube API with creds"""
        now = time.time()
        
        # Check recheck timer
        recheck_time = self.opponent_recheck.get(player_name, 0)
        if now < recheck_time:
            cached = self.youtube_cache.get(player_name)
            if cached:
                return cached[0], cached[1]
            return False, None
        
        try:
            # Build YouTube API request
            url = "https://www.googleapis.com/youtube/v3/search"
            headers = {"Authorization": f"Bearer {creds.token}"}
            
            # Search for channel
            params = {
                "part": "snippet",
                "q": player_name,
                "type": "channel",
                "maxResults": 1
            }
            
            response = requests.get(url, headers=headers, params=params, timeout=5)
            data = response.json()
            
            if not data.get("items"):
                self.opponent_recheck[player_name] = now + YOUTUBE_RECHECK
                return False, None
            
            channel_id = data["items"][0]["id"]["channelId"]
            channel_name = data["items"][0]["snippet"]["title"]
            
            # Check if live
            live_params = {
                "part": "snippet",
                "channelId": channel_id,
                "eventType": "live",
                "type": "video",
                "maxResults": 1
            }
            
            live_response = requests.get(url, headers=headers, params=live_params, timeout=5)
            live_data = live_response.json()
            
            is_live = bool(live_data.get("items"))
            
            self.youtube_cache[player_name] = (is_live, channel_name)
            self.opponent_recheck[player_name] = now + YOUTUBE_RECHECK
            
            return is_live, channel_name
        
        except Exception as e:
            print(f"[⚠️] API check failed for {player_name}: {e}")
            self.opponent_recheck[player_name] = now + YOUTUBE_RECHECK
            return False, None
    
    def capture_screen_fast(self):
        """Fast screen capture for VPS"""
        try:
            import mss
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                screenshot = sct.grab(monitor)
                frame = cv2.cvtColor(
                    cv2.UMat(screenshot).get() if hasattr(screenshot, 'get') else screenshot,
                    cv2.COLOR_RGBA2BGR
                )
                return frame
        except:
            cap = cv2.VideoCapture(0)
            ret, frame = cap.read()
            cap.release()
            return frame if ret else None
    
    def extract_kill_feed(self, frame):
        """OCR kill feed"""
        if frame is None:
            return []
        
        # Adjust for your resolution
        feed_region = frame[80:500, 1000:1600]
        
        gray = cv2.cvtColor(feed_region, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)
        
        try:
            text = pytesseract.image_to_string(thresh, config='--psm 6')
            
            patterns = [
                r'(\w+)\s+(?:eliminated|killed|defeated|knocked)\s+(\w+)',
                r'(\w+)\s+>\s+(\w+)',
            ]
            
            matches = []
            for pattern in patterns:
                found = re.findall(pattern, text, re.IGNORECASE)
                matches.extend(found)
            
            return matches
        except:
            return []
    
    def is_self_or_teammate(self, name):
        """Filter self and teammates"""
        clean = name.lower().strip()
        
        if clean == self.ign.lower():
            return True
        if any(clean == t.lower() for t in self.teammates):
            return True
        
        return False
    
    def should_notify(self, name):
        """Check cooldown"""
        now = datetime.now()
        last = self.tracked_opponents.get(name)
        
        if last is None:
            return True
        
        return (now - last).total_seconds() >= SPAM_COOLDOWN
    
    def get_commentary(self, channel_name):
        """Rotate commentary"""
        msg = COMMENTARIES[self.commentary_index % len(COMMENTARIES)]
        self.commentary_index += 1
        return msg.format(name=channel_name)
    
    def process_kills(self, kills, creds):
        """Process kills with non-blocking checks"""
        for killer, victim in kills:
            opponents = []
            
            if not self.is_self_or_teammate(killer):
                opponents.append(killer)
            if not self.is_self_or_teammate(victim):
                opponents.append(victim)
            
            for opponent in opponents:
                kill_id = f"{opponent}_{int(time.time() / 60)}"
                
                if kill_id in self.processed_kills:
                    continue
                
                self.processed_kills.add(kill_id)
                
                if not self.should_notify(opponent):
                    continue
                
                # Non-blocking YouTube check
                threading.Thread(
                    target=self._async_check_and_notify,
                    args=(opponent, creds),
                    daemon=True
                ).start()
    
    def _async_check_and_notify(self, opponent, creds):
        """Async YouTube check"""
        is_live, channel_name = self.check_youtube_live_via_api(opponent, creds)
        
        if is_live and channel_name:
            commentary = self.get_commentary(channel_name)
            timestamp = datetime.now().strftime('%H:%M:%S')
            
            print(f"\n[🔴 LIVE] {commentary}")
            print(f"[⏱️] {timestamp}\n")
            
            self.tracked_opponents[opponent] = datetime.now()
    
    def run(self, creds):
        """Main loop"""
        print("\n" + "="*60)
        print("[✓] BGMI VPS Tracker Ready")
        print(f"[👤] IGN: {self.ign}")
        print(f"[👥] Teammates: {', '.join(self.teammates)}")
        print(f"[⏱️] Check interval: {CHECK_INTERVAL}s")
        print(f"[🔄] YouTube recheck: {YOUTUBE_RECHECK}s (10 mins)")
        print(f"[🔐] Authenticated: Yes")
        print("="*60 + "\n")
        
        try:
            while True:
                frame = self.capture_screen_fast()
                kills = self.extract_kill_feed(frame)
                
                if kills:
                    self.process_kills(kills, creds)
                
                time.sleep(CHECK_INTERVAL)
        
        except KeyboardInterrupt:
            print("\n[✓] Tracker stopped.")
            self.cleanup()
    
    def cleanup(self):
        """Cleanup"""
        if self.browser:
            self.browser.quit()
        
        with open("tracker_log.json", "w") as f:
            log = {
                "ign": self.ign,
                "teammates": self.teammates,
                "opponents": {
                    name: ts.isoformat() 
                    for name, ts in self.tracked_opponents.items()
                }
            }
            json.dump(log, f, indent=2)
        
        print("[✓] Data saved.")

def main():
    print("[🚀] BGMI VPS Tracker")
    print("[📋] Requirements: Brave browser, YouTube API credentials\n")
    
    tracker = BGMIVPSTracker()
    
    # Step 1: Auth
    creds = tracker.setup_gmail_auth()
    if not creds:
        print("[✗] Cannot proceed without authentication")
        return
    
    # Step 2: Brave browser (optional)
    tracker.init_brave_browser()
    
    # Step 3: Detect IGN
    tracker.detect_ign_and_teammates()
    
    # Step 4: Run
    tracker.run(creds)

if __name__ == "__main__":
    main()
