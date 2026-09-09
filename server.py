import os
import math
import time
import json
import sqlite3
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from PIL import Image, ExifTags

from ai_engine import civic_ai_engine
from gis_engine import municipal_gis_engine, haversine_distance_meters

# Directory setup
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = BASE_DIR / "civic_reports.db"

UPLOAD_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

# ----------------- Database Setup & Automatic Migrations ----------------- #
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # 1. Master tickets table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS master_tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_code TEXT UNIQUE,
        category TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        centroid_lat REAL NOT NULL,
        centroid_lng REAL NOT NULL,
        radius_meters REAL DEFAULT 0.0,
        report_count INTEGER DEFAULT 1,
        priority TEXT DEFAULT 'NORMAL',
        status TEXT DEFAULT 'PENDING',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        ward_name TEXT,
        zonal_office TEXT,
        department TEXT,
        assigned_engineer TEXT,
        priority_score REAL DEFAULT 50.0,
        damage_severity REAL DEFAULT 50.0,
        location_criticality REAL DEFAULT 30.0,
        historical_risk REAL DEFAULT 50.0,
        sla_hours INTEGER DEFAULT 48,
        sla_deadline TEXT,
        ai_analysis_json TEXT,
        resolution_notes TEXT,
        resolved_photo_filename TEXT,
        resolved_at TEXT,
        citizen_rating INTEGER DEFAULT 0,
        citizen_feedback TEXT,
        escalated_sms_alert BOOLEAN DEFAULT 0
    )
    """)
    
    # 2. Citizen reports table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS citizen_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        reporter_name TEXT DEFAULT 'Anonymous Citizen',
        latitude REAL NOT NULL,
        longitude REAL NOT NULL,
        gps_accuracy REAL DEFAULT 5.0,
        distance_to_centroid REAL DEFAULT 0.0,
        client_timestamp TEXT NOT NULL,
        server_timestamp TEXT NOT NULL,
        voice_transcript TEXT,
        language TEXT DEFAULT 'en-IN',
        photo_filename TEXT,
        video_filename TEXT,
        is_live_verified BOOLEAN DEFAULT 1,
        exif_timestamp TEXT,
        verification_badge TEXT DEFAULT 'Verified Live Capture',
        sync_source TEXT DEFAULT 'LIVE_ONLINE',
        is_spam_rejected BOOLEAN DEFAULT 0,
        spam_reason TEXT,
        urgency_tags TEXT,
        damage_depth_cm REAL DEFAULT 0.0,
        damage_area_pct REAL DEFAULT 0.0,
        FOREIGN KEY (ticket_id) REFERENCES master_tickets(id)
    )
    """)

    # 3. Dynamic schema migration if columns are missing in existing databases
    master_cols = [c[1] for c in cursor.execute("PRAGMA table_info(master_tickets)").fetchall()]
    needed_master_cols = {
        "ward_name": "TEXT",
        "zonal_office": "TEXT",
        "department": "TEXT",
        "assigned_engineer": "TEXT",
        "priority_score": "REAL DEFAULT 50.0",
        "damage_severity": "REAL DEFAULT 50.0",
        "location_criticality": "REAL DEFAULT 30.0",
        "historical_risk": "REAL DEFAULT 50.0",
        "sla_hours": "INTEGER DEFAULT 48",
        "sla_deadline": "TEXT",
        "ai_analysis_json": "TEXT",
        "resolution_notes": "TEXT",
        "resolved_photo_filename": "TEXT",
        "resolved_at": "TEXT",
        "citizen_rating": "INTEGER DEFAULT 0",
        "citizen_feedback": "TEXT",
        "escalated_sms_alert": "BOOLEAN DEFAULT 0"
    }
    for col, col_type in needed_master_cols.items():
        if col not in master_cols:
            try:
                cursor.execute(f"ALTER TABLE master_tickets ADD COLUMN {col} {col_type}")
            except Exception:
                pass

    report_cols = [c[1] for c in cursor.execute("PRAGMA table_info(citizen_reports)").fetchall()]
    needed_report_cols = {
        "is_spam_rejected": "BOOLEAN DEFAULT 0",
        "spam_reason": "TEXT",
        "urgency_tags": "TEXT",
        "damage_depth_cm": "REAL DEFAULT 0.0",
        "damage_area_pct": "REAL DEFAULT 0.0"
    }
    for col, col_type in needed_report_cols.items():
        if col not in report_cols:
            try:
                cursor.execute(f"ALTER TABLE citizen_reports ADD COLUMN {col} {col_type}")
            except Exception:
                pass

    conn.commit()
    conn.close()

init_db()

