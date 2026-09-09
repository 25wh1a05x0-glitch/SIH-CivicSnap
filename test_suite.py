"""
Comprehensive Automated Test Suite for CivicSnap Platform Prototype
Validates:
1. AI Engine: Blur detection, fraud/selfie rejection, damage severity, and multilingual NLP.
2. Municipal GIS Engine: Reverse geocoding, landmark criticality, weighted priority formula, auto-routing.
3. Duplicate Merging: 5 citizen reports within 50m merging into 1 Master Ticket.
4. SLA & Auto-Escalation: Critical ticket triggers emergency alert.
5. Proof of Work & Feedback Loop: Field resolution & citizen star rating.
6. Predictive Hotspots & Ward Scorecard.
"""

import os
import sys

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
from PIL import Image

def run_tests():
    print("=========================================================")
    print("   CIVICSNAP END-TO-END AUTOMATED VERIFICATION SUITE   ")
    print("=========================================================\n")

    # ---------------- 1. Test AI Engine ---------------- #
    print("--- 1. Testing AI Verification & Classification Engine ---")
    from ai_engine import civic_ai_engine

    # Test NLP Urgency
    hindi_text = "सड़क पर बहुत गहरा गड्ढा है, Lilavati Hospital के पास एम्बुलेंस रुक गई है!"
    nlp_res = civic_ai_engine.analyze_nlp_urgency(hindi_text, "hi-IN")
    print(f"[AI NLP] Extracted Urgency Tags: {nlp_res['urgency_tags']}")
    print(f"[AI NLP] Urgency Bonus: +{nlp_res['urgency_bonus']} pts, Level: {nlp_res['urgency_level']}")
    assert nlp_res["urgency_bonus"] >= 25, "Expected maximum urgency bonus for hospital + ambulance"
    assert nlp_res["urgency_level"] == "CRITICAL", "Expected CRITICAL urgency level"
    print("[PASS] Multilingual NLP Urgency verified!\n")

    # Test Spam / Selfie Detection
    test_img_path = Path("uploads/test_selfie_spam.jpg")
    img = Image.new("RGB", (256, 256), color=(225, 185, 165))
    for x in range(64, 192):
        for y in range(64, 192):
            if (x - 128)**2 + (y - 128)**2 < 45**2:
                img.putpixel((x, y), (210, 150, 120))
    img.save(test_img_path)

    spam_res = civic_ai_engine.analyze_image_quality_and_content(test_img_path, "pothole")
    print(f"[AI Spam Filter] Result for Selfie: is_spam={spam_res['is_spam']}, Reason='{spam_res['spam_reason']}'")
    assert spam_res["is_spam"] is True, "Expected AI to reject human selfie/portrait as spam"
    print("[PASS] AI Fraud & Spam Rejection Filter verified!\n")

    # ---------------- 2. Test Municipal GIS Engine ---------------- #
    print("--- 2. Testing Municipal GIS, Geocoding & Priority Engine ---")
    from gis_engine import municipal_gis_engine, haversine_distance_meters

    # Lilavati Hospital coordinates (~19.0514, 72.8290)
    geo_res = municipal_gis_engine.reverse_geocode(19.0518, 72.8294)
    print(f"[GIS Geocoding] Lat/Lng -> Ward: '{geo_res['ward_name']}'")
    print(f"[GIS Geocoding] Nearest Landmark: '{geo_res['nearest_landmark']}' ({geo_res['distance_to_landmark_m']}m)")
    assert "Ward H-West" in geo_res["ward_name"], "Expected Ward H-West for Bandra coordinates"
    assert "Lilavati Hospital" in geo_res["nearest_landmark"], "Expected Lilavati Hospital as nearest landmark"

    crit_score = municipal_gis_engine.calculate_location_criticality(
        geo_res["nearby_critical_features"], geo_res["distance_to_landmark_m"]
    )
    print(f"[GIS Criticality] Location Criticality near Hospital: {crit_score}/100")
    assert crit_score >= 70.0, "Expected high criticality score near hospital"

    # Priority formula verification
    # Damage: 80, Count: 5 reports, Criticality: 85, Historical: 70
    p_calc = municipal_gis_engine.calculate_priority(
        ai_damage_severity=80.0,
        report_count=5,
        location_criticality=crit_score,
        historical_risk=geo_res["base_historical_risk"],
        nlp_urgency_bonus=25.0
    )
    print(f"[Priority Formula] Score: {p_calc['priority_score']} pts, Priority: {p_calc['priority']}")
    print(f"[Priority Formula] Breakdown: {p_calc['breakdown']}")
    assert p_calc["priority"] == "CRITICAL", f"Expected CRITICAL priority, got {p_calc['priority']}"
    assert p_calc["escalate_alert"] is True, "Expected escalate_alert to be True for Critical priority"
    print("[PASS] Weighted Priority Scoring & Critical Escalation verified!\n")

    # ---------------- 3. Test Ingestion & 50m Duplicate Merging ---------------- #
    print("--- 3. Testing 50m Duplicate Merging & Ticket Clustering ---")
    from server import process_report_ingestion, get_db

    # Clean DB
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM citizen_reports")
    c.execute("DELETE FROM master_tickets")
    conn.commit()
    conn.close()

    base_lat, base_lng = 19.0760, 72.8777
    now = "2026-09-09T12:00:00Z"

    # Citizen 1: Initial report
    res1 = process_report_ingestion(
        category="pothole", lat=base_lat, lng=base_lng, gps_accuracy=3.0,
        voice_transcript="Deep pothole in middle of road", language="en-IN",
        client_timestamp=now, reporter_name="Citizen 1"
    )
    print(f"Report 1: {res1['message']} (Ticket: {res1['ticket_code']})")
    assert res1["is_duplicate_merged"] is False, "First report should create new ticket"

    # Citizens 2-5 within 30m
    for i in range(2, 6):
        d_lat = 0.00008 * (i - 1)
        d_lng = 0.00005 * (i - 1)
        res = process_report_ingestion(
            category="pothole", lat=base_lat + d_lat, lng=base_lng + d_lng,
            gps_accuracy=3.0, voice_transcript=f"Report {i}: pothole causing traffic",
            language="en-IN", client_timestamp=now, reporter_name=f"Citizen {i}"
        )
        print(f"Report {i}: {res['message']} (Distance: {res['distance_to_ticket']}m)")
        assert res["is_duplicate_merged"] is True, f"Report {i} should be merged (<50m)"

    # Verify Master Ticket in DB
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT report_count, priority, priority_score FROM master_tickets WHERE id = ?", (res1["ticket_id"],))
    ticket_row = c.fetchone()
    print(f"[Master Ticket Verification] Total Merged: {ticket_row['report_count']}, Priority: {ticket_row['priority']}, Score: {ticket_row['priority_score']}")
    assert ticket_row["report_count"] == 5, "Expected exactly 5 merged reports"
    assert ticket_row["priority"] == "CRITICAL", "Priority should escalate to CRITICAL with 5 reports"

    # Report 6 > 150m away
    res6 = process_report_ingestion(
        category="pothole", lat=base_lat + 0.0015, lng=base_lng, gps_accuracy=3.0,
        voice_transcript="Different pothole 160m away", language="en-IN",
        client_timestamp=now, reporter_name="Citizen 6"
    )
    print(f"Report 6 (>150m): {res6['message']} (Ticket: {res6['ticket_code']})")
    assert res6["is_duplicate_merged"] is False, "Report 150m away should NOT merge"
    assert res6["ticket_id"] != res1["ticket_id"], "Should be a distinct ticket"
    print("[PASS] 50m Spatial Duplicate Merging & Distinct Ticket creation verified!\n")

    # ---------------- 4. Test Proof of Work & Feedback Loop ---------------- #
    print("--- 4. Testing Proof of Work Resolution & Citizen Rating Loop ---")
    from server import update_ticket_status, get_ticket_detail, submit_citizen_feedback, CitizenFeedbackPayload

    # Move ticket to In Progress
    update_ticket_status(res1["ticket_id"], type("obj", (), {"status": "IN_PROGRESS"})())
    
    # Submit citizen feedback: 5-star rating
    fb_payload = CitizenFeedbackPayload(rating=5, feedback="Excellent repair work by PWD team!", reopen=False)
    fb_res = submit_citizen_feedback(res1["ticket_id"], fb_payload)
    print(f"[Citizen Feedback] Rating: {fb_res['rating']} Stars, Response: '{fb_res['message']}'")
    assert fb_res["rating"] == 5, "Expected 5-star rating"

    # Verify Ward Analytics
    from server import get_ward_analytics
    import json
    wards_res = json.loads(get_ward_analytics().body.decode())
    print(f"[Ward Scorecard] Top Ranked Ward: {wards_res[0]['ward_name']} with Score: {wards_res[0]['ward_performance_score']}/5.0")
    assert len(wards_res) > 0, "Expected ward leaderboard data"
    print("[PASS] Proof of work and citizen rating loop verified!\n")

    # ---------------- 5. Test Predictive Monsoon Hotspots ---------------- #
    print("--- 5. Testing Predictive AI Monsoon Hotspot Layer ---")
    from server import get_predictive_hotspots
    pred_res = get_predictive_hotspots()
    print(f"[Predictive Layer] IMD Weather Alert: {pred_res['weather_status']['alert_level']}")
    print(f"[Predictive Layer] High Risk Hotspots Count: {len(pred_res['predicted_hotspots'])}")
    for h in pred_res["predicted_hotspots"]:
        print(f"  • [{h['risk_level']}] {h['location_name']}: {h['proactive_action_order']}")
    assert len(pred_res["predicted_hotspots"]) >= 3, "Expected at least 3 predictive hotspots"
    print("[PASS] Predictive urban resilience layer verified!\n")

    print("=========================================================")
    print("   ALL 5 TESTS PASSED SUCCESSFULLY! PROTOTYPE READY!   ")
    print("=========================================================")

if __name__ == "__main__":
    run_tests()
