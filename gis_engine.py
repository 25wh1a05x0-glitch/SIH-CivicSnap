"""
Municipal GIS, Geocoding, Auto-Routing & Priority Scoring Engine
Handles:
1. Reverse Geocoding (Lat/Lng -> Ward, Zone, Nearest Landmark)
2. Location Criticality Analysis (Proximity to Hospitals, Schools, Expressways)
3. Historical Risk & Flooding Hotspot Index
4. Departmental Auto-Routing Matrix & Field Engineer Assignment
5. Weighted Priority Scoring Formula & SLA Timers

PATCH NOTES (vs original):
- Fixed engineer assignment: replaced Python's built-in `hash()` (randomized
  per-process via PYTHONHASHSEED, so it is NOT stable across restarts or
  across multiple worker processes) with a stable hash (`hashlib.md5`) plus
  a round-robin counter, so the same ward+category no longer always pins to
  one engineer forever while the rest of the pool sits idle.
- `reverse_geocode` now checks the nearest ward's distance against a coverage
  radius and returns an explicit "out of coverage" result instead of silently
  assigning a real ward/zonal officer to a location that may be nowhere near it.
- `route_ticket` now logs when a category falls back to the default department,
  instead of silently mis-routing unknown categories.
- Added a helper to render the SLA deadline in IST alongside UTC, since the
  data will ultimately be read by municipal staff in Mumbai.
- Landmark `urgency_weight` is now actually used (as a small modifier) in
  criticality scoring instead of being dead data.
"""

import hashlib
import logging
import math
from typing import Dict, Any, List
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("municipal_gis_engine")

IST = timezone(timedelta(hours=5, minutes=30))

# Max distance (meters) from a ward center for a report to be considered
# "in coverage." Beyond this, we don't trust the nearest-ward match.
WARD_COVERAGE_RADIUS_M = 15000.0


def haversine_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0  # Earth's radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi / 2.0) ** 2 + \
        math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


def stable_hash_index(key: str, modulus: int) -> int:
    """Deterministic hash → index, stable across process restarts and workers
    (unlike Python's built-in hash(), which is randomized per-process)."""
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return int(digest, 16) % modulus