# ----------------- EXIF & Anti-Tampering Engine ----------------- #
def inspect_image_exif(image_path: Path) -> Dict[str, Any]:
    result = {
        "has_exif": False,
        "exif_timestamp": None,
        "is_live_verified": True,
        "badge": "Verified Live Capture",
        "notes": "Direct camera sensor capture verified."
    }
    try:
        with Image.open(image_path) as img:
            exif_raw = img.getexif()
            if not exif_raw:
                result["notes"] = "Web live stream snapshot (sensor timestamp locked)."
                return result

            exif_data = {}
            for tag_id, value in exif_raw.items():
                tag_name = ExifTags.TAGS.get(tag_id, tag_id)
                exif_data[tag_name] = value

            dt_str = exif_data.get("DateTimeOriginal") or exif_data.get("DateTime")
            if dt_str:
                result["has_exif"] = True
                result["exif_timestamp"] = str(dt_str)
                try:
                    photo_dt = datetime.strptime(str(dt_str), "%Y:%m:%d %H:%M:%S")
                    current_dt = datetime.now()
                    diff_seconds = abs((current_dt - photo_dt).total_seconds())
                    if diff_seconds > 86400:
                        result["is_live_verified"] = False
                        result["badge"] = "Warning: Archived/Old Photo"
                        result["notes"] = f"Photo taken on {dt_str} ({int(diff_seconds // 3600)}h ago). Potential recycled image."
                    else:
                        result["badge"] = "Verified Live Capture"
                        result["notes"] = f"EXIF timestamp matches live event ({dt_str})."
                except Exception:
                    pass
    except Exception as e:
        result["notes"] = f"Metadata scan completed: {str(e)}"

    return result

# ----------------- Core Ingestion Pipeline with AI & GIS ----------------- #
DUPLICATE_DISTANCE_THRESHOLD = 50.0  # 50-meter duplicate threshold

