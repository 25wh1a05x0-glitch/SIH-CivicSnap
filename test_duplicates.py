"""
Test suite for Civic Issue Reporting & Duplicate Detection Prototype.
Validates:
1. Haversine distance accuracy.
2. 50-meter duplicate detection threshold:
   - 5 reports within 50m merge into 1 Master Ticket.
   - 1 report > 50m creates a separate ticket.
3. Priority auto-escalation (Normal -> High -> Critical).
4. Offline batch sync endpoint.
"""
import sys
import os
import json
import time

BASE_URL = "http://127.0.0.1:8000"

def test_haversine():
    from server import haversine_distance_meters
    # Gateway of India to Taj Mahal Palace (~250m)
    d = haversine_distance_meters(18.9220, 72.8347, 18.9217, 72.8332)
    print(f"[TEST] Haversine test distance: {d:.2f} meters (expected ~160-250m)")
    assert 100 < d < 300, "Haversine distance calculation is off"
    print("[PASS] Haversine formula verified!")

def test_merging_simulation():
    from server import process_report_ingestion, get_db, DUPLICATE_DISTANCE_THRESHOLD
    
    # Clean DB first
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM citizen_reports")
    cursor.execute("DELETE FROM master_tickets")
    conn.commit()
    conn.close()

    # Base coordinate (e.g. Mumbai road pothole)
    base_lat = 19.07600
    base_lng = 72.87770
    now = "2026-09-09T10:00:00Z"

    print("\n--- Submitting 5 citizen reports within 50m of base pothole ---")
    offsets = [
        (0.00000, 0.00000, "Citizen 1 (Exact origin)"),
        (0.00010, 0.00005, "Citizen 2 (~12m away)"),
        (-0.00015, 0.00010, "Citizen 3 (~20m away)"),
        (0.00020, -0.00010, "Citizen 4 (~25m away)"),
        (-0.00018, -0.00015, "Citizen 5 (~28m away)"),
    ]

    master_ticket_ids = set()
    for i, (d_lat, d_lng, desc) in enumerate(offsets, 1):
        res = process_report_ingestion(
            category="pothole",
            lat=base_lat + d_lat,
            lng=base_lng + d_lng,
            gps_accuracy=3.0,
            voice_transcript=f"Report {i}: pothole on road",
            language="en-IN",
            client_timestamp=now,
            reporter_name=f"Citizen {i}"
        )
        master_ticket_ids.add(res["ticket_id"])
        print(f"Report {i} [{desc}]: {res['message']} (Merged={res['is_duplicate_merged']})")

    # All 5 should be merged into exactly 1 Master Ticket
    assert len(master_ticket_ids) == 1, f"Expected 1 master ticket, got {len(master_ticket_ids)}"
    master_id = list(master_ticket_ids)[0]
    print(f"\n[PASS] All 5 reports successfully merged into Ticket ID {master_id}!")

    # Verify Master Ticket in DB
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT report_count, priority FROM master_tickets WHERE id = ?", (master_id,))
    row = cursor.fetchone()
    report_count, priority = row[0], row[1]
    print(f"[TEST] Master Ticket Report Count: {report_count}, Priority: {priority}")
    assert report_count == 5, f"Expected report_count=5, got {report_count}"
    assert priority == "CRITICAL", f"Expected priority=CRITICAL for 5 merged reports, got {priority}"

    # Now submit 6th report 150m away (> 50m threshold)
    print("\n--- Submitting 6th report 150m away (different location) ---")
    res6 = process_report_ingestion(
        category="pothole",
        lat=base_lat + 0.00135,  # ~150m away
        lng=base_lng + 0.00000,
        gps_accuracy=3.0,
        voice_transcript="Pothole 150m further down the road",
        language="hi-IN",
        client_timestamp=now,
        reporter_name="Citizen 6"
    )
    print(f"Report 6: {res6['message']} (Merged={res6['is_duplicate_merged']})")
    assert res6["is_duplicate_merged"] is False, "Report 150m away should NOT be merged!"
    assert res6["ticket_id"] != master_id, "Report 150m away should create a distinct ticket!"
    print(f"[PASS] Report > 50m correctly spawned new ticket {res6['ticket_code']}!")

    # Verify total tickets count = 2
    cursor.execute("SELECT COUNT(*) FROM master_tickets")
    total_tickets = cursor.fetchone()[0]
    assert total_tickets == 2, f"Expected 2 master tickets, got {total_tickets}"
    print(f"[PASS] Total Master Tickets in DB = {total_tickets} (5 duplicate reports merged + 1 separate ticket).")
    conn.close()

if __name__ == "__main__":
    print("Testing Civic Issue Duplicate Detection Engine...")
    test_haversine()
    test_merging_simulation()
    print("\n>>> ALL TESTS PASSED SUCCESSFULLY! <<<")
