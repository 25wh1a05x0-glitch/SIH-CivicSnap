"""
Civic AI Verification & Classification Engine
Handles:
1. Image Quality & Blur Detection
2. Fraud / Spam / Selfie Rejection Filtering
3. Damage Severity & Depth / Area Estimation (Potholes, Waterlogging, Debris)
4. Multilingual NLP Urgency & Criticality Analysis (Hindi, Marathi, English)

PATCH NOTES (vs original):
- Fixed missing `ExifTags` import (this was a guaranteed NameError on any image with EXIF data).
- Split AI-generation markers from general editing-software markers. Only true
  generative-AI tools trigger a hard "AI FRAUD" rejection now. Editing software
  (Photoshop/GIMP/Canva) is logged as a soft flag instead of an outright reject,
  since cropping/rotating a real photo in a gallery app shouldn't get it rejected.
- Softened the "missing camera hardware EXIF" rule from a hard reject to a soft
  flag. Many real photos lose EXIF data in transit (WhatsApp, Telegram, etc.),
  so hard-rejecting on this basis will false-positive on genuine reports.
- Selfie/skin-tone heuristic now requires BOTH high skin ratio AND low road-texture
  ratio before rejecting, to reduce false positives on photos that include a
  bystander's hand/limb (e.g. injury reports) alongside the actual civic damage.
- Exceptions are now logged instead of silently swallowed.
"""

import logging
from pathlib import Path
from typing import Dict, Any
from PIL import Image, ImageFilter, ImageStat, ExifTags

logger = logging.getLogger("civic_ai_engine")