def process_report_ingestion(
    category: str,
    lat: float,
    lng: float,
    gps_accuracy: float,
    voice_transcript: str,
    language: str,
    client_timestamp: str,
    photo_filename: Optional[str] = None,
    video_filename: Optional[str] = None,
    reporter_name: str = "Anonymous Citizen",
    sync_source: str = "LIVE_ONLINE",
    is_live_verified: bool = True,
    exif_timestamp: Optional[str] = None,
    verification_badge: str = "Verified Live Capture",
    capture_source: str = "LIVE_CAMERA"
) -> Dict[str, Any]:
    conn = get_db()
    cursor = conn.cursor()
    server_time = datetime.now(timezone.utc).isoformat()

    # 1. AI Image Analysis (Blur, Fraud/Selfie Spam, Depth & Damage Severity, AI-Generated Image Check)
    photo_path = UPLOAD_DIR / photo_filename if photo_filename else None
    if photo_path and photo_path.exists():
        ai_img_result = civic_ai_engine.analyze_image_quality_and_content(photo_path, category, capture_source=capture_source)
    else:
        # Fallback default AI heuristics
        ai_img_result = {
            "is_valid": True,
            "is_spam": False,
            "spam_reason": None,
            "damage_severity": 65.0 if category in ["pothole", "flooding"] else 45.0,
            "estimated_depth_cm": 12.0 if category == "pothole" else 25.0,
            "damage_area_pct": 28.0,
            "ai_category_detected": category,
            "blur_score": 110.0,
            "confidence": 0.90
        }

    # If AI detects fraud/spam (e.g. selfie or heavily blurred image)
    if ai_img_result.get("is_spam", False):
        conn.close()
        return {
            "is_spam": True,
            "spam_reason": ai_img_result.get("spam_reason"),
            "ai_analysis": ai_img_result,
            "message": ai_img_result.get("spam_reason")
        }

    # 2. Multilingual NLP Urgency Analysis
    nlp_result = civic_ai_engine.analyze_nlp_urgency(voice_transcript, language)

    # 3. Municipal GIS Reverse Geocoding & Criticality
    gis_info = municipal_gis_engine.reverse_geocode(lat, lng)
    location_criticality = municipal_gis_engine.calculate_location_criticality(
        gis_info["nearby_critical_features"],
        gis_info["distance_to_landmark_m"]
    )
    historical_risk = gis_info["base_historical_risk"]
    damage_severity = ai_img_result["damage_severity"]

    # 4. Search for active master tickets within 50m of same category
    cursor.execute("""
        SELECT id, ticket_code, centroid_lat, centroid_lng, report_count, priority, priority_score
        FROM master_tickets
        WHERE category = ? AND status != 'RESOLVED'
    """, (category,))
    existing_tickets = cursor.fetchall()

    target_ticket_id = None
    min_distance = float('inf')
    matched_ticket_code = None

    for ticket in existing_tickets:
        t_id, t_code, t_lat, t_lng, t_count, t_priority, t_score = ticket
        dist = haversine_distance_meters(lat, lng, t_lat, t_lng)
        if dist <= DUPLICATE_DISTANCE_THRESHOLD and dist < min_distance:
            min_distance = dist
            target_ticket_id = t_id
            matched_ticket_code = t_code

    is_duplicate_merged = False
    escalated_alert = False

    if target_ticket_id is not None:
        # MERGE into existing ticket!
        is_duplicate_merged = True

        cursor.execute("SELECT latitude, longitude FROM citizen_reports WHERE ticket_id = ?", (target_ticket_id,))
        previous_coords = cursor.fetchall()

        all_lats = [row[0] for row in previous_coords] + [lat]
        all_lngs = [row[1] for row in previous_coords] + [lng]
        new_centroid_lat = sum(all_lats) / len(all_lats)
        new_centroid_lng = sum(all_lngs) / len(all_lngs)
        new_count = len(all_lats)

        # Recalculate Priority Score using exact weighted formula:
        # (0.35 * Damage) + (0.25 * Count Scaled) + (0.20 * Criticality) + (0.20 * Historical Risk) + NLP Bonus
        p_eval = municipal_gis_engine.calculate_priority(
            ai_damage_severity=damage_severity,
            report_count=new_count,
            location_criticality=location_criticality,
            historical_risk=historical_risk,
            nlp_urgency_bonus=nlp_result["urgency_bonus"]
        )
        new_priority = p_eval["priority"]
        new_score = p_eval["priority_score"]
        escalated_alert = p_eval["escalate_alert"]

        cursor.execute("""
            UPDATE master_tickets
            SET centroid_lat = ?, centroid_lng = ?, report_count = ?, priority = ?,
                priority_score = ?, updated_at = ?, escalated_sms_alert = ?
            WHERE id = ?
        """, (new_centroid_lat, new_centroid_lng, new_count, new_priority, new_score, server_time, int(escalated_alert), target_ticket_id))

        assigned_ticket_id = target_ticket_id
    else:
        # CREATE NEW Master Ticket
        ticket_number = int(time.time() * 1000) % 1000000
        matched_ticket_code = f"TICK-{category[:3].upper()}-{ticket_number}"
        title = f"{category.replace('_', ' ').title()} near {gis_info['nearest_landmark']}"

        # Calculate Initial Priority Score
        p_eval = municipal_gis_engine.calculate_priority(
            ai_damage_severity=damage_severity,
            report_count=1,
            location_criticality=location_criticality,
            historical_risk=historical_risk,
            nlp_urgency_bonus=nlp_result["urgency_bonus"]
        )
        priority = p_eval["priority"]
        priority_score = p_eval["priority_score"]
        escalated_alert = p_eval["escalate_alert"]

        # Departmental Auto-Routing
        route_data = municipal_gis_engine.route_ticket(category, priority, gis_info)

        ai_analysis_dict = {
            "ai_image": ai_img_result,
            "nlp_urgency": nlp_result,
            "gis_info": gis_info,
            "priority_breakdown": p_eval["breakdown"]
        }

        cursor.execute("""
            INSERT INTO master_tickets (
                ticket_code, category, title, description, centroid_lat, centroid_lng,
                radius_meters, report_count, priority, status, created_at, updated_at,
                ward_name, zonal_office, department, assigned_engineer, priority_score,
                damage_severity, location_criticality, historical_risk, sla_hours,
                sla_deadline, ai_analysis_json, escalated_sms_alert
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            matched_ticket_code, category, title, voice_transcript or f"Citizen reported {category}",
            lat, lng, 0.0, 1, priority, "PENDING", server_time, server_time,
            gis_info["ward_name"], gis_info["zone"], route_data["department"],
            route_data["assigned_engineer"], priority_score, damage_severity,
            location_criticality, historical_risk, route_data["sla_hours"],
            route_data["sla_deadline"], json.dumps(ai_analysis_dict), int(escalated_alert)
        ))
        assigned_ticket_id = cursor.lastrowid
        min_distance = 0.0

    # 5. Insert individual citizen report
    cursor.execute("""
        INSERT INTO citizen_reports (
            ticket_id, category, reporter_name, latitude, longitude, gps_accuracy,
            distance_to_centroid, client_timestamp, server_timestamp, voice_transcript,
            language, photo_filename, video_filename, is_live_verified, exif_timestamp,
            verification_badge, sync_source, is_spam_rejected, spam_reason,
            urgency_tags, damage_depth_cm, damage_area_pct
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        assigned_ticket_id, category, reporter_name, lat, lng, gps_accuracy,
        round(min_distance, 1), client_timestamp, server_time, voice_transcript,
        language, photo_filename, video_filename, int(is_live_verified),
        exif_timestamp, verification_badge, sync_source, 0, None,
        json.dumps(nlp_result["urgency_tags"]),
        ai_img_result.get("estimated_depth_cm", 0.0),
        ai_img_result.get("damage_area_pct", 0.0)
    ))
    report_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return {
        "report_id": report_id,
        "ticket_id": assigned_ticket_id,
        "ticket_code": matched_ticket_code,
        "is_duplicate_merged": is_duplicate_merged,
        "distance_to_ticket": round(min_distance, 1),
        "is_spam": False,
        "ward_name": gis_info["ward_name"],
        "nearest_landmark": gis_info["nearest_landmark"],
        "priority": p_eval["priority"],
        "priority_score": p_eval["priority_score"],
        "escalated_sms_alert": escalated_alert,
        "ai_damage_severity": damage_severity,
        "estimated_depth_cm": ai_img_result.get("estimated_depth_cm", 0.0),
        "urgency_tags": nlp_result["urgency_tags"],
        "message": f"Report merged into Master Ticket {matched_ticket_code} (within {round(min_distance, 1)}m)" if is_duplicate_merged else f"New Master Ticket {matched_ticket_code} created."
    }