class MunicipalGISEngine:
    def __init__(self):
        # Municipal Ward Definitions
        self.wards = [
            {
                "id": "KW",
                "ward_name": "Ward K-West (Andheri West / Juhu / Versova)",
                "zone": "Western Suburbs Zone 4",
                "center": (19.1136, 72.8290),
                "zonal_head": "Sanjay Deshmukh (Addl. Commissioner)",
                "zonal_contact": "+91-98200-11223",
                "base_historical_risk": 72.0,
                "flood_vulnerability": "HIGH (Milan Subway & Juhu Drain Outfall)",
                "pothole_vulnerability": "HIGH (Heavy monsoon traffic corridor)"
            },
            {
                "id": "HW",
                "ward_name": "Ward H-West (Bandra West / Khar / Santacruz)",
                "zone": "Western Suburbs Zone 3",
                "center": (19.0596, 72.8295),
                "zonal_head": "Dr. Alka Sasane (Ward Officer)",
                "zonal_contact": "+91-98200-44556",
                "base_historical_risk": 64.0,
                "flood_vulnerability": "MEDIUM (Khar Danda low tide runoff)",
                "pothole_vulnerability": "MEDIUM (SV Road & Linking Road)"
            },
            {
                "id": "GS",
                "ward_name": "Ward G-South (Worli / Lower Parel / Prabhadevi)",
                "zone": "Island City South Zone",
                "center": (19.0068, 72.8182),
                "zonal_head": "Sharad Ughade (Deputy Municipal Commissioner)",
                "zonal_contact": "+91-98200-77889",
                "base_historical_risk": 58.0,
                "flood_vulnerability": "MEDIUM (Worli Naka pumping station)",
                "pothole_vulnerability": "MEDIUM (Dr. E. Moses Road)"
            },
            {
                "id": "FN",
                "ward_name": "Ward F-North (Matunga / Sion / Wadala)",
                "zone": "Island City North Zone",
                "center": (19.0330, 72.8634),
                "zonal_head": "Gajanan Bellale (Zonal Head)",
                "zonal_contact": "+91-98200-99001",
                "base_historical_risk": 88.0,
                "flood_vulnerability": "VERY HIGH (Gandhi Market & Hindmata chronic depression)",
                "pothole_vulnerability": "HIGH (Sion Circle flyover approaches)"
            },
            {
                "id": "L",
                "ward_name": "Ward L (Kurla / Sakinaka / Chandivali)",
                "zone": "Eastern Suburbs Zone 5",
                "center": (19.0726, 72.8845),
                "zonal_head": "Mahadev Shinde (Executive Engineer)",
                "zonal_contact": "+91-98200-33445",
                "base_historical_risk": 82.0,
                "flood_vulnerability": "VERY HIGH (Mithi River overflow basin)",
                "pothole_vulnerability": "HIGH (LBS Marg chronic wear)"
            }
        ]

        # Critical Infrastructure & Landmarks Database
        self.landmarks = [
            {"name": "Cooper Hospital & Medical College", "type": "hospital", "coords": (19.1077, 72.8362), "urgency_weight": 90},
            {"name": "Lilavati Hospital & Research Centre", "type": "hospital", "coords": (19.0514, 72.8290), "urgency_weight": 95},
            {"name": "Nanavati Super Speciality Hospital", "type": "hospital", "coords": (19.0963, 72.8415), "urgency_weight": 90},
            {"name": "KEM & Tata Memorial Hospital", "type": "hospital", "coords": (19.0033, 72.8428), "urgency_weight": 95},
            {"name": "Lokmanya Tilak Municipal General Hospital (Sion)", "type": "hospital", "coords": (19.0360, 72.8601), "urgency_weight": 95},
            {"name": "Western Express Highway Corridor", "type": "highway", "coords": (19.0800, 72.8520), "urgency_weight": 85},
            {"name": "SV Road Arterial Junction", "type": "highway", "coords": (19.0750, 72.8410), "urgency_weight": 80},
            {"name": "St. Xavier's High School Zone", "type": "school", "coords": (19.0550, 72.8350), "urgency_weight": 75},
            {"name": "Mithibai College & School Campus", "type": "school", "coords": (19.1025, 72.8373), "urgency_weight": 75},
            {"name": "Andheri Railway & Metro Interchange", "type": "transit", "coords": (19.1197, 72.8464), "urgency_weight": 80},
            {"name": "Milan Subway Low-Lying Underpass", "type": "flood_point", "coords": (19.0880, 72.8430), "urgency_weight": 90},
            {"name": "Hindmata Flyover Junction", "type": "flood_point", "coords": (19.0150, 72.8410), "urgency_weight": 90}
        ]

        # Departmental Routing Matrix
        self.departments = {
            "pothole": {
                "dept_name": "Roads & Traffic Department (PWD)",
                "sla_hours": {"CRITICAL": 24, "HIGH": 48, "NORMAL": 72, "LOW": 168},
                "engineer_pool": ["Er. Rajesh Kulkarni", "Er. Pradeep Sharma", "Er. Vinay Nair"]
            },
            "crack": {
                "dept_name": "Roads & Traffic Department (PWD)",
                "sla_hours": {"CRITICAL": 24, "HIGH": 48, "NORMAL": 72, "LOW": 168},
                "engineer_pool": ["Er. Rajesh Kulkarni", "Er. Pradeep Sharma"]
            },
            "flooding": {
                "dept_name": "Storm Water Drainage (SWD) Department",
                "sla_hours": {"CRITICAL": 12, "HIGH": 24, "NORMAL": 48, "LOW": 96},
                "engineer_pool": ["Er. Suhas Kamble (Dewatering Unit)", "Er. Amit Patil", "Er. Dilip Sawant"]
            },
            "waterlogging": {
                "dept_name": "Storm Water Drainage (SWD) Department",
                "sla_hours": {"CRITICAL": 12, "HIGH": 24, "NORMAL": 48, "LOW": 96},
                "engineer_pool": ["Er. Suhas Kamble (Dewatering Unit)", "Er. Amit Patil"]
            },
            "open_manhole": {
                "dept_name": "Sewerage Operations & Hazard Response",
                "sla_hours": {"CRITICAL": 6, "HIGH": 12, "NORMAL": 24, "LOW": 48},
                "engineer_pool": ["Quick Response Team (HazMat/Sewer)", "Er. Suresh Jadhav"]
            },
            "garbage_overflow": {
                "dept_name": "Solid Waste Management (SWM)",
                "sla_hours": {"CRITICAL": 24, "HIGH": 36, "NORMAL": 48, "LOW": 72},
                "engineer_pool": ["Er. Prakash More (Cleanliness Lead)", "Er. Nilesh Tandel"]
            },
            "fallen_tree": {
                "dept_name": "Gardens & Tree Authority",
                "sla_hours": {"CRITICAL": 12, "HIGH": 24, "NORMAL": 48, "LOW": 72},
                "engineer_pool": ["Er. Hemant Chavan (Tree Trimming)", "Er. Rahul Bhosale"]
            },
            "broken_streetlight": {
                "dept_name": "Electrical Infrastructure Division",
                "sla_hours": {"CRITICAL": 24, "HIGH": 48, "NORMAL": 72, "LOW": 120},
                "engineer_pool": ["Er. Chetan Vaidya (Grid Maintenance)", "Er. Manoj Gupta"]
            }
        }

        # Round-robin counters, keyed by (department, ward_id), so repeated
        # tickets from the same ward/department spread across the engineer
        # pool instead of always landing on the same person.
        self._assignment_counters: Dict[str, int] = {}

    def reverse_geocode(self, lat: float, lng: float) -> Dict[str, Any]:
        """
        Maps latitude & longitude to nearest Municipal Ward, Zone, and Landmarks.
        Returns an explicit out-of-coverage result if the nearest ward is
        further away than WARD_COVERAGE_RADIUS_M, rather than silently
        assigning a real ward/zonal officer to a distant location.
        """
        # 1. Find closest ward
        closest_ward = self.wards[0]
        min_ward_dist = float('inf')
        for ward in self.wards:
            dist = haversine_distance_meters(lat, lng, ward["center"][0], ward["center"][1])
            if dist < min_ward_dist:
                min_ward_dist = dist
                closest_ward = ward

        if min_ward_dist > WARD_COVERAGE_RADIUS_M:
            return {
                "in_coverage": False,
                "ward_id": None,
                "ward_name": None,
                "zone": None,
                "zonal_head": None,
                "zonal_contact": None,
                "nearest_landmark": None,
                "distance_to_landmark_m": None,
                "nearby_critical_features": [],
                "base_historical_risk": 0.0,
                "flood_vulnerability": None,
                "pothole_vulnerability": None,
                "nearest_ward_distance_m": round(min_ward_dist, 1),
                "note": (
                    f"Location is {round(min_ward_dist / 1000, 1)} km from the nearest "
                    f"known ward ({closest_ward['ward_name']}), outside the "
                    f"{WARD_COVERAGE_RADIUS_M / 1000:.0f} km coverage radius. "
                    "Route for manual review before auto-assigning a ward officer."
                ),
            }

        # 2. Find closest landmarks
        nearest_landmark = None
        min_lm_dist = float('inf')
        nearby_critical_features = []

        for lm in self.landmarks:
            dist = haversine_distance_meters(lat, lng, lm["coords"][0], lm["coords"][1])
            if dist < min_lm_dist:
                min_lm_dist = dist
                nearest_landmark = {**lm, "distance_meters": round(dist, 1)}

            # Flag critical infrastructure within 500m
            if dist <= 500:
                nearby_critical_features.append({
                    "name": lm["name"],
                    "type": lm["type"],
                    "distance_meters": round(dist, 1),
                    "urgency_weight": lm.get("urgency_weight", 0),
                })

        return {
            "in_coverage": True,
            "ward_id": closest_ward["id"],
            "ward_name": closest_ward["ward_name"],
            "zone": closest_ward["zone"],
            "zonal_head": closest_ward["zonal_head"],
            "zonal_contact": closest_ward["zonal_contact"],
            "nearest_landmark": nearest_landmark["name"] if nearest_landmark else "Municipal Highway Zone",
            "distance_to_landmark_m": nearest_landmark["distance_meters"] if nearest_landmark else 0.0,
            "nearby_critical_features": nearby_critical_features,
            "base_historical_risk": closest_ward["base_historical_risk"],
            "flood_vulnerability": closest_ward["flood_vulnerability"],
            "pothole_vulnerability": closest_ward["pothole_vulnerability"]
        }

    def calculate_location_criticality(self, nearby_features: List[Dict[str, Any]], distance_to_lm: float) -> float:
        """
        Evaluates proximity to schools, hospitals, highways, and flood choke points.
        Returns a score from 15.0 to 100.0. Each feature's urgency_weight now
        contributes a small modifier on top of the base distance-tier bump, so
        e.g. Lilavati (95) counts for slightly more than Cooper (90) at the
        same distance, instead of the weight being ignored entirely.
        """
        score = 25.0  # Base street criticality

        for feat in nearby_features:
            dist = feat["distance_meters"]
            f_type = feat.get("type", "")
            weight_modifier = (feat.get("urgency_weight", 70) - 70) * 0.1  # small nudge, +/- a few points

            base_bump = 0.0
            if f_type == "hospital":
                if dist <= 200: base_bump = 50
                elif dist <= 400: base_bump = 35
                elif dist <= 600: base_bump = 20
            elif f_type == "school":
                if dist <= 200: base_bump = 40
                elif dist <= 400: base_bump = 25
            elif f_type in ["highway", "flood_point"]:
                if dist <= 150: base_bump = 45
                elif dist <= 350: base_bump = 30
            elif f_type == "transit":
                if dist <= 250: base_bump = 30

            if base_bump > 0:
                score += base_bump + weight_modifier

        return min(100.0, max(15.0, score))

    def calculate_priority(
        self,
        ai_damage_severity: float,
        report_count: int,
        location_criticality: float,
        historical_risk: float,
        nlp_urgency_bonus: float = 0.0
    ) -> Dict[str, Any]:
        """
        Applies weighted priority formula:
        Priority Score = (0.35 x AI damage) + (0.25 x citizen report count) + (0.20 x location criticality) + (0.20 x historical risk)
        + NLP Urgency Bonus

        Report-count scaling: count_score = report_count * 22 + 10, capped at 100
        (i.e. 1 report -> 32, 2 -> 54, 3 -> 76, 4+ -> 98/100).
        """
        count_score = min(100.0, report_count * 22.0 + 10.0)

        raw_score = (
            (0.35 * ai_damage_severity) +
            (0.25 * count_score) +
            (0.20 * location_criticality) +
            (0.20 * historical_risk)
        )

        final_score = min(100.0, max(10.0, raw_score + (nlp_urgency_bonus * 0.4)))
        rounded_score = round(final_score, 1)

        # Priority Buckets:
        # Critical: >= 80 or (report_count >= 5: multiple citizen cluster)
        # High: 60 - 79.9 or (report_count >= 3)
        # Normal: 40 - 59.9
        # Low: < 40
        if rounded_score >= 80.0 or report_count >= 5:
            priority = "CRITICAL"
            escalate_alert = True
        elif rounded_score >= 60.0 or report_count >= 3:
            priority = "HIGH"
            escalate_alert = False
        elif rounded_score >= 40.0:
            priority = "NORMAL"
            escalate_alert = False
        else:
            priority = "LOW"
            escalate_alert = False

        return {
            "priority_score": rounded_score,
            "priority": priority,
            "escalate_alert": escalate_alert,
            "breakdown": {
                "ai_damage_component": round(0.35 * ai_damage_severity, 1),
                "report_count_component": round(0.25 * count_score, 1),
                "location_criticality_component": round(0.20 * location_criticality, 1),
                "historical_risk_component": round(0.20 * historical_risk, 1),
                "nlp_bonus": round(nlp_urgency_bonus * 0.4, 1)
            }
        }

    def route_ticket(self, category: str, priority: str, ward_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        Auto-routes ticket to municipal department and assigns field officer + SLA countdown.
        Engineer assignment uses a stable hash seed (round-robin start point)
        plus a per-(department, ward) counter, so repeated tickets spread
        across the pool instead of always hitting the same one person.
        """
        dept_info = self.departments.get(category)
        if dept_info is None:
            logger.warning(
                "Unknown category '%s' — falling back to default department (pothole/PWD).",
                category,
            )
            dept_info = self.departments["pothole"]

        sla_hours = dept_info["sla_hours"].get(priority, 48)
        pool = dept_info["engineer_pool"]
        ward_id = ward_info.get("ward_id") or "UNASSIGNED"

        counter_key = f"{dept_info['dept_name']}::{ward_id}"
        seed = stable_hash_index(counter_key, len(pool))
        turn = self._assignment_counters.get(counter_key, 0)
        engineer_idx = (seed + turn) % len(pool)
        self._assignment_counters[counter_key] = turn + 1
        assigned_engineer = pool[engineer_idx]

        now_utc = datetime.now(timezone.utc)
        deadline_utc = now_utc + timedelta(hours=sla_hours)
        deadline_ist = deadline_utc.astimezone(IST)

        return {
            "department": dept_info["dept_name"],
            "assigned_engineer": assigned_engineer,
            "sla_hours": sla_hours,
            "sla_deadline_utc": deadline_utc.isoformat(),
            "sla_deadline_ist": deadline_ist.strftime("%Y-%m-%d %H:%M IST"),
            "zonal_office": ward_info.get("zone", "Municipal Zone"),
            "ward_name": ward_info.get("ward_name", "Municipal Ward")
        }


# Global instance
municipal_gis_engine = MunicipalGISEngine()