class CivicAIEngine:
    def __init__(self):
        # Lexicon for multilingual NLP urgency analysis
        self.urgency_keywords = {
            "hospital": {"weight": 25, "tag": "🏥 Near Hospital / Medical Access"},
            "ambulance": {"weight": 25, "tag": "🚑 Ambulance Route Impeded"},
            "school": {"weight": 20, "tag": "🏫 Near School Zone / Children Safety"},
            "highway": {"weight": 20, "tag": "🛣️ High-Speed Expressway Corridor"},
            "expressway": {"weight": 20, "tag": "🛣️ High-Speed Expressway Corridor"},
            "accident": {"weight": 22, "tag": "⚠️ Active Accident / Vehicle Damage Risk"},
            "deep": {"weight": 15, "tag": "🕳️ Hazardous Depth"},
            "danger": {"weight": 18, "tag": "⚠️ Severe Hazard"},
            "dangerous": {"weight": 18, "tag": "⚠️ Severe Hazard"},
            "injury": {"weight": 20, "tag": "🩹 Injury Reported"},
            "stuck": {"weight": 15, "tag": "🚗 Vehicle Immobilized"},
            "water entering": {"weight": 25, "tag": "🌊 Water Inundation into Dwellings"},
            "overflow": {"weight": 16, "tag": "🌊 Severe Drainage Overflow"},
            "submerged": {"weight": 22, "tag": "🌊 Road Section Submerged"},
            "fall": {"weight": 15, "tag": "⚠️ Pedestrian Fall Hazard"},
            "blocked": {"weight": 14, "tag": "🛑 Traffic Gridlock Hazard"},
            # Hindi (Devanagari)
            "अस्पताल": {"weight": 25, "tag": "🏥 Near Hospital / Medical Access"},
            "एम्बुलेंस": {"weight": 25, "tag": "🚑 Ambulance Route Impeded"},
            "स्कूल": {"weight": 20, "tag": "🏫 Near School Zone / Children Safety"},
            "हाईवे": {"weight": 20, "tag": "🛣️ High-Speed Expressway Corridor"},
            "दुर्घटना": {"weight": 22, "tag": "⚠️ Active Accident / Vehicle Damage Risk"},
            "गड्ढा": {"weight": 12, "tag": "🕳️ Road Damage"},
            "गहरा": {"weight": 15, "tag": "🕳️ Hazardous Depth"},
            "खतरा": {"weight": 18, "tag": "⚠️ Severe Hazard"},
            "खतरनाक": {"weight": 18, "tag": "⚠️ Severe Hazard"},
            "जाम": {"weight": 12, "tag": "🛑 Traffic Gridlock Hazard"},
            "पानी भर गया": {"weight": 22, "tag": "🌊 Road Section Submerged"},
            "घर में पानी": {"weight": 25, "tag": "🌊 Water Inundation into Dwellings"},
            "चोट": {"weight": 20, "tag": "🩹 Injury Reported"},
            # Marathi
            "रुग्णालय": {"weight": 25, "tag": "🏥 Near Hospital / Medical Access"},
            "शाळा": {"weight": 20, "tag": "🏫 Near School Zone / Children Safety"},
            "अपघात": {"weight": 22, "tag": "⚠️ Active Accident / Vehicle Damage Risk"},
            "खड्डा": {"weight": 12, "tag": "🕳️ Road Damage"},
            "धोकादायक": {"weight": 18, "tag": "⚠️ Severe Hazard"},
            "पाणी भरले": {"weight": 22, "tag": "🌊 Road Section Submerged"},
            "पाणी शिरले": {"weight": 25, "tag": "🌊 Water Inundation into Dwellings"},
            "गाडी अडकली": {"weight": 15, "tag": "🚗 Vehicle Immobilized"},
            "तातडीने": {"weight": 16, "tag": "⚡ Urgent Response Requested"},
        }

        # Only genuine generative-AI tools trigger a hard fraud rejection.
        self.generative_ai_markers = [
            "midjourney", "dall-e", "dalle", "stable diffusion", "stablediffusion",
            "novelai", "adobe firefly", "firefly", "comfyui", "civitai",
            "bing image creator", "leonardo.ai", "synthetic image", "genai",
        ]
        # General editing software: logged as a soft flag only, not rejected.
        self.editing_software_markers = [
            "photoshop", "gimp", "canva", "chatgpt", "gemini", "prompt",
        ]

    def analyze_image_quality_and_content(
        self, image_path: Path, claimed_category: str, capture_source: str = "LIVE_CAMERA"
    ) -> Dict[str, Any]:
        """
        Inspects the uploaded image for:
        1. Blurriness (gradient edge variance).
        2. Genuine AI-generated / synthetic image detection.
        3. Soft camera-hardware provenance check (flag, not hard reject).
        4. Fraud / Spam detection (selfies, portraits, non-civic photos).
        5. Visual damage severity & depth/area estimation.
        """
        if not image_path.exists():
            return {
                "is_valid": True,
                "is_spam": False,
                "spam_reason": None,
                "damage_severity": 50.0,
                "estimated_depth_cm": 8.0,
                "damage_area_pct": 20.0,
                "ai_category_detected": claimed_category,
                "blur_score": 120.0,
                "confidence": 0.85,
                "provenance_flags": [],
            }

        provenance_flags: list = []

        try:
            with Image.open(image_path) as img:
                # --- Metadata inspection ---
                exif_raw = img.getexif()
                software_str = ""
                has_camera_hardware = False

                if exif_raw:
                    for tag_id, value in exif_raw.items():
                        tag_name = ExifTags.TAGS.get(tag_id, tag_id)
                        if tag_name == "Software":
                            software_str = str(value).lower()
                        if tag_name in ["Make", "Model", "FocalLength", "ISOSpeedRatings"]:
                            has_camera_hardware = True

                raw_info_str = (str(img.info) + " " + software_str).lower()

                # 2. Genuine generative-AI detection (hard reject)
                detected_ai_marker = next(
                    (m for m in self.generative_ai_markers if m in raw_info_str), None
                )
                if detected_ai_marker:
                    return {
                        "is_valid": False,
                        "is_spam": True,
                        "is_ai_synthetic": True,
                        "spam_reason": (
                            f"AI FRAUD DETECTED: Image contains generative-AI metadata "
                            f"({detected_ai_marker.title()}). AI-generated photos are rejected."
                        ),
                        "damage_severity": 0.0,
                        "estimated_depth_cm": 0.0,
                        "damage_area_pct": 0.0,
                        "ai_category_detected": "rejected_ai_synthetic",
                        "blur_score": 100.0,
                        "confidence": 0.99,
                        "provenance_flags": [detected_ai_marker],
                    }

                # Editing software: soft flag only, does not block submission.
                detected_editor = next(
                    (m for m in self.editing_software_markers if m in raw_info_str), None
                )
                if detected_editor:
                    provenance_flags.append(f"edited_with:{detected_editor}")

                # 3. Camera-hardware provenance: soft flag, not a hard reject.
                # Many genuine photos lose EXIF in transit (WhatsApp, compression, etc.),
                # so this alone is not reliable evidence of fraud.
                if capture_source == "FILE_UPLOAD" and not has_camera_hardware:
                    provenance_flags.append("no_camera_exif_on_upload")

                img_rgb = img.convert("RGB")
                sample_img = img_rgb.resize((256, 256))

                # 1. Blur Detection using Laplacian-like edge filter
                gray = sample_img.convert("L")
                edges = gray.filter(ImageFilter.FIND_EDGES)
                stat = ImageStat.Stat(edges)
                blur_score = stat.var[0]  # low variance = blurry

                if blur_score < 18.0:
                    return {
                        "is_valid": False,
                        "is_spam": True,
                        "spam_reason": "Image is severely blurred or motion-degraded. Please retake a clear photo.",
                        "damage_severity": 0.0,
                        "estimated_depth_cm": 0.0,
                        "damage_area_pct": 0.0,
                        "ai_category_detected": "rejected_blurry",
                        "blur_score": round(blur_score, 1),
                        "confidence": 0.95,
                        "provenance_flags": provenance_flags,
                    }

                # 4. Fraud / Spam / Selfie Detection
                center_box = (64, 64, 192, 192)
                center_crop = sample_img.crop(center_box)
                center_pixels = list(center_crop.getdata())

                skin_count = 0
                dark_asphalt_count = 0
                water_reflection_count = 0

                for r, g, b in center_pixels:
                    if (
                        r > 95 and g > 40 and b > 20
                        and (max(r, g, b) - min(r, g, b) > 15)
                        and abs(r - g) > 15 and r > g and r > b
                    ):
                        skin_count += 1

                    brightness = (r + g + b) / 3
                    saturation = (max(r, g, b) - min(r, g, b)) / (brightness + 1e-5)
                    if brightness < 90 and saturation < 0.35:
                        dark_asphalt_count += 1
                    elif 80 < brightness < 180 and saturation < 0.25:
                        water_reflection_count += 1

                total_center_pixels = len(center_pixels)
                skin_ratio = skin_count / total_center_pixels
                road_texture_ratio = (dark_asphalt_count + water_reflection_count) / total_center_pixels

                # Require high skin ratio AND low road-texture ratio to reject as
                # selfie/portrait, so a hand/limb in an injury photo doesn't
                # trigger a false rejection.
                if skin_ratio > 0.55 and road_texture_ratio < 0.15:
                    return {
                        "is_valid": False,
                        "is_spam": True,
                        "spam_reason": "Image classified as human selfie/portrait. Only public civic damage photos are accepted.",
                        "damage_severity": 0.0,
                        "estimated_depth_cm": 0.0,
                        "damage_area_pct": 0.0,
                        "ai_category_detected": "rejected_selfie",
                        "blur_score": round(blur_score, 1),
                        "confidence": 0.85,
                        "provenance_flags": provenance_flags,
                    }

                # 5. Severity & Damage Estimation (heuristic proxy, not a
                # true measurement — see module docstring / patch notes).
                edge_stat = ImageStat.Stat(edges.crop(center_box))
                edge_intensity = edge_stat.mean[0]

                if claimed_category in ["pothole", "crack", "road_damage"]:
                    contrast_ratio = min(1.0, edge_intensity / 45.0)
                    damage_area_pct = min(60.0, max(12.0, contrast_ratio * 45.0 + (1.0 - road_texture_ratio) * 15.0))
                    estimated_depth_cm = round(4.0 + (damage_area_pct / 60.0) * 16.0, 1)
                    damage_severity = min(98.0, max(25.0, (damage_area_pct * 1.1) + (estimated_depth_cm * 2.5)))
                    detected_class = "pothole" if estimated_depth_cm >= 8.0 else "road_crack"

                elif claimed_category in ["flooding", "waterlogging"]:
                    water_ratio = water_reflection_count / total_center_pixels
                    damage_area_pct = min(90.0, max(20.0, water_ratio * 100.0 + 30.0))
                    estimated_depth_cm = round(10.0 + (damage_area_pct / 90.0) * 35.0, 1)
                    damage_severity = min(99.0, max(35.0, 30.0 + (damage_area_pct * 0.7)))
                    detected_class = "waterlogging"

                elif claimed_category == "open_manhole":
                    damage_severity = 90.0
                    estimated_depth_cm = 85.0
                    damage_area_pct = 15.0
                    detected_class = "open_manhole"

                else:
                    damage_severity = 55.0
                    estimated_depth_cm = 5.0
                    damage_area_pct = 25.0
                    detected_class = claimed_category

                return {
                    "is_valid": True,
                    "is_spam": False,
                    "spam_reason": None,
                    "damage_severity": round(damage_severity, 1),
                    "estimated_depth_cm": estimated_depth_cm,
                    "damage_area_pct": round(damage_area_pct, 1),
                    "ai_category_detected": detected_class,
                    "blur_score": round(blur_score, 1),
                    "confidence": round(min(0.98, 0.75 + (blur_score / 300.0)), 2),
                    "provenance_flags": provenance_flags,
                }

        except Exception as e:
            logger.exception("Image analysis failed for %s: %s", image_path, e)
            return {
                "is_valid": True,
                "is_spam": False,
                "spam_reason": None,
                "damage_severity": 50.0,
                "estimated_depth_cm": 8.0,
                "damage_area_pct": 20.0,
                "ai_category_detected": claimed_category,
                "blur_score": 100.0,
                "confidence": 0.80,
                "provenance_flags": provenance_flags,
                "error": str(e),
            }

    def analyze_nlp_urgency(self, text: str, language: str = "en-IN") -> Dict[str, Any]:
        """
        Extracts urgency indicators, context keywords, and calculates urgency boost.
        Supports English, Hindi, and Marathi text.
        """
        if not text or not text.strip():
            return {
                "urgency_bonus": 0,
                "urgency_tags": [],
                "extracted_keywords": [],
                "urgency_level": "NORMAL",
            }

        text_lower = text.lower()
        extracted_tags = set()
        matched_keywords = []
        total_weight = 0

        for keyword, data in self.urgency_keywords.items():
            if keyword in text_lower:
                matched_keywords.append(keyword)
                extracted_tags.add(data["tag"])
                total_weight += data["weight"]

        urgency_bonus = min(25, total_weight)

        if total_weight >= 35:
            urgency_level = "CRITICAL"
        elif total_weight >= 20:
            urgency_level = "HIGH"
        elif total_weight > 0:
            urgency_level = "ELEVATED"
        else:
            urgency_level = "NORMAL"

        return {
            "urgency_bonus": urgency_bonus,
            "urgency_tags": list(extracted_tags),
            "extracted_keywords": matched_keywords,
            "urgency_level": urgency_level,
        }


# Global instance
civic_ai_engine = CivicAIEngine()