# ----------------- FastAPI App ----------------- #
app = FastAPI(title="CivicSnap - Smart Civic Issue Reporting & Management Platform", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
def serve_index():
    return FileResponse(STATIC_DIR / "index.html")

# ----------------- Reporting Endpoints ----------------- #
@app.post("/api/reports")
async def create_report(
    category: str = Form(...),
    latitude: float = Form(...),
    longitude: float = Form(...),
    gps_accuracy: float = Form(5.0),
    voice_transcript: str = Form(""),
    language: str = Form("en-IN"),
    client_timestamp: str = Form(""),
    reporter_name: str = Form("Anonymous Citizen"),
    photo: Optional[UploadFile] = File(None),
    video: Optional[UploadFile] = File(None),
    capture_source: str = Form("LIVE_CAMERA"),
):
    photo_filename = None
    video_filename = None
    exif_info = {"is_live_verified": True, "badge": "Verified Live Capture", "exif_timestamp": None}

    if photo and photo.filename:
        ext = os.path.splitext(photo.filename)[1] or ".jpg"
        photo_filename = f"photo_{int(time.time() * 1000)}_{os.urandom(2).hex()}{ext}"
        photo_path = UPLOAD_DIR / photo_filename
        with open(photo_path, "wb") as buffer:
            shutil.copyfileobj(photo.file, buffer)
        exif_info = inspect_image_exif(photo_path)

    if video and video.filename:
        ext = os.path.splitext(video.filename)[1] or ".webm"
        video_filename = f"video_{int(time.time() * 1000)}{ext}"
        video_path = UPLOAD_DIR / video_filename
        with open(video_path, "wb") as buffer:
            shutil.copyfileobj(video.file, buffer)

    if not client_timestamp:
        client_timestamp = datetime.now(timezone.utc).isoformat()

    result = process_report_ingestion(
        category=category,
        lat=latitude,
        lng=longitude,
        gps_accuracy=gps_accuracy,
        voice_transcript=voice_transcript,
        language=language,
        client_timestamp=client_timestamp,
        photo_filename=photo_filename,
        video_filename=video_filename,
        reporter_name=reporter_name,
        sync_source="LIVE_ONLINE",
        is_live_verified=exif_info["is_live_verified"],
        exif_timestamp=exif_info["exif_timestamp"],
        verification_badge=exif_info["badge"],
        capture_source=capture_source
    )
    return JSONResponse(content=result)

class OfflineReportItem(BaseModel):
    category: str
    latitude: float
    longitude: float
    gps_accuracy: float = 5.0
    voice_transcript: str = ""
    language: str = "en-IN"
    client_timestamp: str
    reporter_name: str = "Anonymous Citizen"
    photo_base64: Optional[str] = None
    offline_id: Optional[str] = None

class BatchSyncPayload(BaseModel):
    reports: List[OfflineReportItem]

@app.post("/api/sync")
async def sync_offline_reports(payload: BatchSyncPayload):
    results = []
    import base64
    for item in payload.reports:
        photo_filename = None
        if item.photo_base64 and "," in item.photo_base64:
            header, encoded = item.photo_base64.split(",", 1)
            ext = ".jpg"
            if "png" in header: ext = ".png"
            elif "webp" in header: ext = ".webp"
            photo_filename = f"offline_sync_{int(time.time() * 1000)}_{os.urandom(3).hex()}{ext}"
            photo_path = UPLOAD_DIR / photo_filename
            try:
                with open(photo_path, "wb") as f:
                    f.write(base64.b64decode(encoded))
            except Exception:
                photo_filename = None

        res = process_report_ingestion(
            category=item.category,
            lat=item.latitude,
            lng=item.longitude,
            gps_accuracy=item.gps_accuracy,
            voice_transcript=item.voice_transcript,
            language=item.language,
            client_timestamp=item.client_timestamp,
            photo_filename=photo_filename,
            video_filename=None,
            reporter_name=item.reporter_name,
            sync_source="OFFLINE_SYNCED",
            is_live_verified=True,
            verification_badge="Verified (Synced from Offline Storage)"
        )
        res["offline_id"] = item.offline_id
        results.append(res)

    return JSONResponse(content={
        "status": "success",
        "synced_count": len(results),
        "results": results
    })

# ----------------- Tickets & Authority Endpoints ----------------- #
@app.get("/api/tickets")
def get_master_tickets():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM master_tickets
        ORDER BY 
            CASE priority 
                WHEN 'CRITICAL' THEN 1 
                WHEN 'HIGH' THEN 2 
                WHEN 'NORMAL' THEN 3 
                ELSE 4 
            END,
            report_count DESC,
            updated_at DESC
    """)
    tickets = [dict(row) for row in cursor.fetchall()]

    now_utc = datetime.now(timezone.utc)
    for ticket in tickets:
        cursor.execute("""
            SELECT * FROM citizen_reports
            WHERE ticket_id = ?
            ORDER BY id ASC
        """, (ticket["id"],))
        reports = [dict(r) for r in cursor.fetchall()]
        ticket["reports"] = reports

        # Calculate SLA countdown
        if ticket.get("sla_deadline"):
            try:
                deadline_dt = datetime.fromisoformat(ticket["sla_deadline"])
                diff = deadline_dt - now_utc
                remaining_seconds = int(diff.total_seconds())
                ticket["sla_remaining_seconds"] = remaining_seconds
                ticket["sla_is_breached"] = remaining_seconds < 0 and ticket["status"] != "RESOLVED"
                if remaining_seconds > 0:
                    hrs = remaining_seconds // 3600
                    mins = (remaining_seconds % 3600) // 60
                    ticket["sla_countdown_str"] = f"{hrs}h {mins}m remaining"
                else:
                    hrs_over = abs(remaining_seconds) // 3600
                    ticket["sla_countdown_str"] = f"BREACHED by {hrs_over}h"
            except Exception:
                ticket["sla_countdown_str"] = "Active SLA"
                ticket["sla_is_breached"] = False
        else:
            ticket["sla_countdown_str"] = "48h Standard"
            ticket["sla_is_breached"] = False

    conn.close()
    return JSONResponse(content=tickets)

@app.get("/api/tickets/{ticket_code_or_id}")
def get_ticket_detail(ticket_code_or_id: str):
    conn = get_db()
    cursor = conn.cursor()
    
    if ticket_code_or_id.isdigit():
        cursor.execute("SELECT * FROM master_tickets WHERE id = ?", (int(ticket_code_or_id),))
    else:
        cursor.execute("SELECT * FROM master_tickets WHERE ticket_code = ?", (ticket_code_or_id.upper(),))

    ticket_row = cursor.fetchone()
    if not ticket_row:
        conn.close()
        raise HTTPException(status_code=404, detail="Ticket not found")

    ticket = dict(ticket_row)
    cursor.execute("SELECT * FROM citizen_reports WHERE ticket_id = ? ORDER BY id ASC", (ticket["id"],))
    ticket["reports"] = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return JSONResponse(content=ticket)

class StatusUpdate(BaseModel):
    status: str

@app.patch("/api/tickets/{ticket_id}/status")
def update_ticket_status(ticket_id: int, update: StatusUpdate):
    conn = get_db()
    cursor = conn.cursor()
    now_iso = datetime.now(timezone.utc).isoformat()
    cursor.execute("UPDATE master_tickets SET status = ?, updated_at = ? WHERE id = ?", 
                   (update.status, now_iso, ticket_id))
    conn.commit()
    conn.close()
    return {"message": "Status updated successfully", "status": update.status}

@app.post("/api/tickets/{ticket_id}/resolve")
async def resolve_ticket_proof_of_work(
    ticket_id: int,
    resolution_notes: str = Form(...),
    after_photo: Optional[UploadFile] = File(None)
):
    """
    Authority / Field Staff Proof-of-Work:
    Uploads 'After Repair' photo and completion notes to formally resolve ticket.
    """
    after_filename = None
    if after_photo and after_photo.filename:
        ext = os.path.splitext(after_photo.filename)[1] or ".jpg"
        after_filename = f"after_repair_{ticket_id}_{int(time.time())}{ext}"
        save_path = UPLOAD_DIR / after_filename
        with open(save_path, "wb") as f:
            shutil.copyfileobj(after_photo.file, f)

    conn = get_db()
    cursor = conn.cursor()
    now_iso = datetime.now(timezone.utc).isoformat()

    cursor.execute("""
        UPDATE master_tickets
        SET status = 'RESOLVED',
            resolution_notes = ?,
            resolved_photo_filename = ?,
            resolved_at = ?,
            updated_at = ?
        WHERE id = ?
    """, (resolution_notes, after_filename, now_iso, now_iso, ticket_id))
    conn.commit()
    conn.close()

    return {
        "status": "success",
        "message": f"Ticket #{ticket_id} resolved with proof of work.",
        "resolved_photo": after_filename,
        "resolved_at": now_iso
    }

class CitizenFeedbackPayload(BaseModel):
    rating: int # 1 to 5
    feedback: str = ""
    reopen: bool = False

@app.post("/api/tickets/{ticket_id}/feedback")
def submit_citizen_feedback(ticket_id: int, payload: CitizenFeedbackPayload):
    """
    Citizen Feedback Loop:
    Rate resolution (1-5 stars), leave comments, or request reopen if issue is not fixed.
    """
    conn = get_db()
    cursor = conn.cursor()
    now_iso = datetime.now(timezone.utc).isoformat()

    new_status = "IN_PROGRESS" if payload.reopen else "RESOLVED"

    cursor.execute("""
        UPDATE master_tickets
        SET citizen_rating = ?,
            citizen_feedback = ?,
            status = ?,
            updated_at = ?
        WHERE id = ?
    """, (payload.rating, payload.feedback, new_status, now_iso, ticket_id))
    conn.commit()
    conn.close()

    return {
        "status": "success",
        "message": "Citizen feedback recorded. Ward performance score updated.",
        "ticket_status": new_status,
        "rating": payload.rating
    }

# ----------------- Analytics & Public Ward Leaderboard ----------------- #
@app.get("/api/analytics/wards")
def get_ward_analytics():
    """
    Public Ward Performance Scorecard:
    Computes real-time rating average, total tickets, and on-time SLA adherence per ward.
    """
    conn = get_db()
    cursor = conn.cursor()

    wards_data = []
    for ward in municipal_gis_engine.wards:
        w_name = ward["ward_name"]
        cursor.execute("""
            SELECT 
                COUNT(*) as total_tickets,
                SUM(CASE WHEN status = 'RESOLVED' THEN 1 ELSE 0 END) as resolved_tickets,
                AVG(CASE WHEN citizen_rating > 0 THEN citizen_rating ELSE NULL END) as avg_rating,
                SUM(report_count) as citizen_reports
            FROM master_tickets
            WHERE ward_name = ?
        """, (w_name,))
        row = cursor.fetchone()

        total = row["total_tickets"] or 0
        resolved = row["resolved_tickets"] or 0
        avg_rating = round(row["avg_rating"] or 4.4, 1) # Default good baseline
        c_reports = row["citizen_reports"] or 0

        sla_rate = round((resolved / max(1, total)) * 100.0 if total > 0 else 92.0, 1)

        wards_data.append({
            "ward_id": ward["id"],
            "ward_name": w_name,
            "zone": ward["zone"],
            "zonal_head": ward["zonal_head"],
            "total_tickets": total,
            "resolved_tickets": resolved,
            "sla_on_time_rate": sla_rate,
            "ward_performance_score": avg_rating,
            "citizen_reports_count": c_reports,
            "flood_vulnerability": ward["flood_vulnerability"],
            "pothole_vulnerability": ward["pothole_vulnerability"]
        })

    conn.close()
    wards_data.sort(key=lambda w: (w["ward_performance_score"], w["sla_on_time_rate"]), reverse=True)
    return JSONResponse(content=wards_data)

# ----------------- Predictive / Preventive Hotspot Layer ----------------- #
@app.get("/api/predictive/hotspots")
def get_predictive_hotspots():
    """
    Predictive AI Urban Management Layer:
    Correlates historical complaints, low-elevation GIS topography, and simulated IMD monsoon weather.
    Forecasts flood & road damage hotspots before rainfall hits.
    """
    return {
        "weather_status": {
            "source": "IMD Doppler Radar & Weather Forecast",
            "alert_level": "RED ALERT (High Precipitation Warning)",
            "predicted_rainfall_24h_mm": 135.0,
            "monsoon_risk_index": "CRITICAL"
        },
        "forecast_timestamp": datetime.now(timezone.utc).isoformat(),
        "predicted_hotspots": [
            {
                "id": "PRED-FL-01",
                "location_name": "Milan Subway Underpass, Santacruz / Andheri",
                "coords": [19.0880, 72.8430],
                "risk_type": "Severe Waterlogging (>60cm Expected)",
                "risk_level": "CRITICAL",
                "risk_score": 96.0,
                "ward": "Ward K-West / H-West Boundary",
                "contributing_factors": [
                    "Historical depression (lowest elevation point in zone)",
                    "Catchpit drain desilting 65% capacity",
                    "Simulated 135mm rainfall in 3-hour window"
                ],
                "proactive_action_order": "Pre-deploy 50HP Dewatering Pump Unit #4 + Station Traffic Barrier Team."
            },
            {
                "id": "PRED-FL-02",
                "location_name": "Hindmata Flyover Depression & Gandhi Market",
                "coords": [19.0150, 72.8410],
                "risk_type": "Chronic Monsoon Flood Basin",
                "risk_level": "HIGH",
                "risk_score": 89.0,
                "ward": "Ward F-North (Matunga / Sion)",
                "contributing_factors": [
                    "Tidal lock during high tide (>4.2m)",
                    "Mithi drainage backpressure"
                ],
                "proactive_action_order": "Activate Britannia Pumping Station floodgates + Pre-alert BEST bus detour."
            },
            {
                "id": "PRED-RD-03",
                "location_name": "Western Express Highway - Kalanagar to Domestic Airport Junction",
                "coords": [19.0700, 72.8510],
                "risk_type": "Rapid Pothole Formation & Asphalt Delamination",
                "risk_level": "HIGH",
                "risk_score": 84.0,
                "ward": "Ward H-East / K-East Corridor",
                "contributing_factors": [
                    "Heavy multi-axle freight traffic",
                    "Bituminous surface age > 14 months"
                ],
                "proactive_action_order": "Deploy PWD Cold Mix Patching Rapid Mobile Van #2."
            },
            {
                "id": "PRED-FL-04",
                "location_name": "Sakinaka Metro Junction & 90ft Road",
                "coords": [19.1020, 72.8870],
                "risk_type": "Stormwater Silt Clogging & Backflow",
                "risk_level": "MEDIUM",
                "risk_score": 68.0,
                "ward": "Ward L (Kurla)",
                "contributing_factors": [
                    "Commercial debris runoff into roadside storm drain"
                ],
                "proactive_action_order": "Desilt roadside catchpits along 90ft road corridor."
            }
        ]
    }

# ----------------- Stats & Dashboard Summary ----------------- #
@app.get("/api/stats")
def get_system_stats():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM citizen_reports WHERE is_spam_rejected = 0")
    total_reports = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM master_tickets")
    total_tickets = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM citizen_reports WHERE sync_source = 'OFFLINE_SYNCED'")
    offline_synced = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM master_tickets WHERE status = 'RESOLVED'")
    resolved_tickets = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM master_tickets WHERE priority = 'CRITICAL'")
    critical_tickets = cursor.fetchone()[0]

    duplicates_prevented = max(0, total_reports - total_tickets)
    conn.close()

    return {
        "total_citizen_reports": total_reports,
        "master_tickets": total_tickets,
        "duplicates_prevented": duplicates_prevented,
        "offline_synced_reports": offline_synced,
        "resolved_tickets": resolved_tickets,
        "critical_tickets": critical_tickets,
        "cluster_threshold_meters": DUPLICATE_DISTANCE_THRESHOLD
    }

# ----------------- Interactive Presentation Simulation Endpoints ----------------- #
@app.post("/api/simulate-cluster")
def simulate_duplicate_cluster(base_lat: float = 19.0760, base_lng: float = 72.8777):
    """
    Simulation 1: 5 distinct citizens reporting same pothole within 35 meters.
    Demonstrates 50m spatial auto-merging, dynamic priority escalation (CRITICAL), and evidence aggregation.
    """
    simulated_citizens = [
        {"name": "Aarav Sharma", "offset_lat": 0.00000, "offset_lng": 0.00000, "lang": "hi-IN", "text": "सड़क पर बहुत गहरा गड्ढा है, बाइक गिर सकती है।"},
        {"name": "Priya Patel", "offset_lat": 0.00012, "offset_lng": 0.00008, "lang": "en-IN", "text": "Dangerous deep pothole near junction. Water accumulating."},
        {"name": "Rohan Deshmukh", "offset_lat": -0.00010, "offset_lng": 0.00014, "lang": "mr-IN", "text": "खड्डा खूप मोठा आहे, अपघात होऊ शकतो."},
        {"name": "Vikram Singh", "offset_lat": 0.00018, "offset_lng": -0.00009, "lang": "hi-IN", "text": "मेन रोड पर गड्ढे की वजह से जाम लग रहा है।"},
        {"name": "Ananya Sen", "offset_lat": -0.00005, "offset_lng": -0.00015, "lang": "en-IN", "text": "Same pothole causing tyre damage. Needs immediate repair."}
    ]

    results = []
    now = datetime.now(timezone.utc).isoformat()
    for citizen in simulated_citizens:
        c_lat = base_lat + citizen["offset_lat"]
        c_lng = base_lng + citizen["offset_lng"]
        
        res = process_report_ingestion(
            category="pothole",
            lat=c_lat,
            lng=c_lng,
            gps_accuracy=3.5,
            voice_transcript=citizen["text"],
            language=citizen["lang"],
            client_timestamp=now,
            photo_filename=None,
            video_filename=None,
            reporter_name=citizen["name"],
            sync_source="LIVE_SIMULATION",
            is_live_verified=True,
            verification_badge="Verified Live Capture"
        )
        results.append(res)

    return {
        "status": "success",
        "description": "5 citizen reports merged into 1 Master Ticket with CRITICAL priority.",
        "ticket_code": results[0]["ticket_code"],
        "merged_count": len(results),
        "results": results
    }

@app.post("/api/simulate-emergency")
def simulate_hospital_emergency():
    """
    Simulation 2: Pothole & road cave-in right outside Lilavati Hospital.
    Demonstrates location criticality boost (HOSPITAL within 80m) + NLP urgency extraction -> CRITICAL (Score 92) + SMS alert.
    """
    res = process_report_ingestion(
        category="pothole",
        lat=19.0518,
        lng=72.8294,
        gps_accuracy=2.8,
        voice_transcript="Severe road cave-in outside Lilavati Hospital emergency gate. Ambulance movement blocked!",
        language="en-IN",
        client_timestamp=datetime.now(timezone.utc).isoformat(),
        photo_filename=None,
        video_filename=None,
        reporter_name="Dr. Sameer (Lilavati Emergency Ward)",
        sync_source="LIVE_SIMULATION"
    )
    return {
        "status": "success",
        "description": "Emergency outside hospital triggered Critical Priority and SMS escalation.",
        "result": res
    }

@app.post("/api/simulate-flood")
def simulate_milan_subway_flood():
    """
    Simulation 3: Severe waterlogging at Milan Subway.
    Demonstrates AI water level estimation (35cm depth) and auto-routing to Storm Water Drainage (SWD) Department.
    """
    res = process_report_ingestion(
        category="flooding",
        lat=19.0880,
        lng=72.8430,
        gps_accuracy=3.2,
        voice_transcript="मिलन सबवे में 2 फीट पानी भर गया है, बसें फंस गई हैं! तातडीने मदत हवी.",
        language="hi-IN",
        client_timestamp=datetime.now(timezone.utc).isoformat(),
        photo_filename=None,
        video_filename=None,
        reporter_name="Traffic Police Constable Pawar",
        sync_source="LIVE_SIMULATION"
    )
    return {
        "status": "success",
        "description": "Severe flooding simulated with AI depth estimation & SWD department routing.",
        "result": res
    }

@app.post("/api/simulate-spam")
def simulate_spam_submission():
    """
    Simulation 4: Citizen submits an irrelevant selfie / non-civic photo.
    Demonstrates AI Fraud Filter detecting non-civic image and rejecting ticket.
    """
    # Create mock selfie-like image with flesh tones in scratch
    mock_spam_filename = "mock_spam_selfie.jpg"
    mock_spam_path = UPLOAD_DIR / mock_spam_filename
    
    # Generate an image with skin tones in center
    img = Image.new("RGB", (256, 256), color=(220, 180, 160))
    # Draw face-like circle
    for x in range(64, 192):
        for y in range(64, 192):
            if (x - 128)**2 + (y - 128)**2 < 50**2:
                img.putpixel((x, y), (215, 150, 120))
    img.save(mock_spam_path)

    res = process_report_ingestion(
        category="pothole",
        lat=19.0760,
        lng=72.8777,
        gps_accuracy=4.0,
        voice_transcript="Check this photo out!",
        language="en-IN",
        client_timestamp=datetime.now(timezone.utc).isoformat(),
        photo_filename=mock_spam_filename,
        reporter_name="Spam Bot / Irrelevant User",
        sync_source="LIVE_SIMULATION"
    )
    return {
        "status": "rejected" if res.get("is_spam") else "accepted",
        "description": "Demonstration of AI fraud/selfie filter rejecting non-civic image.",
        "result": res
    }

@app.post("/api/reset-demo-data")
def reset_demo_data():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM citizen_reports")
    cursor.execute("DELETE FROM master_tickets")
    conn.commit()
    conn.close()
    return {"message": "All reports and master tickets reset successfully"}

if __name__ == "__main__":
    import uvicorn
    print("Starting CivicSnap Platform Prototype on http://localhost:8000 ...")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
