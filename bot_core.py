
import re
import time
import cv2
import threading
import tkinter as tk
import ctypes
import json
import copy
import hashlib
from collections import OrderedDict
import urllib.error
import urllib.request
from tkinter import ttk, messagebox
import numpy as np
import pyautogui
import pygetwindow as gw
from rapidocr_onnxruntime import RapidOCR

try:
    import win32gui
    import win32con
except Exception:
    win32gui = None
    win32con = None

# Initialize OCR Engine
ocr = RapidOCR()
BUILDER_OCR_CACHE = OrderedDict()
BUILDER_MENU_CROP = (0.10, 0.70, 0.38, 0.70)
BUILDER_SIDEBAR_CROP = (0.12, 0.89, 0.75, 1.0)
CONNECTION_POPUP_RECOVERY_LOCK = threading.Lock()


def builder_sidebar_visible(img, read_region):
    """The fixed title remains visible even after the upgrade list is scrolled."""
    results = read_region(img, (0.05, 0.115, 0.80, 0.98)) or []
    title = re.sub(r"[^a-z]", "", " ".join(str(item[1]).lower() for item in results))
    return "buildings" in title and "heroes" in title


def builder_list_snapshot(frame, menu_crop):
    """Small image of list contents for detecting a scroll boundary."""
    h, w = frame.shape[:2]
    ymin, ymax, xmin, xmax = menu_crop
    patch = frame[int(h*ymin):int(h*ymax), int(w*xmin):int(w*xmax)]
    return cv2.resize(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY), (80, 120))


def builder_list_unchanged(previous, current):
    return previous is not None and float(cv2.absdiff(previous, current).mean()) < 1.2


def normalize_wall_level_text(text):
    """Correct digit-shaped OCR letters only in an explicit Wall level token."""
    text = str(text)
    if not re.search(r"\bwa[l1i]{2}\b", text, re.I):
        return text
    return re.sub(
        r"\b(level|levei|leve1|leve\|)\s*([0-9sSoOiIlLbB]{1,2})(?=\b|[)])",
        lambda match: "Level " + match.group(2).translate(str.maketrans(
            {"s": "5", "S": "5", "o": "0", "O": "0", "i": "1", "I": "1",
             "l": "1", "L": "1", "b": "8", "B": "8"})),
        text, flags=re.I,
    )


def read_selected_wall_title(frame, read_region):
    # Keep the title large enough for text detection in smaller game windows.
    if frame.shape[1] < 1300:
        scale = 1300.0 / frame.shape[1]
        frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return read_region(frame, (0.64, 0.80, 0.34, 0.66))


def wall_resource_button(frame, read_region, resource, expected_cost, report=None):
    """Locate a priced Upgrade card, never a colored object in the village."""
    original_h, original_w = frame.shape[:2]
    scale = max(1.0, 1300.0 / original_w)
    if scale != 1.0:
        frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    h, w = frame.shape[:2]
    box = (0.68, 0.91, 0.25, 0.85)
    results = read_region(frame, box) or []
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    unit = h / 730.0
    def bounds(item):
        points = item[0]
        return (w * box[2] + min(p[0] for p in points) / 2,
                h * box[0] + min(p[1] for p in points) / 2,
                w * box[2] + max(p[0] for p in points) / 2,
                h * box[0] + max(p[1] for p in points) / 2)
    upgrades = [bounds(item) for item in results
                if re.sub(r'[^a-z]', '', str(item[1]).lower()) == 'upgrade']
    candidates = []
    for item in results:
        text = str(item[1])
        # OCR sometimes includes the following coin as @/copyright in the
        # numeric box. Keep the numeric price strict and allow only icon marks.
        if not re.fullmatch(r'\s*\d[\d,:.\s]*\s*[@©®]?\s*', text):
            continue
        if int(re.sub(r'\D', '', text)) != expected_cost:
            continue
        left, top, right, bottom = bounds(item)
        price_x, price_y = (left + right) / 2, (top + bottom) / 2
        # The icon immediately follows the price within the same card.
        patch = hsv[max(0,int(top-3*unit)):min(h,int(bottom+3*unit)),
                    max(0,int(right-20*unit)):min(w,int(right+22*unit))]
        if not patch.size:
            continue
        def icon_area(low, high):
            mask = cv2.inRange(patch, np.array(low), np.array(high))
            count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
            areas = []
            for px,py,pw,ph,area in stats[1:]:
                # The circular icon is isolated inside the price strip. Card
                # borders reach the patch edge and must not vote for Gold.
                if px <= 0 or py <= 0 or px+pw >= mask.shape[1] or py+ph >= mask.shape[0]:
                    continue
                if 4*unit <= pw <= 21*unit and 4*unit <= ph <= 21*unit and .55 <= pw/ph <= 1.6:
                    areas.append(area)
            return max(areas, default=0)
        gold = icon_area([20,120,150], [40,255,255])
        elixir = icon_area([135,70,80], [179,255,255])
        chosen, other = (gold,elixir) if resource == 'Gold' else (elixir,gold)
        if resource not in ('Gold','Elixir') or chosen < 15*unit*unit or chosen <= other:
            continue
        for ux1,uy1,ux2,uy2 in upgrades:
            ux,uy = (ux1+ux2)/2, (uy1+uy2)/2
            if abs(ux-price_x) <= 22*unit and 30*unit <= uy-price_y <= 85*unit:
                candidates.append({'x':int(ux/scale), 'y':int(uy/scale)})
    # Ambiguous cards must be reread instead of clicking a nearby wall.
    if len(candidates) != 1 and report:
        report(f"[Wall Button Read]: Expected {resource} {expected_cost:,}; "
               f"Upgrade labels={len(upgrades)}, matching cards={len(candidates)}; "
               f"visible text: {' | '.join(str(item[1]) for item in results)}")
    return candidates[0] if len(candidates) == 1 else None


def settled_wall_resource_button(capture, read_region, resource, cost, sleep, running, report):
    """Discard pre-close coordinates and require two closed-menu button reads."""
    previous = None
    for attempt in range(5):
        if not running():
            return None
        frame = capture()
        if builder_sidebar_visible(frame, read_region):
            previous = None
            report("[Wall Button Settle]: Builder panel is still visible; waiting for it to close.")
        else:
            button = wall_resource_button(frame, read_region, resource, cost, report)
            if button is not None:
                if previous is not None and abs(button['x']-previous['x']) <= 3 and abs(button['y']-previous['y']) <= 3:
                    report(f"[Wall Button Settle]: Closed-menu {resource} Upgrade position confirmed at {button['x']}, {button['y']}.")
                    return button
                previous = button
            else:
                previous = None
        if attempt < 4 and not sleep(.35):
            return None
    return None


def wall_confirmation_button(frame, read_region, expected_cost, expected_level):
    title = " ".join(str(item[1]).lower() for item in
                     (read_region(frame, (0.07, 0.15, 0.30, 0.75)) or []))
    title = re.sub(r"[^a-z0-9]+", " ", normalize_wall_level_text(title).lower())
    match = re.search(r"upgrade\s+wall\s+to\s+level\s*(\d+)", title)
    if not match or int(match.group(1)) != expected_level:
        return None
    box = (0.81, 0.95, 0.63, 0.80)
    results = read_region(frame, box) or []
    confirm = next((item for item in results if "confirm" in str(item[1]).lower()), None)
    prices = [item for item in results if re.fullmatch(r"\s*\d[\d,:.\s]*\s*", str(item[1]))]
    if not confirm or not prices:
        return None
    # The lower crossed-out price is the old price, not the actual charge.
    price = min(prices, key=lambda item: min(point[1] for point in item[0]))
    if int(re.sub(r"\D", "", price[1])) != expected_cost:
        return None
    h, w = frame.shape[:2]
    bbox = confirm[0]
    return (int(w * box[2] + (bbox[0][0] + bbox[2][0]) / 4),
            int(h * box[0] + (bbox[0][1] + bbox[2][1]) / 4))


def village_home_layout(img, read_region):
    """Recognize village controls in both the original and Battle/Build HUD."""
    def words(box):
        return set(re.findall(r"[a-z]+", " ".join(
            str(item[1]).lower() for item in (read_region(img, box) or [])
        )))

    left = words((0.82, 1.0, 0.035, 0.20))
    if not left.intersection({"battle", "attack"}):
        return None
    right = words((0.80, 1.0, 0.87, 1.0))
    if "battle" in left and "build" in right:
        return "battle-build"
    if "shop" in right:
        return "classic"
    # Shop moved halfway up the right edge in the new village HUD.
    if "shop" in words((0.44, 0.66, 0.87, 1.0)):
        return "battle-build" if "battle" in left else "classic"
    return None


def village_builder_anchor(img, read_region):
    """Return a frame-relative builder counter position before opening its menu."""
    if village_home_layout(img, read_region) != "battle-build":
        return 0.50, 0.06
    # The left counter above Build is builders; the right counter is the lab.
    box = (0.76, 0.86, 0.90, 0.96)
    for bbox, text, *_ in (read_region(img, box) or []):
        if re.fullmatch(r"\s*\d+\s*/\s*\d+\s*", str(text)):
            h, w = img.shape[:2]
            # run_ocr_on_region returns coordinates from a 2x resized crop.
            return (int(w * box[2]) + (bbox[0][0] + bbox[2][0]) / 4) / w, (int(h * box[0]) + (bbox[0][1] + bbox[2][1]) / 4) / h
    return 0.93, 0.82


def run_layout_independent_builder_ocr(img, crop_box=BUILDER_MENU_CROP):
    """Read the translucent Builder list without depending on the village behind it."""
    h_orig, w_orig = img.shape[:2]
    ymin, ymax, xmin, xmax = crop_box
    cropped = img[
        int(h_orig * ymin):int(h_orig * ymax),
        int(w_orig * xmin):int(w_orig * xmax),
    ]
    if cropped.size == 0:
        return None

    if crop_box == BUILDER_SIDEBAR_CROP:
        # Reuse OCR only for identical pixels, dimensions, and engine. Fresh
        # captures still verify position; any moved row or changed digit misses
        # this cache. Return copies so callers cannot alter later reads.
        cache_key = (id(ocr), h_orig, w_orig,
                     hashlib.blake2s(cropped.tobytes()).digest())
        if cache_key in BUILDER_OCR_CACHE:
            BUILDER_OCR_CACHE.move_to_end(cache_key)
            return copy.deepcopy(BUILDER_OCR_CACHE[cache_key])
        # Opaque cards have clean text. Preserve both white and red prices;
        # the old translucent-panel mask discards unaffordable red prices.
        normalization = max(1.0, 1300.0 / w_orig)
        enlarged = cv2.resize(cropped, None, fx=2 * normalization, fy=2 * normalization, interpolation=cv2.INTER_CUBIC)
        color_results, _ = ocr(enlarged, use_cls=False)
        results = list(color_results or [])
        if not results:
            gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
            results, _ = ocr(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), use_cls=False)
            results = list(results or [])
        # Read labels in overlapping short strips as well. Whole-panel OCR
        # downsizes a tall list, causing individual Wall labels to disappear.
        # Strips preserve text size at every vertical position; overlap keeps
        # labels intact at strip boundaries. Screen text is always upright.
        label_x1, label_x2 = int(enlarged.shape[1] * .12), int(enlarged.shape[1] * .73)
        tile_height = min(480, enlarged.shape[0])
        starts = sorted(set(range(0, max(1, enlarged.shape[0]-tile_height+1), 352))
                        | {max(0, enlarged.shape[0]-tile_height)})
        recovered = list(color_results or [])
        for tile_y in starts:
            tile_results, _ = ocr(enlarged[tile_y:tile_y+tile_height, label_x1:label_x2], use_cls=False)
            for item in tile_results or []:
                recovered.append([[[point[0]+label_x1, point[1]+tile_y] for point in item[0]], *item[1:]])
        for item in recovered:
            label = re.sub(r"[^a-z0-9]", "", str(item[1]).lower())
            label = label.replace("wa11", "wall").replace("wal1", "wall").replace("waii", "wall").replace("wa1l", "wall")
            if not re.fullmatch(r"wall(?:x[0-9oilsb]+)?", label):
                continue
            center_y = sum(point[1] for point in item[0]) / 4
            results = [existing for existing in results if not (
                abs(sum(point[1] for point in existing[0]) / 4 - center_y) < 20
                and abs(existing[0][0][0] - item[0][0][0]) < 35
            )]
            results.append(item)
        if results and normalization != 1.0:
            for item in results:
                item[0] = [[point[0] / normalization, point[1] / normalization] for point in item[0]]
        BUILDER_OCR_CACHE[cache_key] = copy.deepcopy(results)
        while len(BUILDER_OCR_CACHE) > 8:
            BUILDER_OCR_CACHE.popitem(last=False)
        return results

    gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(cropped, cv2.COLOR_BGR2HSV)
    variants = [
        (
            "isolated-text",
            cv2.inRange(
                hsv,
                np.array([0, 0, 135]),
                np.array([179, 110, 255]),
            ),
        ),
        (
            "isolated-text-relaxed",
            cv2.inRange(
                hsv,
                np.array([0, 0, 115]),
                np.array([179, 150, 255]),
            ),
        ),
        (
            "local-contrast",
            cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray),
        ),
        ("grayscale-fallback", gray),
    ]

    def normalize_wall_text(text):
        normalized = re.sub(r"[^a-z0-9]", "", str(text).lower())
        return (
            normalized
            .replace("wa11", "wall")
            .replace("wal1", "wall")
            .replace("waii", "wall")
            .replace("wa1l", "wall")
        )

    def result_quality(results):
        if not results:
            return -1

        items = []
        for item in results:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            bbox, text = item[0], str(item[1])
            x_mid = (bbox[0][0] + bbox[2][0]) / 2
            y_mid = (bbox[0][1] + bbox[2][1]) / 2
            items.append((bbox, text, x_mid, y_mid))

        score = 0
        for _, text, wall_x, wall_y in items:
            wall_text = normalize_wall_text(text)
            if not re.fullmatch(r"wall(?:x\d+)?", wall_text):
                continue
            score += 80
            if re.fullmatch(r"wallx\d+", wall_text):
                score += 30

            for _, candidate_text, cost_x, cost_y in items:
                if cost_x <= wall_x + 40 or abs(cost_y - wall_y) > 45:
                    continue
                cleaned = re.sub(r"^[^\dOoSs]+", "", candidate_text)
                cleaned = cleaned.replace("O", "0").replace("o", "0")
                if re.fullmatch(r"\s*\d[\d,.\s]*\s*", cleaned):
                    digits = re.sub(r"\D", "", cleaned)
                    if len(digits) >= 3:
                        score += 120
                        break

        # Prefer isolated text when two passes find the same structured row.
        return score

    best_results = None
    best_score = -1
    for variant_index, (_, variant) in enumerate(variants):
        enlarged = cv2.resize(
            variant,
            None,
            fx=2,
            fy=2,
            interpolation=cv2.INTER_CUBIC,
        )
        ocr_ready = cv2.cvtColor(enlarged, cv2.COLOR_GRAY2BGR)
        results, _ = ocr(ocr_ready)
        score = result_quality(results)
        if variant_index == 0 and results and len(results) >= 2:
            return results
        if score > best_score:
            best_results = results
            best_score = score

        # A counted Wall row plus a price is already an unambiguous menu read.
        if score >= 230 and variant_index < 2:
            break

    return best_results


def builder_menu_results_show_open(results):
    """Return True only when OCR contains the Builder overlay's row structure."""
    if not results:
        return False

    texts = [str(item[1]).strip().lower() for item in results if len(item) >= 2]
    combined = " ".join(texts)
    structural_markers = (
        "upgrades in progress",
        "suggested upgrades",
        "other upgrades",
        "available",
    )
    if any(marker in combined for marker in structural_markers):
        return True

    # When the list is already scrolled, its headings may be off-screen. The
    # overlay still contains several upgrade-price rows; the village underneath
    # the closed overlay does not produce this dense price pattern in this crop.
    price_like_rows = 0
    for text in texts:
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 3:
            price_like_rows += 1
    return len(texts) >= 6 and price_like_rows >= 2


def builder_menu_overlay_visible(frame):
    """Detect the dark Builder panel even when it is scrolled to empty rows."""
    if frame is None or frame.size == 0:
        return False

    frame_h, frame_w = frame.shape[:2]
    value = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)[:, :, 2]
    y1, y2 = int(frame_h * 0.16), int(frame_h * 0.64)
    inner = value[
        y1:y2,
        int(frame_w * 0.40):int(frame_w * 0.68),
    ]
    left_outer = value[
        y1:y2,
        int(frame_w * 0.30):int(frame_w * 0.36),
    ]
    right_outer = value[
        y1:y2,
        int(frame_w * 0.72):int(frame_w * 0.78),
    ]
    right_inside_edge = value[
        y1:y2,
        int(frame_w * 0.695):int(frame_w * 0.70),
    ]
    right_outside_edge = value[
        y1:y2,
        int(frame_w * 0.70):int(frame_w * 0.705),
    ]
    if (
        inner.size == 0
        or left_outer.size == 0
        or right_outer.size == 0
        or right_inside_edge.size == 0
        or right_outside_edge.size == 0
    ):
        return False

    outer_mean = float(
        np.concatenate((left_outer.ravel(), right_outer.ravel())).mean()
    )
    inner_mean = float(inner.mean())
    inner_median = float(np.median(inner))
    inner_dark_fraction = float((inner < 90).mean())
    right_edge_contrast = (
        float(right_outside_edge.mean()) - float(right_inside_edge.mean())
    )

    return (
        outer_mean > 1
        and inner_mean / outer_mean < 0.78
        and inner_median < 110
        and inner_dark_fraction > 0.50
        and right_edge_contrast > 15
    )


def match_end_screen_visible(frame):
    """Detect the battle result screen without relying on one OCR word."""
    if frame is None or frame.size == 0:
        return False

    frame_h, frame_w = frame.shape[:2]
    button_crop = frame[
        int(frame_h * 0.72):int(frame_h * 0.96),
        int(frame_w * 0.32):int(frame_w * 0.70),
    ]
    button_text = ""
    if button_crop.size:
        gray = cv2.cvtColor(button_crop, cv2.COLOR_BGR2GRAY)
        enlarged = cv2.resize(
            gray,
            None,
            fx=2,
            fy=2,
            interpolation=cv2.INTER_CUBIC,
        )
        button_results, _ = ocr(cv2.cvtColor(enlarged, cv2.COLOR_GRAY2BGR))
        button_text = " ".join(
            str(item[1]).lower() for item in button_results or []
        )
        normalized_button = re.sub(r"[^a-z]+", "", button_text)
        if "returnhome" in normalized_button or "return" in normalized_button:
            return True

    scan_results, _ = ocr(frame)
    detected_text = " ".join(
        str(item[1]).lower() for item in scan_results or []
    )
    normalized_text = re.sub(r"[^a-z]+", " ", detected_text)
    if "return" in normalized_text and "home" in normalized_text:
        return True

    result_markers = (
        "victory",
        "defeat",
        "total damage",
        "you got",
        "troops expended",
        "bonus",
    )
    marker_count = sum(marker in normalized_text for marker in result_markers)

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    return_band = hsv[
        int(frame_h * 0.80):int(frame_h * 0.93),
        int(frame_w * 0.43):int(frame_w * 0.60),
    ]
    if return_band.size == 0:
        return False
    green_mask = cv2.inRange(
        return_band,
        np.array([35, 70, 80]),
        np.array([95, 255, 255]),
    )
    green_fraction = cv2.countNonZero(green_mask) / float(green_mask.size)
    return marker_count >= 2 and green_fraction >= 0.12


def locate_connection_lost_retry(frame):
    """Return the Try Again button center when the connection-loss dialog is visible."""
    if frame is None or frame.size == 0:
        return None

    frame_h, frame_w = frame.shape[:2]

    # Avoid running OCR on every ordinary frame. The connection dialog creates a
    # large, nearly neutral dark panel across the center of the game window.
    modal_probe = frame[
        int(frame_h * 0.38):int(frame_h * 0.65),
        int(frame_w * 0.30):int(frame_w * 0.75),
    ]
    if modal_probe.size == 0:
        return None
    probe_hsv = cv2.cvtColor(modal_probe, cv2.COLOR_BGR2HSV)
    dark_neutral_fraction = float(
        (
            (probe_hsv[:, :, 2] < 80)
            & (probe_hsv[:, :, 1] < 80)
        ).mean()
    )
    if dark_neutral_fraction < 0.45:
        return None

    crop_y1, crop_y2 = int(frame_h * 0.30), int(frame_h * 0.72)
    crop_x1, crop_x2 = int(frame_w * 0.25), int(frame_w * 0.78)
    dialog_crop = frame[crop_y1:crop_y2, crop_x1:crop_x2]
    if dialog_crop.size == 0:
        return None

    gray = cv2.cvtColor(dialog_crop, cv2.COLOR_BGR2GRAY)
    enlarged = cv2.resize(
        gray,
        None,
        fx=2,
        fy=2,
        interpolation=cv2.INTER_CUBIC,
    )
    results, _ = ocr(cv2.cvtColor(enlarged, cv2.COLOR_GRAY2BGR))
    if not results:
        return None

    normalized_items = []
    for item in results:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        normalized_items.append(
            (item[0], re.sub(r"[^a-z]+", "", str(item[1]).lower()))
        )

    combined = "".join(text for _, text in normalized_items)
    connection_lost = (
        "connectionlost" in combined
        or "lostconnection" in combined
    )
    retry_present = "tryagain" in combined
    if not connection_lost or not retry_present:
        return None

    button_items = [
        (bbox, normalized_text)
        for bbox, normalized_text in normalized_items
        if "tryagain" in normalized_text and len(normalized_text) <= 12
    ]
    for bbox, normalized_text in sorted(
        button_items,
        key=lambda item: len(item[1]),
    ):
        center_x = (bbox[0][0] + bbox[2][0]) / 2.0
        center_y = (bbox[0][1] + bbox[2][1]) / 2.0
        return (
            crop_x1 + int(center_x / 2.0),
            crop_y1 + int(center_y / 2.0),
        )

    # OCR sometimes merges the prompt sentence and misses the separate button
    # bbox. This fallback is relative to the confirmed dialog, not the village.
    return int(frame_w * 0.37), int(frame_h * 0.59)


def detect_builder_cost_resource(frame, cost_bbox, menu_results, menu_crop):
    """Classify the resource icon immediately beside a Builder-list price."""
    if frame is None or not menu_results:
        return "Unknown"

    frame_h, frame_w = frame.shape[:2]
    crop_ymin, crop_ymax, crop_xmin, crop_xmax = menu_crop
    crop_w = frame_w * (crop_xmax - crop_xmin)
    crop_h = frame_h * (crop_ymax - crop_ymin)
    max_ocr_x = max(point[0] for item in menu_results for point in item[0])
    max_ocr_y = max(point[1] for item in menu_results for point in item[0])
    # Builder OCR always enlarges by two, including sparsely populated pages.
    ocr_scale_x = ocr_scale_y = 2.0

    bbox_xs = [point[0] for point in cost_bbox]
    bbox_ys = [point[1] for point in cost_bbox]
    cost_left = int(
        frame_w * crop_xmin + (min(bbox_xs) / ocr_scale_x)
    )
    cost_right = int(
        frame_w * crop_xmin + (max(bbox_xs) / ocr_scale_x)
    )
    cost_y = int(
        frame_h * crop_ymin
        + (((min(bbox_ys) + max(bbox_ys)) / 2) / ocr_scale_y)
    )

    text_x1 = max(0, cost_left)
    text_x2 = min(frame_w, cost_right + 4)
    text_y1 = max(0, cost_y - 11)
    text_y2 = min(frame_h, cost_y + 11)
    text_patch = frame[text_y1:text_y2, text_x1:text_x2]
    if text_patch.size:
        tb = text_patch[:, :, 0].astype(int)
        tg = text_patch[:, :, 1].astype(int)
        tr = text_patch[:, :, 2].astype(int)
        # Brown sidebar cards are not red price text.
        red_pixels = int(((tr > 180) & (tr - tg > 90) & (tr - tb > 80)).sum())
        if red_pixels >= 12:
            return "Unavailable"

    icon_anchor = cost_right if menu_crop == BUILDER_SIDEBAR_CROP else cost_left
    search_x1 = max(0, icon_anchor - (2 if menu_crop == BUILDER_SIDEBAR_CROP else 45))
    search_x2 = min(frame_w, icon_anchor + 25)
    search_y1 = max(0, cost_y - 16)
    search_y2 = min(frame_h, cost_y + 16)
    icon_patch = frame[search_y1:search_y2, search_x1:search_x2]
    if icon_patch.size == 0:
        return "Unknown"

    hsv_patch = cv2.cvtColor(icon_patch, cv2.COLOR_BGR2HSV)
    resource_ranges = {
        "Gold": (
            np.array([12, 90, 90]),
            np.array([48, 255, 255]),
        ),
        "Elixir": (
            np.array([132, 60, 70]),
            np.array([179, 255, 255]),
        ),
    }
    if menu_crop == BUILDER_SIDEBAR_CROP:
        resource_ranges["Gold"] = (np.array([20, 120, 150]), np.array([40, 255, 255]))
    candidates = []
    for resource_name, (lower, upper) in resource_ranges.items():
        mask = cv2.inRange(hsv_patch, lower, upper)
        count, _, stats, centers = cv2.connectedComponentsWithStats(mask, 8)
        for component_index in range(1, count):
            local_x, local_y, icon_w, icon_h, area = stats[component_index]
            if not (6 <= area <= 220):
                continue
            if not (3 <= icon_w <= 20 and 3 <= icon_h <= 20):
                continue
            aspect = icon_w / max(1.0, float(icon_h))
            if not 0.45 <= aspect <= 1.85:
                continue

            center_x = search_x1 + float(centers[component_index][0])
            center_y = search_y1 + float(centers[component_index][1])
            x_distance = abs(center_x - icon_anchor)
            y_distance = abs(center_y - cost_y)
            if x_distance > 34 or y_distance > 12:
                continue
            score = (area * 2.0) - (x_distance * 12.0) - (y_distance * 4.0)
            candidates.append((score, area, -x_distance, resource_name))

    if not candidates:
        return "Unknown"
    candidates.sort(reverse=True)
    best = candidates[0]
    if best[0] < 0:
        return "Unknown"
    return best[3]


