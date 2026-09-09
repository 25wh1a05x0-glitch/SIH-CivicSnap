# CivicSnap — Smart Civic Issue Reporting & Duplicate Detection Prototype

A next-generation civic issue reporting prototype designed for rapid response during municipal maintenance and urban disaster events (flooding, waterlogging, pothole clusters).

---

## 🌟 Core Features Implemented

### 1. One-Tap Photo / Video Capture
- **Live Camera Viewfinder**: Direct hardware camera stream using `navigator.mediaDevices.getUserMedia`.
- **Instant Snap**: One-tap shutter button captures photo, burns live GPS coordinates and timestamp onto the frame, and generates high-res snapshot.
- **Mobile Hardware Fallback**: `<input type="file" capture="environment">` fallback for seamless mobile browser support.

### 2. Auto-Captures GPS + Timestamp (EXIF & Device Sensors) — Anti-Fake / Anti-Old Photo
- **Live Geolocation Locking**: Automatically queries `navigator.geolocation.getCurrentPosition({ enableHighAccuracy: true })` with accuracy readout.
- **Hardware Timestamp Lock**: Timestamp is frozen at the millisecond the shutter button is pressed.
- **Server-Side EXIF Verification**: Server inspects EXIF metadata using Pillow `Image.getexif()` to detect recycled or archived photo uploads (>24 hours old). Live submissions receive the **"Verified Live Capture"** badge.

### 3. Short Voice-to-Text Multilingual Description (Hindi, Regional, English)
- **Web Speech API Integration**: Integrated with real-time continuous speech recognition.
- **Supported Indian Languages**:
  - **English (India)** (`en-IN`)
  - **हिन्दी (Hindi)** (`hi-IN`)
  - **मराठी (Marathi)** (`mr-IN`)
  - **தமிழ் (Tamil)** (`ta-IN`)
  - **বাংলা (Bengali)** (`bn-IN`)
- **Pulsing Mic Indicator**: Visual animated audio wave during dictation with real-time text injection into the editable description field.

### 4. Offline-First Capability (Disaster / Flooding Resilience)
- **Local Device Queue**: Built-in browser `IndexedDB` storage (`CivicSnapOfflineDB`).
- **Disaster Network Drop Handling**: Automatically detects when network connectivity drops (`offline` event) or simulates network loss via the presentation control bar.
- **Safe Persistence**: Citizen reports (photos in Base64, GPS coordinates, voice notes, timestamps) are preserved in local storage with zero network signal.
- **Auto-Sync & Manual Sync**: Automatically syncs queued reports in batch when internet returns (`/api/sync`), preventing municipal data loss.

### 5. Duplicate Detection & Auto-Merging within 50 Meters
- **Haversine Distance Engine**: Computes exact geodesic distance between reports.
- **Auto-Merge Window**: If multiple citizens report the same issue category within **50 meters**:
  - Automatically merges them into **1 Master Ticket**.
  - Recalculates the cluster centroid.
  - Dynamically escalates priority (1-2 reports = `NORMAL`, 3-4 reports = `HIGH`, 5+ reports = `CRITICAL`).
  - Aggregates evidence: view photos, individual timestamps, voice notes, and citizen distances side-by-side in the Evidence Drawer.
  - Prevents municipal dashboard clutter.

---

## 📁 Project Structure

```
SIH/
├── server.py             # FastAPI backend with SQLite, Haversine engine & EXIF analysis
├── test_duplicates.py    # Automated test suite validating 50m merging and priority escalation
├── requirements.txt      # Python dependencies
├── run.bat               # One-click Windows startup script
├── uploads/              # Storage directory for captured photos and videos
├── static/
│   ├── index.html        # Dual portal: Citizen Reporter & Municipal Command Center
│   ├── app.js            # Offline queue, speech recognition, camera, and Leaflet map logic
│   └── styles.css        # Responsive styling and animations
└── civic_reports.db      # SQLite database (master tickets & citizen reports)
```

---

## 🚀 How to Run the Prototype

### 1. Start the Server
Double click `run.bat` or run:
```powershell
python server.py
```
The server will start on: **`http://localhost:8000`**

### 2. Run Automated Verification Tests
```powershell
python test_duplicates.py
```
This tests:
- Haversine distance accuracy.
- 5 citizen reports submitted within 50m merging into 1 Master Ticket with `CRITICAL` priority.
- A 6th report 150m away correctly creating a distinct ticket.

---

## 🧪 Interactive Presentation Demonstration Guide

When showing this prototype to evaluators or judges, use the **Simulation Control Bar** at the top:

1. **Demonstrate 50m Duplicate Merging**:
   - Click the button: **`⚡ Simulate 5 Citizens within 50m`**
   - The system simulates 5 distinct citizens reporting a pothole within 35 meters (with voice transcripts in Hindi, Marathi, and English).
   - The view switches to the **Municipal Command Center**:
     - Observe the **Master Ticket** created with **`CRITICAL`** priority.
     - Observe the **50-meter translucent buffer zone** on the Leaflet map.
     - Click **"View Evidence (5 Citizens)"** to see all 5 photos, timestamps, Hindi/English speech notes, and distances from the center.

2. **Demonstrate Flooding / Offline Capability**:
   - Click **`📶 Simulate Flooding (Cut Signal)`** in the top bar.
   - The status pill turns red: **"Offline (Disaster Sim)"** and an alert banner appears.
   - Snap a photo and submit a report.
   - Notice the report is immediately saved in local device memory with a counter: `1 report queued locally`.
   - Click **`Restore Signal (Go Online)`**.
   - Notice the automatic batch upload (`/api/sync`) and success notification!

3. **Demonstrate Multilingual Voice-to-Text**:
   - In the Citizen Reporting tab, pick **हिन्दी (Hindi)** or **मराठी (Marathi)**.
   - Click the microphone button and speak.
   - Watch the transcribed text instantly appear in the text area.
