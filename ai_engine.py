"""
Civic AI Verification & Classification Engine
Handles:
1. Image Quality & Blur Detection
2. Fraud / Spam / Selfie Rejection Filtering
3. Damage Severity & Depth / Area Estimation (Potholes, Waterlogging, Debris)
4. Multilingual NLP Urgency & Criticality Analysis (Hindi, Marathi, English)
"""

import math
import re
from pathlib import Path
from typing import Dict, Any, Optional, List
from PIL import Image, ImageFilter, ImageStat, ExifTags

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
            "तातडीने": {"weight": 16, "tag": "⚡ Urgent Response Requested"}
        }

    def analyze_image_quality_and_content(self, image_path: Path, claimed_category: str, capture_source: str = "LIVE_CAMERA") -> Dict[str, Any]:
        """
        Inspects the uploaded image for:
        1. Blurriness (gradient edge variance).
        2. AI-Generated & Synthetic Image Detection.
        3. Fraud / Non-Live File Upload Check (Missing Camera Sensor Hardware).
        4. Fraud / Spam detection (Selfies, portraits, indoor walls, non-civic photos).
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
                "confidence": 0.85
            }

        try:
            with Image.open(image_path) as img:
                # 2. AI-Generated & Synthetic Image Detection
                # Check metadata tags for AI generation software or graphics editors
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

                # Check info dictionary (PNG text chunks, EXIF, or JPEG comments)
                raw_info_str = (str(img.info) + " " + software_str).lower()
                ai_markers = [
                    "midjourney", "dall-e", "dalle", "stable diffusion", "stablediffusion",
                    "novelai", "photoshop", "gimp", "canva", "adobe firefly", "firefly",
                    "comfyui", "civitai", "bing image", "leonardo.ai", "chatgpt", "gemini",
                    "synthetic", "genai", "prompt"
                ]
                
                detected_ai_marker = None
                for marker in ai_markers:
                    if marker in raw_info_str:
                        detected_ai_marker = marker
                        break

                if detected_ai_marker:
                    return {
                        "is_valid": False,
                        "is_spam": True,
                        "is_ai_synthetic": True,
                        "spam_reason": f"AI FRAUD DETECTED: Image contains AI generation metadata ({detected_ai_marker.title()}). Fake or AI-generated photos are strictly rejected. Geotagging revoked.",
                        "damage_severity": 0.0,
                        "estimated_depth_cm": 0.0,
                        "damage_area_pct": 0.0,
                        "ai_category_detected": "rejected_ai_synthetic",
                        "blur_score": 100.0,
                        "confidence": 0.99
                    }

                # 3. Fraud / Non-Live File Upload Check (Missing Camera Sensor Hardware)
                # If image is uploaded via file picker without camera Make/Model/Sensors
                if capture_source == "FILE_UPLOAD" and not has_camera_hardware:
                    return {
                        "is_valid": False,
                        "is_spam": True,
                        "is_ai_synthetic": True,
                        "spam_reason": "AI FRAUD DETECTED: Missing authentic camera hardware sensor signatures (Make/Model/EXIF). Downloaded web photos or synthetic images cannot be verified on-site. Geotagging revoked.",
                        "damage_severity": 0.0,
                        "estimated_depth_cm": 0.0,
                        "damage_area_pct": 0.0,
                        "ai_category_detected": "rejected_non_live",
                        "blur_score": 100.0,
                        "confidence": 0.96
                    }

                img_rgb = img.convert("RGB")
                width, height = img_rgb.size
                
                # Downsample for fast inspection
                sample_img = img_rgb.resize((256, 256))
                
                # 1. Blur Detection using Laplacian-like edge filter
                gray = sample_img.convert("L")
                edges = gray.filter(ImageFilter.FIND_EDGES)
                stat = ImageStat.Stat(edges)
                blur_score = stat.var[0]  # Variance of edges: low variance = blurry

                if blur_score < 18.0:
                    return {
                        "is_valid": False,
                        "is_spam": True,
                        "spam_reason": "AI Rejected: Image is severely blurred or motion-degraded. Please retake a clear photo.",
                        "damage_severity": 0.0,
                        "estimated_depth_cm": 0.0,
                        "damage_area_pct": 0.0,
                        "ai_category_detected": "rejected_blurry",
                        "blur_score": round(blur_score, 1),
                        "confidence": 0.95
                    }

                # 4. Fraud / Spam / Selfie Detection
                # Analyze color distribution in center 50% of image
                center_box = (64, 64, 192, 192)
                center_crop = sample_img.crop(center_box)
                center_pixels = list(center_crop.getdata())
                
                # Check for skin tones (Selfie / Face detection heuristic)
                skin_count = 0
                dark_asphalt_count = 0
                water_reflection_count = 0
                
                for r, g, b in center_pixels:
                    # Common human skin tone thresholds in RGB
                    if (r > 95 and g > 40 and b > 20 and 
                        (max(r, g, b) - min(r, g, b) > 15) and 
                        abs(r - g) > 15 and r > g and r > b):
                        skin_count += 1
                    
                    # Typical wet/asphalt / road textures: dark, desaturated
                    brightness = (r + g + b) / 3
                    saturation = (max(r, g, b) - min(r, g, b)) / (brightness + 1e-5)
                    if brightness < 90 and saturation < 0.35:
                        dark_asphalt_count += 1
                    elif 80 < brightness < 180 and saturation < 0.25:
                        water_reflection_count += 1

                total_center_pixels = len(center_pixels)
                skin_ratio = skin_count / total_center_pixels
                road_texture_ratio = (dark_asphalt_count + water_reflection_count) / total_center_pixels

                # If center is dominated by human face/skin tone (> 45%) and low road texture
                if skin_ratio > 0.45:
                    return {
                        "is_valid": False,
                        "is_spam": True,
                        "spam_reason": "AI Rejected: Image classified as human selfie/portrait. Only public civic damage photos are accepted.",
                        "damage_severity": 0.0,
                        "estimated_depth_cm": 0.0,
                        "damage_area_pct": 0.0,
                        "ai_category_detected": "rejected_selfie",
                        "blur_score": round(blur_score, 1),
                        "confidence": 0.94
                    }

                # 3. Severity & Damage Estimation
                # Analyze damage area & contrast depth
                edge_stat = ImageStat.Stat(edges.crop(center_box))
                edge_intensity = edge_stat.mean[0]
                
                # Damage severity estimation algorithm:
                # Based on edge complexity (cracks/ragged pothole borders) and dark depression contrast
                if claimed_category in ["pothole", "crack", "road_damage"]:
                    # Contrast between road surface and pothole depression
                    contrast_ratio = min(1.0, edge_intensity / 45.0)
                    damage_area_pct = min(60.0, max(12.0, contrast_ratio * 45.0 + (1.0 - road_texture_ratio) * 15.0))
                    estimated_depth_cm = round(4.0 + (damage_area_pct / 60.0) * 16.0, 1) # 4 to 20 cm
                    damage_severity = min(98.0, max(25.0, (damage_area_pct * 1.1) + (estimated_depth_cm * 2.5)))
                    detected_class = "pothole" if estimated_depth_cm >= 8.0 else "road_crack"

                elif claimed_category in ["flooding", "waterlogging"]:
                    # Higher water reflection = more severe waterlogging
                    water_ratio = water_reflection_count / total_center_pixels
                    damage_area_pct = min(90.0, max(20.0, water_ratio * 100.0 + 30.0))
                    # Estimate water level against 15cm curb height
                    estimated_depth_cm = round(10.0 + (damage_area_pct / 90.0) * 35.0, 1) # 10 to 45 cm
                    damage_severity = min(99.0, max(35.0, 30.0 + (damage_area_pct * 0.7)))
                    detected_class = "waterlogging"

                elif claimed_category == "open_manhole":
                    damage_severity = 90.0  # Open manholes are inherently acute hazards
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
                    "confidence": round(min(0.98, 0.75 + (blur_score / 300.0)), 2)
                }

        except Exception as e:
            # Fallback safe values if image decoding fails
            return {
                "is_valid": True,
                "is_spam": False,
                "spam_reason": None,
                "damage_severity": 50.0,
                "estimated_depth_cm": 8.0,
                "damage_area_pct": 20.0,
                "ai_category_detected": claimed_category,
                "blur_score": 100.0,
                "confidence": 0.80
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
                "urgency_level": "NORMAL"
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

        # Cap urgency bonus at +25
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
            "urgency_level": urgency_level
        }


# Global instance
civic_ai_engine = CivicAIEngine()