PLAYER_STATS_URL = "https://uaamvbxklmonvobssdfw.supabase.co/rest/v1/rpc/report_player_stats"
SUPABASE_ANON_KEY = "sb_publishable_0QUOfNfbHNz_0T2I299zvA_Gk3jQgyW"

class FancyCoCBotGUI:
    def __init__(self, root, session_info=None, on_logout=None, license_check=None):
        self.root = root
        self.session_info = session_info or {}
        self.on_logout = on_logout
        self.license_check = license_check
        self.root.title("Clash of Clans Premium Automation Terminal")
        self.root.geometry("600x750")
        self.root.configure(bg="#121212")
        self.root.resizable(False, False)
        
        # Core Threading Controls
        self.bot_running = False
        self.bot_thread = None
        self.match_loot_miss_count = 0
        
        # --- MODERN DARK THEME CONFIGURATIONS ---
        style = ttk.Style()
        style.theme_use('clam')
        style.configure(".", background="#121212", foreground="#E0E0E0")
        style.configure("TLabelframe", background="#1A1A1A", bordercolor="#333333", relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background="#1A1A1A", foreground="#4CAF50", font=("Arial", 10, "bold"))
        style.configure("TLabel", background="#1A1A1A", foreground="#CCCCCC", font=("Arial", 9))
        style.configure("TCheckbutton", background="#1A1A1A", foreground="#CCCCCC", font=("Arial", 9))
        style.configure("TSeparator", background="#333333")
        
        # --- TOP LEVEL STYLIZED STAT BOARD ---
        stat_frame = tk.Frame(root, bg="#1A1A1A", highlightbackground="#333333", highlightthickness=1)
        stat_frame.pack(fill="x", padx=15, pady=12)
        
        # Gold Card
        gold_box = tk.Frame(stat_frame, bg="#252525", bd=0, padx=10, pady=8)
        gold_box.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        tk.Label(gold_box, text="CURRENT GOLD", bg="#252525", fg="#FFB300", font=("Arial", 9, "bold")).pack(anchor="w")
        self.lbl_live_gold = tk.Label(gold_box, text="0", bg="#252525", fg="#FFFFFF", font=("Consolas", 18, "bold"))
        self.lbl_live_gold.pack(anchor="w", pady=2)
        
        # Elixir Card
        elixir_box = tk.Frame(stat_frame, bg="#252525", bd=0, padx=10, pady=8)
        elixir_box.pack(side="right", fill="both", expand=True, padx=8, pady=8)
        tk.Label(elixir_box, text="CURRENT ELIXIR", bg="#252525", fg="#E040FB", font=("Arial", 9, "bold")).pack(anchor="w")
        self.lbl_live_elixir = tk.Label(elixir_box, text="0", bg="#252525", fg="#FFFFFF", font=("Consolas", 18, "bold"))
        self.lbl_live_elixir.pack(anchor="w", pady=2)

        # --- MIDDLE ZONE CONFIGURATION PANEL ---
        settings_frame = ttk.LabelFrame(root, text=" Strategy Parameter Rules ", padding=15)
        settings_frame.pack(fill="x", padx=15, pady=5)
        
        self.enable_walls_var = tk.BooleanVar(value=True)
        self.chk_walls = ttk.Checkbutton(
            settings_frame, 
            text="Enable Automatic Wall Upgrades (Burn excess wealth)", 
            variable=self.enable_walls_var,
            command=self.toggle_upgrade_limit_state
        )
        self.chk_walls.grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
        
        ttk.Label(settings_frame, text="Start buying walls when resources hit:").grid(row=1, column=0, sticky="w", pady=6)
        self.txt_upgrade_limit = tk.Entry(settings_frame, width=15, bg="#2D2D2D", fg="#FFFFFF", bd=0, insertbackground="white", font=("Consolas", 10))
        self.txt_upgrade_limit.insert(0, "20,000,000")
        self.txt_upgrade_limit.grid(row=1, column=1, sticky="e", pady=6)
        
        ttk.Separator(settings_frame, orient="horizontal").grid(row=2, column=0, columnspan=2, sticky="ew", pady=8)
        
        ttk.Label(settings_frame, text="Minimum Enemy Gold required to attack:").grid(row=3, column=0, sticky="w", pady=4)
        self.txt_min_gold = tk.Entry(settings_frame, width=15, bg="#2D2D2D", fg="#FFFFFF", bd=0, insertbackground="white", font=("Consolas", 10))
        self.txt_min_gold.insert(0, "1,000,000")
        self.txt_min_gold.grid(row=3, column=1, sticky="e", pady=4)
        
        ttk.Label(settings_frame, text="Minimum Enemy Elixir required to attack:").grid(row=4, column=0, sticky="w", pady=4)
        self.txt_min_elixir = tk.Entry(settings_frame, width=15, bg="#2D2D2D", fg="#FFFFFF", bd=0, insertbackground="white", font=("Consolas", 10))
        self.txt_min_elixir.insert(0, "1,000,000")
        self.txt_min_elixir.grid(row=4, column=1, sticky="e", pady=4)
        
        ttk.Label(settings_frame, text="Scroll pages:").grid(row=5, column=0, sticky="w", pady=4)
        self.scroll_pages_var = tk.IntVar(value=11)
        self.txt_scroll_pages = tk.Spinbox(
            settings_frame, from_=1, to=20, textvariable=self.scroll_pages_var,
            width=15, bg="#2D2D2D", fg="#FFFFFF", bd=0,
            insertbackground="white", font=("Consolas", 10), justify="right"
        )
        self.txt_scroll_pages.grid(row=5, column=1, sticky="e", pady=4)
        
        # --- EMBEDDED SYSTEM TERMINAL CORE ---
        terminal_frame = ttk.LabelFrame(root, text=" Live Operations Terminal Feed ", padding=8)
        terminal_frame.pack(fill="both", expand=True, padx=15, pady=10)
        
        self.terminal_box = tk.Text(terminal_frame, height=14, width=60, state="disabled", bg="#0A0A0A", fg="#39FF14", font=("Consolas", 9), insertbackground="white")
        self.terminal_box.pack(side="left", fill="both", expand=True)
        
        term_scrollbar = ttk.Scrollbar(terminal_frame, command=self.terminal_box.yview)
        term_scrollbar.pack(side="right", fill="y")
        self.terminal_box.config(yscrollcommand=term_scrollbar.set)
        
        # --- CONTROL TRIGGER BUTTON ---
        self.btn_toggle = tk.Button(
            root, 
            text="â–¶ LAUNCH BOT ENGINE", 
            bg="#2E7D32", 
            fg="white", 
            font=("Arial", 11, "bold"), 
            bd=0, 
            pady=10, 
            activebackground="#1B5E20", 
            activeforeground="white",
            command=self.toggle_bot_engine
        )
        self.btn_toggle.pack(fill="x", padx=15, pady=15)
        
        self.write_terminal("Terminal System Active. Standing by for app window initialization...")

    def toggle_upgrade_limit_state(self):
        if self.enable_walls_var.get():
            self.txt_upgrade_limit.config(state="normal", bg="#2D2D2D", fg="#FFFFFF")
        else:
            self.txt_upgrade_limit.config(state="disabled", bg="#1A1A1A", fg="#555555")

    def __init__(self, root, session_info=None, on_logout=None, license_check=None):
        self.root = root
        self.session_info = session_info or {}
        self.on_logout = on_logout
        self.license_check = license_check
        if not self.session_info.get("license_key"):
            messagebox.showerror("License required", "Open CoC Auto Clash from the protected login app and enter a valid license key.")
            self.root.after(100, self.root.destroy)
            return
        root.title("CoC Auto Clash")
        root.geometry("760x860")
        root.configure(bg="#07111F")
        root.resizable(False, False)
        self.bot_running = False
        self.bot_thread = None
        self.match_loot_miss_count = 0

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background="#07111F", foreground="#EAF2FF")
        style.configure("TLabelframe", background="#0F1B2D", bordercolor="#263A56", relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background="#0F1B2D", foreground="#FFD166", font=("Segoe UI", 10, "bold"))
        style.configure("TLabel", background="#0F1B2D", foreground="#C9D6E8", font=("Segoe UI", 9))
        style.configure("TSeparator", background="#263A56")

        header = tk.Frame(root, bg="#07111F")
        header.pack(fill="x", padx=24, pady=(20, 10))
        title_row = tk.Frame(header, bg="#07111F")
        title_row.pack(fill="x")
        brand_block = tk.Frame(title_row, bg="#07111F")
        brand_block.pack(side="left")
        tk.Label(brand_block, text="CoC Auto Clash", bg="#07111F", fg="#FFD166", font=("Segoe UI", 24, "bold")).pack(anchor="w")
        tk.Label(brand_block, text="BY TANISH NAGAR", bg="#07111F", fg="#8FA3BC", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        tk.Button(title_row, text="Logout", bg="#263A56", fg="#EAF2FF", activebackground="#334C70", activeforeground="#FFFFFF", relief="flat", font=("Segoe UI", 9, "bold"), command=self.logout_from_app).pack(side="right", pady=(4, 0))
        tk.Label(header, text="Resource scanner, farming controls, and license-protected desktop access.", bg="#07111F", fg="#98A9C2", font=("Segoe UI", 10)).pack(anchor="w", pady=(3, 0))

        account_bar = tk.Frame(root, bg="#101C2F", highlightbackground="#263A56", highlightthickness=1)
        account_bar.pack(fill="x", padx=24, pady=(4, 10))
        account_email = self.session_info.get("user_email", "Licensed user")
        license_expiry = self.session_info.get("expires_at") or "Lifetime"
        tk.Label(account_bar, text=f"Logged in: {account_email}", bg="#101C2F", fg="#EAF2FF", font=("Segoe UI", 9, "bold")).pack(side="left", padx=16, pady=10)
        tk.Label(account_bar, text=f"Expires: {license_expiry}", bg="#101C2F", fg="#98A9C2", font=("Segoe UI", 9)).pack(side="right", padx=16)

        status_bar = tk.Frame(root, bg="#0F1B2D", bd=0, padx=18, pady=14, highlightbackground="#263A56", highlightthickness=1)
        status_bar.pack(fill="x", padx=24, pady=(0, 10))
        tk.Label(status_bar, text="Ready", bg="#0F1B2D", fg="#35D07F", font=("Segoe UI", 12, "bold")).pack(side="left")
        tk.Label(status_bar, text="Connect your game window, set targets, then start the farming run.", bg="#0F1B2D", fg="#B8C6DA", font=("Segoe UI", 9)).pack(side="left", padx=18)

        stat_frame = tk.Frame(root, bg="#07111F")
        stat_frame.pack(fill="x", padx=24, pady=(0, 8))
        gold_box = tk.Frame(stat_frame, bg="#0F1B2D", bd=0, padx=18, pady=14, highlightbackground="#263A56", highlightthickness=1)
        gold_box.pack(side="left", fill="both", expand=True, padx=(0, 8))
        tk.Label(gold_box, text="Village Gold", bg="#0F1B2D", fg="#FFD166", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.lbl_live_gold = tk.Label(gold_box, text="0", bg="#0F1B2D", fg="#FFFFFF", font=("Consolas", 24, "bold"))
        self.lbl_live_gold.pack(anchor="w")
        tk.Label(gold_box, text="Live OCR reading", bg="#0F1B2D", fg="#7F91AA", font=("Segoe UI", 8)).pack(anchor="w", pady=(8, 0))
        elixir_box = tk.Frame(stat_frame, bg="#0F1B2D", bd=0, padx=18, pady=14, highlightbackground="#263A56", highlightthickness=1)
        elixir_box.pack(side="left", fill="both", expand=True, padx=(8, 0))
        tk.Label(elixir_box, text="Village Elixir", bg="#0F1B2D", fg="#D678FF", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.lbl_live_elixir = tk.Label(elixir_box, text="0", bg="#0F1B2D", fg="#FFFFFF", font=("Consolas", 24, "bold"))
        self.lbl_live_elixir.pack(anchor="w")
        tk.Label(elixir_box, text="Live OCR reading", bg="#0F1B2D", fg="#7F91AA", font=("Segoe UI", 8)).pack(anchor="w", pady=(8, 0))

        settings_frame = ttk.LabelFrame(root, text=" Farming Strategy ", padding=14)
        settings_frame.pack(fill="x", padx=24, pady=(0, 8))
        settings_frame.columnconfigure(0, weight=1)
        self.enable_walls_var = tk.BooleanVar(value=True)
        wall_toggle_row = tk.Frame(settings_frame, bg="#0F1B2D")
        wall_toggle_row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=4)
        tk.Label(wall_toggle_row, text="Use extra resources for wall upgrades", bg="#0F1B2D", fg="#C9D6E8", font=("Segoe UI", 9)).pack(side="left")
        self.wall_toggle = tk.Button(wall_toggle_row, text="ON", width=8, bg="#35D07F", fg="#07111F", activebackground="#49E895", activeforeground="#07111F", relief="flat", font=("Segoe UI", 9, "bold"), command=self.toggle_wall_upgrade_switch)
        self.wall_toggle.pack(side="right")
        ttk.Label(settings_frame, text="Wall upgrade starts at:").grid(row=1, column=0, sticky="w", pady=6)
        self.txt_upgrade_limit = self._premium_entry(settings_frame, "20,000,000", 1)
        ttk.Separator(settings_frame, orient="horizontal").grid(row=2, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Label(settings_frame, text="Minimum enemy gold:").grid(row=3, column=0, sticky="w", pady=4)
        self.txt_min_gold = self._premium_entry(settings_frame, "1,000,000", 3)
        ttk.Label(settings_frame, text="Minimum enemy elixir:").grid(row=4, column=0, sticky="w", pady=4)
        self.txt_min_elixir = self._premium_entry(settings_frame, "1,000,000", 4)
        ttk.Label(settings_frame, text="Scroll pages:").grid(row=5, column=0, sticky="w", pady=4)
        self.scroll_pages_var = tk.IntVar(value=11)
        self.txt_scroll_pages = tk.Spinbox(settings_frame, from_=1, to=20, textvariable=self.scroll_pages_var, width=13, bg="#091321", fg="#FFFFFF", bd=0, insertbackground="white", font=("Consolas", 11), justify="right")
        self.txt_scroll_pages.grid(row=5, column=1, sticky="e", pady=4)
        self.btn_toggle = tk.Button(settings_frame, text="Start Farming Run", bg="#FFD166", fg="#08111F", font=("Segoe UI", 12, "bold"), bd=0, pady=10, activebackground="#FFE08A", activeforeground="#08111F", command=self.toggle_bot_engine)
        self.btn_toggle.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 10))

        terminal_frame = ttk.LabelFrame(root, text=" Activity Log ", padding=8)
        terminal_frame.pack(fill="both", expand=True, padx=24, pady=(0, 20))
        self.terminal_box = tk.Text(terminal_frame, height=10, width=60, state="disabled", bg="#091321", fg="#BFE7FF", font=("Consolas", 9), insertbackground="white", relief="flat", padx=8, pady=8)
        self.terminal_box.pack(side="left", fill="both", expand=True)
        term_scrollbar = ttk.Scrollbar(terminal_frame, command=self.terminal_box.yview)
        term_scrollbar.pack(side="right", fill="y")
        self.terminal_box.config(yscrollcommand=term_scrollbar.set)
        self.write_terminal("CoC Auto Clash is ready. Open Clash of Clans and start when prepared.")

    def _premium_entry(self, parent, value, row):
        entry = tk.Entry(parent, width=15, bg="#091321", fg="#FFFFFF", bd=0, insertbackground="white", font=("Consolas", 11), justify="right")
        entry.insert(0, value)
        entry.grid(row=row, column=1, sticky="e", pady=4)
        return entry

    def toggle_upgrade_limit_state(self):
        if self.enable_walls_var.get():
            self.txt_upgrade_limit.config(state="normal", bg="#091321", fg="#FFFFFF")
            self.wall_toggle.config(text="ON", bg="#35D07F", fg="#07111F", activebackground="#49E895", activeforeground="#07111F")
        else:
            self.txt_upgrade_limit.config(state="disabled", bg="#0F1B2D", fg="#5B6B80")
            self.wall_toggle.config(text="OFF", bg="#263A56", fg="#B8C6DA", activebackground="#334C70", activeforeground="#FFFFFF")

    def toggle_wall_upgrade_switch(self):
        self.enable_walls_var.set(not self.enable_walls_var.get())
        self.toggle_upgrade_limit_state()

    def logout_from_app(self):
        self.bot_running = False
        if self.on_logout:
            self.on_logout("Logged out.")

    def write_terminal(self, text):
        self._capture_stats_log(text)
        self.root.after(0, lambda: self._unsafe_write_terminal(text))

    def _unsafe_write_terminal(self, text):
        self.terminal_box.config(state="normal")
        self.terminal_box.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.terminal_box.see(tk.END)
        self.terminal_box.config(state="disabled")

    def update_gui_metrics(self, gold_val, elixir_val):
        gold_number = max(0, int(gold_val or 0))
        elixir_number = max(0, int(elixir_val or 0))
        changed = False
        if gold_number > 0:
            changed = changed or gold_number != getattr(self, "_stats_gold", None)
            self._stats_gold = gold_number
        if elixir_number > 0:
            changed = changed or elixir_number != getattr(self, "_stats_elixir", None)
            self._stats_elixir = elixir_number
        if gold_number > 0:
            self._last_stable_gold = gold_number
        if elixir_number > 0:
            self._last_stable_elixir = elixir_number
        self._report_player_stats(force=changed)
        self.root.after(0, lambda: self.lbl_live_gold.config(text=f"{gold_val:,}"))
        self.root.after(0, lambda: self.lbl_live_elixir.config(text=f"{elixir_val:,}"))

    def _capture_stats_log(self, text):
        old_status = getattr(self, "_stats_activity_status", "")
        self._stats_activity_status = old_status or "Running"
        lowered = text.lower()
        if any(marker in lowered for marker in (
            "[matchmaking", "[deploy]", "combat", "[battle end]",
            "[farming launch]", "clicking find a match",
        )):
            self._stats_activity_status = "Attacking"
        elif any(marker in lowered for marker in (
            "[upgrades routine]", "[upgrades scan]", "[upgrades refind]",
            "wall upgrade", "wallet drain",
        )):
            self._stats_activity_status = "Upgrading walls"
        elif any(marker in lowered for marker in (
            "[village hub status]", "home screen confirmed",
            "home screen detected",
        )):
            self._stats_activity_status = "Home village"
        elif "stopping background automation" in lowered:
            self._stats_activity_status = "Stopped"
        if self._stats_activity_status != old_status:
            self._report_player_stats(force=True)
        if "[Upgrades Routine]: Scanning builder menu" in text:
            self._stats_wall_groups = {}
        if "[Upgrades Scan]:" in text:
            groups = getattr(self, "_stats_wall_groups", {})
            for count_text in re.findall(r"(?i)\bWall(?:\s*x\s*(\d+))?\s*->", text):
                count = int(count_text) if count_text else 1
                label = "Wall" if count == 1 else f"Wall x{count}"
                groups[label] = count
            self._stats_wall_groups = groups
            if groups:
                self._stats_walls_remaining = sum(groups.values())
        if "Cheapest affordable wall noted:" in text:
            resource_match = re.search(r"\|\s*(Gold|Elixir|Unknown)\s+Cost", text, re.I)
            cost_match = re.search(r"Cost\s+([\d,]+)", text, re.I)
            self._stats_pending_resource = resource_match.group(1).title() if resource_match else "Unknown"
            self._stats_pending_cost = int(cost_match.group(1).replace(",", "")) if cost_match else None
            selected_group = re.search(r"(?i)Cheapest affordable wall noted:\s*(Wall(?:\s*x\s*\d+)?)", text)
            self._stats_pending_wall_group = selected_group.group(1).replace("X", "x") if selected_group else ""
            self._report_player_stats(force=True)
        if "Wall upgrade confirmed." in text:
            balances = re.search(
                r"Gold:\s*[\d,]+->([\d,]+)\s*\|\s*Elixir:\s*[\d,]+->([\d,]+)",
                text,
                re.I,
            )
            if balances:
                self._stats_gold = int(balances.group(1).replace(",", ""))
                self._stats_elixir = int(balances.group(2).replace(",", ""))
            if getattr(self, "_stats_walls_remaining", None) is not None:
                self._stats_walls_remaining = max(0, self._stats_walls_remaining - 1)
            groups = getattr(self, "_stats_wall_groups", {})
            selected = getattr(self, "_stats_pending_wall_group", "")
            if selected in groups:
                groups[selected] = max(0, groups[selected] - 1)
                if groups[selected] == 0:
                    groups.pop(selected, None)
            self._report_player_stats(
                event_type="wall_upgrade",
                force=True,
                resource=getattr(self, "_stats_pending_resource", "Unknown"),
                cost=getattr(self, "_stats_pending_cost", None),
            )

    def _report_player_stats(self, event_type="heartbeat", force=False, resource=None, cost=None):
        session_info = getattr(self, "session_info", {}) or {}
        if session_info.get("stats_enabled", True) is False:
            return
        license_key = session_info.get("license_key", "")
        device_id = session_info.get("device_id", "")
        if not license_key or not device_id:
            return
        now = time.time()
        payload = {
            "p_event_type": event_type,
            "p_license_key": license_key,
            "p_device_id": device_id,
            "p_device_name": session_info.get("device_name", ""),
            "p_current_gold": getattr(self, "_stats_gold", 0),
            "p_current_elixir": getattr(self, "_stats_elixir", 0),
            "p_walls_remaining": getattr(self, "_stats_walls_remaining", None),
            "p_wall_groups": [
                {"label": label, "count": count}
                for label, count in getattr(self, "_stats_wall_groups", {}).items()
            ],
            "p_activity_status": getattr(self, "_stats_activity_status", "Running"),
            "p_resource": resource,
            "p_cost": cost,
        }
        fingerprint = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        unchanged = fingerprint == getattr(self, "_stats_last_fingerprint", "")
        if not force and unchanged and now - getattr(self, "_stats_last_report_at", 0) < 1800:
            return
        self._stats_last_report_at = now
        self._stats_last_fingerprint = fingerprint
        threading.Thread(target=self._send_player_stats, args=(payload,), daemon=True).start()

    def _send_player_stats(self, payload):
        try:
            request = urllib.request.Request(
                PLAYER_STATS_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "apikey": SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=6) as response:
                result = json.loads(response.read().decode("utf-8") or "{}")
                if result.get("stats_enabled") is False:
                    self.session_info["stats_enabled"] = False
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read().decode("utf-8") or "{}")
                if result.get("stats_enabled") is False:
                    self.session_info["stats_enabled"] = False
            except Exception:
                pass
        except Exception:
            # Stats reporting must never interrupt farming.
            pass

    def bot_sleep(self, seconds):
        deadline = time.time() + seconds
        while self.bot_running and time.time() < deadline:
            time.sleep(min(0.1, deadline - time.time()))
        return self.bot_running

    def clean_input_string(self, text_str, default_value):
        digits = re.sub(r'[^\d]', '', text_str)
        if not digits:
            return default_value
        return int(digits)

    def get_scroll_page_limit(self):
        try:
            value = int(self.scroll_pages_var.get())
        except (TypeError, ValueError, tk.TclError):
            value = 11
        value = max(1, min(20, value))
        self.scroll_pages_var.set(value)
        return value

    def force_license_logout(self, reason):
        self.bot_running = False
        if self.on_logout:
            self.on_logout(reason)

    def toggle_bot_engine(self):
        if not self.bot_running:
            if self.license_check:
                ok, reason = self.license_check()
                if not ok:
                    self.force_license_logout(reason)
                    return
            self.clean_input_string(self.txt_min_gold.get(), 1000000)
            self.clean_input_string(self.txt_min_elixir.get(), 1000000)
            if self.enable_walls_var.get():
                self.clean_input_string(self.txt_upgrade_limit.get(), 20000000)
                
            self.bot_running = True
            self.btn_toggle.config(text="â–  HALT BOT AUTOMATION", bg="#C62828", activebackground="#B71C1C")
            self.write_terminal("Fired execution loop hooks. Parsing environment parameters...")
            
            self.bot_thread = threading.Thread(target=self.core_bot_loop, daemon=True)
            self.bot_thread.start()
        else:
            self.bot_running = False
            self.btn_toggle.config(text="â–¶ LAUNCH BOT ENGINE", bg="#2E7D32", activebackground="#1B5E20")
            self.write_terminal("Gracefully stopping background automation execution blocks...")
            pyautogui.press("esc")

    def get_coc_window(self):
        try:
            candidates = []
            for win in gw.getAllWindows():
                title = win.title.lower()
                if not title:
                    continue

                is_bot_window = "premium automation terminal" in title
                is_game_window = "google play games" in title or title.startswith("clash of clans -")
                is_large_enough = win.width >= 800 and win.height >= 500

                if is_game_window and not is_bot_window and is_large_enough:
                    priority = 0 if title.startswith("clash of clans -") else 1
                    candidates.append((priority, win))

            if candidates:
                candidates.sort(key=lambda item: item[0])
                win = candidates[0][1]
                if win.isMinimized:
                    win.restore()
                win.activate()
                time.sleep(1)
                self.write_terminal(f"[Window Bind]: Using game window '{win.title}' ({win.width}x{win.height}).")
                return win.left, win.top, win.width, win.height
        except Exception as e:
            self.write_terminal(f"Error capturing window handle: {e}")
        return None

    def press_real_escape(self):
        try:
            target_win = None
            for win in gw.getAllWindows():
                title = win.title.lower()
                if title.startswith("clash of clans -") or "google play games" in title:
                    target_win = win
                    if win.isMinimized:
                        win.restore()
                    win.activate()
                    break
            if target_win is not None and hasattr(target_win, "_hWnd"):
                ctypes.windll.user32.SetForegroundWindow(target_win._hWnd)
                ctypes.windll.user32.BringWindowToTop(target_win._hWnd)
            time.sleep(0.15)

            if target_win is not None and hasattr(target_win, "_hWnd") and win32gui and win32con:
                vk_escape = 0x1B
                scan_escape = 0x01
                lparam_down = 1 | (scan_escape << 16)
                lparam_up = 1 | (scan_escape << 16) | (0xC0 << 24)
                win32gui.PostMessage(target_win._hWnd, win32con.WM_KEYDOWN, vk_escape, lparam_down)
                time.sleep(0.05)
                win32gui.PostMessage(target_win._hWnd, win32con.WM_KEYUP, vk_escape, lparam_up)
                return

            class KEYBDINPUT(ctypes.Structure):
                _fields_ = [
                    ("wVk", ctypes.c_ushort),
                    ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
                ]

            class INPUTUNION(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT)]

            class INPUT(ctypes.Structure):
                _fields_ = [
                    ("type", ctypes.c_ulong),
                    ("union", INPUTUNION),
                ]

            input_keyboard = 1
            keyeventf_keyup = 0x0002
            keyeventf_scancode = 0x0008
            esc_scan_code = 0x01
            extra = ctypes.c_ulong(0)

            press = INPUT(
                type=input_keyboard,
                union=INPUTUNION(
                    ki=KEYBDINPUT(0, esc_scan_code, keyeventf_scancode, 0, ctypes.pointer(extra))
                ),
            )
            release = INPUT(
                type=input_keyboard,
                union=INPUTUNION(
                    ki=KEYBDINPUT(0, esc_scan_code, keyeventf_scancode | keyeventf_keyup, 0, ctypes.pointer(extra))
                ),
            )

            ctypes.windll.user32.SendInput(1, ctypes.byref(press), ctypes.sizeof(INPUT))
            time.sleep(0.08)
            ctypes.windll.user32.SendInput(1, ctypes.byref(release), ctypes.sizeof(INPUT))

        except Exception as e:
            self.write_terminal(f"[State Recovery]: Windows Esc failed, using fallback Esc: {e}")
            pyautogui.press("esc")

    def capture_screen(self, win_rect):
        x, y, w, h = win_rect
        def grab_frame():
            screenshot = pyautogui.screenshot(region=(x, y, w, h))
            return cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

        frame = grab_frame()
        retry_button = locate_connection_lost_retry(frame)
        if retry_button is None:
            return frame

        with CONNECTION_POPUP_RECOVERY_LOCK:
            frame = grab_frame()
            retry_button = locate_connection_lost_retry(frame)
            for attempt in range(1, 4):
                if retry_button is None:
                    break
                btn_x = x + retry_button[0]
                btn_y = y + retry_button[1]
                self.write_terminal(
                    f"[Connection Recovery]: Connection lost popup detected. "
                    f"Clicking Try Again (attempt {attempt}/3): "
                    f"[X: {btn_x}, Y: {btn_y}]"
                )
                pyautogui.click(btn_x, btn_y)
                time.sleep(1.5)
                frame = grab_frame()
                retry_button = locate_connection_lost_retry(frame)

            if retry_button is None:
                self.write_terminal(
                    "[Connection Recovery]: Connection popup cleared. "
                    "Resuming automation."
                )
            else:
                self.write_terminal(
                    "[Connection Recovery]: Popup is still visible; "
                    "it will be retried on the next screen check."
                )
        return frame

    def run_ocr_on_region(self, img, crop_box):
        h_orig, w_orig = img.shape[:2]
        ymin, ymax, xmin, xmax = crop_box
        cropped = img[int(h_orig*ymin):int(h_orig*ymax), int(w_orig*xmin):int(w_orig*xmax)]
        if cropped.size == 0:
            return None
        gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        ocr_ready = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        results, _ = ocr(ocr_ready)
        return results

    def get_resources_by_corner(self, img):
        enemy_gold, enemy_elixir = self.get_enemy_loot(img)

        def read_resource_value(crop_boxes, max_value=50000000, robust=False):
            candidates = []

            for crop_box in crop_boxes:
                results_by_scale = []
                h_orig, w_orig = img.shape[:2]
                ymin, ymax, xmin, xmax = crop_box
                cropped = img[int(h_orig*ymin):int(h_orig*ymax), int(w_orig*xmin):int(w_orig*xmax)]
                if cropped.size == 0:
                    continue

                scales = (3, 4) if robust else (3,)
                use_inverted = (False, True) if robust else (False,)
                for scale in scales:
                    gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
                    for invert in use_inverted:
                        candidate_gray = 255 - gray if invert else gray
                        candidate_gray = cv2.resize(candidate_gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
                        ocr_ready = cv2.cvtColor(candidate_gray, cv2.COLOR_GRAY2BGR)
                        results, _ = ocr(ocr_ready)
                        if results:
                            results_by_scale.append(results)

                for results in results_by_scale:
                    rows = []
                    sorted_items = sorted(results, key=lambda item: (item[0][0][1] + item[0][2][1]) / 2)
                    for item in sorted_items:
                        bbox, text_string = item[0], item[1]
                        digits = re.sub(r'\D', '', text_string)
                        if not digits:
                            continue

                        x_mid = (bbox[0][0] + bbox[2][0]) / 2
                        y_mid = (bbox[0][1] + bbox[2][1]) / 2
                        placed = False
                        for row in rows:
                            if abs(row["y"] - y_mid) <= 45:
                                row["parts"].append((x_mid, digits))
                                row["y"] = (row["y"] + y_mid) / 2
                                placed = True
                                break
                        if not placed:
                            rows.append({"y": y_mid, "parts": [(x_mid, digits)]})

                    for row in rows:
                        ordered_parts = sorted(row["parts"], key=lambda part: part[0])
                        cleaned_parts = []
                        for part_index, (part_x, part_digits) in enumerate(ordered_parts):
                            previous_len = len(ordered_parts[part_index - 1][1]) if part_index > 0 else 0
                            next_len = len(ordered_parts[part_index + 1][1]) if part_index < len(ordered_parts) - 1 else 0
                            if len(part_digits) == 1 and previous_len >= 2 and next_len >= 2:
                                continue
                            cleaned_parts.append(part_digits)
                        combined_digits = "".join(cleaned_parts)
                        if not combined_digits:
                            continue

                        value = int(combined_digits)
                        digit_count = len(combined_digits)
                        if 0 < value <= max_value:
                            candidates.append((value, digit_count))

            if not candidates:
                return 0

            digit_counts = {}
            for _, digit_count in candidates:
                digit_counts[digit_count] = digit_counts.get(digit_count, 0) + 1
            # Wallet OCR often returns the trailing digits several times and the
            # complete value only once. Prefer the longest valid counter reading;
            # the max_value guard above rejects impossible concatenations.
            best_digit_count = max(digit_counts)
            values = sorted(value for value, digit_count in candidates if digit_count == best_digit_count)
            if not values:
                values = sorted(value for value, _ in candidates)
            return values[len(values) // 2]

        your_gold = read_resource_value([
            (0.045, 0.110, 0.800, 0.970),
        ], robust=True)
        your_elixir = read_resource_value([
            (0.125, 0.195, 0.82, 0.97),
            (0.115, 0.205, 0.80, 0.98),
        ], robust=False)

        return enemy_gold, enemy_elixir, your_gold, your_elixir

    def get_enemy_loot(self, img):
        left_results = self.run_ocr_on_region(img, (0.10, 0.30, 0.03, 0.20))
        enemy_gold, enemy_elixir = 0, 0
        if left_results:
            sorted_left = sorted(left_results, key=lambda x: (x[0][0][1] + x[0][2][1]) / 2)
            enemy_candidates = []
            for item in sorted_left:
                bbox_coords = item[0]
                text_string = item[1]
                digits = re.sub(r'\D', '', text_string) # FIXED: Cleans text using the extracted string
                if digits:
                    value = int(digits)
                    # Available loot can be small, but this rejects timer/troop noise like 24s or 20/20.
                    if 100 <= value <= 50000000:
                        y_mid = (bbox_coords[0][1] + bbox_coords[2][1]) / 2
                        enemy_candidates.append((y_mid, value, text_string))
            if len(enemy_candidates) >= 2:
                enemy_candidates.sort(key=lambda item: item[0])
                enemy_gold = enemy_candidates[0][1]
                enemy_elixir = enemy_candidates[1][1]
        return enemy_gold, enemy_elixir

    def check_match_end(self, img):
        return True if match_end_screen_visible(img) else None

    def is_home_screen(self, img):
        return village_home_layout(img, self.run_ocr_on_region) is not None

    def dismiss_okay_popup(self, img, win_rect):
        scan_results, _ = ocr(img)
        if not scan_results:
            return False

        detected_text = " ".join(item[1].lower() for item in scan_results)
        if "star bonus received" not in detected_text and "okay" not in detected_text:
            return False

        x, y, w, h = win_rect
        if "star bonus received" in detected_text:
            btn_x = x + int(w * 0.52)
            btn_y = y + int(h * 0.84)
            self.write_terminal(f"[Popup Recovery]: Star bonus popup detected. Clicking Okay: [X: {btn_x}, Y: {btn_y}]")
            pyautogui.click(btn_x, btn_y)
            return True

        for item in scan_results:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            bbox, text = item[0], str(item[1])
            if "okay" in text.lower():
                btn_x = x + int((bbox[0][0] + bbox[2][0]) / 2)
                btn_y = y + int((bbox[0][1] + bbox[2][1]) / 2)
                self.write_terminal(f"[Popup Recovery]: Clicking Okay button: [X: {btn_x}, Y: {btn_y}]")
                pyautogui.click(btn_x, btn_y)
                return True

        return False

    def dismiss_reload_popup(self, img, win_rect):
        scan_results, _ = ocr(img)
        if not scan_results:
            return False

        detected_text = " ".join(item[1].lower() for item in scan_results)
        if "disconnected due to inactivity" not in detected_text and "reload game" not in detected_text:
            return False

        x, y, w, h = win_rect
        for item in scan_results:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            bbox, text = item[0], str(item[1])
            if "reload game" in text.lower():
                btn_x = x + int((bbox[0][0] + bbox[2][0]) / 2)
                btn_y = y + int((bbox[0][1] + bbox[2][1]) / 2)
                self.write_terminal(f"[Popup Recovery]: Disconnected/reload popup detected. Clicking Reload Game: [X: {btn_x}, Y: {btn_y}]")
                pyautogui.click(btn_x, btn_y)
                break
        else:
            self.write_terminal("[Popup Recovery]: Disconnected/reload popup detected but Reload Game button text not found.")
            return True

        self.write_terminal("[Popup Recovery]: Waiting for home screen to return after reload...")
        wait_deadline = time.time() + 60
        while self.bot_running and time.time() < wait_deadline:
            if not self.bot_sleep(2.0):
                return True
            current_win_rect = self.get_coc_window()
            if not current_win_rect:
                continue
            current_frame = self.capture_screen(current_win_rect)
            if self.is_home_screen(current_frame):
                self.write_terminal("[Popup Recovery]: Home screen confirmed after reload.")
                break
        else:
            self.write_terminal("[Popup Recovery]: Timed out waiting for home screen after reload.")

        return True

    def is_attack_screen(self, enemy_gold, enemy_elixir):
        return enemy_gold > 0 and enemy_elixir > 0

    def is_match_ui_screen(self, img):
        scan_results, _ = ocr(img)
        if not scan_results:
            return False

        detected_text = " ".join(item[1].lower() for item in scan_results)
        match_markers = [
            "available loot",
            "battle starts",
            "battle ends",
            "end battle",
            "surrender",
            "next",
            "overall damage",
            "deploy",
        ]
        return any(marker in detected_text for marker in match_markers)

    def screen_has_text(self, win_rect, keywords, timeout=8):
        deadline = time.time() + timeout
        keywords = [keyword.lower() for keyword in keywords]
        while time.time() < deadline and self.bot_running:
            frame = self.capture_screen(win_rect)
            scan_results, _ = ocr(frame)
            if scan_results:
                detected_text = " ".join(item[1].lower() for item in scan_results)
                if any(keyword in detected_text for keyword in keywords):
                    return True
            time.sleep(0.2)
        return False

    def find_and_upgrade_walls(self, win_rect, current_gold, current_elixir):
        self.last_wall_scan_blocked_by_cost = False
        x, y, w, h = win_rect

        def parse_wall_options(menu_results, frame=None, menu_crop=None):
            wall_options = []
            if not menu_results:
                return wall_options

            def detect_cost_resource(cost_bbox):
                if frame is None or menu_crop is None:
                    return "Unknown"
                return detect_builder_cost_resource(
                    frame,
                    cost_bbox,
                    menu_results,
                    menu_crop,
                )

            rows = []
            for item in menu_results:
                bbox, text = item[0], item[1]
                x_mid = (bbox[0][0] + bbox[2][0]) / 2
                y_mid = (bbox[0][1] + bbox[2][1]) / 2
                placed = False

                for row in rows:
                    if abs(row["y"] - y_mid) <= 45:
                        row["items"].append({"bbox": bbox, "text": text, "x": x_mid, "y": y_mid})
                        row["y"] = (row["y"] + y_mid) / 2
                        placed = True
                        break

                if not placed:
                    rows.append({"y": y_mid, "items": [{"bbox": bbox, "text": text, "x": x_mid, "y": y_mid}]})

            all_items = [
                {"bbox": item[0], "text": item[1], "x": (item[0][0][0] + item[0][2][0]) / 2, "y": (item[0][0][1] + item[0][2][1]) / 2}
                for item in menu_results
            ]

            def is_wall_label(text):
                raw_text = text.lower()
                normalized = re.sub(r'[^a-z0-9]', '', text.lower())
                normalized = (
                    normalized
                    .replace('wa11', 'wall')
                    .replace('wal1', 'wall')
                    .replace('waii', 'wall')
                    .replace('wa1l', 'wall')
                )
                if "level" in normalized:
                    return False
                # A Builder-menu wall label is only "Wall" or "Wall x<count>".
                # Do not accept arbitrary OCR text that merely begins with
                # "wal"; that caused unrelated rows to become wall candidates.
                return bool(re.fullmatch(r"wall(?:x\d+)?", normalized))

            def extract_embedded_wall_cost(text):
                # Handles the case where RapidOCR merges the wall label, wall
                # count, and upgrade cost into a single OCR result, e.g.
                # "Wall x99 18,000". Strips the "Wall" label and the "x<count>"
                # portion so the count (e.g. x99) can never be mistaken for the
                # cost, normalizes common OCR digit mistakes, then extracts the
                # remaining numeric value(s), requiring at least 4 digits.
                working = text
                working = re.sub(r'(?i)wa[l1i]{1,2}', '', working)
                working = re.sub(r'(?i)x\s*[\dOo]+', '', working)
                normalized = working.replace('O', '0').replace('o', '0')
                raw_numbers = re.findall(r'\d[\d,\.]*', normalized)
                candidates = [re.sub(r'\D', '', raw) for raw in raw_numbers]
                candidates = [digits for digits in candidates if digits]
                if not candidates:
                    return None
                long_candidates = [digits for digits in candidates if len(digits) >= 4]
                if not long_candidates:
                    return None
                return int(max(long_candidates, key=lambda digits: int(digits)))

            max_item_x = max((part["x"] for part in all_items), default=0)

            for wall_item in sorted([part for part in all_items if is_wall_label(part["text"])], key=lambda part: part["y"]):
                nearby_costs = []
                for part in all_items:
                    if part is wall_item or is_wall_label(part["text"]):
                        continue
                    if part["x"] <= wall_item["x"] + 40:
                        continue
                    if max_item_x and part["x"] < max_item_x * 0.62:
                        continue
                    y_gap = abs(part["y"] - wall_item["y"])
                    if y_gap > 42:
                        continue

                    cleaned_part = re.sub(r'^0\d\s*:', '', part["text"])
                    cleaned_part = re.sub(r'^[^\dOoSs]+', '', cleaned_part)
                    cleaned_part = re.sub(r'(?i)\bx\s*\d+\b', '', cleaned_part)
                    cleaned_part = re.sub(r'(?i)(?<![a-z])s(?=\s*\d)', '5', cleaned_part)
                    cleaned_part = re.sub(r'(?i)^\s*s\s*$', '5', cleaned_part)
                    cleaned_part = cleaned_part.replace('O', '0').replace('o', '0')
                    digits = re.sub(r'\D', '', cleaned_part)
                    if digits:
                        nearby_costs.append((y_gap, part["x"], digits, part["bbox"]))

                if not nearby_costs:
                    embedded_cost = extract_embedded_wall_cost(wall_item["text"])
                    if embedded_cost is not None and embedded_cost >= 50:
                        cost_bbox = wall_item["bbox"]
                        resource_type = detect_cost_resource(cost_bbox)
                        row_bbox = [
                            [wall_item["bbox"][0][0], wall_item["bbox"][0][1]],
                            [wall_item["bbox"][2][0], wall_item["bbox"][2][1]]
                        ]
                        wall_options.append({
                            "cost": embedded_cost,
                            "wall_bbox": wall_item["bbox"],
                            "cost_bbox": cost_bbox,
                            "row_bbox": row_bbox,
                            "has_wall_count": bool(re.search(r'(?i)\bwa[l1i]{1,2}\s*x\s*\d+', wall_item["text"])),
                            "resource": resource_type,
                            "text": f"{wall_item['text']} -> {embedded_cost:,}"
                        })
                    continue

                nearby_costs.sort(key=lambda item: (item[0], item[1]))
                cost_digits = nearby_costs[0][2]
                if not cost_digits:
                    continue
                cost = int(cost_digits)
                if cost >= 50 and len(cost_digits) >= 2:
                    cost_bbox = nearby_costs[0][3]
                    resource_type = detect_cost_resource(cost_bbox)
                    row_bbox = [
                        [
                            min(wall_item["bbox"][0][0], cost_bbox[0][0]),
                            min(wall_item["bbox"][0][1], cost_bbox[0][1])
                        ],
                        [
                            max(wall_item["bbox"][2][0], cost_bbox[2][0]),
                            max(wall_item["bbox"][2][1], cost_bbox[2][1])
                        ]
                    ]
                    wall_options.append({
                        "cost": cost,
                        "wall_bbox": wall_item["bbox"],
                        "cost_bbox": cost_bbox,
                        "row_bbox": row_bbox,
                        "has_wall_count": bool(re.search(r'(?i)\bwa[l1i]{1,2}\s*x\s*\d+', wall_item["text"])),
                        "resource": resource_type,
                        "text": f"{wall_item['text']} -> {cost:,}"
                    })

            return wall_options

        def click_wall_option(target, menu_results, menu_crop):
            crop_ymin, crop_ymax, crop_xmin, crop_xmax = menu_crop
            crop_w = w * (crop_xmax - crop_xmin)
            crop_h = h * (crop_ymax - crop_ymin)
            max_ocr_x = max(point[0] for item in menu_results for point in item[0])
            max_ocr_y = max(point[1] for item in menu_results for point in item[0])
            ocr_scale_x = 2.0 if max_ocr_x > crop_w * 1.15 else 1.0
            ocr_scale_y = 2.0 if max_ocr_y > crop_h * 1.15 else 1.0

            def bbox_screen_center(bbox):
                xs = [point[0] for point in bbox]
                ys = [point[1] for point in bbox]
                local_x = ((min(xs) + max(xs)) / 2) / ocr_scale_x
                local_y = ((min(ys) + max(ys)) / 2) / ocr_scale_y
                return int(x + (w * crop_xmin) + local_x), int(y + (h * crop_ymin) + local_y)

            current_frame = self.capture_screen(win_rect)
            current_menu_results = run_layout_independent_builder_ocr(current_frame, menu_crop)
            current_options = parse_wall_options(current_menu_results, current_frame, menu_crop)
            visible_matches = [
                option for option in current_options
                if same_wall_target(
                    option,
                    target["cost"],
                    target.get("resource", "Unknown"),
                    target.get("has_wall_count", False),
                )
            ]
            if not visible_matches:
                self.write_terminal("[Upgrades Routine]: Target wall row changed before click. Recomputing scan.")
                return False

            current_target = visible_matches[0]
            row_x, row_y = bbox_screen_center(current_target["row_bbox"])
            self.write_terminal(f"Selecting Wall row from OCR row bbox: [X: {row_x}, Y: {row_y}] OCR scale: {ocr_scale_x}x{ocr_scale_y}")
            pyautogui.click(row_x, row_y)
            if not self.bot_sleep(0.8):
                return False

            selected_frame = self.capture_screen(win_rect)
            selected_results, _ = ocr(selected_frame)
            # Validate the selected-item title only in the bottom selection
            # panel. Full-screen OCR can still see a "Wall" word left behind
            # in another UI element and would otherwise approve a wrong item.
            selected_title_results = self.run_ocr_on_region(
                selected_frame, (0.60, 0.88, 0.30, 0.70)
            )
            selected_title = " ".join(
                str(item[1]).lower() for item in selected_title_results or []
            )
            if not re.search(r'wall\s*\(?\s*level', selected_title):
                self.write_terminal(
                    f"[Upgrades Routine]: Selected item is not a Wall dialog ({selected_title or 'no title OCR'}). Skipping it."
                )
                return False
            self.write_terminal(f"[Upgrades Routine]: Selected Wall dialog confirmed: {selected_title}")

            wall_resource = target.get("resource", "Unknown")
            use_gold_for_wall = wall_resource in ("Gold", "Unknown") and current_gold >= target["cost"]
            use_elixir_for_wall = wall_resource in ("Elixir", "Unknown") and current_elixir >= target["cost"]
            def find_upgrade_button_from_dialog(desired_resource):
                target_digits = str(target["cost"])
                candidates = []
                for item in selected_results or []:
                    if not isinstance(item, (list, tuple)) or len(item) < 2:
                        continue
                    bbox, text = item[0], str(item[1])
                    digits = re.sub(r'\D', '', text)
                    if digits != target_digits:
                        continue
                    bx1 = int(min(point[0] for point in bbox))
                    by1 = int(min(point[1] for point in bbox))
                    bx2 = int(max(point[0] for point in bbox))
                    by2 = int(max(point[1] for point in bbox))
                    if by1 < int(h * 0.55):
                        continue

                    icon_patch = selected_frame[
                        max(0, by1 - 8):min(h, by2 + 18),
                        max(0, bx2 - 2):min(w, bx2 + 34)
                    ]
                    resource = "Unknown"
                    gold_score = 0
                    elixir_score = 0
                    if icon_patch.size:
                        hsv = cv2.cvtColor(icon_patch, cv2.COLOR_BGR2HSV)
                        gold_score = int(cv2.countNonZero(cv2.inRange(hsv, np.array([15, 70, 90]), np.array([45, 255, 255]))))
                        elixir_score = int(cv2.countNonZero(cv2.bitwise_or(
                            cv2.inRange(hsv, np.array([130, 45, 75]), np.array([179, 255, 255])),
                            cv2.inRange(hsv, np.array([0, 45, 75]), np.array([8, 255, 255]))
                        )))
                        if elixir_score >= 8 and elixir_score >= gold_score:
                            resource = "Elixir"
                        elif gold_score >= 8:
                            resource = "Gold"
                    candidates.append({
                        "resource": resource,
                        "x": (bx1 + bx2) // 2,
                        "y": min(h - 1, by2 + 48),
                        "gold_score": gold_score,
                        "elixir_score": elixir_score
                    })

                exact = [c for c in candidates if c["resource"] == desired_resource]
                if exact:
                    return sorted(exact, key=lambda c: c["x"])[-1 if desired_resource == "Elixir" else 0]
                if desired_resource == "Elixir" and candidates:
                    return sorted(candidates, key=lambda c: c["x"])[-1]
                if desired_resource == "Gold" and candidates:
                    return sorted(candidates, key=lambda c: c["x"])[0]
                return None

            if use_gold_for_wall:
                upgrade_resource = "Gold"
                button = find_upgrade_button_from_dialog(upgrade_resource)
                if button:
                    upgrade_x, upgrade_y = x + button["x"], y + button["y"]
                else:
                    upgrade_x = x + int(w * 0.56)
                    upgrade_y = y + int(h * 0.81)
            elif use_elixir_for_wall:
                upgrade_resource = "Elixir"
                button = find_upgrade_button_from_dialog(upgrade_resource)
                if button:
                    upgrade_x, upgrade_y = x + button["x"], y + button["y"]
                else:
                    upgrade_x = x + int(w * 0.64)
                    upgrade_y = y + int(h * 0.81)
            else:
                self.write_terminal(f"[Upgrades Routine]: Cannot afford {wall_resource} wall cost {target['cost']:,}.")
                return False

            self.write_terminal(f"Clicking Wall {upgrade_resource} Upgrade button: [X: {upgrade_x}, Y: {upgrade_y}]")
            pyautogui.click(upgrade_x, upgrade_y)
            if not self.bot_sleep(1.2):
                return False

            confirm_x = x + int(w * 0.71)
            confirm_y = y + int(h * 0.85)
            self.write_terminal(f"Clicking Wall Confirm button: [X: {confirm_x}, Y: {confirm_y}]")
            pyautogui.click(confirm_x, confirm_y)
            if not self.bot_sleep(2.0):
                return False

            verify_frame = self.capture_screen(win_rect)
            _, _, after_gold, after_elixir = self.get_resources_by_corner(verify_frame)
            self.update_gui_metrics(after_gold, after_elixir)
            if after_gold == 0 and after_elixir == 0:
                self.write_terminal("[Upgrades Routine]: Could not verify wallet after wall click. Stopping wall drain.")
                return False

            if after_gold < current_gold or after_elixir < current_elixir:
                self.write_terminal(f"[Upgrades Routine]: Wall upgrade confirmed. Gold: {current_gold:,}->{after_gold:,} | Elixir: {current_elixir:,}->{after_elixir:,}")
                self.write_terminal("[Upgrades Routine]: Closing wall/builder panel after successful upgrade so the next batch starts clean...")
                pyautogui.click(x + int(w * 0.82), y + int(h * 0.50))
                if not self.bot_sleep(0.7):
                    return False
                return True

            self.write_terminal("[Upgrades Routine]: Wall upgrade did not spend resources. Stopping wall drain.")
            return False

        frame = self.capture_screen(win_rect)
        _, _, fresh_gold, fresh_elixir = self.get_resources_by_corner(frame)
        if fresh_gold > 0 or fresh_elixir > 0:
            current_gold, current_elixir = fresh_gold, fresh_elixir
            self.update_gui_metrics(current_gold, current_elixir)
            self.write_terminal(f"[Upgrades Routine]: Fresh wallet check - Gold: {current_gold:,} | Elixir: {current_elixir:,}")

        scan_results, _ = ocr(frame)
        if scan_results:
            detected_text = " ".join(item[1].lower() for item in scan_results)
            if "join a clan" in detected_text or "join now" in detected_text:
                self.write_terminal("[Upgrades Routine]: Closing clan panel before wall upgrade retry...")
                pyautogui.click(x + int(w * 0.38), y + int(h * 0.49))
                time.sleep(1.0)

        self.write_terminal("[Upgrades Routine]: Tapping Builder dropdown panel overlay anchor...")
        pyautogui.click(x + int(w * 0.50), y + int(h * 0.06))
        if not self.bot_sleep(2.0):
            return False
        
        menu_crop = BUILDER_MENU_CROP
        max_scroll_pages = self.get_scroll_page_limit()
        wall_scroll_amount = -350

        def reset_builder_list_to_top(scrolled_pages, reset_step=500, buffer_scrolls=2):
            total_scrolled_down_distance = max(0, scrolled_pages) * abs(wall_scroll_amount)
            clicks_needed = ((total_scrolled_down_distance + reset_step - 1) // reset_step) + buffer_scrolls
            pyautogui.moveTo(x + int(w * 0.50), y + int(h * 0.50))
            for _ in range(clicks_needed):
                pyautogui.scroll(reset_step)
                time.sleep(0.15)
        # The Builder menu can remember its prior scroll position after an
        # upgrade. Normalize it before every fresh scan so page 1 always means
        # the same on every wall-upgrade batch.
        self.write_terminal("[Upgrades Routine]: Normalizing builder list to top before scan...")
        reset_builder_list_to_top(max_scroll_pages - 1)
        if not self.bot_sleep(0.5):
            return False
        self.write_terminal(f"[Upgrades Routine]: Scanning builder menu up to {max_scroll_pages} pages for cheapest wall...")
        
        def is_affordable_wall(option):
            if option["cost"] < 500:
                return False
            resource = option.get("resource", "Unknown")
            if resource == "Unavailable":
                return False
            if resource == "Gold":
                return current_gold >= option["cost"]
            if resource == "Elixir":
                return current_elixir >= option["cost"]
            return current_gold >= option["cost"] or current_elixir >= option["cost"]

        def same_wall_target(option, target_cost, target_resource, require_wall_count=False):
            if require_wall_count and not option.get("has_wall_count"):
                return False
            option_resource = option.get("resource", "Unknown")
            resources_match = (
                option_resource == target_resource
                or option_resource == "Unknown"
                or target_resource == "Unknown"
            )
            return option["cost"] == target_cost and resources_match and is_affordable_wall(option)

        def near_wall_target(option, target_cost, target_resource):
            option_resource = option.get("resource", "Unknown")
            resources_match = (
                option_resource == target_resource
                or option_resource == "Unknown"
                or target_resource == "Unknown"
            )
            if not resources_match or not is_affordable_wall(option):
                return False
            allowed_gap = max(500, int(target_cost * 0.20))
            return abs(option["cost"] - target_cost) <= allowed_gap

        def scan_builder_pages(scan_label):
            found_options = []
            for page_index in range(max_scroll_pages):
                if not self.bot_running:
                    break

                frame = self.capture_screen(win_rect)
                menu_results = self.run_ocr_on_region(frame, menu_crop)
                page_wall_options = parse_wall_options(menu_results, frame, menu_crop)
                for option in page_wall_options:
                    option["page_index"] = page_index
                    option["menu_results"] = menu_results
                    found_options.append(option)

                if page_wall_options:
                    page_summary = "; ".join(
                        f"{option['text']} | {option.get('resource', 'Unknown')} Cost {option['cost']:,}"
                        for option in sorted(page_wall_options, key=lambda option: option["cost"])
                    )
                    self.write_terminal(
                        f"[{scan_label}]: Page {page_index + 1} wall rows found: {page_summary}"
                    )
                else:
                    self.write_terminal(f"[{scan_label}]: Page {page_index + 1} has no wall row.")

                if page_index < max_scroll_pages - 1:
                    pyautogui.moveTo(x + int(w * 0.50), y + int(h * 0.50))
                    pyautogui.scroll(wall_scroll_amount)
                    if not self.bot_sleep(0.8):
                        break

            return found_options

        def refresh_current_page_match(target_cost, target_resource, require_wall_count=False, attempts=2):
            last_results = None
            for attempt in range(attempts):
                time.sleep(0.25 if attempt == 0 else 0.18)
                fresh_frame = self.capture_screen(win_rect)
                fresh_results = self.run_ocr_on_region(fresh_frame, menu_crop)
                last_results = fresh_results
                fresh_options = parse_wall_options(fresh_results, fresh_frame, menu_crop)
                fresh_matches = [
                    option for option in fresh_options
                    if same_wall_target(option, target_cost, target_resource, require_wall_count)
                ]
                if fresh_matches:
                    return fresh_matches[0], fresh_results
                if attempt == 0:
                    self.write_terminal("[Upgrades Routine]: Fresh OCR missed wall after settle. Retrying same page once...")
            return None, last_results

        all_wall_options = scan_builder_pages("Upgrades Scan")
        counted_wall_options = [
            option for option in all_wall_options if option.get("has_wall_count")
        ]
        target_pool = counted_wall_options or all_wall_options
        if counted_wall_options and len(counted_wall_options) != len(all_wall_options):
            ignored_count = len(all_wall_options) - len(counted_wall_options)
            self.write_terminal(
                f"[Upgrades Scan]: Ignoring {ignored_count} ambiguous bare 'Wall' OCR row(s); counted Wall xN rows are available."
            )
        affordable_walls = [option for option in target_pool if is_affordable_wall(option)]

        if affordable_walls:
            target = min(affordable_walls, key=lambda option: option["cost"])
            target_resource = target.get("resource", "Unknown")
            self.write_terminal(
                f"[Upgrades Routine]: Cheapest affordable wall noted: "
                f"{target['text']} | {target_resource} Cost {target['cost']:,}"
            )

            # The menu remains open after the first scan.  Return through the
            # same list rather than closing/reopening it, which avoids losing
            # the current Builder-menu state.
            self.write_terminal("[Upgrades Routine]: Builder menu remains open. Returning to top for refind...")
            reset_builder_list_to_top(max_scroll_pages - 1)
            if not self.bot_sleep(0.8):
                return False

            target_page_index = max(0, min(max_scroll_pages - 1, int(target.get("page_index", 0))))
            if target_page_index:
                self.write_terminal(f"[Upgrades Refind]: Jumping directly to remembered page {target_page_index + 1}...")
                for _ in range(target_page_index):
                    pyautogui.scroll(wall_scroll_amount)
                    if not self.bot_sleep(0.8):
                        break

            for page_index in range(target_page_index, max_scroll_pages):
                if not self.bot_running:
                    break

                frame = self.capture_screen(win_rect)
                menu_results = self.run_ocr_on_region(frame, menu_crop)
                page_wall_options = parse_wall_options(menu_results, frame, menu_crop)
                matching_options = [
                    option for option in page_wall_options
                    if same_wall_target(
                        option,
                        target["cost"],
                        target_resource,
                        target.get("has_wall_count", False),
                    )
                ]

                if page_wall_options:
                    page_summary = "; ".join(
                        f"{option['text']} | {option.get('resource', 'Unknown')} Cost {option['cost']:,}"
                        for option in sorted(page_wall_options, key=lambda option: option["cost"])
                    )
                    self.write_terminal(
                        f"[Upgrades Refind]: Page {page_index + 1} wall rows found: {page_summary}"
                    )
                else:
                    self.write_terminal(f"[Upgrades Refind]: Page {page_index + 1} has no wall row.")

                if matching_options:
                    self.write_terminal("[Upgrades Routine]: Noted cheapest wall found again. Settling and refreshing OCR...")
                    fresh_match, fresh_results = refresh_current_page_match(
                        target["cost"],
                        target_resource,
                        target.get("has_wall_count", False),
                    )
                    if not fresh_match:
                        self.write_terminal("[Upgrades Routine]: Fresh OCR missed wall twice. Continuing scan.")
                        if page_index < max_scroll_pages - 1:
                            pyautogui.moveTo(x + int(w * 0.50), y + int(h * 0.50))
                            pyautogui.scroll(wall_scroll_amount)
                            if not self.bot_sleep(0.8):
                                break
                            continue
                        break
                    self.write_terminal("[Upgrades Routine]: Fresh OCR confirmed wall. Upgrading now...")
                    click_result = click_wall_option(fresh_match, fresh_results, menu_crop)
                    if click_result:
                        return True
                    self.write_terminal("[Upgrades Routine]: Wall click did not spend resources. Retrying wall drain instead of farming.")
                    return True

                if page_index < max_scroll_pages - 1:
                    pyautogui.moveTo(x + int(w * 0.50), y + int(h * 0.50))
                    pyautogui.scroll(wall_scroll_amount)
                    if not self.bot_sleep(0.8):
                        break

            self.write_terminal("[Upgrades Routine]: Refind missed noted wall. Returning to top in the open builder menu...")
            reset_builder_list_to_top(max_scroll_pages - 1, reset_step=650)
            if not self.bot_sleep(0.8):
                return False

            frame = self.capture_screen(win_rect)
            menu_results = self.run_ocr_on_region(frame, menu_crop)
            page_wall_options = parse_wall_options(menu_results, frame, menu_crop)
            matching_options = [
                option for option in page_wall_options
                if same_wall_target(
                    option,
                    target["cost"],
                    target_resource,
                    target.get("has_wall_count", False),
                )
            ]
            if matching_options:
                self.write_terminal("[Upgrades Routine]: Noted wall found on final top-page retry. Upgrading now...")
                click_result = click_wall_option(matching_options[0], menu_results, menu_crop)
                if click_result:
                    return True
                self.write_terminal("[Upgrades Routine]: Wall click did not spend resources. Retrying wall drain instead of farming.")
                return True

            self.write_terminal("[Upgrades Routine]: Could not find noted cheapest wall after reopening. Retrying wall scan instead of farming.")
            pyautogui.click(x + int(w * 0.82), y + int(h * 0.50))
            time.sleep(0.5)
            return True

        if all_wall_options:
            self.last_wall_scan_blocked_by_cost = True
            cheapest_wall = min(all_wall_options, key=lambda option: option["cost"])
            self.write_terminal(f"[Upgrades Routine]: Cheapest wall found costs {cheapest_wall['cost']:,}, but wallet cannot afford it.")
        else:
            self.write_terminal("[Upgrades Routine]: No wall rows found after full builder menu scan.")

        self.write_terminal("[Upgrades Routine]: Exiting builder panel matrix state profiles...")
        pyautogui.click(x + int(w * 0.82), y + int(h * 0.50))
        return False

    def core_bot_loop(self):
        while self.bot_running:
            min_gold = self.clean_input_string(self.txt_min_gold.get(), 1000000)
            min_elixir = self.clean_input_string(self.txt_min_elixir.get(), 1000000)
            upgrade_trigger = self.clean_input_string(self.txt_upgrade_limit.get(), 20000000) if self.enable_walls_var.get() else 999999999999
            
            win_rect = self.get_coc_window()
            if not win_rect:
                self.write_terminal("[System State Warning]: Cannot bind game app canvas windows. Retrying...")
                time.sleep(4)
                continue
                
            x, y, w, h = win_rect
            frame = self.capture_screen(win_rect)
            if self.dismiss_reload_popup(frame, win_rect):
                time.sleep(0.8)
                continue
            if self.dismiss_okay_popup(frame, win_rect):
                time.sleep(0.8)
                continue
            enemy_g, enemy_e = self.get_enemy_loot(frame)
            home_visible = False
            your_g, your_e = 0, 0
            if not self.is_attack_screen(enemy_g, enemy_e):
                home_visible = self.is_home_screen(frame)
                if home_visible:
                    _, _, your_g, your_e = self.get_resources_by_corner(frame)
            match_ui_visible = False if home_visible else self.is_match_ui_screen(frame)
            
            # --- EVALUATION MODULE A: ACTIVE MULTIPLAYER BATTLE MODE ---
            if self.is_attack_screen(enemy_g, enemy_e):
                self.match_loot_miss_count = 0
                self.write_terminal(f"[Matchmaking Assessment] -> Found Loot - Gold: {enemy_g:,} | Elixir: {enemy_e:,}")
                
                if enemy_g >= min_gold and enemy_e >= min_elixir:
                    self.write_terminal("ðŸš€ CRITERIA ACHIEVED! Unleashing combat deployment peripheral grids...")

                    self.write_terminal("[Deploy]: Zooming out battlefield before troop placement...")
                    zoom_x = x + int(w * 0.50)
                    zoom_y = y + int(h * 0.45)
                    pyautogui.click(zoom_x, zoom_y)
                    time.sleep(0.08)
                    pyautogui.moveTo(zoom_x, zoom_y)
                    for _ in range(12):
                        pyautogui.scroll(-10, x=zoom_x, y=zoom_y)
                        try:
                            ctypes.windll.user32.mouse_event(0x0800, 0, 0, -1200, 0)
                        except Exception:
                            pass
                        time.sleep(0.04)
                    pyautogui.keyDown('ctrl')
                    for _ in range(8):
                        pyautogui.scroll(-8, x=zoom_x, y=zoom_y)
                        time.sleep(0.03)
                    pyautogui.keyUp('ctrl')
                    time.sleep(0.25)

                    troop_slots = [0.21, 0.28, 0.34, 0.40]
                    hero_slots = [0.46, 0.52, 0.59, 0.65]
                    spell_slots = [0.72, 0.78, 0.84]
                    troop_deploy_points = [
                        (0.11, 0.50),
                        (0.15, 0.62),
                        (0.24, 0.71),
                        (0.42, 0.76),
                        (0.66, 0.76),
                        (0.86, 0.61),
                        (0.91, 0.48),
                        (0.82, 0.31),
                        (0.67, 0.19),
                        (0.47, 0.16),
                        (0.28, 0.23),
                    ]
                    hero_deploy_points = [
                        (0.14, 0.55),
                        (0.22, 0.67),
                        (0.78, 0.64),
                        (0.82, 0.38),
                    ]
                    spell_deploy_points = [
                        (0.50, 0.45),
                        (0.42, 0.52),
                        (0.58, 0.52),
                        (0.50, 0.36),
                    ]

                    for slot_index, slot_x in enumerate(troop_slots):
                        if not self.bot_running:
                            break

                        select_x = x + int(w * slot_x)
                        select_y = y + int(h * 0.93)
                        self.write_terminal(f"[Deploy]: Selecting troop slot {slot_index + 1}: [X: {select_x}, Y: {select_y}]")
                        pyautogui.click(select_x, select_y)
                        time.sleep(0.12)

                        repeat_count = 3 if slot_index == 1 else 2

                        for _ in range(repeat_count):
                            for point_x, point_y in troop_deploy_points:
                                if not self.bot_running:
                                    break
                                deploy_x = x + int(w * point_x)
                                deploy_y = y + int(h * point_y)
                                pyautogui.click(deploy_x, deploy_y)
                                time.sleep(0.07)

                    for hero_index, slot_x in enumerate(hero_slots):
                        if not self.bot_running:
                            break

                        select_x = x + int(w * slot_x)
                        select_y = y + int(h * 0.93)
                        self.write_terminal(f"[Deploy]: Selecting hero slot {hero_index + 1}: [X: {select_x}, Y: {select_y}]")
                        pyautogui.click(select_x, select_y)
                        time.sleep(0.12)

                        for point_x, point_y in hero_deploy_points:
                            if not self.bot_running:
                                break
                            deploy_x = x + int(w * point_x)
                            deploy_y = y + int(h * point_y)
                            pyautogui.click(deploy_x, deploy_y)
                            time.sleep(0.08)

                    for spell_index, slot_x in enumerate(spell_slots):
                        if not self.bot_running:
                            break

                        select_x = x + int(w * slot_x)
                        select_y = y + int(h * 0.93)
                        self.write_terminal(f"[Deploy]: Selecting spell slot {spell_index + 1}: [X: {select_x}, Y: {select_y}]")
                        pyautogui.click(select_x, select_y)
                        time.sleep(0.12)

                        for point_x, point_y in spell_deploy_points:
                            if not self.bot_running:
                                break
                            deploy_x = x + int(w * point_x)
                            deploy_y = y + int(h * point_y)
                            pyautogui.click(deploy_x, deploy_y)
                            time.sleep(0.08)
                        
                    self.write_terminal("â³ Full military deployment executed. Locking inside combat monitors...")
                    
                    combat_engagement = True
                    last_home_check = 0
                    while combat_engagement and self.bot_running:
                        if not self.bot_sleep(3):
                            break
                        combat_capture = self.capture_screen(win_rect)
                        if self.dismiss_okay_popup(combat_capture, win_rect):
                            combat_engagement = False
                            self.bot_sleep(0.8)
                            continue
                        termination_box = self.check_match_end(combat_capture)
                        
                        if termination_box:
                            self.write_terminal("ðŸ Match completion signature verified! Navigating return pathways...")
                            btn_x = x + int(w * 0.52)
                            btn_y = y + int(h * 0.86)
                            self.write_terminal(f"[Battle End]: Clicking Return Home button: [X: {btn_x}, Y: {btn_y}]")
                            pyautogui.click(btn_x, btn_y)
                            combat_engagement = False
                            for _ in range(8):
                                if not self.bot_sleep(0.75):
                                    break
                                post_battle_frame = self.capture_screen(win_rect)
                                if self.dismiss_okay_popup(post_battle_frame, win_rect):
                                    self.bot_sleep(1.0)
                                    break
                                if self.is_home_screen(post_battle_frame):
                                    break
                            continue

                        now = time.time()
                        if now - last_home_check >= 60:
                            last_home_check = now
                            if self.is_home_screen(combat_capture):
                                self.write_terminal("[State Recovery]: Home screen detected while monitoring battle. Resuming village cycle...")
                                combat_engagement = False
                else:
                    self.match_loot_miss_count = 0
                    self.write_terminal("âŒ Asset yield below thresholds. Activating 'Next Match' skip sequences...")
                    next_x = x + int(w * 0.93)
                    next_y = y + int(h * 0.75)
                    self.write_terminal(f"[Matchmaking Skip]: Clicking Next button: [X: {next_x}, Y: {next_y}]")
                    pyautogui.click(next_x, next_y)
                    time.sleep(1.0)
                    
            # --- EVALUATION MODULE B: VILLAGE HUB MONITOR OVERVIEW ---
            elif match_ui_visible:
                self.match_loot_miss_count += 1
                if self.match_loot_miss_count >= 2:
                    next_x = x + int(w * 0.93)
                    next_y = y + int(h * 0.75)
                    self.write_terminal(f"[Matchmaking Recovery]: Loot OCR missed {self.match_loot_miss_count} times. Clicking Next: [X: {next_x}, Y: {next_y}]")
                    pyautogui.click(next_x, next_y)
                    self.match_loot_miss_count = 0
                    time.sleep(1.0)
                else:
                    self.write_terminal("[State Check]: Match screen detected but loot OCR missed. Rechecking shortly...")
                    time.sleep(0.5)

            else:
                self.match_loot_miss_count = 0
                if home_visible:
                    self.write_terminal("[State Check]: Home screen detected from Shop button.")
                self.write_terminal(f"[Village Hub Status] -> Gold Balance: {your_g:,} | Elixir Balance: {your_e:,}")
                self.update_gui_metrics(your_g, your_e)
                
                if self.enable_walls_var.get() and your_g >= upgrade_trigger and your_e >= upgrade_trigger:
                    self.write_terminal("[Wallet Overflow Alert]: Capital reserves hit milestone trigger! Entering drainage loop...")
                    wall_scan_misses = 0
                    while self.bot_running:
                        expected_wallet = getattr(self, "_wall_expected_wallet", None)
                        if isinstance(expected_wallet, tuple) and len(expected_wallet) == 2:
                            g_now, e_now = expected_wallet
                            self._wall_expected_wallet = None
                            self.write_terminal(
                                f"[Upgrades Routine]: Using calculated post-upgrade wallet - "
                                f"Gold: {g_now:,} | Elixir: {e_now:,}"
                            )
                        else:
                            refreshed_frame = self.capture_screen(win_rect)
                            _, _, g_now, e_now = self.get_resources_by_corner(refreshed_frame)
                        self.update_gui_metrics(g_now, e_now)
                        has_upgraded = self.find_and_upgrade_walls(win_rect, g_now, e_now)
                        if not self.bot_running:
                            break
                        if has_upgraded:
                            wall_scan_misses = 0
                            time.sleep(0.5)
                            continue
                        if (g_now > 0 or e_now > 0) and not getattr(self, "last_wall_scan_blocked_by_cost", False) and wall_scan_misses < 3:
                            wall_scan_misses += 1
                            self.write_terminal("[Wallet Drain Retry]: Wall scan missed during drain. Rechecking before attack...")
                            time.sleep(0.8)
                            continue
                        break
                    self.write_terminal("[Wallet Drain Complete]: No affordable wall found. Returning to farming cycle...")

                if self.bot_running and home_visible:
                    self.write_terminal("[Farming Strategy Trigger]: Balance below trigger milestone or upgrades disabled. Launching attack...")
                    village_attack_x = x + int(w * 0.09)
                    village_attack_y = y + int(h * 0.90)
                    self.write_terminal(f"[Farming Launch]: Clicking village Attack button: [X: {village_attack_x}, Y: {village_attack_y}]")
                    pyautogui.click(village_attack_x, village_attack_y)
                    if not self.screen_has_text(win_rect, ["find a match", "multiplayer"], timeout=2.5):
                        self.write_terminal("[Farming Launch]: Multiplayer screen not detected. Retrying village Attack button...")
                        pyautogui.click(village_attack_x, village_attack_y)
                        if not self.screen_has_text(win_rect, ["find a match", "multiplayer"], timeout=2.5):
                            self.write_terminal("[Farming Launch]: Battle menu not recognized. Rechecking screen before further clicks.")
                            continue

                    find_match_x = x + int(w * 0.23)
                    find_match_y = y + int(h * 0.72)
                    self.write_terminal(f"[Farming Launch]: Clicking Find a Match button: [X: {find_match_x}, Y: {find_match_y}]")
                    pyautogui.click(find_match_x, find_match_y)
                    if not self.screen_has_text(win_rect, ["my army", "attack"], timeout=3):
                        self.write_terminal("[Farming Launch]: Army screen not detected. Retrying Find a Match button...")
                        pyautogui.click(find_match_x, find_match_y)
                        if not self.screen_has_text(win_rect, ["my army", "attack"], timeout=2.5):
                            self.write_terminal("[Farming Launch]: Army screen not recognized. Rechecking screen before further clicks.")
                            continue

                    final_attack_x = x + int(w * 0.85)
                    final_attack_y = y + int(h * 0.86)
                    self.write_terminal(f"[Farming Launch]: Clicking army Attack button: [X: {final_attack_x}, Y: {final_attack_y}]")
                    pyautogui.click(final_attack_x, final_attack_y)
                    time.sleep(1.0)
                elif self.bot_running:
                    self.write_terminal("[State Recovery]: Unknown screen detected. Sending real Esc key to close overlay/menu.")
                    self.press_real_escape()
                    time.sleep(1.0)

if __name__ == "__main__":
    root = tk.Tk()
    app = FancyCoCBotGUI(root)
    root.mainloop()

# The premium EXE keeps the UI/login class above. These overrides are the
# complete automation implementation from the supplied Python file.
_INCOMPLETE_AUTOMATION_OVERRIDE = r'''
class FancyCoCBotGUI(FancyCoCBotGUI):
    def get_coc_window(self):
        try:
            candidates = []
            for win in gw.getAllWindows():
                title = win.title.lower()
                if not title:
                    continue

                is_bot_window = "premium automation terminal" in title
                is_game_window = "google play games" in title or title.startswith("clash of clans -")
                is_large_enough = win.width >= 800 and win.height >= 500

                if is_game_window and not is_bot_window and is_large_enough:
                    priority = 0 if title.startswith("clash of clans -") else 1
                    candidates.append((priority, win))

            if candidates:
                candidates.sort(key=lambda item: item[0])
                win = candidates[0][1]
                if win.isMinimized:
                    win.restore()
                win.activate()
                time.sleep(1)
                self.write_terminal(f"[Window Bind]: Using game window '{win.title}' ({win.width}x{win.height}).")
                return win.left, win.top, win.width, win.height
        except Exception as e:
            self.write_terminal(f"Error capturing window handle: {e}")
        return None

    def press_real_escape(self):
        try:
            target_win = None
            for win in gw.getAllWindows():
                title = win.title.lower()
                if title.startswith("clash of clans -") or "google play games" in title:
                    target_win = win
                    if win.isMinimized:
                        win.restore()
                    win.activate()
                    break
            if target_win is not None and hasattr(target_win, "_hWnd"):
                ctypes.windll.user32.SetForegroundWindow(target_win._hWnd)
                ctypes.windll.user32.BringWindowToTop(target_win._hWnd)
            time.sleep(0.15)

            if target_win is not None and hasattr(target_win, "_hWnd") and win32gui and win32con:
                vk_escape = 0x1B
                scan_escape = 0x01
                lparam_down = 1 | (scan_escape << 16)
                lparam_up = 1 | (scan_escape << 16) | (0xC0 << 24)
                win32gui.PostMessage(target_win._hWnd, win32con.WM_KEYDOWN, vk_escape, lparam_down)
                time.sleep(0.05)
                win32gui.PostMessage(target_win._hWnd, win32con.WM_KEYUP, vk_escape, lparam_up)
                return

            class KEYBDINPUT(ctypes.Structure):
                _fields_ = [
                    ("wVk", ctypes.c_ushort),
                    ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
                ]

            class INPUTUNION(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT)]

            class INPUT(ctypes.Structure):
                _fields_ = [
                    ("type", ctypes.c_ulong),
                    ("union", INPUTUNION),
                ]

            input_keyboard = 1
            keyeventf_keyup = 0x0002
            keyeventf_scancode = 0x0008
            esc_scan_code = 0x01
            extra = ctypes.c_ulong(0)

            press = INPUT(
                type=input_keyboard,
                union=INPUTUNION(
                    ki=KEYBDINPUT(0, esc_scan_code, keyeventf_scancode, 0, ctypes.pointer(extra))
                ),
            )
            release = INPUT(
                type=input_keyboard,
                union=INPUTUNION(
                    ki=KEYBDINPUT(0, esc_scan_code, keyeventf_scancode | keyeventf_keyup, 0, ctypes.pointer(extra))
                ),
            )

            ctypes.windll.user32.SendInput(1, ctypes.byref(press), ctypes.sizeof(INPUT))
            time.sleep(0.08)
            ctypes.windll.user32.SendInput(1, ctypes.byref(release), ctypes.sizeof(INPUT))

        except Exception as e:
            self.write_terminal(f"[State Recovery]: Windows Esc failed, using fallback Esc: {e}")
            pyautogui.press("esc")

    def capture_screen(self, win_rect):
        x, y, w, h = win_rect
        def grab_frame():
            screenshot = pyautogui.screenshot(region=(x, y, w, h))
            return cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

        frame = grab_frame()
        retry_button = locate_connection_lost_retry(frame)
        if retry_button is None:
            return frame

        with CONNECTION_POPUP_RECOVERY_LOCK:
            frame = grab_frame()
            retry_button = locate_connection_lost_retry(frame)
            for attempt in range(1, 4):
                if retry_button is None:
                    break
                btn_x = x + retry_button[0]
                btn_y = y + retry_button[1]
                self.write_terminal(
                    f"[Connection Recovery]: Connection lost popup detected. "
                    f"Clicking Try Again (attempt {attempt}/3): "
                    f"[X: {btn_x}, Y: {btn_y}]"
                )
                pyautogui.click(btn_x, btn_y)
                time.sleep(1.5)
                frame = grab_frame()
                retry_button = locate_connection_lost_retry(frame)

            if retry_button is None:
                self.write_terminal(
                    "[Connection Recovery]: Connection popup cleared. "
                    "Resuming automation."
                )
            else:
                self.write_terminal(
                    "[Connection Recovery]: Popup is still visible; "
                    "it will be retried on the next screen check."
                )
        return frame

    def run_ocr_on_region(self, img, crop_box):
        h_orig, w_orig = img.shape[:2]
        ymin, ymax, xmin, xmax = crop_box
        cropped = img[int(h_orig*ymin):int(h_orig*ymax), int(w_orig*xmin):int(w_orig*xmax)]
        if cropped.size == 0:
            return None
        gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        ocr_ready = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        results, _ = ocr(ocr_ready)
        return results

    def get_resources_by_corner(self, img):
        enemy_gold, enemy_elixir = self.get_enemy_loot(img)

        def read_resource_value(crop_boxes, max_value=50000000, robust=False):
            candidates = []

            for crop_box in crop_boxes:
                results_by_scale = []
                h_orig, w_orig = img.shape[:2]
                ymin, ymax, xmin, xmax = crop_box
                cropped = img[int(h_orig*ymin):int(h_orig*ymax), int(w_orig*xmin):int(w_orig*xmax)]
                if cropped.size == 0:
                    continue

                scales = (3, 4) if robust else (3,)
                use_inverted = (False, True) if robust else (False,)
                for scale in scales:
                    gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
                    for invert in use_inverted:
                        candidate_gray = 255 - gray if invert else gray
                        candidate_gray = cv2.resize(candidate_gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
                        ocr_ready = cv2.cvtColor(candidate_gray, cv2.COLOR_GRAY2BGR)
                        results, _ = ocr(ocr_ready)
                        if results:
                            results_by_scale.append(results)

                for results in results_by_scale:
                    rows = []
                    sorted_items = sorted(results, key=lambda item: (item[0][0][1] + item[0][2][1]) / 2)
                    for item in sorted_items:
                        bbox, text_string = item[0], item[1]
                        digits = re.sub(r'\D', '', text_string)
                        if not digits:
                            continue

                        x_mid = (bbox[0][0] + bbox[2][0]) / 2
                        y_mid = (bbox[0][1] + bbox[2][1]) / 2
                        placed = False
                        for row in rows:
                            if abs(row["y"] - y_mid) <= 45:
                                row["parts"].append((x_mid, digits))
                                row["y"] = (row["y"] + y_mid) / 2
                                placed = True
                                break
                        if not placed:
                            rows.append({"y": y_mid, "parts": [(x_mid, digits)]})

                    for row in rows:
                        ordered_parts = sorted(row["parts"], key=lambda part: part[0])
                        cleaned_parts = []
                        for part_index, (part_x, part_digits) in enumerate(ordered_parts):
                            previous_len = len(ordered_parts[part_index - 1][1]) if part_index > 0 else 0
                            next_len = len(ordered_parts[part_index + 1][1]) if part_index < len(ordered_parts) - 1 else 0
                            if len(part_digits) == 1 and previous_len >= 2 and next_len >= 2:
                                continue
                            cleaned_parts.append(part_digits)
                        combined_digits = "".join(cleaned_parts)
                        if not combined_digits:
                            continue

                        value = int(combined_digits)
                        digit_count = len(combined_digits)
                        if 0 < value <= max_value:
                            candidates.append((value, digit_count))

            if not candidates:
                return 0

            digit_counts = {}
            for _, digit_count in candidates:
                digit_counts[digit_count] = digit_counts.get(digit_count, 0) + 1
            # Wallet OCR often returns the trailing digits several times and the
            # complete value only once. Prefer the longest valid counter reading;
            # the max_value guard above rejects impossible concatenations.
            best_digit_count = max(digit_counts)
            values = sorted(value for value, digit_count in candidates if digit_count == best_digit_count)
            if not values:
                values = sorted(value for value, _ in candidates)
            return values[len(values) // 2]

        your_gold = read_resource_value([
            (0.045, 0.110, 0.800, 0.970),
        ], robust=True)
        your_elixir = read_resource_value([
            (0.125, 0.195, 0.82, 0.97),
            (0.115, 0.205, 0.80, 0.98),
        ], robust=False)

        def reject_trailing_fragment(resource_name, value):
            previous = int(getattr(self, f"_last_stable_{resource_name}", 0) or 0)
            if previous > 0 and value > 0:
                previous_digits = str(previous)
                value_digits = str(value)
                if (
                    len(previous_digits) >= len(value_digits) + 2
                    and previous_digits.endswith(value_digits)
                ):
                    self.write_terminal(
                        f"[Wallet OCR]: Ignoring truncated {resource_name.title()} "
                        f"reading {value:,}; keeping {previous:,}."
                    )
                    return previous
            return value

        your_gold = reject_trailing_fragment("gold", your_gold)
        your_elixir = reject_trailing_fragment("elixir", your_elixir)
        return enemy_gold, enemy_elixir, your_gold, your_elixir

    def get_enemy_loot(self, img):
        left_results = self.run_ocr_on_region(img, (0.10, 0.30, 0.03, 0.20))
        enemy_gold, enemy_elixir = 0, 0
        if left_results:
            sorted_left = sorted(left_results, key=lambda x: (x[0][0][1] + x[0][2][1]) / 2)
            enemy_candidates = []
            for item in sorted_left:
                bbox_coords = item[0]
                text_string = item[1]
                digits = re.sub(r'\D', '', text_string) # FIXED: Cleans text using the extracted string
                if digits:
                    value = int(digits)
                    # Available loot can be small, but this rejects timer/troop noise like 24s or 20/20.
                    if 100 <= value <= 50000000:
                        y_mid = (bbox_coords[0][1] + bbox_coords[2][1]) / 2
                        enemy_candidates.append((y_mid, value, text_string))
            if len(enemy_candidates) >= 2:
                enemy_candidates.sort(key=lambda item: item[0])
                enemy_gold = enemy_candidates[0][1]
                enemy_elixir = enemy_candidates[1][1]
        return enemy_gold, enemy_elixir

    def check_match_end(self, img):
        return True if match_end_screen_visible(img) else None

    def is_home_screen(self, img):
        return village_home_layout(img, self.run_ocr_on_region) is not None

    def dismiss_okay_popup(self, img, win_rect):
        scan_results, _ = ocr(img)
        if not scan_results:
            return False

        detected_text = " ".join(item[1].lower() for item in scan_results)
        if "star bonus received" not in detected_text and "okay" not in detected_text:
            return False

        x, y, w, h = win_rect
        if "star bonus received" in detected_text:
            btn_x = x + int(w * 0.52)
            btn_y = y + int(h * 0.84)
            self.write_terminal(f"[Popup Recovery]: Star bonus popup detected. Clicking Okay: [X: {btn_x}, Y: {btn_y}]")
            pyautogui.click(btn_x, btn_y)
            return True

        for item in scan_results:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            bbox, text = item[0], str(item[1])
            if "okay" in text.lower():
                btn_x = x + int((bbox[0][0] + bbox[2][0]) / 2)
                btn_y = y + int((bbox[0][1] + bbox[2][1]) / 2)
                self.write_terminal(f"[Popup Recovery]: Clicking Okay button: [X: {btn_x}, Y: {btn_y}]")
                pyautogui.click(btn_x, btn_y)
                return True

        return False

    def dismiss_reload_popup(self, img, win_rect):
        scan_results, _ = ocr(img)
        if not scan_results:
            return False

        detected_text = " ".join(item[1].lower() for item in scan_results)
        if "disconnected due to inactivity" not in detected_text and "reload game" not in detected_text:
            return False

        x, y, w, h = win_rect
        for item in scan_results:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            bbox, text = item[0], str(item[1])
            if "reload game" in text.lower():
                btn_x = x + int((bbox[0][0] + bbox[2][0]) / 2)
                btn_y = y + int((bbox[0][1] + bbox[2][1]) / 2)
                self.write_terminal(f"[Popup Recovery]: Disconnected/reload popup detected. Clicking Reload Game: [X: {btn_x}, Y: {btn_y}]")
                pyautogui.click(btn_x, btn_y)
                break
        else:
            self.write_terminal("[Popup Recovery]: Disconnected/reload popup detected but Reload Game button text not found.")
            return True

        self.write_terminal("[Popup Recovery]: Waiting for home screen to return after reload...")
        wait_deadline = time.time() + 60
        while self.bot_running and time.time() < wait_deadline:
            if not self.bot_sleep(2.0):
                return True
            current_win_rect = self.get_coc_window()
            if not current_win_rect:
                continue
            current_frame = self.capture_screen(current_win_rect)
            if self.is_home_screen(current_frame):
                self.write_terminal("[Popup Recovery]: Home screen confirmed after reload.")
                break
        else:
            self.write_terminal("[Popup Recovery]: Timed out waiting for home screen after reload.")

        return True

    def is_attack_screen(self, enemy_gold, enemy_elixir):
        return enemy_gold > 0 and enemy_elixir > 0

    def is_match_ui_screen(self, img):
        scan_results, _ = ocr(img)
        if not scan_results:
            return False

        detected_text = " ".join(item[1].lower() for item in scan_results)
        match_markers = [
            "available loot",
            "battle starts",
            "battle ends",
            "end battle",
            "surrender",
            "next",
            "overall damage",
            "deploy",
        ]
        return any(marker in detected_text for marker in match_markers)

    def screen_has_text(self, win_rect, keywords, timeout=8):
        deadline = time.time() + timeout
        keywords = [keyword.lower() for keyword in keywords]
        while time.time() < deadline and self.bot_running:
            frame = self.capture_screen(win_rect)
            scan_results, _ = ocr(frame)
            if scan_results:
                detected_text = " ".join(item[1].lower() for item in scan_results)
                if any(keyword in detected_text for keyword in keywords):
                    return True
            time.sleep(0.2)
        return False

    @staticmethod
    def _is_plausible_wall_wallet_reading(
        before_gold,
        before_elixir,
        after_gold,
        after_elixir,
        upgrade_resource,
        upgrade_cost,
    ):
        if after_gold <= 0 or after_elixir <= 0 or upgrade_cost <= 0:
            return False

        if upgrade_resource == "Gold":
            chosen_before, chosen_after = before_gold, after_gold
            other_before, other_after = before_elixir, after_elixir
        elif upgrade_resource == "Elixir":
            chosen_before, chosen_after = before_elixir, after_elixir
            other_before, other_after = before_gold, after_gold
        else:
            return False

        chosen_spend = chosen_before - chosen_after
        cost_tolerance = max(50_000, int(upgrade_cost * 0.08))
        other_tolerance = max(50_000, int(max(other_before, 1) * 0.02))

        return (
            abs(chosen_spend - upgrade_cost) <= cost_tolerance
            and abs(other_after - other_before) <= other_tolerance
        )

    def find_and_upgrade_walls(self, win_rect, current_gold, current_elixir):
        self.last_wall_scan_blocked_by_cost = False
        x, y, w, h = win_rect

        def parse_wall_options(menu_results, frame=None, menu_crop=None):
            wall_options = []
            if not menu_results:
                return wall_options

            def detect_cost_resource(cost_bbox):
                if frame is None or menu_crop is None:
                    return "Unknown"

                crop_ymin, crop_ymax, crop_xmin, crop_xmax = menu_crop
                crop_w = w * (crop_xmax - crop_xmin)
                crop_h = h * (crop_ymax - crop_ymin)
                max_ocr_x = max(point[0] for item in menu_results for point in item[0])
                max_ocr_y = max(point[1] for item in menu_results for point in item[0])
                ocr_scale_x = 2.0 if max_ocr_x > crop_w * 1.15 else 1.0
                ocr_scale_y = 2.0 if max_ocr_y > crop_h * 1.15 else 1.0

                icon_x = int((cost_bbox[0][0] / ocr_scale_x) - 18)
                icon_y = int(((cost_bbox[0][1] + cost_bbox[2][1]) / 2) / ocr_scale_y)
                frame_h, frame_w = frame.shape[:2]
                screen_x = int(w * crop_xmin) + icon_x
                screen_y = int(h * crop_ymin) + icon_y

                x1 = max(0, screen_x - 10)
                x2 = min(frame_w, screen_x + 12)
                y1 = max(0, screen_y - 10)
                y2 = min(frame_h, screen_y + 12)
                patch = frame[y1:y2, x1:x2]
                if patch.size == 0:
                    return "Unknown"

                b = patch[:, :, 0].astype(int)
                g = patch[:, :, 1].astype(int)
                r = patch[:, :, 2].astype(int)
                gold_pixels = ((r > 150) & (g > 110) & (b < 120)).sum()
                elixir_pixels = ((r > 120) & (b > 120) & (g < 120)).sum()

                text_x1 = max(0, int(w * crop_xmin + (cost_bbox[0][0] / ocr_scale_x)) - 4)
                text_x2 = min(frame_w, int(w * crop_xmin + (cost_bbox[2][0] / ocr_scale_x)) + 4)
                text_y1 = max(0, int(h * crop_ymin + (cost_bbox[0][1] / ocr_scale_y)) - 4)
                text_y2 = min(frame_h, int(h * crop_ymin + (cost_bbox[2][1] / ocr_scale_y)) + 4)
                text_patch = frame[text_y1:text_y2, text_x1:text_x2]
                if text_patch.size:
                    tb = text_patch[:, :, 0].astype(int)
                    tg = text_patch[:, :, 1].astype(int)
                    tr = text_patch[:, :, 2].astype(int)
                    red_pixels = ((tr > 145) & (tg < 110) & (tb < 120)).sum()
                    if red_pixels >= 12:
                        return "Unavailable"

                if gold_pixels > elixir_pixels * 1.4 and gold_pixels >= 8:
                    return "Gold"
                if elixir_pixels > gold_pixels * 1.4 and elixir_pixels >= 8:
                    return "Elixir"
                return "Unk…5854 tokens truncated…ption for option in page_wall_options
                            if near_wall_target(option, target["cost"], target_resource)
                        ],
                        key=lambda option: abs(option["cost"] - target["cost"])
                    )

                if page_wall_options:
                    page_summary = "; ".join(
                        f"{option['text']} | {option.get('resource', 'Unknown')} Cost {option['cost']:,}"
                        for option in sorted(page_wall_options, key=lambda option: option["cost"])
                    )
                    self.write_terminal(
                        f"[Upgrades Refind]: Page {page_index + 1} wall rows found: {page_summary}"
                    )
                else:
                    self.write_terminal(f"[Upgrades Refind]: Page {page_index + 1} has no wall row.")

                if matching_options:
                    self.write_terminal("[Upgrades Routine]: Noted cheapest wall found again. Settling and refreshing OCR...")
                    fresh_match, fresh_results = refresh_current_page_match(target["cost"], target_resource)
                    if not fresh_match:
                        self.write_terminal("[Upgrades Routine]: Fresh OCR missed wall twice. Continuing scan.")
                        if page_index < max_scroll_pages - 1:
                            pyautogui.moveTo(x + int(w * 0.50), y + int(h * 0.50))
                            pyautogui.scroll(wall_scroll_amount)
                            if not self.bot_sleep(0.8):
                                break
                            continue
                        break
                    self.write_terminal("[Upgrades Routine]: Fresh OCR confirmed wall. Upgrading now...")
                    click_result = click_wall_option(fresh_match, fresh_results, menu_crop)
                    if click_result:
                        return True
                    self.write_terminal("[Upgrades Routine]: Wall click did not spend resources. Retrying wall drain instead of farming.")
                    return True

                if page_index < max_scroll_pages - 1:
                    pyautogui.moveTo(x + int(w * 0.50), y + int(h * 0.50))
                    pyautogui.scroll(wall_scroll_amount)
                    if not self.bot_sleep(0.8):
                        break

            self.write_terminal("[Upgrades Routine]: Refind missed noted wall. Trying one final top-page reset...")
            pyautogui.click(x + int(w * 0.82), y + int(h * 0.50))
            if not self.bot_sleep(0.8):
                return False
            pyautogui.click(x + int(w * 0.50), y + int(h * 0.06))
            if not self.bot_sleep(1.2):
                return False
            if not ensure_builder_menu_open("final top-page retry"):
                return False
            reset_builder_list_to_top(max_scroll_pages - 1, reset_step=650)

            frame = self.capture_screen(win_rect)
            menu_results = self.run_ocr_on_region(frame, menu_crop)
            page_wall_options = parse_wall_options(menu_results, frame, menu_crop)
            matching_options = [
                option for option in page_wall_options
                if same_wall_target(option, target["cost"], target_resource)
            ]
            if not matching_options:
                matching_options = sorted(
                    [
                        option for option in page_wall_options
                        if near_wall_target(option, target["cost"], target_resource)
                    ],
                    key=lambda option: abs(option["cost"] - target["cost"])
                )
            if matching_options:
                self.write_terminal("[Upgrades Routine]: Noted wall found on final top-page retry. Upgrading now...")
                matching_options[0]["identity"] = target_identity
                click_result = click_wall_option(matching_options[0], menu_results, menu_crop)
                if click_result:
                    return True
                self.write_terminal("[Upgrades Routine]: Wall click did not spend resources. Retrying wall drain instead of farming.")
                return True

            self.write_terminal("[Upgrades Routine]: Could not find noted cheapest wall after reopening. Retrying wall scan instead of farming.")
            pyautogui.click(x + int(w * 0.82), y + int(h * 0.50))
            time.sleep(0.5)
            return True

        if all_wall_options:
            self.last_wall_scan_blocked_by_cost = True
            cheapest_wall = min(all_wall_options, key=lambda option: option["cost"])
            self.write_terminal(f"[Upgrades Routine]: Cheapest wall found costs {cheapest_wall['cost']:,}, but wallet cannot afford it.")
        else:
            self.write_terminal("[Upgrades Routine]: No wall rows found after full builder menu scan.")

        self.write_terminal("[Upgrades Routine]: Exiting builder panel matrix state profiles...")
        pyautogui.click(x + int(w * 0.82), y + int(h * 0.50))
        return False

    def core_bot_loop(self):
        while self.bot_running:
            min_gold = self.clean_input_string(self.txt_min_gold.get(), 1000000)
            min_elixir = self.clean_input_string(self.txt_min_elixir.get(), 1000000)
            upgrade_trigger = self.clean_input_string(self.txt_upgrade_limit.get(), 20000000) if self.enable_walls_var.get() else 999999999999
            
            win_rect = self.get_coc_window()
            if not win_rect:
                self.write_terminal("[System State Warning]: Cannot bind game app canvas windows. Retrying...")
                time.sleep(4)
                continue
                
            x, y, w, h = win_rect
            frame = self.capture_screen(win_rect)
            if self.dismiss_reload_popup(frame, win_rect):
                time.sleep(0.8)
                continue
            if self.dismiss_okay_popup(frame, win_rect):
                time.sleep(0.8)
                continue
            enemy_g, enemy_e = self.get_enemy_loot(frame)
            home_visible = False
            your_g, your_e = 0, 0
            if not self.is_attack_screen(enemy_g, enemy_e):
                home_visible = self.is_home_screen(frame)
                if home_visible:
                    _, _, your_g, your_e = self.get_resources_by_corner(frame)
            match_ui_visible = False if home_visible else self.is_match_ui_screen(frame)
            
            # --- EVALUATION MODULE A: ACTIVE MULTIPLAYER BATTLE MODE ---
            if self.is_attack_screen(enemy_g, enemy_e):
                self.match_loot_miss_count = 0
                self.write_terminal(f"[Matchmaking Assessment] -> Found Loot - Gold: {enemy_g:,} | Elixir: {enemy_e:,}")
                
                if enemy_g >= min_gold and enemy_e >= min_elixir:
                    self.write_terminal("Ã°Å¸Å¡â‚¬ CRITERIA ACHIEVED! Unleashing combat deployment peripheral grids...")

                    self.write_terminal("[Deploy]: Zooming out battlefield before troop placement...")
                    zoom_x = x + int(w * 0.50)
                    zoom_y = y + int(h * 0.45)
                    pyautogui.click(zoom_x, zoom_y)
                    time.sleep(0.08)
                    pyautogui.moveTo(zoom_x, zoom_y)
                    for _ in range(12):
                        pyautogui.scroll(-10, x=zoom_x, y=zoom_y)
                        try:
                            ctypes.windll.user32.mouse_event(0x0800, 0, 0, -1200, 0)
                        except Exception:
                            pass
                        time.sleep(0.04)
                    pyautogui.keyDown('ctrl')
                    for _ in range(8):
                        pyautogui.scroll(-8, x=zoom_x, y=zoom_y)
                        time.sleep(0.03)
                    pyautogui.keyUp('ctrl')
                    time.sleep(0.25)

                    troop_slots = [0.21, 0.28, 0.34, 0.40]
                    hero_slots = [0.46, 0.52, 0.59, 0.65]
                    spell_slots = [0.72, 0.78, 0.84]
                    troop_deploy_points = [
                        (0.11, 0.50),
                        (0.15, 0.62),
                        (0.24, 0.71),
                        (0.42, 0.76),
                        (0.66, 0.76),
                        (0.86, 0.61),
                        (0.91, 0.48),
                        (0.82, 0.31),
                        (0.67, 0.19),
                        (0.47, 0.16),
                        (0.28, 0.23),
                    ]
                    hero_deploy_points = [
                        (0.14, 0.55),
                        (0.22, 0.67),
                        (0.78, 0.64),
                        (0.82, 0.38),
                    ]
                    spell_deploy_points = [
                        (0.50, 0.45),
                        (0.42, 0.52),
                        (0.58, 0.52),
                        (0.50, 0.36),
                    ]

                    for slot_index, slot_x in enumerate(troop_slots):
                        if not self.bot_running:
                            break

                        select_x = x + int(w * slot_x)
                        select_y = y + int(h * 0.93)
                        self.write_terminal(f"[Deploy]: Selecting troop slot {slot_index + 1}: [X: {select_x}, Y: {select_y}]")
                        pyautogui.click(select_x, select_y)
                        time.sleep(0.12)

                        repeat_count = 3 if slot_index == 1 else 2

                        for _ in range(repeat_count):
                            for point_x, point_y in troop_deploy_points:
                                if not self.bot_running:
                                    break
                                deploy_x = x + int(w * point_x)
                                deploy_y = y + int(h * point_y)
                                pyautogui.click(deploy_x, deploy_y)
                                time.sleep(0.07)

                    for hero_index, slot_x in enumerate(hero_slots):
                        if not self.bot_running:
                            break

                        select_x = x + int(w * slot_x)
                        select_y = y + int(h * 0.93)
                        self.write_terminal(f"[Deploy]: Selecting hero slot {hero_index + 1}: [X: {select_x}, Y: {select_y}]")
                        pyautogui.click(select_x, select_y)
                        time.sleep(0.12)

                        for point_x, point_y in hero_deploy_points:
                            if not self.bot_running:
                                break
                            deploy_x = x + int(w * point_x)
                            deploy_y = y + int(h * point_y)
                            pyautogui.click(deploy_x, deploy_y)
                            time.sleep(0.08)

                    for spell_index, slot_x in enumerate(spell_slots):
                        if not self.bot_running:
                            break

                        select_x = x + int(w * slot_x)
                        select_y = y + int(h * 0.93)
                        self.write_terminal(f"[Deploy]: Selecting spell slot {spell_index + 1}: [X: {select_x}, Y: {select_y}]")
                        pyautogui.click(select_x, select_y)
                        time.sleep(0.12)

                        for point_x, point_y in spell_deploy_points:
                            if not self.bot_running:
                                break
                            deploy_x = x + int(w * point_x)
                            deploy_y = y + int(h * point_y)
                            pyautogui.click(deploy_x, deploy_y)
                            time.sleep(0.08)
                        
                    self.write_terminal("Ã¢ÂÂ³ Full military deployment executed. Locking inside combat monitors...")
                    
                    combat_engagement = True
                    last_home_check = 0
                    while combat_engagement and self.bot_running:
                        if not self.bot_sleep(3):
                            break
                        combat_capture = self.capture_screen(win_rect)
                        if self.dismiss_okay_popup(combat_capture, win_rect):
                            combat_engagement = False
                            self.bot_sleep(0.8)
                            continue
                        termination_box = self.check_match_end(combat_capture)
                        
                        if termination_box:
                            self.write_terminal("Ã°Å¸ÂÂ Match completion signature verified! Navigating return pathways...")
                            btn_x = x + int(w * 0.52)
                            btn_y = y + int(h * 0.86)
                            home_confirmed = False
                            for click_attempt in range(1, 4):
                                self.write_terminal(
                                    f"[Battle End]: Clicking Return Home button "
                                    f"(attempt {click_attempt}/3): "
                                    f"[X: {btn_x}, Y: {btn_y}]"
                                )
                                pyautogui.click(btn_x, btn_y)
                                for _ in range(6):
                                    if not self.bot_sleep(0.75):
                                        break
                                    post_battle_frame = self.capture_screen(win_rect)
                                    if self.dismiss_okay_popup(post_battle_frame, win_rect):
                                        self.bot_sleep(0.8)
                                        continue
                                    if self.is_home_screen(post_battle_frame):
                                        home_confirmed = True
                                        break
                                if home_confirmed or not self.bot_running:
                                    break

                            if home_confirmed:
                                self.write_terminal(
                                    "[Battle End]: Home screen confirmed after "
                                    "Return Home."
                                )
                                combat_engagement = False
                            else:
                                self.write_terminal(
                                    "[Battle End]: Home screen not confirmed yet. "
                                    "Continuing result-screen monitoring."
                                )
                            continue

                        now = time.time()
                        if now - last_home_check >= 60:
                            last_home_check = now
                            if self.is_home_screen(combat_capture):
                                self.write_terminal("[State Recovery]: Home screen detected while monitoring battle. Resuming village cycle...")
                                combat_engagement = False
                else:
                    self.match_loot_miss_count = 0
                    self.write_terminal("Ã¢ÂÅ’ Asset yield below thresholds. Activating 'Next Match' skip sequences...")
                    next_x = x + int(w * 0.93)
                    next_y = y + int(h * 0.75)
                    self.write_terminal(f"[Matchmaking Skip]: Clicking Next button: [X: {next_x}, Y: {next_y}]")
                    pyautogui.click(next_x, next_y)
                    time.sleep(1.0)
                    
            # --- EVALUATION MODULE B: VILLAGE HUB MONITOR OVERVIEW ---
            elif match_ui_visible:
                self.match_loot_miss_count += 1
                if self.match_loot_miss_count >= 2:
                    next_x = x + int(w * 0.93)
                    next_y = y + int(h * 0.75)
                    self.write_terminal(f"[Matchmaking Recovery]: Loot OCR missed {self.match_loot_miss_count} times. Clicking Next: [X: {next_x}, Y: {next_y}]")
                    pyautogui.click(next_x, next_y)
                    self.match_loot_miss_count = 0
                    time.sleep(1.0)
                else:
                    self.write_terminal("[State Check]: Match screen detected but loot OCR missed. Rechecking shortly...")
                    time.sleep(0.5)

            else:
                self.match_loot_miss_count = 0
                if home_visible:
                    self.write_terminal("[State Check]: Home screen detected from Shop button.")
                self.write_terminal(f"[Village Hub Status] -> Gold Balance: {your_g:,} | Elixir Balance: {your_e:,}")
                self.update_gui_metrics(your_g, your_e)
                
                if self.enable_walls_var.get() and your_g >= upgrade_trigger and your_e >= upgrade_trigger:
                    self.write_terminal("[Wallet Overflow Alert]: Capital reserves hit milestone trigger! Entering drainage loop...")
                    wall_scan_misses = 0
                    while self.bot_running:
                        expected_wallet = getattr(self, "_wall_expected_wallet", None)
                        if isinstance(expected_wallet, tuple) and len(expected_wallet) == 2:
                            g_now, e_now = expected_wallet
                            self._wall_expected_wallet = None
                            self.write_terminal(
                                f"[Upgrades Routine]: Using calculated post-upgrade wallet - "
                                f"Gold: {g_now:,} | Elixir: {e_now:,}"
                            )
                        else:
                            refreshed_frame = self.capture_screen(win_rect)
                            _, _, g_now, e_now = self.get_resources_by_corner(refreshed_frame)
                        self.update_gui_metrics(g_now, e_now)
                        has_upgraded = self.find_and_upgrade_walls(win_rect, g_now, e_now)
                        if not self.bot_running:
                            break
                        if has_upgraded:
                            wall_scan_misses = 0
                            time.sleep(0.5)
                            continue
                        if (g_now > 0 or e_now > 0) and not getattr(self, "last_wall_scan_blocked_by_cost", False) and wall_scan_misses < 3:
                            wall_scan_misses += 1
                            self.write_terminal("[Wallet Drain Retry]: Wall scan missed during drain. Rechecking before attack...")
                            time.sleep(0.8)
                            continue
                        break
                    self.write_terminal("[Wallet Drain Complete]: No affordable wall found. Returning to farming cycle...")

                if self.bot_running and home_visible:
                    self.write_terminal("[Farming Strategy Trigger]: Balance below trigger milestone or upgrades disabled. Launching attack...")
                    village_attack_x = x + int(w * 0.09)
                    village_attack_y = y + int(h * 0.90)
                    self.write_terminal(f"[Farming Launch]: Clicking village Attack button: [X: {village_attack_x}, Y: {village_attack_y}]")
                    pyautogui.click(village_attack_x, village_attack_y)
                    if not self.screen_has_text(win_rect, ["find a match", "multiplayer"], timeout=2.5):
                        self.write_terminal("[Farming Launch]: Multiplayer screen not detected. Retrying village Attack button...")
                        pyautogui.click(village_attack_x, village_attack_y)
                        if not self.screen_has_text(win_rect, ["find a match", "multiplayer"], timeout=2.5):
                            self.write_terminal("[Farming Launch]: Battle menu not recognized. Rechecking screen before further clicks.")
                            continue

                    find_match_x = x + int(w * 0.23)
                    find_match_y = y + int(h * 0.72)
                    self.write_terminal(f"[Farming Launch]: Clicking Find a Match button: [X: {find_match_x}, Y: {find_match_y}]")
                    pyautogui.click(find_match_x, find_match_y)
                    if not self.screen_has_text(win_rect, ["my army", "attack"], timeout=3):
                        self.write_terminal("[Farming Launch]: Army screen not detected. Retrying Find a Match button...")
                        pyautogui.click(find_match_x, find_match_y)
                        if not self.screen_has_text(win_rect, ["my army", "attack"], timeout=2.5):
                            self.write_terminal("[Farming Launch]: Army screen not recognized. Rechecking screen before further clicks.")
                            continue

                    final_attack_x = x + int(w * 0.85)
                    final_attack_y = y + int(h * 0.86)
                    self.write_terminal(f"[Farming Launch]: Clicking army Attack button: [X: {final_attack_x}, Y: {final_attack_y}]")
                    pyautogui.click(final_attack_x, final_attack_y)
                    time.sleep(1.0)
                elif self.bot_running:
                    self.write_terminal("[State Recovery]: Unknown screen detected. Sending real Esc key to close overlay/menu.")
                    self.press_real_escape()
                    time.sleep(1.0)


class FancyCoCBotGUI(FancyCoCBotGUI):
    """Keep the premium EXE Start/Stop presentation around the supplied bot."""

    @staticmethod
    def _is_plausible_wall_wallet_reading(
        before_gold,
        before_elixir,
        after_gold,
        after_elixir,
        upgrade_resource,
        upgrade_cost,
    ):
        if after_gold <= 0 or after_elixir <= 0 or upgrade_cost <= 0:
            return False

        if upgrade_resource == "Gold":
            chosen_before, chosen_after = before_gold, after_gold
            other_before, other_after = before_elixir, after_elixir
        elif upgrade_resource == "Elixir":
            chosen_before, chosen_after = before_elixir, after_elixir
            other_before, other_after = before_gold, after_gold
        else:
            return False

        chosen_spend = chosen_before - chosen_after
        cost_tolerance = max(50_000, int(upgrade_cost * 0.08))
        other_tolerance = max(50_000, int(max(other_before, 1) * 0.02))

        return (
            abs(chosen_spend - upgrade_cost) <= cost_tolerance
            and abs(other_after - other_before) <= other_tolerance
        )

    def toggle_bot_engine(self):
        if not self.bot_running:
            if self.license_check:
                ok, reason = self.license_check()
                if not ok:
                    self.force_license_logout(reason)
                    return
            self.clean_input_string(self.txt_min_gold.get(), 1000000)
            self.clean_input_string(self.txt_min_elixir.get(), 1000000)
            if self.enable_walls_var.get():
                self.clean_input_string(self.txt_upgrade_limit.get(), 20000000)

            self.bot_running = True
            self.btn_toggle.config(
                text="Stop Farming Run",
                bg="#FF6B6B",
                fg="#08111F",
                activebackground="#FF8585",
                activeforeground="#08111F",
            )
            self.write_terminal("Fired execution loop hooks. Parsing environment parameters...")
            self.bot_thread = threading.Thread(target=self.core_bot_loop, daemon=True)
            self.bot_thread.start()
        else:
            self.bot_running = False
            self.btn_toggle.config(
                text="Start Farming Run",
                bg="#FFD166",
                fg="#08111F",
                activebackground="#FFE08A",
                activeforeground="#08111F",
            )
            self.write_terminal("Gracefully stopping background automation execution blocks...")
            pyautogui.press("esc")
'''

class FancyCoCBotGUI(FancyCoCBotGUI):
    @staticmethod
    def _is_plausible_wall_wallet_reading(
        before_gold,
        before_elixir,
        after_gold,
        after_elixir,
        upgrade_resource,
        upgrade_cost,
    ):
        if after_gold <= 0 or after_elixir <= 0 or upgrade_cost <= 0:
            return False

        if upgrade_resource == "Gold":
            chosen_before, chosen_after = before_gold, after_gold
            other_before, other_after = before_elixir, after_elixir
        elif upgrade_resource == "Elixir":
            chosen_before, chosen_after = before_elixir, after_elixir
            other_before, other_after = before_gold, after_gold
        else:
            return False

        chosen_spend = chosen_before - chosen_after
        cost_tolerance = max(50_000, int(upgrade_cost * 0.08))
        other_tolerance = max(50_000, int(max(other_before, 1) * 0.02))

        return (
            abs(chosen_spend - upgrade_cost) <= cost_tolerance
            and abs(other_after - other_before) <= other_tolerance
        )

    def _run_bot_worker(self):
        try:
            self.core_bot_loop()
        except Exception as error:
            self.write_terminal(f"[Run Control]: Run stopped after error: {error}")
        finally:
            self.bot_running = False
            self.write_terminal("[Run Control]: Worker finished. Ready for a new run.")
            # Do not join a worker on Tk's thread: OCR can take several seconds.
            self.root.after(0, self._refresh_stopped_worker_button)

    def _refresh_stopped_worker_button(self):
        if not self.bot_running:
            self.btn_toggle.config(text="Start Farming Run", bg="#FFD166", fg="#08111F",
                                   activebackground="#FFE08A", activeforeground="#08111F")

    def toggle_bot_engine(self):
        if not self.bot_running:
            previous_worker = getattr(self, "bot_thread", None)
            if previous_worker is not None and previous_worker.is_alive():
                self.write_terminal("[Run Control]: Previous run is still stopping. Start again once it has finished.")
                return
            if self.license_check:
                ok, reason = self.license_check()
                if not ok:
                    self.force_license_logout(reason)
                    return
            self.clean_input_string(self.txt_min_gold.get(), 1000000)
            self.clean_input_string(self.txt_min_elixir.get(), 1000000)
            if self.enable_walls_var.get():
                self.clean_input_string(self.txt_upgrade_limit.get(), 20000000)

            self.bot_running = True
            self.btn_toggle.config(
                text="Stop Farming Run",
                bg="#FF6B6B",
                fg="#08111F",
                activebackground="#FF8585",
                activeforeground="#08111F",
            )
            self.write_terminal("Fired execution loop hooks. Parsing environment parameters...")
            self.bot_thread = threading.Thread(target=self._run_bot_worker, daemon=True)
            self.bot_thread.start()
        else:
            self.bot_running = False
            self.btn_toggle.config(
                text="Start Farming Run",
                bg="#FFD166",
                fg="#08111F",
                activebackground="#FFE08A",
                activeforeground="#08111F",
            )
            self.write_terminal("Gracefully stopping background automation execution blocks...")


    def get_coc_window(self):
        try:
            candidates = []
            for win in gw.getAllWindows():
                title = win.title.lower()
                if not title:
                    continue

                is_bot_window = "premium automation terminal" in title
                is_game_window = "google play games" in title or title.startswith("clash of clans -")
                is_large_enough = win.width >= 800 and win.height >= 500

                if is_game_window and not is_bot_window and is_large_enough:
                    priority = 0 if title.startswith("clash of clans -") else 1
                    candidates.append((priority, win))

            if candidates:
                candidates.sort(key=lambda item: item[0])
                win = candidates[0][1]
                if win.isMinimized:
                    win.restore()
                win.activate()
                time.sleep(1)
                self.write_terminal(f"[Window Bind]: Using game window '{win.title}' ({win.width}x{win.height}).")
                return win.left, win.top, win.width, win.height
        except Exception as e:
            self.write_terminal(f"Error capturing window handle: {e}")
        return None

    def press_real_escape(self):
        try:
            target_win = None
            for win in gw.getAllWindows():
                title = win.title.lower()
                if title.startswith("clash of clans -") or "google play games" in title:
                    target_win = win
                    if win.isMinimized:
                        win.restore()
                    win.activate()
                    break
            if target_win is not None and hasattr(target_win, "_hWnd"):
                ctypes.windll.user32.SetForegroundWindow(target_win._hWnd)
                ctypes.windll.user32.BringWindowToTop(target_win._hWnd)
            time.sleep(0.15)

            if target_win is not None and hasattr(target_win, "_hWnd") and win32gui and win32con:
                vk_escape = 0x1B
                scan_escape = 0x01
                lparam_down = 1 | (scan_escape << 16)
                lparam_up = 1 | (scan_escape << 16) | (0xC0 << 24)
                win32gui.PostMessage(target_win._hWnd, win32con.WM_KEYDOWN, vk_escape, lparam_down)
                time.sleep(0.05)
                win32gui.PostMessage(target_win._hWnd, win32con.WM_KEYUP, vk_escape, lparam_up)
                return

            class KEYBDINPUT(ctypes.Structure):
                _fields_ = [
                    ("wVk", ctypes.c_ushort),
                    ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
                ]

            class INPUTUNION(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT)]

            class INPUT(ctypes.Structure):
                _fields_ = [
                    ("type", ctypes.c_ulong),
                    ("union", INPUTUNION),
                ]

            input_keyboard = 1
            keyeventf_keyup = 0x0002
            keyeventf_scancode = 0x0008
            esc_scan_code = 0x01
            extra = ctypes.c_ulong(0)

            press = INPUT(
                type=input_keyboard,
                union=INPUTUNION(
                    ki=KEYBDINPUT(0, esc_scan_code, keyeventf_scancode, 0, ctypes.pointer(extra))
                ),
            )
            release = INPUT(
                type=input_keyboard,
                union=INPUTUNION(
                    ki=KEYBDINPUT(0, esc_scan_code, keyeventf_scancode | keyeventf_keyup, 0, ctypes.pointer(extra))
                ),
            )

            ctypes.windll.user32.SendInput(1, ctypes.byref(press), ctypes.sizeof(INPUT))
            time.sleep(0.08)
            ctypes.windll.user32.SendInput(1, ctypes.byref(release), ctypes.sizeof(INPUT))

        except Exception as e:
            self.write_terminal(f"[State Recovery]: Windows Esc failed, using fallback Esc: {e}")
            pyautogui.press("esc")

    def capture_screen(self, win_rect):
        x, y, w, h = win_rect
        def grab_frame():
            screenshot = pyautogui.screenshot(region=(x, y, w, h))
            return cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

        frame = grab_frame()
        retry_button = locate_connection_lost_retry(frame)
        if retry_button is None:
            return frame

        with CONNECTION_POPUP_RECOVERY_LOCK:
            frame = grab_frame()
            retry_button = locate_connection_lost_retry(frame)
            for attempt in range(1, 4):
                if retry_button is None:
                    break
                btn_x = x + retry_button[0]
                btn_y = y + retry_button[1]
                self.write_terminal(
                    f"[Connection Recovery]: Connection lost popup detected. "
                    f"Clicking Try Again (attempt {attempt}/3): "
                    f"[X: {btn_x}, Y: {btn_y}]"
                )
                pyautogui.click(btn_x, btn_y)
                time.sleep(1.5)
                frame = grab_frame()
                retry_button = locate_connection_lost_retry(frame)

            if retry_button is None:
                self.write_terminal(
                    "[Connection Recovery]: Connection popup cleared. "
                    "Resuming automation."
                )
            else:
                self.write_terminal(
                    "[Connection Recovery]: Popup is still visible; "
                    "it will be retried on the next screen check."
                )
        return frame

    def run_ocr_on_region(self, img, crop_box):
        h_orig, w_orig = img.shape[:2]
        ymin, ymax, xmin, xmax = crop_box
        cropped = img[int(h_orig*ymin):int(h_orig*ymax), int(w_orig*xmin):int(w_orig*xmax)]
        if cropped.size == 0:
            return None
        gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        ocr_ready = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        results, _ = ocr(ocr_ready)
        return results

    def get_resources_by_corner(self, img):
        enemy_gold, enemy_elixir = self.get_enemy_loot(img)

        def read_resource_value(crop_boxes, max_value=50000000, robust=False):
            candidates = []

            for crop_box in crop_boxes:
                results_by_scale = []
                h_orig, w_orig = img.shape[:2]
                ymin, ymax, xmin, xmax = crop_box
                cropped = img[int(h_orig*ymin):int(h_orig*ymax), int(w_orig*xmin):int(w_orig*xmax)]
                if cropped.size == 0:
                    continue

                scales = (3, 4) if robust else (3,)
                use_inverted = (False, True) if robust else (False,)
                for scale in scales:
                    gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
                    for invert in use_inverted:
                        candidate_gray = 255 - gray if invert else gray
                        candidate_gray = cv2.resize(candidate_gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
                        ocr_ready = cv2.cvtColor(candidate_gray, cv2.COLOR_GRAY2BGR)
                        results, _ = ocr(ocr_ready)
                        if results:
                            results_by_scale.append(results)

                for results in results_by_scale:
                    rows = []
                    sorted_items = sorted(results, key=lambda item: (item[0][0][1] + item[0][2][1]) / 2)
                    for item in sorted_items:
                        bbox, text_string = item[0], item[1]
                        digits = re.sub(r'\D', '', text_string)
                        if not digits:
                            continue

                        x_mid = (bbox[0][0] + bbox[2][0]) / 2
                        y_mid = (bbox[0][1] + bbox[2][1]) / 2
                        placed = False
                        for row in rows:
                            if abs(row["y"] - y_mid) <= 45:
                                row["parts"].append((x_mid, digits))
                                row["y"] = (row["y"] + y_mid) / 2
                                placed = True
                                break
                        if not placed:
                            rows.append({"y": y_mid, "parts": [(x_mid, digits)]})

                    for row in rows:
                        ordered_parts = sorted(row["parts"], key=lambda part: part[0])
                        cleaned_parts = []
                        for part_index, (part_x, part_digits) in enumerate(ordered_parts):
                            previous_len = len(ordered_parts[part_index - 1][1]) if part_index > 0 else 0
                            next_len = len(ordered_parts[part_index + 1][1]) if part_index < len(ordered_parts) - 1 else 0
                            if len(part_digits) == 1 and previous_len >= 2 and next_len >= 2:
                                continue
                            cleaned_parts.append(part_digits)
                        combined_digits = "".join(cleaned_parts)
                        if not combined_digits:
                            continue

                        value = int(combined_digits)
                        digit_count = len(combined_digits)
                        if 0 < value <= max_value:
                            candidates.append((value, digit_count))

            if not candidates:
                return 0

            digit_counts = {}
            for _, digit_count in candidates:
                digit_counts[digit_count] = digit_counts.get(digit_count, 0) + 1
            # Wallet OCR often returns the trailing digits several times and the
            # complete value only once. Prefer the longest valid counter reading;
            # the max_value guard above rejects impossible concatenations.
            best_digit_count = max(digit_counts)
            values = sorted(value for value, digit_count in candidates if digit_count == best_digit_count)
            if not values:
                values = sorted(value for value, _ in candidates)
            return values[len(values) // 2]

        your_gold = read_resource_value([
            (0.045, 0.110, 0.800, 0.970),
        ], robust=True)
        your_elixir = read_resource_value([
            (0.125, 0.195, 0.82, 0.97),
            (0.115, 0.205, 0.80, 0.98),
        ], robust=False)

        def reject_trailing_fragment(resource_name, value):
            previous = int(getattr(self, f"_last_stable_{resource_name}", 0) or 0)
            if previous > 0 and value > 0:
                previous_digits = str(previous)
                value_digits = str(value)
                if (
                    len(previous_digits) >= len(value_digits) + 2
                    and previous_digits.endswith(value_digits)
                ):
                    self.write_terminal(
                        f"[Wallet OCR]: Ignoring truncated {resource_name.title()} "
                        f"reading {value:,}; keeping {previous:,}."
                    )
                    return previous
            return value

        your_gold = reject_trailing_fragment("gold", your_gold)
        your_elixir = reject_trailing_fragment("elixir", your_elixir)
        return enemy_gold, enemy_elixir, your_gold, your_elixir

    def get_enemy_loot(self, img):
        left_results = self.run_ocr_on_region(img, (0.10, 0.30, 0.03, 0.20))
        enemy_gold, enemy_elixir = 0, 0
        if left_results:
            sorted_left = sorted(left_results, key=lambda x: (x[0][0][1] + x[0][2][1]) / 2)
            enemy_candidates = []
            for item in sorted_left:
                bbox_coords = item[0]
                text_string = item[1]
                digits = re.sub(r'\D', '', text_string) # FIXED: Cleans text using the extracted string
                if digits:
                    value = int(digits)
                    # Available loot can be small, but this rejects timer/troop noise like 24s or 20/20.
                    if 100 <= value <= 50000000:
                        y_mid = (bbox_coords[0][1] + bbox_coords[2][1]) / 2
                        enemy_candidates.append((y_mid, value, text_string))
            if len(enemy_candidates) >= 2:
                enemy_candidates.sort(key=lambda item: item[0])
                enemy_gold = enemy_candidates[0][1]
                enemy_elixir = enemy_candidates[1][1]
        return enemy_gold, enemy_elixir

    def check_match_end(self, img):
        return True if match_end_screen_visible(img) else None

    def is_home_screen(self, img):
        return village_home_layout(img, self.run_ocr_on_region) is not None

    def dismiss_okay_popup(self, img, win_rect):
        scan_results, _ = ocr(img)
        if not scan_results:
            return False

        detected_text = " ".join(item[1].lower() for item in scan_results)
        if "star bonus received" not in detected_text and "okay" not in detected_text:
            return False

        x, y, w, h = win_rect
        if "star bonus received" in detected_text:
            btn_x = x + int(w * 0.52)
            btn_y = y + int(h * 0.84)
            self.write_terminal(f"[Popup Recovery]: Star bonus popup detected. Clicking Okay: [X: {btn_x}, Y: {btn_y}]")
            pyautogui.click(btn_x, btn_y)
            return True

        for item in scan_results:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            bbox, text = item[0], str(item[1])
            if "okay" in text.lower():
                btn_x = x + int((bbox[0][0] + bbox[2][0]) / 2)
                btn_y = y + int((bbox[0][1] + bbox[2][1]) / 2)
                self.write_terminal(f"[Popup Recovery]: Clicking Okay button: [X: {btn_x}, Y: {btn_y}]")
                pyautogui.click(btn_x, btn_y)
                return True

        return False

    def dismiss_reload_popup(self, img, win_rect):
        scan_results, _ = ocr(img)
        if not scan_results:
            return False

        detected_text = " ".join(item[1].lower() for item in scan_results)
        if "disconnected due to inactivity" not in detected_text and "reload game" not in detected_text:
            return False

        x, y, w, h = win_rect
        for item in scan_results:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            bbox, text = item[0], str(item[1])
            if "reload game" in text.lower():
                btn_x = x + int((bbox[0][0] + bbox[2][0]) / 2)
                btn_y = y + int((bbox[0][1] + bbox[2][1]) / 2)
                self.write_terminal(f"[Popup Recovery]: Disconnected/reload popup detected. Clicking Reload Game: [X: {btn_x}, Y: {btn_y}]")
                pyautogui.click(btn_x, btn_y)
                break
        else:
            self.write_terminal("[Popup Recovery]: Disconnected/reload popup detected but Reload Game button text not found.")
            return True

        self.write_terminal("[Popup Recovery]: Waiting for home screen to return after reload...")
        wait_deadline = time.time() + 60
        while self.bot_running and time.time() < wait_deadline:
            if not self.bot_sleep(2.0):
                return True
            current_win_rect = self.get_coc_window()
            if not current_win_rect:
                continue
            current_frame = self.capture_screen(current_win_rect)
            if self.is_home_screen(current_frame):
                self.write_terminal("[Popup Recovery]: Home screen confirmed after reload.")
                break
        else:
            self.write_terminal("[Popup Recovery]: Timed out waiting for home screen after reload.")

        return True

    def is_attack_screen(self, enemy_gold, enemy_elixir):
        return enemy_gold > 0 and enemy_elixir > 0

    def is_match_ui_screen(self, img):
        scan_results, _ = ocr(img)
        if not scan_results:
            return False

        detected_text = " ".join(item[1].lower() for item in scan_results)
        match_markers = [
            "available loot",
            "battle starts",
            "battle ends",
            "end battle",
            "surrender",
            "next",
            "overall damage",
            "deploy",
        ]
        return any(marker in detected_text for marker in match_markers)

    def screen_has_text(self, win_rect, keywords, timeout=8):
        deadline = time.time() + timeout
        keywords = [keyword.lower() for keyword in keywords]
        while time.time() < deadline and self.bot_running:
            frame = self.capture_screen(win_rect)
            scan_results, _ = ocr(frame)
            if scan_results:
                detected_text = " ".join(item[1].lower() for item in scan_results)
                if any(keyword in detected_text for keyword in keywords):
                    return True
            time.sleep(0.2)
        return False

    def find_and_upgrade_walls(self, win_rect, current_gold, current_elixir):
        self.last_wall_scan_blocked_by_cost = False
        x, y, w, h = win_rect

        def parse_wall_options(menu_results, frame=None, menu_crop=None):
            wall_options = []
            if not menu_results:
                return wall_options

            def detect_cost_resource(cost_bbox):
                if frame is None or menu_crop is None:
                    return "Unknown"
                return detect_builder_cost_resource(
                    frame,
                    cost_bbox,
                    menu_results,
                    menu_crop,
                )

            rows = []
            for item in menu_results:
                bbox, text = item[0], item[1]
                x_mid = (bbox[0][0] + bbox[2][0]) / 2
                y_mid = (bbox[0][1] + bbox[2][1]) / 2
                placed = False

                for row in rows:
                    if abs(row["y"] - y_mid) <= 45:
                        row["items"].append({"bbox": bbox, "text": text, "x": x_mid, "y": y_mid})
                        row["y"] = (row["y"] + y_mid) / 2
                        placed = True
                        break

                if not placed:
                    rows.append({"y": y_mid, "items": [{"bbox": bbox, "text": text, "x": x_mid, "y": y_mid}]})

            all_items = [
                {"bbox": item[0], "text": item[1], "x": (item[0][0][0] + item[0][2][0]) / 2, "y": (item[0][0][1] + item[0][2][1]) / 2}
                for item in menu_results
            ]

            def is_wall_label(text):
                raw_text = text.lower()
                normalized = re.sub(r'[^a-z0-9]', '', text.lower())
                normalized = (
                    normalized
                    .replace('wa11', 'wall')
                    .replace('wal1', 'wall')
                    .replace('waii', 'wall')
                    .replace('wa1l', 'wall')
                )
                if "level" in normalized:
                    return False
                return bool(re.fullmatch(r"wall(?:x[0-9oilsb]+)?", normalized))

            def is_split_selected_wall_title(wall_item):
                normalized = re.sub(r'[^a-z0-9]', '', wall_item["text"].lower())
                normalized = (
                    normalized
                    .replace('wa11', 'wall')
                    .replace('wal1', 'wall')
                    .replace('waii', 'wall')
                    .replace('wa1l', 'wall')
                )
                if normalized != "wall":
                    return False
                for part in all_items:
                    if part is wall_item:
                        continue
                    nearby_text = re.sub(r'[^a-z0-9]', '', part["text"].lower())
                    if (
                        "level" in nearby_text
                        and abs(part["y"] - wall_item["y"]) <= 55
                        and abs(part["x"] - wall_item["x"]) <= 320
                    ):
                        return True
                return False

            def extract_embedded_wall_cost(text):
                # Handles the case where RapidOCR merges the wall label, wall
                # count, and upgrade cost into a single OCR result, e.g.
                # "Wall x99 18,000". Strips the "Wall" label and the "x<count>"
                # portion so the count (e.g. x99) can never be mistaken for the
                # cost, normalizes common OCR digit mistakes, then extracts the
                # remaining numeric value(s), requiring at least 4 digits.
                working = text
                working = re.sub(r'(?i)wa[l1i]{1,2}', '', working)
                working = re.sub(r'(?i)x\s*[\dOo]+', '', working)
                normalized = working.replace('O', '0').replace('o', '0')
                raw_numbers = re.findall(r'\d[\d,\.]*', normalized)
                candidates = [re.sub(r'\D', '', raw) for raw in raw_numbers]
                candidates = [digits for digits in candidates if digits]
                if not candidates:
                    return None
                long_candidates = [digits for digits in candidates if len(digits) >= 4]
                if not long_candidates:
                    return None
                return int(max(long_candidates, key=lambda digits: int(digits)))

            max_item_x = max((part["x"] for part in all_items), default=0)

            wall_items = [
                part for part in all_items
                if is_wall_label(part["text"])
                and not is_split_selected_wall_title(part)
            ]

            for wall_item in sorted(wall_items, key=lambda part: part["y"]):
                wall_top = min(point[1] for point in wall_item["bbox"])
                wall_bottom = max(point[1] for point in wall_item["bbox"])
                wall_height = max(1, wall_bottom - wall_top)
                nearby_costs = []
                for part in all_items:
                    if part is wall_item or is_wall_label(part["text"]):
                        continue
                    if part["x"] <= wall_item["x"] + 40:
                        continue
                    if max_item_x and part["x"] < max_item_x * 0.62:
                        continue
                    y_gap = abs(part["y"] - wall_item["y"])
                    sidebar_row = menu_crop == BUILDER_SIDEBAR_CROP
                    signed_gap = part["y"] - wall_item["y"]
                    if sidebar_row:
                        # Price is below the label, but above the next card.
                        if not -8 <= signed_gap <= frame.shape[0] * 0.065:
                            continue
                    elif y_gap > 42:
                        continue

                    part_top = min(point[1] for point in part["bbox"])
                    part_bottom = max(point[1] for point in part["bbox"])
                    part_height = max(1, part_bottom - part_top)
                    vertical_overlap = max(
                        0,
                        min(wall_bottom, part_bottom) - max(wall_top, part_top)
                    )
                    minimum_overlap = max(3, min(wall_height, part_height) * 0.35)
                    if not sidebar_row and vertical_overlap < minimum_overlap:
                        continue

                    cleaned_part = re.sub(r'^0\d\s*:', '', part["text"])
                    cleaned_part = re.sub(r'^[^\dOoSs]+', '', cleaned_part)
                    cleaned_part = re.sub(r'(?i)\bx\s*\d+\b', '', cleaned_part)
                    cleaned_part = re.sub(r'(?i)(?<![a-z])s(?=\s*\d)', '5', cleaned_part)
                    cleaned_part = re.sub(r'(?i)^\s*s\s*$', '5', cleaned_part)
                    cleaned_part = cleaned_part.replace('O', '0').replace('o', '0')
                    if not re.fullmatch(r'\s*\d[\d,:.\s]*\s*', cleaned_part):
                        continue
                    digits = re.sub(r'\D', '', cleaned_part)
                    if digits:
                        nearby_costs.append((y_gap, part["x"], digits, part["bbox"]))

                if not nearby_costs:
                    embedded_cost = extract_embedded_wall_cost(wall_item["text"])
                    if embedded_cost is not None and embedded_cost >= 50:
                        cost_bbox = wall_item["bbox"]
                        resource_type = detect_cost_resource(cost_bbox)
                        row_bbox = [
                            [wall_item["bbox"][0][0], wall_item["bbox"][0][1]],
                            [wall_item["bbox"][2][0], wall_item["bbox"][2][1]]
                        ]
                        wall_options.append({
                            "cost": embedded_cost,
                            "wall_bbox": wall_item["bbox"],
                            "cost_bbox": cost_bbox,
                            "row_bbox": row_bbox,
                            "has_wall_count": bool(
                                re.search(
                                    r'(?i)\bwa[l1i]{1,2}\s*x\s*\d+',
                                    wall_item["text"]
                                )
                            ),
                            "resource": resource_type,
                            "text": f"{wall_item['text']} -> {embedded_cost:,}"
                        })
                    continue

                nearby_costs.sort(key=lambda item: (item[0], item[1]))
                cost_digits = nearby_costs[0][2]
                if not cost_digits:
                    continue
                cost = int(cost_digits)
                if cost >= 50 and len(cost_digits) >= 2:
                    cost_bbox = nearby_costs[0][3]
                    resource_type = detect_cost_resource(cost_bbox)
                    row_bbox = [
                        [
                            min(wall_item["bbox"][0][0], cost_bbox[0][0]),
                            min(wall_item["bbox"][0][1], cost_bbox[0][1])
                        ],
                        [
                            max(wall_item["bbox"][2][0], cost_bbox[2][0]),
                            max(wall_item["bbox"][2][1], cost_bbox[2][1])
                        ]
                    ]
                    wall_options.append({
                        "cost": cost,
                        "wall_bbox": wall_item["bbox"],
                        "cost_bbox": cost_bbox,
                        "row_bbox": row_bbox,
                        "has_wall_count": bool(
                            re.search(
                                r'(?i)\bwa[l1i]{1,2}\s*x\s*\d+',
                                wall_item["text"]
                            )
                        ),
                        "resource": resource_type,
                        "text": f"{wall_item['text']} -> {cost:,}"
                    })

            return wall_options

        def click_wall_option(target, menu_results, menu_crop):
            if not menu_results or not target.get("wall_bbox"):
                self.write_terminal(
                    "[Upgrades Routine]: Confirmed Wall row has no usable OCR "
                    "bounding box. Recomputing scan."
                )
                return False

            crop_ymin, crop_ymax, crop_xmin, crop_xmax = menu_crop
            crop_w = w * (crop_xmax - crop_xmin)
            crop_h = h * (crop_ymax - crop_ymin)
            max_ocr_x = max(point[0] for item in menu_results for point in item[0])
            max_ocr_y = max(point[1] for item in menu_results for point in item[0])
            ocr_scale_x = ocr_scale_y = 2.0

            def bbox_screen_center(bbox):
                xs = [point[0] for point in bbox]
                ys = [point[1] for point in bbox]
                local_x = ((min(xs) + max(xs)) / 2) / ocr_scale_x
                local_y = ((min(ys) + max(ys)) / 2) / ocr_scale_y
                return int(x + (w * crop_xmin) + local_x), int(y + (h * crop_ymin) + local_y)

            # The caller just performed a settled OCR refresh and passed the
            # confirmed target plus the exact result set that produced its
            # bounding box. Re-OCRing here introduced a race where one transient
            # miss cancelled a valid click milliseconds after confirmation.
            # The matched price is inside the intended card even if the label
            # bbox has OCR noise. Clicking its centre avoids adjacent wall rows.
            row_x, row_y = bbox_screen_center(target.get("cost_bbox") or target["wall_bbox"])
            self.write_terminal(
                "[Upgrades Routine]: Using freshly confirmed Wall row for click; "
                "skipping redundant pre-click OCR."
            )
            self.write_terminal(f"Selecting verified Wall price from OCR bbox: [X: {row_x}, Y: {row_y}] OCR scale: {ocr_scale_x}x{ocr_scale_y}")
            if not self.bot_running:
                return False
            pyautogui.click(row_x, row_y)
            if not self.bot_sleep(1.0 if sidebar else 0.8):
                return False

            if sidebar and not close_builder_menu():
                return False

            selected_frame = None
            selected_results = None
            selected_title = ""

            def is_wall_dialog_title(text):
                normalized = str(text).lower()
                normalized = (
                    normalized
                    .replace('wa11', 'wall')
                    .replace('wal1', 'wall')
                    .replace('waii', 'wall')
                    .replace('wa1l', 'wall')
                    .replace('levei', 'level')
                    .replace('leve1', 'level')
                    .replace('leve|', 'level')
                )
                normalized = re.sub(r'[^a-z0-9]+', ' ', normalized)
                # RapidOCR can join one stray glyph to the level label or merge
                # a nearby village label into the same row, for example
                # "Resear Wall (levelH18)". This OCR is already restricted to
                # the selected-title strip, so require Wall + a numeric level
                # within that row while tolerating one OCR-noise letter.
                return bool(
                    re.search(
                        r'\bwall\b.{0,18}\blevel[a-z]?\s*\d{1,2}\b',
                        normalized,
                    )
                )

            def selected_title_rows(title_results):
                rows = []
                for item in title_results or []:
                    if not isinstance(item, (list, tuple)) or len(item) < 2:
                        continue
                    bbox, text = item[0], normalize_wall_level_text(item[1]).strip()
                    if not text:
                        continue
                    x_mid = (bbox[0][0] + bbox[2][0]) / 2
                    y_mid = (bbox[0][1] + bbox[2][1]) / 2
                    row = next(
                        (candidate for candidate in rows if abs(candidate["y"] - y_mid) <= 38),
                        None,
                    )
                    if row is None:
                        row = {"y": y_mid, "parts": []}
                        rows.append(row)
                    row["parts"].append((x_mid, text))

                return [
                    " ".join(text for _, text in sorted(row["parts"], key=lambda part: part[0]))
                    for row in sorted(rows, key=lambda candidate: candidate["y"])
                ]

            for title_attempt in range(2):
                selected_frame = self.capture_screen(win_rect)
                selected_title_results = read_selected_wall_title(selected_frame, self.run_ocr_on_region)
                title_rows = selected_title_rows(selected_title_results)
                selected_title = next(
                    (row for row in title_rows if is_wall_dialog_title(row)),
                    "",
                )
                if is_wall_dialog_title(selected_title):
                    break
                if title_attempt == 0:
                    time.sleep(0.25)

            if not is_wall_dialog_title(selected_title):
                visible_rows = " | ".join(title_rows) if title_rows else "no title OCR"
                self.write_terminal(
                    f"[Upgrades Routine]: Selected item is not a Wall dialog "
                    f"({visible_rows}). Skipping false detection."
                )
                return False
            self.write_terminal(
                f"[Upgrades Routine]: Selected Wall dialog confirmed: {selected_title}"
            )

            wall_resource = target.get("resource", "Unknown")
            use_gold_for_wall = wall_resource in ("Gold", "Unknown") and current_gold >= target["cost"]
            use_elixir_for_wall = wall_resource in ("Elixir", "Unknown") and current_elixir >= target["cost"]

            def find_upgrade_button_from_icon(desired_resource):
                # The card row moves when the builder panel closes. Never use
                # a coordinate read before that transition for the input.
                return settled_wall_resource_button(
                    lambda: self.capture_screen(win_rect), self.run_ocr_on_region,
                    desired_resource, target["cost"], self.bot_sleep,
                    lambda: self.bot_running, self.write_terminal)

            if use_gold_for_wall:
                upgrade_resource = "Gold"
                button = find_upgrade_button_from_icon(upgrade_resource)
            elif use_elixir_for_wall:
                upgrade_resource = "Elixir"
                button = find_upgrade_button_from_icon(upgrade_resource)
            else:
                self.write_terminal(f"[Upgrades Routine]: Cannot afford {wall_resource} wall cost {target['cost']:,}.")
                return False

            if not button:
                self.write_terminal(
                    f"[Upgrades Routine]: {upgrade_resource} upgrade button was "
                    f"not confidently identified by OCR/color. Refusing to click "
                    f"another resource button."
                )
                return False

            upgrade_x, upgrade_y = x + button["x"], y + button["y"]
            self.write_terminal(f"Clicking Wall {upgrade_resource} Upgrade button: [X: {upgrade_x}, Y: {upgrade_y}]")
            if not self.bot_running:
                return False
            pyautogui.click(upgrade_x, upgrade_y)
            # This position becomes part of the confirmation price. Clear the
            # cursor before OCR so it cannot hide the leading price digits.
            pyautogui.moveTo(x + int(w * .10), y + int(h * .07), _pause=False)
            if not self.bot_sleep(1.2):
                return False

            previous_level = int(re.search(r"\d+", selected_title).group())
            confirmation_frame = self.capture_screen(win_rect)
            confirm_point = wall_confirmation_button(
                confirmation_frame, self.run_ocr_on_region, target["cost"], previous_level + 1
            )
            if confirm_point is None:
                self.write_terminal("[Upgrades Routine]: Wall confirmation title/price mismatch; upgrade not confirmed.")
                self.press_real_escape()
                return False
            confirm_x, confirm_y = x + confirm_point[0], y + confirm_point[1]
            self.write_terminal(f"Clicking Wall Confirm button: [X: {confirm_x}, Y: {confirm_y}]")
            if not self.bot_running:
                return False
            pyautogui.click(confirm_x, confirm_y)
            if not self.bot_sleep(1.2):
                return False

            upgraded = False
            for _ in range(3):
                after_frame = self.capture_screen(win_rect)
                after_titles = selected_title_rows(read_selected_wall_title(after_frame, self.run_ocr_on_region))
                upgraded = any(
                    is_wall_dialog_title(title)
                    and int(re.search(r"\d+", title).group()) == previous_level + 1
                    for title in after_titles
                )
                if upgraded:
                    break
                if not self.bot_sleep(0.5):
                    return False
            if not upgraded:
                self.write_terminal("[Upgrades Routine]: Wall level increase not verified; refreshing wallet before retry.")
                self._wall_expected_wallet = None
                return False

            if upgrade_resource == "Gold":
                after_gold = max(0, current_gold - target["cost"])
                after_elixir = current_elixir
            else:
                after_gold = current_gold
                after_elixir = max(0, current_elixir - target["cost"])

            # The top counters animate after a large spend and OCR frequently sees
            # only their final few digits. The selected resource, displayed cost,
            # and pre-click wallet are already known, so keep an exact local balance
            # for the next wall pass instead of running slow OCR on the animation.
            self._wall_expected_wallet = (after_gold, after_elixir)
            self.update_gui_metrics(after_gold, after_elixir)
            self.write_terminal(
                f"[Upgrades Routine]: Wall upgrade confirmed. "
                f"Gold: {current_gold:,}->{after_gold:,} | "
                f"Elixir: {current_elixir:,}->{after_elixir:,}"
            )
            return True

        expected_wallet = getattr(self, "_wall_expected_wallet", None)
        if (
            isinstance(expected_wallet, tuple)
            and len(expected_wallet) == 2
            and expected_wallet[0] >= 0
            and expected_wallet[1] >= 0
        ):
            current_gold, current_elixir = expected_wallet
            self._wall_expected_wallet = None
            self.write_terminal(
                f"[Upgrades Routine]: Using calculated post-upgrade wallet - "
                f"Gold: {current_gold:,} | Elixir: {current_elixir:,}"
            )

        frame = self.capture_screen(win_rect)
        if current_gold > 0 or current_elixir > 0:
            self.update_gui_metrics(current_gold, current_elixir)
            self.write_terminal(f"[Upgrades Routine]: Fresh wallet check - Gold: {current_gold:,} | Elixir: {current_elixir:,}")

        scan_results, _ = ocr(frame)
        if scan_results:
            detected_text = " ".join(item[1].lower() for item in scan_results)
            if "join a clan" in detected_text or "join now" in detected_text:
                self.write_terminal("[Upgrades Routine]: Closing clan panel before wall upgrade retry...")
                pyautogui.click(x + int(w * 0.38), y + int(h * 0.49))
                time.sleep(1.0)

        panel_frame = self.capture_screen(win_rect)
        sidebar = builder_sidebar_visible(panel_frame, self.run_ocr_on_region)
        builder_rx, builder_ry = village_builder_anchor(panel_frame, self.run_ocr_on_region)
        if sidebar:
            builder_rx, builder_ry = 0.93, 0.82
        sidebar = sidebar or builder_rx > 0.80
        builder_x, builder_y = x + int(w * builder_rx), y + int(h * builder_ry)
        menu_crop = BUILDER_SIDEBAR_CROP if sidebar else BUILDER_MENU_CROP

        def panel_is_open(probe_frame):
            nonlocal sidebar, menu_crop
            if builder_sidebar_visible(probe_frame, self.run_ocr_on_region):
                sidebar = True
                menu_crop = BUILDER_SIDEBAR_CROP
                return True
            if sidebar:
                return False
            return builder_menu_overlay_visible(probe_frame) or builder_menu_results_show_open(
                run_layout_independent_builder_ocr(probe_frame, menu_crop)
            )

        def close_builder_menu():
            if not self.bot_running:
                return False
            probe_frame = self.capture_screen(win_rect)
            if panel_is_open(probe_frame):
                if sidebar:
                    pyautogui.click(x + int(w * 0.986), y + int(h * 0.081))
                else:
                    pyautogui.click(builder_x, builder_y)
                if not self.bot_sleep(0.7):
                    return False
                if sidebar and builder_sidebar_visible(self.capture_screen(win_rect), self.run_ocr_on_region):
                    self.write_terminal("[Upgrades Routine]: Builder panel did not close; stopping this upgrade attempt.")
                    return False
                return True
            return True

        def ensure_builder_menu_open(context):
            if not self.bot_running:
                return False
            if panel_is_open(self.capture_screen(win_rect)):
                return True
            self.write_terminal(f"[Upgrades Routine]: Opening builder list for {context}...")
            pyautogui.click(builder_x, builder_y)
            if not self.bot_sleep(1.2):
                return False
            if panel_is_open(self.capture_screen(win_rect)):
                return True
            self.write_terminal("[Upgrades Routine]: Builder list not confirmed; skipping scan.")
            return False

        def settled_builder_frame():
            # OCR refresh before a row click handles settling; animated cards
            # must not block scanning in a pixel-stability loop.
            if not self.bot_running:
                return None
            return self.capture_screen(win_rect)

        def move_to_builder_list():
            if not self.bot_running:
                return False
            ymin, ymax, xmin, xmax = menu_crop
            pyautogui.moveTo(x + int(w * (xmin + xmax) / 2), y + int(h * (ymin + ymax) / 2), _pause=False)

        max_scroll_pages = self.get_scroll_page_limit()
        wall_scroll_amount = -350

        if not ensure_builder_menu_open("initial scan"):
            return False

        def scroll_builder_page(direction=-1):
            move_to_builder_list()
            if sidebar:
                # Windows PyAutoGUI forwards raw wheel deltas, not notch counts.
                # One notch is 120; this HUD needs several paced events per page.
                for _ in range(4):
                    if not self.bot_running:
                        return
                    pyautogui.scroll(direction * 8 * 120, _pause=False)
                    if not self.bot_sleep(0.04):
                        return
            else:
                pyautogui.scroll(wall_scroll_amount if direction < 0 else 500)

        def reset_builder_list_to_top(scrolled_pages, reset_step=500, buffer_scrolls=2):
            if not self.bot_running:
                return False
            if sidebar:
                # Bounded by the user's setting; no separate 60-page reset.
                # The fixed "Suggested upgrades" heading means we're at top.
                top_frame = self.capture_screen(win_rect)
                top_text = " ".join(str(item[1]).lower() for item in (
                    self.run_ocr_on_region(top_frame, (0.115, 0.22, 0.75, 1.0)) or []
                ))
                if "suggested" in top_text:
                    return
                reset_pages = min(max_scroll_pages, max(1, scrolled_pages + 1))
                for _ in range(reset_pages):
                    if not self.bot_running:
                        return
                    scroll_builder_page(1)
                self.bot_sleep(1.0)
                return
            total_scrolled_down_distance = max(0, scrolled_pages) * abs(wall_scroll_amount)
            clicks_needed = ((total_scrolled_down_distance + reset_step - 1) // reset_step) + buffer_scrolls
            move_to_builder_list()
            for _ in range(clicks_needed):
                pyautogui.scroll(reset_step)
                time.sleep(0.15)
        self.write_terminal("[Upgrades Routine]: Resetting builder list to top before scan...")
        reset_builder_list_to_top(max_scroll_pages - 1)
        self.bot_sleep(1.0 if sidebar else 0.5)
        self.write_terminal(f"[Upgrades Routine]: Scanning builder menu up to {max_scroll_pages} pages for cheapest wall...")

        def wall_identity(option):
            label_text = str(option.get("text", "")).split("->", 1)[0]
            normalized = re.sub(r'[^a-z0-9]', '', label_text.lower())
            normalized = (
                normalized
                .replace('wa11', 'wall')
                .replace('wal1', 'wall')
                .replace('waii', 'wall')
                .replace('wa1l', 'wall')
            )
            match = re.fullmatch(r"wall(?:x[0-9oilsb]+)?", normalized)
            return ("wall" if sidebar else match.group(0)) if match else ""

        def is_affordable_wall(option):
            if option["cost"] < 50:
                return False
            resource = option.get("resource", "Unknown")
            if resource == "Unavailable":
                return False
            if resource == "Gold":
                return current_gold >= option["cost"]
            if resource == "Elixir":
                return current_elixir >= option["cost"]
            return current_gold >= option["cost"] or current_elixir >= option["cost"]

        def same_wall_target(option, target_cost, target_resource, target_identity=""):
            if target_identity and wall_identity(option) != target_identity:
                return False
            option_resource = option.get("resource", "Unknown")
            resources_match = (
                option_resource == target_resource
                or option_resource == "Unknown"
                or target_resource == "Unknown"
            )
            if sidebar:
                # Either currency can upgrade a wall; the list may change its
                # preferred currency. Use the fresh row's affordable resource.
                resources_match = True
            return option["cost"] == target_cost and resources_match and is_affordable_wall(option)

        def near_wall_target(option, target_cost, target_resource, target_identity=""):
            if sidebar:
                return same_wall_target(option, target_cost, target_resource, target_identity)
            if target_identity and wall_identity(option) != target_identity:
                return False
            option_resource = option.get("resource", "Unknown")
            resources_match = (
                option_resource == target_resource
                or option_resource == "Unknown"
                or target_resource == "Unknown"
            )
            if not resources_match or not is_affordable_wall(option):
                return False
            allowed_gap = max(500, int(target_cost * 0.20))
            return abs(option["cost"] - target_cost) <= allowed_gap

        def scan_builder_pages(scan_label):
            found_options = []
            for page_index in range(max_scroll_pages):
                if not self.bot_running:
                    break

                self.write_terminal(f"[{scan_label}]: Reading page {page_index + 1}/{max_scroll_pages}...")
                frame = settled_builder_frame()
                if frame is None:
                    return []
                if sidebar:
                    if not builder_sidebar_visible(frame, self.run_ocr_on_region):
                        self.write_terminal("[Upgrades Routine]: Builder panel closed during scan; stopping.")
                        return []
                menu_results = run_layout_independent_builder_ocr(frame, menu_crop)
                if not self.bot_running:
                    return []
                page_wall_options = parse_wall_options(menu_results, frame, menu_crop)
                if sidebar and not page_wall_options:
                    # A missed OCR pass must not immediately discard this page.
                    if not self.bot_sleep(0.35):
                        return []
                    frame = settled_builder_frame()
                    if frame is None:
                        return []
                    menu_results = run_layout_independent_builder_ocr(frame, menu_crop)
                    if not self.bot_running:
                        return []
                    page_wall_options = parse_wall_options(menu_results, frame, menu_crop)
                if sidebar and page_wall_options:
                    # Confirm a row twice even if it appears on only one page.
                    time.sleep(0.2)
                    verify_frame = self.capture_screen(win_rect)
                    verify_results = run_layout_independent_builder_ocr(verify_frame, menu_crop)
                    if not self.bot_running:
                        return []
                    verified = parse_wall_options(verify_results, verify_frame, menu_crop)
                    # Keep rows recovered by the second read as well. Previously
                    # a cheaper wall found here was silently thrown away.
                    for recovered in verified:
                        if not any(wall_identity(other) == wall_identity(recovered)
                                   and other["cost"] == recovered["cost"]
                                   and other.get("resource") == recovered.get("resource")
                                   for other in page_wall_options):
                            recovered = dict(recovered)
                            recovered["menu_results"] = verify_results
                            page_wall_options.append(recovered)
                    for option in page_wall_options:
                        option["repeat_confirmed"] = any(
                            wall_identity(other) == wall_identity(option)
                            and other["cost"] == option["cost"]
                            and other.get("resource") == option.get("resource")
                            for other in verified
                        )
                for option in page_wall_options:
                    option["page_index"] = page_index
                    option.setdefault("menu_results", menu_results)
                    found_options.append(option)

                if page_wall_options:
                    page_summary = "; ".join(
                        f"{option['text']} | {option.get('resource', 'Unknown')} Cost {option['cost']:,}"
                        for option in sorted(page_wall_options, key=lambda option: option["cost"])
                    )
                    self.write_terminal(
                        f"[{scan_label}]: Page {page_index + 1} wall rows found: {page_summary}"
                    )
                else:
                    self.write_terminal(f"[{scan_label}]: Page {page_index + 1} has no wall row.")

                if page_index < max_scroll_pages - 1:
                    move_to_builder_list()
                    scroll_builder_page()
                    if not self.bot_sleep(1.0 if sidebar else 0.8):
                        break

            return found_options

        def stabilize_scanned_wall_options(options):
            grouped = {}
            for option in options:
                identity = wall_identity(option)
                if not identity:
                    continue
                option["identity"] = identity
                group_key = (identity, option["cost"], option.get("resource", "Unknown")) if sidebar else identity
                grouped.setdefault(group_key, {}).setdefault(option["cost"], []).append(option)

            stable_options = []
            for group_key, costs in grouped.items():
                identity = group_key[0] if sidebar else group_key
                ranked_costs = sorted(
                    costs.items(),
                    key=lambda item: (
                        -len({entry["page_index"] for entry in item[1]}),
                        item[0],
                    ),
                )
                winning_cost, winning_entries = ranked_costs[0]
                winning_pages = sorted(
                    {entry["page_index"] + 1 for entry in winning_entries}
                )

                for rejected_cost, rejected_entries in ranked_costs[1:]:
                    rejected_pages = sorted(
                        {entry["page_index"] + 1 for entry in rejected_entries}
                    )
                    self.write_terminal(
                        f"[Upgrades OCR Consensus]: Rejected conflicting "
                        f"{identity} cost {rejected_cost:,} seen on "
                        f"page(s) {rejected_pages}; stable candidate is "
                        f"{winning_cost:,}."
                    )

                if len(winning_pages) < 2 and not any(entry.get("repeat_confirmed") for entry in winning_entries):
                    self.write_terminal(
                        f"[Upgrades OCR Consensus]: Ignoring one-off "
                        f"{identity} cost {winning_cost:,} from page "
                        f"{winning_pages[0]}; it was not repeated on another page."
                    )
                    continue

                known_resources = [
                    entry.get("resource", "Unknown")
                    for entry in winning_entries
                    if entry.get("resource") in ("Gold", "Elixir")
                ]
                representative = dict(
                    next(
                        (
                            entry for entry in winning_entries
                            if entry.get("resource") in ("Gold", "Elixir")
                        ),
                        winning_entries[0],
                    )
                )
                if known_resources:
                    representative["resource"] = max(
                        set(known_resources),
                        key=known_resources.count,
                    )
                representative["identity"] = identity
                stable_options.append(representative)
                self.write_terminal(
                    f"[Upgrades OCR Consensus]: Confirmed {identity} cost "
                    f"{winning_cost:,} across pages {winning_pages}."
                )

            return stable_options

        def refresh_current_page_match(
            target_cost,
            target_resource,
            target_identity,
            attempts=4,
        ):
            last_results = None
            previous_match = None
            for attempt in range(attempts):
                time.sleep(0.25 if attempt == 0 else 0.18)
                fresh_frame = settled_builder_frame()
                if fresh_frame is None:
                    return None, last_results
                fresh_results = run_layout_independent_builder_ocr(fresh_frame, menu_crop)
                last_results = fresh_results
                fresh_options = parse_wall_options(fresh_results, fresh_frame, menu_crop)
                candidates = [option for option in fresh_options
                              if wall_identity(option) and is_affordable_wall(option)
                              and (same_wall_target(option, target_cost, target_resource, target_identity)
                                   or (sidebar and option["cost"] < target_cost))]
                if candidates:
                    candidate = min(candidates, key=lambda option: option["cost"])
                    def anchor(option):
                        wall = option["wall_bbox"]
                        price = option["cost_bbox"]
                        return (sum(p[0] for p in wall)/len(wall), sum(p[1] for p in wall)/len(wall),
                                sum(p[0] for p in price)/len(price), sum(p[1] for p in price)/len(price))
                    same_price = previous_match is not None and candidate["cost"] == previous_match["cost"]
                    same_resource = previous_match is not None and candidate.get("resource") == previous_match.get("resource")
                    stable = same_price and same_resource and all(
                        abs(left-right) <= 6 * w / 1300
                        for left,right in zip(anchor(candidate),anchor(previous_match)))
                    if not sidebar or stable:
                        candidate["identity"] = wall_identity(candidate)
                        self.write_terminal(f"[Wall Row Verify]: Price {candidate['cost']:,} and row position agree across fresh reads.")
                        return candidate, fresh_results
                    if previous_match is not None:
                        self.write_terminal("[Wall Row Verify]: Row moved or price changed; discarding old coordinates.")
                    previous_match = candidate
                else:
                    previous_match = None
                if not self.bot_running:
                    return None, last_results
            return None, last_results

        all_wall_options = scan_builder_pages("Upgrades Scan")
        if not self.bot_running:
            return False
        all_wall_options = stabilize_scanned_wall_options(all_wall_options)
        affordable_walls = [option for option in all_wall_options if is_affordable_wall(option)]

        if sidebar and affordable_walls:
            target = min(affordable_walls, key=lambda option: option["cost"])
            self.write_terminal(f"[Wall Selection]: Scan found affordable walls from {target['cost']:,}. Checking visible rows without reopening the list.")
            # The scan ends on the last page. Search back through the same open
            # list instead of closing it, resetting its order, and losing rows.
            for page_index in range(max_scroll_pages - 1, -1, -1):
                if not self.bot_running:
                    return False
                fresh, results = refresh_current_page_match(
                    target["cost"], target.get("resource", "Unknown"), "wall")
                if fresh:
                    self.write_terminal(f"[Wall Selection]: Verified visible wall at {fresh['cost']:,}; selecting its current row.")
                    return click_wall_option(fresh, results, menu_crop)
                if page_index > 0:
                    self.write_terminal("[Wall Selection]: Checking previous page in the same open list...")
                    scroll_builder_page(1)
                    if not self.bot_sleep(1.0):
                        return False
            # Do not report a successful upgrade or loop forever on a stale row.
            self.write_terminal("[Wall Selection]: Visible wall verification failed; ending this attempt without spending resources.")
            return False

        if affordable_walls:
            target = min(affordable_walls, key=lambda option: option["cost"])
            target_resource = target.get("resource", "Unknown")
            target_identity = target.get("identity", "")
            self.write_terminal(
                f"[Upgrades Routine]: Cheapest affordable wall noted: "
                f"{target['text']} | {target_resource} Cost {target['cost']:,}"
            )

            self.write_terminal("[Upgrades Routine]: Closing and reopening builder menu to find noted wall again...")
            if not close_builder_menu():
                return False
            if not ensure_builder_menu_open("wall refind"):
                return False
            self.write_terminal("[Upgrades Routine]: Resetting builder list to top before refind...")
            reset_builder_list_to_top(max_scroll_pages - 1)
            time.sleep(0.8)

            # List positions can change when resource availability or counts change.
            target_page_index = 0 if sidebar else max(0, min(max_scroll_pages - 1, int(target.get("page_index", 0))))
            if target_page_index:
                self.write_terminal(f"[Upgrades Refind]: Jumping directly to remembered page {target_page_index + 1}...")
                for _ in range(target_page_index):
                    scroll_builder_page()
                    if not self.bot_sleep(1.0 if sidebar else 0.8):
                        break

            for page_index in range(target_page_index, max_scroll_pages):
                if not self.bot_running:
                    break

                frame = self.capture_screen(win_rect)
                menu_results = run_layout_independent_builder_ocr(frame, menu_crop)
                page_wall_options = parse_wall_options(menu_results, frame, menu_crop)
                if sidebar:
                    cheaper = [option for option in page_wall_options
                               if wall_identity(option) and is_affordable_wall(option)
                               and option["cost"] < target["cost"]]
                    if cheaper:
                        target = min(cheaper, key=lambda option: option["cost"])
                        target_resource = target.get("resource", "Unknown")
                        target_identity = wall_identity(target)
                        self.write_terminal(f"[Upgrades Refind]: Found cheaper affordable wall: {target['text']}. Updating target.")
                matching_options = [
                    option for option in page_wall_options
                    if same_wall_target(
                        option,
                        target["cost"],
                        target_resource,
                        target_identity,
                    )
                ]
                if not matching_options:
                    matching_options = sorted(
                        [
                            option for option in page_wall_options
                            if near_wall_target(
                                option,
                                target["cost"],
                                target_resource,
                                target_identity,
                            )
                        ],
                        key=lambda option: abs(option["cost"] - target["cost"])
                    )

                if page_wall_options:
                    page_summary = "; ".join(
                        f"{option['text']} | {option.get('resource', 'Unknown')} Cost {option['cost']:,}"
                        for option in sorted(page_wall_options, key=lambda option: option["cost"])
                    )
                    self.write_terminal(
                        f"[Upgrades Refind]: Page {page_index + 1} wall rows found: {page_summary}"
                    )
                else:
                    self.write_terminal(f"[Upgrades Refind]: Page {page_index + 1} has no wall row.")

                if matching_options:
                    self.write_terminal("[Upgrades Routine]: Noted cheapest wall found again. Settling and refreshing OCR...")
                    fresh_match, fresh_results = refresh_current_page_match(
                        target["cost"],
                        target_resource,
                        target_identity,
                    )
                    if not fresh_match:
                        self.write_terminal("[Upgrades Routine]: Fresh OCR missed wall twice. Continuing scan.")
                        if page_index < max_scroll_pages - 1:
                            move_to_builder_list()
                            scroll_builder_page()
                            if not self.bot_sleep(1.0 if sidebar else 0.8):
                                break
                            continue
                        break
                    self.write_terminal("[Upgrades Routine]: Fresh OCR confirmed wall. Upgrading now...")
                    click_result = click_wall_option(fresh_match, fresh_results, menu_crop)
                    if click_result:
                        return True
                    self.write_terminal("[Upgrades Routine]: Wall click did not spend resources. Retrying wall drain instead of farming.")
                    return True

                if page_index < max_scroll_pages - 1:
                    move_to_builder_list()
                    scroll_builder_page()
                    if not self.bot_sleep(1.0 if sidebar else 0.8):
                        break

            if not self.bot_running:
                return False
            self.write_terminal("[Upgrades Routine]: Refind missed noted wall. Trying one final top-page reset...")
            if not close_builder_menu():
                return False
            if not ensure_builder_menu_open("final top-page retry"):
                return False
            reset_builder_list_to_top(max_scroll_pages - 1, reset_step=650)

            frame = self.capture_screen(win_rect)
            menu_results = run_layout_independent_builder_ocr(frame, menu_crop)
            page_wall_options = parse_wall_options(menu_results, frame, menu_crop)
            matching_options = [
                option for option in page_wall_options
                if same_wall_target(
                    option,
                    target["cost"],
                    target_resource,
                    target_identity,
                )
            ]
            if not matching_options:
                matching_options = sorted(
                    [
                        option for option in page_wall_options
                        if near_wall_target(
                            option,
                            target["cost"],
                            target_resource,
                            target_identity,
                        )
                    ],
                    key=lambda option: abs(option["cost"] - target["cost"])
                )
            if matching_options:
                self.write_terminal("[Upgrades Routine]: Noted wall found on final top-page retry. Upgrading now...")
                click_result = click_wall_option(matching_options[0], menu_results, menu_crop)
                if click_result:
                    return True
                self.write_terminal("[Upgrades Routine]: Wall click did not spend resources. Retrying wall drain instead of farming.")
                return True

            self.write_terminal("[Upgrades Routine]: Could not find noted cheapest wall after reopening. Retrying wall scan instead of farming.")
            close_builder_menu()
            return True

        if all_wall_options:
            self.last_wall_scan_blocked_by_cost = True
            cheapest_wall = min(all_wall_options, key=lambda option: option["cost"])
            self.write_terminal(f"[Upgrades Routine]: Cheapest wall found costs {cheapest_wall['cost']:,}, but wallet cannot afford it.")
        else:
            self.write_terminal("[Upgrades Routine]: No wall rows found after full builder menu scan.")

        self.write_terminal("[Upgrades Routine]: Exiting builder panel matrix state profiles...")
        close_builder_menu()
        return False

    def core_bot_loop(self):
        while self.bot_running:
            min_gold = self.clean_input_string(self.txt_min_gold.get(), 1000000)
            min_elixir = self.clean_input_string(self.txt_min_elixir.get(), 1000000)
            upgrade_trigger = self.clean_input_string(self.txt_upgrade_limit.get(), 20000000) if self.enable_walls_var.get() else 999999999999
            
            win_rect = self.get_coc_window()
            if not win_rect:
                self.write_terminal("[System State Warning]: Cannot bind game app canvas windows. Retrying...")
                time.sleep(4)
                continue
                
            x, y, w, h = win_rect
            frame = self.capture_screen(win_rect)
            if self.dismiss_reload_popup(frame, win_rect):
                time.sleep(0.8)
                continue
            if self.dismiss_okay_popup(frame, win_rect):
                time.sleep(0.8)
                continue
            if builder_sidebar_visible(frame, self.run_ocr_on_region):
                self.write_terminal("[State Recovery]: Closing open builder list before village cycle.")
                pyautogui.click(x + int(w * 0.986), y + int(h * 0.081))
                self.bot_sleep(0.7)
                continue
            enemy_g, enemy_e = self.get_enemy_loot(frame)
            home_visible = False
            your_g, your_e = 0, 0
            if not self.is_attack_screen(enemy_g, enemy_e):
                home_visible = self.is_home_screen(frame)
                if home_visible:
                    _, _, your_g, your_e = self.get_resources_by_corner(frame)
            match_ui_visible = False if home_visible else self.is_match_ui_screen(frame)
            
            # --- EVALUATION MODULE A: ACTIVE MULTIPLAYER BATTLE MODE ---
            if self.is_attack_screen(enemy_g, enemy_e):
                self.match_loot_miss_count = 0
                self.write_terminal(f"[Matchmaking Assessment] -> Found Loot - Gold: {enemy_g:,} | Elixir: {enemy_e:,}")
                
                if enemy_g >= min_gold and enemy_e >= min_elixir:
                    self.write_terminal("Ã°Å¸Å¡â‚¬ CRITERIA ACHIEVED! Unleashing combat deployment peripheral grids...")

                    self.write_terminal("[Deploy]: Zooming out battlefield before troop placement...")
                    zoom_x = x + int(w * 0.50)
                    zoom_y = y + int(h * 0.45)
                    pyautogui.click(zoom_x, zoom_y)
                    time.sleep(0.08)
                    pyautogui.moveTo(zoom_x, zoom_y)
                    for _ in range(12):
                        pyautogui.scroll(-10, x=zoom_x, y=zoom_y)
                        try:
                            ctypes.windll.user32.mouse_event(0x0800, 0, 0, -1200, 0)
                        except Exception:
                            pass
                        time.sleep(0.04)
                    pyautogui.keyDown('ctrl')
                    for _ in range(8):
                        pyautogui.scroll(-8, x=zoom_x, y=zoom_y)
                        time.sleep(0.03)
                    pyautogui.keyUp('ctrl')
                    time.sleep(0.25)

                    troop_slots = [0.21, 0.28, 0.34, 0.40]
                    hero_slots = [0.46, 0.52, 0.59, 0.65]
                    spell_slots = [0.72, 0.78, 0.84]
                    troop_deploy_points = [
                        (0.11, 0.50),
                        (0.15, 0.62),
                        (0.24, 0.71),
                        (0.42, 0.76),
                        (0.66, 0.76),
                        (0.86, 0.61),
                        (0.91, 0.48),
                        (0.82, 0.31),
                        (0.67, 0.19),
                        (0.47, 0.16),
                        (0.28, 0.23),
                    ]
                    hero_deploy_points = [
                        (0.14, 0.55),
                        (0.22, 0.67),
                        (0.78, 0.64),
                        (0.82, 0.38),
                    ]
                    spell_deploy_points = [
                        (0.50, 0.45),
                        (0.42, 0.52),
                        (0.58, 0.52),
                        (0.50, 0.36),
                    ]

                    for slot_index, slot_x in enumerate(troop_slots):
                        if not self.bot_running:
                            break

                        select_x = x + int(w * slot_x)
                        select_y = y + int(h * 0.93)
                        self.write_terminal(f"[Deploy]: Selecting troop slot {slot_index + 1}: [X: {select_x}, Y: {select_y}]")
                        pyautogui.click(select_x, select_y)
                        time.sleep(0.12)

                        repeat_count = 3 if slot_index == 1 else 2

                        for _ in range(repeat_count):
                            for point_x, point_y in troop_deploy_points:
                                if not self.bot_running:
                                    break
                                deploy_x = x + int(w * point_x)
                                deploy_y = y + int(h * point_y)
                                pyautogui.click(deploy_x, deploy_y)
                                time.sleep(0.07)

                    for hero_index, slot_x in enumerate(hero_slots):
                        if not self.bot_running:
                            break

                        select_x = x + int(w * slot_x)
                        select_y = y + int(h * 0.93)
                        self.write_terminal(f"[Deploy]: Selecting hero slot {hero_index + 1}: [X: {select_x}, Y: {select_y}]")
                        pyautogui.click(select_x, select_y)
                        time.sleep(0.12)

                        for point_x, point_y in hero_deploy_points:
                            if not self.bot_running:
                                break
                            deploy_x = x + int(w * point_x)
                            deploy_y = y + int(h * point_y)
                            pyautogui.click(deploy_x, deploy_y)
                            time.sleep(0.08)

                    for spell_index, slot_x in enumerate(spell_slots):
                        if not self.bot_running:
                            break

                        select_x = x + int(w * slot_x)
                        select_y = y + int(h * 0.93)
                        self.write_terminal(f"[Deploy]: Selecting spell slot {spell_index + 1}: [X: {select_x}, Y: {select_y}]")
                        pyautogui.click(select_x, select_y)
                        time.sleep(0.12)

                        for point_x, point_y in spell_deploy_points:
                            if not self.bot_running:
                                break
                            deploy_x = x + int(w * point_x)
                            deploy_y = y + int(h * point_y)
                            pyautogui.click(deploy_x, deploy_y)
                            time.sleep(0.08)
                        
                    self.write_terminal("Ã¢ÂÂ³ Full military deployment executed. Locking inside combat monitors...")
                    
                    combat_engagement = True
                    last_home_check = 0
                    while combat_engagement and self.bot_running:
                        if not self.bot_sleep(3):
                            break
                        combat_capture = self.capture_screen(win_rect)
                        if self.dismiss_okay_popup(combat_capture, win_rect):
                            combat_engagement = False
                            self.bot_sleep(0.8)
                            continue
                        termination_box = self.check_match_end(combat_capture)
                        
                        if termination_box:
                            self.write_terminal("Ã°Å¸ÂÂ Match completion signature verified! Navigating return pathways...")
                            btn_x = x + int(w * 0.52)
                            btn_y = y + int(h * 0.86)
                            home_confirmed = False
                            for click_attempt in range(1, 4):
                                self.write_terminal(
                                    f"[Battle End]: Clicking Return Home button "
                                    f"(attempt {click_attempt}/3): "
                                    f"[X: {btn_x}, Y: {btn_y}]"
                                )
                                pyautogui.click(btn_x, btn_y)
                                for _ in range(6):
                                    if not self.bot_sleep(0.75):
                                        break
                                    post_battle_frame = self.capture_screen(win_rect)
                                    if self.dismiss_okay_popup(post_battle_frame, win_rect):
                                        self.bot_sleep(0.8)
                                        continue
                                    if self.is_home_screen(post_battle_frame):
                                        home_confirmed = True
                                        break
                                if home_confirmed or not self.bot_running:
                                    break

                            if home_confirmed:
                                self.write_terminal(
                                    "[Battle End]: Home screen confirmed after "
                                    "Return Home."
                                )
                                combat_engagement = False
                            else:
                                self.write_terminal(
                                    "[Battle End]: Home screen not confirmed yet. "
                                    "Continuing result-screen monitoring."
                                )
                            continue

                        now = time.time()
                        if now - last_home_check >= 60:
                            last_home_check = now
                            if self.is_home_screen(combat_capture):
                                self.write_terminal("[State Recovery]: Home screen detected while monitoring battle. Resuming village cycle...")
                                combat_engagement = False
                else:
                    self.match_loot_miss_count = 0
                    self.write_terminal("Ã¢ÂÅ’ Asset yield below thresholds. Activating 'Next Match' skip sequences...")
                    next_x = x + int(w * 0.93)
                    next_y = y + int(h * 0.75)
                    self.write_terminal(f"[Matchmaking Skip]: Clicking Next button: [X: {next_x}, Y: {next_y}]")
                    pyautogui.click(next_x, next_y)
                    time.sleep(1.0)
                    
            # --- EVALUATION MODULE B: VILLAGE HUB MONITOR OVERVIEW ---
            elif match_ui_visible:
                self.match_loot_miss_count += 1
                if self.match_loot_miss_count >= 2:
                    next_x = x + int(w * 0.93)
                    next_y = y + int(h * 0.75)
                    self.write_terminal(f"[Matchmaking Recovery]: Loot OCR missed {self.match_loot_miss_count} times. Clicking Next: [X: {next_x}, Y: {next_y}]")
                    pyautogui.click(next_x, next_y)
                    self.match_loot_miss_count = 0
                    time.sleep(1.0)
                else:
                    self.write_terminal("[State Check]: Match screen detected but loot OCR missed. Rechecking shortly...")
                    time.sleep(0.5)

            else:
                self.match_loot_miss_count = 0
                if home_visible:
                    self.write_terminal("[State Check]: Home screen detected from Shop button.")
                self.write_terminal(f"[Village Hub Status] -> Gold Balance: {your_g:,} | Elixir Balance: {your_e:,}")
                self.update_gui_metrics(your_g, your_e)
                
                if self.enable_walls_var.get() and your_g >= upgrade_trigger and your_e >= upgrade_trigger:
                    self.write_terminal("[Wallet Overflow Alert]: Capital reserves hit milestone trigger! Entering drainage loop...")
                    wall_scan_misses = 0
                    while self.bot_running:
                        expected_wallet = getattr(self, "_wall_expected_wallet", None)
                        if isinstance(expected_wallet, tuple) and len(expected_wallet) == 2:
                            g_now, e_now = expected_wallet
                            self._wall_expected_wallet = None
                            self.write_terminal(
                                f"[Upgrades Routine]: Using calculated post-upgrade wallet - "
                                f"Gold: {g_now:,} | Elixir: {e_now:,}"
                            )
                        else:
                            refreshed_frame = self.capture_screen(win_rect)
                            _, _, g_now, e_now = self.get_resources_by_corner(refreshed_frame)
                        self.update_gui_metrics(g_now, e_now)
                        has_upgraded = self.find_and_upgrade_walls(win_rect, g_now, e_now)
                        if not self.bot_running:
                            break
                        if has_upgraded:
                            wall_scan_misses = 0
                            time.sleep(0.5)
                            continue
                        if (g_now > 0 or e_now > 0) and not getattr(self, "last_wall_scan_blocked_by_cost", False) and wall_scan_misses < 3:
                            wall_scan_misses += 1
                            self.write_terminal("[Wallet Drain Retry]: Wall scan missed during drain. Rechecking before attack...")
                            time.sleep(0.8)
                            continue
                        break
                    if not self.bot_running:
                        break
                    self.write_terminal("[Wallet Drain Complete]: No affordable wall found. Returning to farming cycle...")

                if self.bot_running and home_visible:
                    self.write_terminal("[Farming Strategy Trigger]: Balance below trigger milestone or upgrades disabled. Launching attack...")
                    village_attack_x = x + int(w * 0.09)
                    village_attack_y = y + int(h * 0.90)
                    self.write_terminal(f"[Farming Launch]: Clicking village Attack button: [X: {village_attack_x}, Y: {village_attack_y}]")
                    pyautogui.click(village_attack_x, village_attack_y)
                    if not self.screen_has_text(win_rect, ["find a match", "multiplayer"], timeout=2.5):
                        self.write_terminal("[Farming Launch]: Multiplayer screen not detected. Retrying village Attack button...")
                        pyautogui.click(village_attack_x, village_attack_y)
                        if not self.screen_has_text(win_rect, ["find a match", "multiplayer"], timeout=2.5):
                            self.write_terminal("[Farming Launch]: Battle menu not recognized. Rechecking screen before further clicks.")
                            continue

                    find_match_x = x + int(w * 0.23)
                    find_match_y = y + int(h * 0.72)
                    self.write_terminal(f"[Farming Launch]: Clicking Find a Match button: [X: {find_match_x}, Y: {find_match_y}]")
                    pyautogui.click(find_match_x, find_match_y)
                    if not self.screen_has_text(win_rect, ["my army", "attack"], timeout=3):
                        self.write_terminal("[Farming Launch]: Army screen not detected. Retrying Find a Match button...")
                        pyautogui.click(find_match_x, find_match_y)
                        if not self.screen_has_text(win_rect, ["my army", "attack"], timeout=2.5):
                            self.write_terminal("[Farming Launch]: Army screen not recognized. Rechecking screen before further clicks.")
                            continue

                    final_attack_x = x + int(w * 0.85)
                    final_attack_y = y + int(h * 0.86)
                    self.write_terminal(f"[Farming Launch]: Clicking army Attack button: [X: {final_attack_x}, Y: {final_attack_y}]")
                    pyautogui.click(final_attack_x, final_attack_y)
                    time.sleep(1.0)
                elif self.bot_running:
                    self.write_terminal("[State Recovery]: Unknown screen detected. Sending real Esc key to close overlay/menu.")
                    self.press_real_escape()
                    time.sleep(1.0)
