@echo off
echo ======================================================================
echo Starting CivicSnap Prototype (FastAPI + PWA)
echo ======================================================================
echo Features:
echo  1. One-tap Photo/Video Capture
echo  2. Auto GPS + Timestamp (EXIF Anti-Fake Verification)
echo  3. Multilingual Voice-to-Text (Hindi, Regional, English)
echo  4. Offline-First Queue (Disaster / Flooding Resilience)
echo  5. 50-Meter Duplicate Detection & Ticket Auto-Merging
echo ======================================================================
echo.
python -m pip install -r requirements.txt
python server.py
pause
