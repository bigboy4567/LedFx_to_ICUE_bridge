import json
import logging
import os
import pathlib
import queue
import re
import selectors
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser

import i18n

try:
    import msvcrt

    _HAS_MSVCRT = True
except Exception:
    msvcrt = None
    _HAS_MSVCRT = False

try:
    import psutil

    _HAS_PSUTIL = True
except ImportError:
    psutil = None
    _HAS_PSUTIL = False


_TK_DLL_DIRS = []


def _bootstrap_tk_runtime():
    candidates = []
    try:
        module_dir = pathlib.Path(__file__).resolve().parent
    except Exception:
        module_dir = pathlib.Path.cwd()

    if getattr(sys, "frozen", False):
        exe_dir = pathlib.Path(sys.executable).resolve().parent
        candidates.extend([exe_dir, exe_dir / "_internal"])
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            mp = pathlib.Path(meipass)
            candidates.extend([mp, mp / "_internal"])
    else:
        candidates.extend([module_dir, module_dir / "_internal"])

    seen = set()
    ordered = []
    for candidate in candidates:
        try:
            key = str(candidate.resolve()).lower()
        except Exception:
            key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(candidate)

    tcl_dir = None
    tk_dir = None
    for candidate in ordered:
        candidate = pathlib.Path(candidate)
        cur_tcl = candidate / "_tcl_data"
        cur_tk = candidate / "_tk_data"
        if tcl_dir is None and cur_tcl.is_dir():
            tcl_dir = cur_tcl
        if tk_dir is None and cur_tk.is_dir():
            tk_dir = cur_tk
        if (candidate / "tcl86t.dll").exists() or (candidate / "tk86t.dll").exists():
            path_value = os.environ.get("PATH", "")
            candidate_str = str(candidate)
            if candidate_str.lower() not in path_value.lower():
                os.environ["PATH"] = candidate_str + os.pathsep + path_value
        if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
            if (candidate / "tcl86t.dll").exists() or (candidate / "tk86t.dll").exists():
                try:
                    _TK_DLL_DIRS.append(os.add_dll_directory(str(candidate)))
                except Exception:
                    pass
    if tcl_dir is not None and not os.environ.get("TCL_LIBRARY"):
        os.environ["TCL_LIBRARY"] = str(tcl_dir)
    if tk_dir is not None and not os.environ.get("TK_LIBRARY"):
        os.environ["TK_LIBRARY"] = str(tk_dir)


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError("config.json invalide: objet JSON attendu.")
    return data


def _resolve_config_path(path):
    p = pathlib.Path(path)
    if p.is_absolute():
        return p
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent / p
    candidates = [pathlib.Path.cwd() / p]
    try:
        candidates.append(pathlib.Path(sys.executable).resolve().parent / p)
    except Exception:
        pass
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        try:
            candidates.append(pathlib.Path(meipass) / p)
        except Exception:
            pass
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return p


def _version_key(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s[0] in ("v", "V"):
        s = s[1:]
    parts = []
    # FIX-2: Split versions on dot, underscore, hyphen, or plus.
    for part in re.split(r"[._\-+]", s):
        if not part:
            continue
        if part.isdigit():
            parts.append(int(part))
            continue
        m = re.match(r"(\\d+)", part)
        if m:
            parts.append(int(m.group(1)))
    return tuple(parts) if parts else None


def _is_newer_version(latest, current):
    latest_key = _version_key(latest)
    current_key = _version_key(current)
    if latest_key is None or current_key is None:
        return str(latest) != str(current)
    return latest_key > current_key


def _fetch_latest_release(repo, timeout=6):
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(
        url, headers={"User-Agent": "ledfx-icue-bridge"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    tag = data.get("tag_name") or ""
    html_url = data.get("html_url") or url
    download_url = None
    for asset in data.get("assets") or []:
        asset_url = asset.get("browser_download_url")
        if asset_url and asset_url.lower().endswith(".exe"):
            download_url = asset_url
            break
    if not download_url:
        download_url = html_url
    return {"version": tag, "url": download_url}


def setup_logging(cfg):
    level_name = str(cfg.get("log_level", "info")).upper()
    level = getattr(logging, level_name, logging.INFO)

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    stream = logging.StreamHandler()
    stream.setLevel(level)
    stream.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    root_logger.setLevel(level)
    root_logger.addHandler(stream)
    return logging.getLogger("bridge")


def _configure_timing_logger():
    # DIAG-0: Force bridge.timing DEBUG records through existing handlers.
    timing_logger = logging.getLogger("bridge.timing")
    timing_logger.setLevel(logging.DEBUG)
    for handler in logging.getLogger().handlers:
        try:
            if handler.level > logging.DEBUG:
                handler.setLevel(logging.DEBUG)
        except Exception:
            pass
    return timing_logger


def get_perf_options(cfg, args):
    enabled = bool(cfg.get("perf_debug", False) or getattr(args, "perf_debug", False))
    threshold_value = getattr(args, "perf_threshold", None)
    if threshold_value is None:
        threshold_value = cfg.get("perf_threshold", 0.20)
    try:
        threshold = max(0.02, float(threshold_value))
    except Exception:
        threshold = 0.20
    try:
        cooldown = max(0.0, float(cfg.get("perf_log_cooldown", 2.0)))
    except Exception:
        cooldown = 2.0
    return enabled, threshold, cooldown


def build_lut(brightness, gamma):
    lut = [0] * 256
    for i in range(256):
        v = i / 255.0
        if gamma != 1.0:
            v = v ** gamma
        v = v * 255.0 * brightness
        if v < 0:
            v = 0
        if v > 255:
            v = 255
        lut[i] = int(v + 0.5)
    return lut


def parse_rgb(value):
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(_clamp_byte(int(v)) for v in value)
    s = str(value).strip()
    if not s:
        return None
    s = s.replace(";", ",").replace(" ", "")
    parts = s.split(",")
    if len(parts) != 3:
        return None
    try:
        return tuple(_clamp_byte(int(p)) for p in parts)
    except Exception:
        return None


def parse_white_balance(value):
    if value is None:
        return None
    r = g = b = None
    if isinstance(value, dict):
        r = value.get("r")
        g = value.get("g")
        b = value.get("b")
    elif isinstance(value, (list, tuple)) and len(value) == 3:
        r, g, b = value
    else:
        return None
    try:
        r = float(r)
        g = float(g)
        b = float(b)
    except Exception:
        return None
    if r > 2.0 or g > 2.0 or b > 2.0:
        r /= 255.0
        g /= 255.0
        b /= 255.0
    r = max(0.0, min(2.0, r))
    g = max(0.0, min(2.0, g))
    b = max(0.0, min(2.0, b))
    return (r, g, b)


def _clamp_byte(v):
    if v < 0:
        return 0
    if v > 255:
        return 255
    return v


def _get_attr(obj, names):
    for name in names:
        if obj is None:
            return None
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def parse_int_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = []
        for v in value:
            raw.extend(str(v).replace(";", ",").split(","))
    else:
        raw = str(value).replace(";", ",").split(",")
    out = []
    for part in raw:
        p = part.strip()
        if not p:
            continue
        try:
            out.append(int(p))
        except Exception:
            pass
    return out


def normalize_protocol(value, fallback="drgb"):
    proto = (value or fallback or "drgb").lower()
    if proto in ("udp", "drgb", "wled"):
        return "wled"
    return proto


def _normalize_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    return [str(value)]


def _match_contains(text, patterns):
    if not patterns:
        return True
    if text is None:
        return False
    t = str(text).lower()
    for p in patterns:
        if p is None:
            continue
        if str(p).lower() in t:
            return True
    return False


def _device_sort_key(dev, sort_key):
    if sort_key == "x":
        return (dev.get("center_x") is None, dev.get("center_x", 0.0))
    if sort_key == "y":
        return (dev.get("center_y") is None, dev.get("center_y", 0.0))
    if sort_key == "xy":
        return (
            dev.get("center_x") is None,
            dev.get("center_x", 0.0),
            dev.get("center_y", 0.0),
        )
    if sort_key == "yx":
        return (
            dev.get("center_y") is None,
            dev.get("center_y", 0.0),
            dev.get("center_x", 0.0),
        )
    if sort_key == "model":
        return str(dev.get("model") or "")
    return str(dev.get("device_id_str") or dev.get("device_id") or "")


def _angle_order(points, start, direction):
    import math
    start = (start or "top").lower()
    direction = (direction or "clockwise").lower()
    start_angle = {
        "right": 0.0,
        "top": math.pi / 2.0,
        "left": math.pi,
        "bottom": -math.pi / 2.0,
    }.get(start, math.pi / 2.0)
    order = []
    for idx, x, y, cx, cy in points:
        angle = math.atan2(y - cy, x - cx)
        if direction == "clockwise":
            delta = (start_angle - angle) % (2 * math.pi)
        else:
            delta = (angle - start_angle) % (2 * math.pi)
        order.append((delta, idx))
    order.sort(key=lambda t: t[0])
    return [idx for _, idx in order]


def _kmeans_2d(points, k, max_iter=20):
    if not points or k <= 1:
        return [points]
    xs = sorted(p[1] for p in points)
    ys = sorted(p[2] for p in points)
    centers = []
    for i in range(k):
        q = i / (k - 1) if k > 1 else 0
        xi = xs[int(q * (len(xs) - 1))]
        yi = ys[int(q * (len(ys) - 1))]
        centers.append((xi, yi))
    for _ in range(max_iter):
        clusters = [[] for _ in range(k)]
        for p in points:
            best = min(range(k), key=lambda j: (p[1] - centers[j][0]) ** 2 + (p[2] - centers[j][1]) ** 2)
            clusters[best].append(p)
        new_centers = []
        for i, c in enumerate(clusters):
            if c:
                mx = sum(p[1] for p in c) / len(c)
                my = sum(p[2] for p in c) / len(c)
                new_centers.append((mx, my))
            else:
                new_centers.append(centers[i])
        shift = max(
            (new_centers[i][0] - centers[i][0]) ** 2 + (new_centers[i][1] - centers[i][1]) ** 2
            for i in range(k)
        )
        centers = new_centers
        if shift < 1e-4:
            break
    clusters = [[] for _ in range(k)]
    for p in points:
        best = min(range(k), key=lambda j: (p[1] - centers[j][0]) ** 2 + (p[2] - centers[j][1]) ** 2)
        clusters[best].append(p)
    # FIX-6: Warn when k-means leaves empty clusters that callers skip.
    empty_count = sum(1 for c in clusters if not c)
    if empty_count:
        logging.warning(
            "k-means produced %s empty cluster(s); requested=%s points=%s",
            empty_count,
            k,
            len(points),
        )
    return clusters


def _cluster_sort_key(center, sort_key):
    x, y = center
    if sort_key == "y":
        return (y, x)
    if sort_key == "xy":
        return (x, y)
    if sort_key == "yx":
        return (y, x)
    return (x, y)


def _normalize_group_order(order, count):
    if not order:
        return None
    try:
        order_list = [int(x) for x in order]
    except Exception:
        return None
    if not order_list:
        return None
    if all(i >= 1 for i in order_list):
        order_list = [i - 1 for i in order_list]
    order_list = [i for i in order_list if 0 <= i < count]
    if not order_list:
        return None
    if len(order_list) < count:
        order_list += [i for i in range(count) if i not in order_list]
    return order_list


def _ring_order(points, outer_leds, inner_leds, start, direction, inner_first, order_mode):
    if not points:
        return []
    order_mode = (order_mode or "index").lower()
    if order_mode == "index":
        indices = [p[0] for p in points]
        outer = indices[: int(outer_leds)] if outer_leds else []
        inner = indices[int(outer_leds) : int(outer_leds) + int(inner_leds)] if inner_leds else []
        if (direction or "").lower() == "counter":
            outer = list(reversed(outer))
            inner = list(reversed(inner))
        return (inner + outer) if inner_first else (outer + inner)

    import math
    cx = sum(p[1] for p in points) / len(points)
    cy = sum(p[2] for p in points) / len(points)
    by_dist = []
    for idx, x, y in points:
        dist = (x - cx) ** 2 + (y - cy) ** 2
        by_dist.append((dist, idx, x, y))
    by_dist.sort(key=lambda t: t[0], reverse=True)
    outer = by_dist[: int(outer_leds)] if outer_leds else []
    inner = by_dist[int(outer_leds) : int(outer_leds) + int(inner_leds)] if inner_leds else []

    outer_pts = [(idx, x, y, cx, cy) for _, idx, x, y in outer]
    inner_pts = [(idx, x, y, cx, cy) for _, idx, x, y in inner]
    outer_order = _angle_order(outer_pts, start, direction)
    inner_order = _angle_order(inner_pts, start, direction)

    if inner_first:
        return inner_order + outer_order
    return outer_order + inner_order


def _build_aio_cluster_orders(
    positions,
    cluster_count=3,
    group_sort="x",
    group_order=None,
    start="top",
    direction="clockwise",
    flip_x=False,
    flip_y=False,
    swap_xy=False,
    pump_first=False,
):
    if not positions:
        return None
    points = []
    for idx, pos in enumerate(positions):
        x = pos.get("x")
        y = pos.get("y")
        if x is None or y is None:
            return None
        fx = -float(x) if flip_x else float(x)
        fy = -float(y) if flip_y else float(y)
        if swap_xy:
            fx, fy = fy, fx
        points.append((idx, fx, fy))
    if not points:
        return None
    try:
        k = int(cluster_count)
    except Exception:
        k = 1
    k = max(1, min(k, len(points)))
    clusters = _kmeans_2d(points, k) if k > 1 else [points]
    cluster_info = []
    for c in clusters:
        if not c:
            continue
        cx = sum(p[1] for p in c) / len(c)
        cy = sum(p[2] for p in c) / len(c)
        cluster_info.append((c, (cx, cy)))
    if not cluster_info:
        return None
    cluster_info.sort(key=lambda t: _cluster_sort_key(t[1], group_sort))
    order_idx = _normalize_group_order(group_order, len(cluster_info))
    if order_idx:
        cluster_info = [cluster_info[i] for i in order_idx]
    elif pump_first and len(cluster_info) > 1:
        gx = sum(p[1] for p in points) / len(points)
        gy = sum(p[2] for p in points) / len(points)
        pump_idx = min(
            range(len(cluster_info)),
            key=lambda i: (cluster_info[i][1][0] - gx) ** 2
            + (cluster_info[i][1][1] - gy) ** 2,
        )
        if pump_idx != 0:
            pump_cluster = cluster_info.pop(pump_idx)
            cluster_info.insert(0, pump_cluster)

    orders = []
    for c, center in cluster_info:
        cx, cy = center
        pts = [(idx, x, y, cx, cy) for idx, x, y in c]
        order = _angle_order(pts, start, direction)
        if not order:
            order = [p[0] for p in c]
        orders.append(order)
    return orders


def _build_pump_lr_pairs(
    positions,
    start="left",
    flip_x=False,
    flip_y=False,
    swap_xy=False,
    allowed_indices=None,
):
    if not positions:
        return None
    allowed = None
    if allowed_indices:
        allowed = set(int(i) for i in allowed_indices)
    points = []
    for idx, pos in enumerate(positions):
        if allowed is not None and idx not in allowed:
            continue
        x = pos.get("x")
        y = pos.get("y")
        if x is None or y is None:
            return None
        fx = -float(x) if flip_x else float(x)
        fy = -float(y) if flip_y else float(y)
        if swap_xy:
            fx, fy = fy, fx
        points.append((idx, fx, fy))
    if not points:
        return None
    cy = sum(p[2] for p in points) / len(points)
    top = [p for p in points if p[2] <= cy]
    bottom = [p for p in points if p[2] > cy]
    start = (start or "left").lower()
    reverse = start == "right"
    top.sort(key=lambda p: (p[1], p[2]), reverse=reverse)
    bottom.sort(key=lambda p: (p[1], p[2]), reverse=reverse)
    pairs = []
    max_len = max(len(top), len(bottom))
    for i in range(max_len):
        pair = []
        if i < len(top):
            pair.append(top[i][0])
        if i < len(bottom):
            pair.append(bottom[i][0])
        if pair:
            pairs.append(pair)
    return pairs


def _normalize_mode(value):
    v = str(value or "").strip().lower()
    if v in ("2", "g", "group", "groupe"):
        return "group"
    if v in ("3", "f", "fusion"):
        return "fusion"
    if v in ("1", "u", "unique"):
        return "unique"
    return None


def _normalize_name(value):
    return str(value or "").strip().lower()


def _cluster_rows_by_y(points, rows_count):
    ys = [p[2] for p in points]
    ys_sorted = sorted(ys)
    if not ys_sorted:
        return []
    if rows_count >= len(ys_sorted):
        return [[p] for p in sorted(points, key=lambda p: p[2])]
    centers = []
    for i in range(rows_count):
        q = i / (rows_count - 1) if rows_count > 1 else 0
        idx = int(q * (len(ys_sorted) - 1))
        centers.append(ys_sorted[idx])
    for _ in range(20):
        clusters = [[] for _ in range(rows_count)]
        for point in points:
            cluster_idx = min(range(rows_count), key=lambda j: abs(point[2] - centers[j]))
            clusters[cluster_idx].append(point)
        new_centers = []
        for i, cluster in enumerate(clusters):
            if cluster:
                new_centers.append(sum(p[2] for p in cluster) / len(cluster))
            else:
                new_centers.append(centers[i])
        delta = max(abs(new_centers[i] - centers[i]) for i in range(rows_count))
        centers = new_centers
        if delta < 1e-3:
            break
    return [cluster for _, cluster in sorted(zip(centers, clusters), key=lambda t: t[0])]


def _compute_serpentine_led_order(
    positions,
    row_tolerance=None,
    first_dir="left",
    row_order="top",
    rows_count=None,
    flip_x=False,
    flip_y=False,
    swap_xy=False,
    mode="serpentine",
):
    if not positions:
        return None
    points = []
    for idx, pos in enumerate(positions):
        x = pos.get("x")
        y = pos.get("y")
        if x is None or y is None:
            return None
        fx = -float(x) if flip_x else float(x)
        fy = -float(y) if flip_y else float(y)
        if swap_xy:
            fx, fy = fy, fx
        points.append((idx, fx, fy))
    rows = []
    if rows_count is not None:
        try:
            k = int(rows_count)
        except Exception:
            k = None
        if k and k > 1:
            rows = _cluster_rows_by_y(points, k)
            # FIX-5: Apply requested top/bottom row order after row clustering.
            rows = sorted(
                rows,
                key=lambda row: sum(p[2] for p in row) / len(row) if row else 0.0,
                reverse=(row_order or "top").lower() == "bottom",
            )
    if not rows:
        ys = sorted({p[2] for p in points})
        if row_tolerance is None or row_tolerance <= 0:
            if len(ys) <= 1:
                tol = 1.0
            else:
                min_diff = None
                for i in range(1, len(ys)):
                    diff = ys[i] - ys[i - 1]
                    if diff > 0 and (min_diff is None or diff < min_diff):
                        min_diff = diff
                tol = (min_diff / 2.0) if min_diff else 1.0
        else:
            tol = float(row_tolerance)

        row_order_val = (row_order or "top").lower()
        if row_order_val == "bottom":
            points = sorted(points, key=lambda p: (-p[2], p[1]))
        else:
            points = sorted(points, key=lambda p: (p[2], p[1]))
        current = []
        current_y = None
        for point in points:
            if current_y is None:
                current_y = point[2]
                current = [point]
                continue
            if abs(point[2] - current_y) <= tol:
                current.append(point)
            else:
                rows.append(current)
                current = [point]
                current_y = point[2]
        if current:
            rows.append(current)

    first_dir = (first_dir or "left").lower()
    mode = (mode or "serpentine").lower()
    order = []
    for idx_row, row in enumerate(rows):
        if mode == "linear":
            reverse = first_dir == "right"
        else:
            if first_dir == "right":
                reverse = (idx_row % 2 == 0)
            else:
                reverse = (idx_row % 2 == 1)
        row_sorted = sorted(row, key=lambda p: p[1], reverse=reverse)
        order.extend([p[0] for p in row_sorted])
    return order


def _enum_to_int(value):
    if hasattr(value, "value"):
        return int(value.value)
    return int(value)


def parse_device_types(value, sdk):
    if not value:
        return []
    items = []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(",", "|").split("|")]
        items = [p for p in parts if p]
    elif isinstance(value, (list, tuple, set)):
        items = [str(p).strip() for p in value if str(p).strip()]
    else:
        return []

    out = []
    for name in items:
        if hasattr(sdk.CorsairDeviceType, name):
            out.append(_enum_to_int(getattr(sdk.CorsairDeviceType, name)))
        else:
            try:
                out.append(int(name))
            except Exception:
                pass
    return out


def parse_device_type_mask(value, sdk):
    if value is None:
        return getattr(sdk.CorsairDeviceType, "CDT_All", 0xFFFFFFFF)
    if isinstance(value, int):
        return value
    items = []
    if isinstance(value, str):
        s = value.strip()
        if "|" in s or "," in s:
            parts = [p.strip() for p in s.replace(",", "|").split("|")]
            items = [p for p in parts if p]
        else:
            if hasattr(sdk.CorsairDeviceType, s):
                return _enum_to_int(getattr(sdk.CorsairDeviceType, s))
            try:
                return int(s)
            except Exception:
                return getattr(sdk.CorsairDeviceType, "CDT_All", 0xFFFFFFFF)
    elif isinstance(value, (list, tuple, set)):
        items = [str(p).strip() for p in value if str(p).strip()]

    if items:
        mask = 0
        for name in items:
            if hasattr(sdk.CorsairDeviceType, name):
                mask |= _enum_to_int(getattr(sdk.CorsairDeviceType, name))
            else:
                try:
                    mask |= int(name)
                except Exception:
                    pass
        if mask:
            return mask

    return getattr(sdk.CorsairDeviceType, "CDT_All", 0xFFFFFFFF)


_bootstrap_tk_runtime()


APP_VERSION = "0.2.0"

_MODE_WINDOW = None
_MODE_EXIT = "__exit__"
_I18N = None


def _prompt_mode_cli(default_mode):
    default_mode = _normalize_mode(default_mode) or "unique"
    while True:
        try:
            raw = input(
                f"Choisir mode [1=unique, 2=groupe, 3=fusion] (defaut {default_mode}): "
            )
        except EOFError:
            return default_mode
        mode = _normalize_mode(raw)
        if mode:
            return mode
        if raw.strip() == "":
            return default_mode
        print("Choix invalide. Tape 1 (unique), 2 (groupe) ou 3 (fusion).")


class _ModeWindowThread:
    def __init__(self, default_mode, i18n_obj):
        self.default_mode = _normalize_mode(default_mode) or "unique"
        self.i18n = i18n_obj
        self.last_mode = self.default_mode
        self.available = False
        self.closed = False
        self._queue = queue.Queue()
        self._ui_queue = queue.Queue()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def _run(self):
        try:
            import tkinter as tk
            from tkinter import ttk
            from tkinter import messagebox
        except Exception:
            self.available = False
            self._ready.set()
            return
        try:
            t = self.i18n.t
            root = tk.Tk()
            root.title(t("window_title"))
            root.resizable(False, False)
            root.configure(bg="#1f1f22")
            self._notified_version = None

            def push_mode(mode):
                self.last_mode = mode
                while True:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        break
                self._queue.put(mode)
                status_label.config(text=t("mode_selected", mode=mode))

            def confirm_quit():
                return messagebox.askyesno(
                    t("confirm_close_title"),
                    t("confirm_close_body"),
                    parent=root,
                )

            def on_close():
                if not confirm_quit():
                    return
                self.closed = True
                try:
                    self._queue.put(_MODE_EXIT)
                except Exception:
                    pass
                root.destroy()

            def toggle_language():
                new_lang = "en" if self.i18n.lang == "fr" else "fr"
                self.i18n.set_lang(new_lang)
                apply_lang()

            def apply_lang():
                root.title(t("window_title"))
                title_label.config(text=t("app_title"))
                subtitle.config(
                    text=t("choose_mode", default_mode=self.default_mode)
                )
                status_label.config(
                    text=t("mode_selected", mode=self.last_mode)
                )
                btn_unique.config(text=t("mode_unique"))
                btn_group.config(text=t("mode_group"))
                btn_fusion.config(text=t("mode_fusion"))
                btn_lang.config(text=t("toggle_language"))
                btn_quit.config(text=t("quit"))

            def process_ui_queue():
                while True:
                    try:
                        item = self._ui_queue.get_nowait()
                    except queue.Empty:
                        item = None
                    if item is None:
                        break
                    if item.get("type") == "update":
                        version = item.get("version") or ""
                        url = item.get("url")
                        if version and self._notified_version != version:
                            self._notified_version = version
                            body = t("update_available_body", version=version)
                            if messagebox.askyesno(
                                t("update_available_title"),
                                body,
                                parent=root,
                            ):
                                if url:
                                    try:
                                        webbrowser.open(url)
                                    except Exception:
                                        pass
                if not self.closed:
                    root.after(300, process_ui_queue)

            root.protocol("WM_DELETE_WINDOW", on_close)
            root.after(300, process_ui_queue)

            style = ttk.Style()
            try:
                style.theme_use("clam")
            except Exception:
                pass
            style.configure(
                "App.TFrame",
                background="#1f1f22",
            )
            style.configure(
                "Title.TLabel",
                background="#1f1f22",
                foreground="#f2f2f2",
                font=("Segoe UI", 12, "bold"),
            )
            style.configure(
                "App.TLabel",
                background="#1f1f22",
                foreground="#cfcfcf",
                font=("Segoe UI", 9),
            )
            style.configure(
                "App.TButton",
                font=("Segoe UI", 10, "bold"),
                padding=6,
            )

            frame = ttk.Frame(root, padding=16, style="App.TFrame")
            frame.grid(row=0, column=0, sticky="nsew")
            title_label = ttk.Label(
                frame,
                text=t("app_title"),
                style="Title.TLabel",
            )
            title_label.grid(row=0, column=0, columnspan=3, pady=(0, 6))

            subtitle = ttk.Label(
                frame,
                text=t("choose_mode", default_mode=self.default_mode),
                style="App.TLabel",
            )
            subtitle.grid(row=1, column=0, columnspan=3, pady=(0, 8))

            ttk.Separator(frame, orient="horizontal").grid(
                row=2, column=0, columnspan=3, sticky="ew", pady=(0, 10)
            )

            btn_unique = ttk.Button(
                frame, text=t("mode_unique"), style="App.TButton", command=lambda: push_mode("unique")
            )
            btn_group = ttk.Button(
                frame, text=t("mode_group"), style="App.TButton", command=lambda: push_mode("group")
            )
            btn_fusion = ttk.Button(
                frame, text=t("mode_fusion"), style="App.TButton", command=lambda: push_mode("fusion")
            )
            btn_unique.grid(row=3, column=0, padx=6)
            btn_group.grid(row=3, column=1, padx=6)
            btn_fusion.grid(row=3, column=2, padx=6)

            status_label = ttk.Label(
                frame,
                text=t("mode_selected", mode=self.default_mode),
                style="App.TLabel",
            )
            status_label.grid(row=4, column=0, columnspan=3, pady=(10, 6))

            btn_lang = ttk.Button(
                frame, text=t("toggle_language"), style="App.TButton", command=toggle_language
            )
            btn_lang.grid(row=5, column=0, columnspan=2, pady=(10, 0), sticky="ew", padx=(0, 6))

            btn_quit = ttk.Button(frame, text=t("quit"), style="App.TButton", command=on_close)
            btn_quit.grid(row=5, column=2, pady=(10, 0), sticky="ew")

            self.available = True
            self._ready.set()
            root.mainloop()
            self.available = False
        except Exception:
            self.available = False
            self._ready.set()

    def wait_for_choice(self, timeout=None):
        if not self.available:
            if timeout is None:
                return self.default_mode
            return None
        try:
            if timeout is None:
                mode = self._queue.get()
            else:
                mode = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        except Exception:
            return self.last_mode or self.default_mode
        return mode or self.default_mode

    def poll(self):
        if not self.available:
            return None
        mode = None
        while True:
            try:
                mode = self._queue.get_nowait()
            except queue.Empty:
                break
        return mode

    def notify_update(self, version, url):
        if not self.available:
            return
        try:
            self._ui_queue.put({"type": "update", "version": version, "url": url})
        except Exception:
            pass


def prompt_mode(default_mode):
    default_mode = _normalize_mode(default_mode) or "unique"
    global _MODE_WINDOW, _I18N
    if _I18N is None:
        _I18N = i18n.get_i18n()
    if _MODE_WINDOW is None or not _MODE_WINDOW.available:
        window = _ModeWindowThread(default_mode, _I18N)
        if not window.available:
            _MODE_WINDOW = None
            return _prompt_mode_cli(default_mode)
        _MODE_WINDOW = window
    while True:
        mode = _MODE_WINDOW.wait_for_choice(timeout=0.15)
        if mode is not None:
            if mode == _MODE_EXIT:
                raise SystemExit(0)
            return mode


def choose_mode(cfg, args, prompt_allowed=True):
    if getattr(args, "mode", None):
        return _normalize_mode(args.mode) or "unique"
    default_mode = _normalize_mode(cfg.get("default_mode")) or "unique"
    if not cfg.get("startup_prompt", True) or not prompt_allowed:
        return default_mode
    return prompt_mode(default_mode)


class CueSdkWrapper:
    def __init__(self):
        try:
            from cuesdk import (
                CueSdk,
                CorsairDeviceFilter,
                CorsairDeviceType,
                CorsairAccessLevel,
                CorsairLedColor,
                CorsairError,
            )
            try:
                from cuesdk import CorsairSessionState
            except Exception:
                CorsairSessionState = None
        except Exception as exc:
            raise RuntimeError(
                "Le module Python 'cuesdk' est requis. Installe-le avec "
                "'py -3 -m pip install -U cuesdk'."
            ) from exc
        self.sdk = CueSdk()
        self.CorsairDeviceFilter = CorsairDeviceFilter
        self.CorsairDeviceType = CorsairDeviceType
        self.CorsairAccessLevel = CorsairAccessLevel
        self.CorsairLedColor = CorsairLedColor
        self.CorsairError = CorsairError
        self.CorsairSessionState = CorsairSessionState

    def _call(self, names, *args):
        for name in names:
            fn = getattr(self.sdk, name, None)
            if fn is not None:
                return fn(*args)
        raise RuntimeError(f"Methode SDK introuvable: {names}")

    def connect(self):
        def _on_state_changed(evt):
            _ = evt
        try:
            return self._call(("connect",), _on_state_changed)
        except TypeError:
            return self._call(("connect",))

    def disconnect(self):
        try:
            return self._call(("disconnect",))
        except Exception:
            return None

    def _control_levels(self, access_level=None):
        levels = []
        if access_level is not None:
            levels.append(access_level)
        else:
            if hasattr(self.CorsairAccessLevel, "CAL_ExclusiveLightingControl"):
                levels.append(self.CorsairAccessLevel.CAL_ExclusiveLightingControl)
            if hasattr(self.CorsairAccessLevel, "CAL_Shared"):
                levels.append(self.CorsairAccessLevel.CAL_Shared)
        deduped = []
        seen = set()
        for level in levels:
            if level is None:
                continue
            try:
                key = int(level)
            except Exception:
                key = level
            if key in seen:
                continue
            seen.add(key)
            deduped.append(level)
        return deduped

    def request_control(self, device_id=None, access_level=None):
        if not device_id:
            return None
        levels = self._control_levels(access_level)
        if not levels:
            return None
        last_err = None
        for name in ("request_control", "requestControl"):
            fn = getattr(self.sdk, name, None)
            if fn is None:
                continue
            for level in levels:
                try:
                    err = fn(device_id, level)
                except TypeError:
                    continue
                except Exception as exc:
                    last_err = exc
                    continue
                last_err = err
                if self.is_success(err):
                    return err
        return last_err

    def release_control(self, device_id=None):
        try:
            return self._call(("release_control", "releaseControl"), device_id)
        except TypeError:
            try:
                return self._call(("release_control", "releaseControl"))
            except Exception:
                return None
        except Exception:
            return None

    def get_devices(self, device_type_mask):
        filt = self.CorsairDeviceFilter(device_type_mask=device_type_mask)
        return self._call(("get_devices", "getDevices"), filt)

    def get_session_state(self):
        return self._call(("get_session_state", "getSessionState"))

    def get_device_info(self, device_id):
        return self._call(("get_device_info", "getDeviceInfo"), device_id)

    def get_led_positions(self, device_id):
        return self._call(("get_led_positions", "getLedPositions"), device_id)

    def set_layer_priority(self, priority):
        return self._call(("set_layer_priority", "setLayerPriority"), int(priority))

    def set_led_colors_buffer(self, device_id, colors):
        return self._call(
            ("set_led_colors_buffer", "setLedColorsBuffer"), device_id, colors
        )

    def set_led_colors(self, device_id, colors):
        return self._call(
            ("set_led_colors", "setLedColors", "set_leds_colors"), device_id, colors
        )

    def flush(self):
        for name in (
            "set_led_colors_flush_buffer_async",
            "setLedColorsFlushBufferAsync",
        ):
            fn = getattr(self.sdk, name, None)
            if fn is None:
                continue
            try:
                res = fn(None)
                return self.CorsairError.CE_Success if res is None else res
            except TypeError:
                try:
                    res = fn(None, None)
                    return self.CorsairError.CE_Success if res is None else res
                except Exception:
                    pass
            except Exception:
                return None
        try:
            res = self._call(
                (
                    "set_led_colors_flush_buffer",
                    "setLedColorsFlushBuffer",
                    "flush_led_colors_buffer",
                )
            )
            return self.CorsairError.CE_Success if res is None else res
        except Exception:
            return None

    def is_success(self, err):
        if err is None:
            return True
        try:
            return int(err) == int(self.CorsairError.CE_Success)
        except Exception:
            return False


class LedMapper:
    def __init__(self, sdk, device_type_mask):
        self.sdk = sdk
        self.device_type_mask = device_type_mask
        self.led_colors_by_device = {}
        self.positions_by_device = {}
        self.order_by_device = {}
        self.global_map = []
        self.total_leds = 0
        # FIX-FREEZE-GIL: Stage RGB bytes separately from CorsairLedColor objects.
        self._staging = {}
        self._staging_lock = threading.Lock()
        self.devices = []
        self.device_types_include = set()
        self.device_types_exclude = set()
        self.direct_async_timeout = 10.0
        self._direct_async_lock = threading.Lock()
        self._direct_async_event = threading.Event()
        self._direct_async_thread = None
        # FIX-FREEZE-1: Store pending device ids only; color copies run on the writer thread.
        self._direct_async_device_ids = None
        self._direct_async_inflight_since = 0.0
        self._direct_async_last_error = None
        try:
            self._success_code = int(self.sdk.CorsairError.CE_Success)
        except Exception:
            self._success_code = 0
        try:
            self._no_control_code = int(self.sdk.CorsairError.CE_NoControl)
        except Exception:
            self._no_control_code = 2
        self.debug_icue = False
        self.perf_debug = False
        self.perf_logger = None
        self.perf_threshold = 0.20
        self.perf_log_cooldown = 2.0
        self._perf_last_log = {}

    def set_debug(self, enabled):
        self.debug_icue = bool(enabled)

    def set_perf_debug(self, enabled, logger=None, threshold=0.20, cooldown=2.0):
        self.perf_debug = bool(enabled)
        self.perf_logger = logger
        try:
            self.perf_threshold = max(0.02, float(threshold))
        except Exception:
            self.perf_threshold = 0.20
        try:
            self.perf_log_cooldown = max(0.0, float(cooldown))
        except Exception:
            self.perf_log_cooldown = 2.0

    def _perf_warn(self, key, message, *args):
        if not self.perf_debug:
            return
        now = time.monotonic()
        last = self._perf_last_log.get(key, 0.0)
        if now - last < self.perf_log_cooldown:
            return
        self._perf_last_log[key] = now
        logger = self.perf_logger or logging.getLogger("bridge")
        logger.warning(message, *args)

    def _perf_apply_warn(self, update_mode, elapsed, device_ids, map_list, ok, err=None):
        if elapsed <= self.perf_threshold:
            return
        self._perf_warn(
            f"apply_{update_mode}",
            "[PERF] apply_frame_map %s lent %.3fs devices=%s leds=%s ok=%s err=%s",
            update_mode,
            elapsed,
            len(device_ids or []),
            len(map_list or []),
            ok,
            err,
        )

    def set_type_filters(self, include_types, exclude_types):
        self.device_types_include = set(include_types or [])
        self.device_types_exclude = set(exclude_types or [])

    def _get_device_type(self, info, dev):
        for obj in (info, dev):
            if obj is None:
                continue
            if hasattr(obj, "type"):
                return self._type_to_int(getattr(obj, "type"))
            if hasattr(obj, "device_type"):
                return self._type_to_int(getattr(obj, "device_type"))
        return None

    def _type_to_int(self, val):
        if val is None:
            return None
        try:
            if hasattr(val, "value"):
                return int(val.value)
            return int(val)
        except Exception:
            return None

    def _get_device_id(self, dev):
        return getattr(dev, "device_id", None) or getattr(dev, "id", None)

    def _get_led_id(self, pos):
        return getattr(pos, "id", None) or getattr(pos, "led_id", None)

    def _get_led_xy(self, pos):
        x = _get_attr(pos, ("cx", "x", "left"))
        y = _get_attr(pos, ("cy", "y", "top"))
        try:
            if x is not None:
                x = float(x)
            if y is not None:
                y = float(y)
        except Exception:
            return None, None
        return x, y

    def apply_serpentine(
        self,
        device_types,
        row_tolerance=None,
        first_dir="left",
        row_order="top",
        rows_count=None,
        flip_x=False,
        flip_y=False,
        swap_xy=False,
        mode="serpentine",
    ):
        if not device_types:
            return
        for dev in self.devices:
            dev_type = dev.get("device_type")
            if dev_type not in device_types:
                continue
            device_id = dev["device_id"]
            positions = self.positions_by_device.get(device_id)
            order = _compute_serpentine_led_order(
                positions,
                row_tolerance=row_tolerance,
                first_dir=first_dir,
                row_order=row_order,
                rows_count=rows_count,
                flip_x=flip_x,
                flip_y=flip_y,
                swap_xy=swap_xy,
                mode=mode,
            )
            if order:
                self.order_by_device[device_id] = order

    def apply_index_order(self, device_types, reverse=False):
        if not device_types:
            return
        for dev in self.devices:
            dev_type = dev.get("device_type")
            if dev_type not in device_types:
                continue
            device_id = dev["device_id"]
            colors = self.led_colors_by_device.get(device_id) or []
            order = list(range(len(colors)))
            if reverse:
                order.reverse()
            self.order_by_device[device_id] = order

    def apply_angle_order(
        self,
        device_types,
        start="left",
        direction="clockwise",
        flip_x=False,
        flip_y=False,
        swap_xy=False,
    ):
        if not device_types:
            return
        for dev in self.devices:
            dev_type = dev.get("device_type")
            if dev_type not in device_types:
                continue
            device_id = dev["device_id"]
            positions = self.positions_by_device.get(device_id)
            if not positions:
                continue
            pts = []
            for idx, pos in enumerate(positions):
                x = pos.get("x")
                y = pos.get("y")
                if x is None or y is None:
                    pts = []
                    break
                fx = -float(x) if flip_x else float(x)
                fy = -float(y) if flip_y else float(y)
                if swap_xy:
                    fx, fy = fy, fx
                pts.append((idx, fx, fy))
            if not pts:
                continue
            cx = sum(p[1] for p in pts) / len(pts)
            cy = sum(p[2] for p in pts) / len(pts)
            order = _angle_order(
                [(idx, x, y, cx, cy) for idx, x, y in pts], start, direction
            )
            if order:
                self.order_by_device[device_id] = order

    def apply_axis_order(self, device_types, axis="auto"):
        if not device_types:
            return
        axis = (axis or "auto").lower()
        for dev in self.devices:
            dev_type = dev.get("device_type")
            if dev_type not in device_types:
                continue
            device_id = dev["device_id"]
            positions = self.positions_by_device.get(device_id)
            if not positions:
                continue
            pts = []
            for idx, pos in enumerate(positions):
                x = pos.get("x")
                y = pos.get("y")
                if x is None or y is None:
                    pts = []
                    break
                pts.append((idx, float(x), float(y)))
            if not pts:
                continue
            xs = [p[1] for p in pts]
            ys = [p[2] for p in pts]
            rx = max(xs) - min(xs)
            ry = max(ys) - min(ys)
            chosen = axis
            if chosen == "auto":
                chosen = "x" if rx >= ry else "y"
            if chosen == "y":
                pts.sort(key=lambda p: (p[2], p[1]))
            else:
                pts.sort(key=lambda p: (p[1], p[2]))
            order = [p[0] for p in pts]
            if order:
                self.order_by_device[device_id] = order

    def apply_aio_cluster(
        self,
        device_types,
        cluster_count=3,
        group_sort="x",
        group_order=None,
        start="top",
        direction="clockwise",
        flip_x=False,
        flip_y=False,
        swap_xy=False,
        pump_first=False,
    ):
        if not device_types:
            return
        for dev in self.devices:
            dev_type = dev.get("device_type")
            if dev_type not in device_types:
                continue
            device_id = dev["device_id"]
            positions = self.positions_by_device.get(device_id)
            orders = _build_aio_cluster_orders(
                positions,
                cluster_count=cluster_count,
                group_sort=group_sort,
                group_order=group_order,
                start=start,
                direction=direction,
                flip_x=flip_x,
                flip_y=flip_y,
                swap_xy=swap_xy,
                pump_first=pump_first,
            )
            if not orders:
                continue
            order = []
            for chunk in orders:
                order.extend(chunk)
            if order:
                self.order_by_device[device_id] = order

    def apply_reverse_order(self, device_types):
        if not device_types:
            return
        for dev in self.devices:
            dev_type = dev.get("device_type")
            if dev_type not in device_types:
                continue
            device_id = dev["device_id"]
            colors = self.led_colors_by_device.get(device_id) or []
            order = self.order_by_device.get(device_id) or list(range(len(colors)))
            order = list(reversed(order))
            self.order_by_device[device_id] = order

    def apply_fan_ring(
        self,
        device_types,
        outer_leds=12,
        inner_leds=4,
        fan_count=None,
        start="top",
        direction="clockwise",
        group_sort="x",
        layout="sequential",
        group_order=None,
        ring_order="index",
        lock_to_first=False,
        inner_first=False,
        flip_x=False,
        flip_y=True,
        swap_xy=False,
    ):
        if not device_types:
            return
        leds_per_fan = int(outer_leds) + int(inner_leds)
        if leds_per_fan <= 0:
            return
        for dev in self.devices:
            dev_type = dev.get("device_type")
            if dev_type not in device_types:
                continue
            device_id = dev["device_id"]
            positions = self.positions_by_device.get(device_id)
            if not positions:
                continue

            total = len(positions)
            k = fan_count
            if k is None:
                if total % leds_per_fan == 0:
                    k = total // leds_per_fan
                else:
                    k = 1
            k = max(1, int(k))

            # Prepare points with normalized coords
            points = []
            for idx, pos in enumerate(positions):
                x = pos.get("x")
                y = pos.get("y")
                if x is None or y is None:
                    points = []
                    break
                nx = -float(x) if flip_x else float(x)
                ny = -float(y) if flip_y else float(y)
                if swap_xy:
                    nx, ny = ny, nx
                points.append((idx, nx, ny))

            layout = (layout or "sequential").lower()
            if layout == "auto":
                layout = "cluster" if points else "sequential"

            if layout == "sequential":
                groups = []
                for fan_idx in range(k):
                    start_i = fan_idx * leds_per_fan
                    end_i = min(total, start_i + leds_per_fan)
                    fan_indices = list(range(start_i, end_i))
                    groups.append(fan_indices)

                order_idx = _normalize_group_order(group_order, len(groups))
                if order_idx:
                    groups = [groups[i] for i in order_idx]

                order = []
                base_pattern = None
                for fan_indices in groups:
                    if points:
                        fan_points = [p for p in points if p[0] in fan_indices]
                        if lock_to_first and base_pattern is not None:
                            ring = [fan_indices[0] + i for i in base_pattern if fan_indices[0] + i in fan_indices]
                        else:
                            ring = _ring_order(
                                fan_points,
                                outer_leds,
                                inner_leds,
                                start,
                                direction,
                                inner_first,
                                ring_order,
                            )
                            if lock_to_first and ring:
                                base_pattern = [i - fan_indices[0] for i in ring]
                        if ring:
                            order.extend(ring)
                        else:
                            order.extend(fan_indices)
                    else:
                        order.extend(fan_indices)
                self.order_by_device[device_id] = order
                continue

            if not points:
                order = []
                for fan_idx in range(k):
                    start_i = fan_idx * leds_per_fan
                    end_i = min(total, start_i + leds_per_fan)
                    order.extend(list(range(start_i, end_i)))
                self.order_by_device[device_id] = order
                continue

            clusters = _kmeans_2d(points, k)
            cluster_info = []
            for c in clusters:
                if not c:
                    continue
                cx = sum(p[1] for p in c) / len(c)
                cy = sum(p[2] for p in c) / len(c)
                cluster_info.append((c, (cx, cy)))
            cluster_info.sort(key=lambda t: _cluster_sort_key(t[1], group_sort))
            order_idx = _normalize_group_order(group_order, len(cluster_info))
            if order_idx:
                cluster_info = [cluster_info[i] for i in order_idx]

            order = []
            for c, _center in cluster_info:
                ring = _ring_order(
                    c, outer_leds, inner_leds, start, direction, inner_first, ring_order
                )
                if ring:
                    order.extend(ring)
                else:
                    order.extend([p[0] for p in c])

            if order:
                self.order_by_device[device_id] = order

    def enumerate(self, clear_on_start):
        devices, err = self.sdk.get_devices(self.device_type_mask)
        if err != self.sdk.CorsairError.CE_Success:
            raise RuntimeError(f"get_devices erreur: {err}")

        for dev in devices:
            device_id = self._get_device_id(dev)
            if device_id is None:
                continue
            positions, perr = self.sdk.get_led_positions(device_id)
            if perr != self.sdk.CorsairError.CE_Success or not positions:
                continue
            info = None
            try:
                info, _ = self.sdk.get_device_info(device_id)
            except Exception:
                info = None
            model = _get_attr(info, ("model",))
            serial = _get_attr(info, ("serial",))
            dev_type = self._get_device_type(info, dev)
            if self.device_types_include and dev_type not in self.device_types_include:
                continue
            if self.device_types_exclude and dev_type in self.device_types_exclude:
                continue
            colors = []
            positions_list = []
            for pos in positions:
                led_id = self._get_led_id(pos)
                if led_id is None:
                    continue
                x, y = self._get_led_xy(pos)
                positions_list.append({"id": led_id, "x": x, "y": y})
                color = self._make_color(led_id, 0, 0, 0)
                colors.append(color)
            if not colors:
                continue
            self.led_colors_by_device[device_id] = colors
            # FIX-FREEZE-GIL: Prepare a plain RGB staging buffer for this device.
            self._staging[device_id] = bytearray(len(colors) * 3)
            self.positions_by_device[device_id] = positions_list
            for idx in range(len(colors)):
                self.global_map.append((device_id, idx))
            stats = self._compute_device_stats(positions_list)
            self.devices.append(
                {
                    "device_id": device_id,
                    "device_id_str": str(device_id),
                    "info": info,
                    "device_type": dev_type,
                    "leds": len(colors),
                    "model": model,
                    "serial": serial,
                    "center_x": stats.get("center_x"),
                    "center_y": stats.get("center_y"),
                    "min_x": stats.get("min_x"),
                    "max_x": stats.get("max_x"),
                    "min_y": stats.get("min_y"),
                    "max_y": stats.get("max_y"),
                }
            )

        self.total_leds = len(self.global_map)
        if self.total_leds == 0:
            raise RuntimeError("Aucun LED detecte via iCUE.")

        if clear_on_start:
            self.apply_frame(bytearray(self.total_leds * 3))

    def apply_frame(self, frame_bytes, lut=None):
        return self.apply_frame_map(
            self.global_map,
            list(self.led_colors_by_device.keys()),
            frame_bytes,
            lut=lut,
            update_mode="direct",
        )

    def _make_color(self, led_id, r, g, b):
        try:
            return self.sdk.CorsairLedColor(led_id, r, g, b, 255)
        except Exception:
            try:
                c = self.sdk.CorsairLedColor(led_id, r, g, b)
                if hasattr(c, "a"):
                    c.a = 255
                return c
            except Exception:
                return self.sdk.CorsairLedColor(led_id, r, g, b, 255)

    def _compute_device_stats(self, positions_list):
        xs = [p["x"] for p in positions_list if p.get("x") is not None]
        ys = [p["y"] for p in positions_list if p.get("y") is not None]
        if not xs or not ys:
            return {}
        return {
            "min_x": min(xs),
            "max_x": max(xs),
            "min_y": min(ys),
            "max_y": max(ys),
            "center_x": (min(xs) + max(xs)) / 2.0,
            "center_y": (min(ys) + max(ys)) / 2.0,
        }

    def get_device_order(self, device_id):
        order = self.order_by_device.get(device_id)
        colors = self.led_colors_by_device.get(device_id) or []
        if order and len(order) == len(colors):
            return order
        return list(range(len(colors)))

    def apply_frame_map(
        self, map_list, device_ids, frame_bytes, lut=None, update_mode="direct_async"
    ):
        if not map_list:
            return False
        if lut is None:
            lut = list(range(256))
        any_ok = False
        apply_start = time.monotonic()

        # FIX-FREEZE-GIL: Main thread writes only plain RGB bytes, never CorsairLedColor fields.
        with self._staging_lock:
            idx = 0
            for entry in map_list:
                wb = None
                src_index = None
                if len(entry) == 4:
                    device_id, led_idx, src_index, wb = entry
                elif len(entry) == 3:
                    device_id, led_idx, src_index = entry
                else:
                    device_id, led_idx = entry
                if src_index is None:
                    offset = idx
                    idx += 3
                else:
                    offset = int(src_index) * 3
                if offset + 2 >= len(frame_bytes):
                    r = g = b = 0
                else:
                    r = lut[frame_bytes[offset]]
                    g = lut[frame_bytes[offset + 1]]
                    b = lut[frame_bytes[offset + 2]]
                if wb:
                    r = int(max(0, min(255, r * wb[0])))
                    g = int(max(0, min(255, g * wb[1])))
                    b = int(max(0, min(255, b * wb[2])))
                staging = self._staging.get(device_id)
                if staging is None:
                    continue
                target_indices = (
                    list(led_idx) if isinstance(led_idx, (list, tuple)) else [led_idx]
                )
                rgb = bytes((r, g, b))
                led_count = len(staging) // 3
                for li in target_indices:
                    try:
                        led_pos = int(li)
                    except Exception:
                        continue
                    if led_pos < 0:
                        led_pos += led_count
                    dst = led_pos * 3
                    if 0 <= dst and dst + 3 <= len(staging):
                        staging[dst : dst + 3] = rgb

        update_mode = (update_mode or "direct_async").lower()
        if update_mode == "auto":
            update_mode = "direct_async"
        if update_mode == "buffer_safe":
            update_mode = "buffer"
        if update_mode == "direct_async":
            ok, err = self._enqueue_direct_async(device_ids)
            if self.debug_icue and not ok:
                print(f"iCUE direct_async echoue: {err}")
            self._perf_apply_warn(
                update_mode,
                time.monotonic() - apply_start,
                device_ids,
                map_list,
                ok,
                err,
            )
            return ok
        if update_mode == "direct":
            # FIX-FREEZE-GIL: Direct writes use staged snapshots, not shared color objects.
            payload = self._copy_device_colors(device_ids)
            for device_id, colors in payload.items():
                okd, errd = self._try_set_direct(device_id, colors)
                any_ok = any_ok or okd
                if self.debug_icue and not okd:
                    print(f"iCUE set_led_colors echoue pour {device_id}: {errd}")
            self._perf_apply_warn(
                update_mode,
                time.monotonic() - apply_start,
                device_ids,
                map_list,
                any_ok,
            )
            return any_ok

        if update_mode == "buffer":
            used_buffer = False
            buffer_success = False
            # FIX-FREEZE-GIL: Buffer writes use staged snapshots, not shared color objects.
            payload = self._copy_device_colors(device_ids)
            for device_id, colors in payload.items():
                okb, errb = self._try_set_buffer(device_id, colors)
                if okb:
                    used_buffer = True
                    buffer_success = True
                elif self.debug_icue:
                    print(f"iCUE set_led_colors_buffer echoue pour {device_id}: {errb}")
            if used_buffer:
                flush_err = self._err_from_result(self.sdk.flush())
                if self._is_success(flush_err):
                    any_ok = any_ok or buffer_success
                elif self.debug_icue:
                    print(f"iCUE flush buffer echoue: {flush_err}")
            self._perf_apply_warn(
                update_mode,
                time.monotonic() - apply_start,
                device_ids,
                map_list,
                any_ok,
            )
            return any_ok

        used_buffer = False
        buffer_success = False
        # FIX-FREEZE-GIL: Fallback writes use staged snapshots, not shared color objects.
        payload = self._copy_device_colors(device_ids)
        for device_id, colors in payload.items():
            okb, errb = self._try_set_buffer(device_id, colors)
            if okb:
                used_buffer = True
                buffer_success = True
            elif self.debug_icue:
                print(f"iCUE set_led_colors_buffer echoue pour {device_id}: {errb}")
        if used_buffer:
            flush_err = self._err_from_result(self.sdk.flush())
            if self._is_success(flush_err):
                any_ok = any_ok or buffer_success
            elif self.debug_icue:
                print(f"iCUE flush buffer echoue: {flush_err}")
        self._perf_apply_warn(
            update_mode,
            time.monotonic() - apply_start,
            device_ids,
            map_list,
            any_ok,
        )
        return any_ok

    def _err_from_result(self, result):
        if result is None:
            return None
        if isinstance(result, tuple):
            if len(result) == 2:
                return result[1]
            if len(result) == 1:
                return result[0]
        return result

    def _is_success(self, err):
        if err is None:
            return True
        try:
            return int(err) == self._success_code
        except Exception:
            return False

    def _is_no_control(self, err):
        if err is None:
            return False
        try:
            return int(err) == self._no_control_code
        except Exception:
            return False

    def _request_device_control(self, device_id):
        try:
            res = self.sdk.request_control(device_id)
        except Exception:
            return False, "exception"
        err = self._err_from_result(res)
        return self._is_success(err), err

    def _retry_after_control(self, device_id, colors, setter):
        okc, errc = self._request_device_control(device_id)
        if not okc:
            return False, errc
        try:
            res = setter(device_id, colors)
        except Exception:
            return False, "exception"
        err = self._err_from_result(res)
        return self._is_success(err), err

    def _copy_device_colors(self, device_ids):
        # DIAG-6: Time worker-side staging snapshot and CorsairLedColor copy.
        copy_total_start = time.monotonic()
        device_ids_list = list(device_ids or [])
        payload = {}
        total_leds = 0
        for device_id in device_ids_list:
            colors = self.led_colors_by_device.get(device_id)
            if not colors:
                continue
            # FIX-FREEZE-GIL: Snapshot staged RGB bytes under lock, then build SDK colors unlocked.
            with self._staging_lock:
                staging = self._staging.get(device_id)
                if staging is None:
                    continue
                snapshot = bytes(staging)
            if not snapshot:
                continue
            copied = []
            led_count = min(len(colors), len(snapshot) // 3)
            total_leds += led_count
            for idx in range(led_count):
                color = colors[idx]
                try:
                    led_id = color.id
                except Exception:
                    continue
                base = idx * 3
                r = snapshot[base]
                g = snapshot[base + 1]
                b = snapshot[base + 2]
                new_color = self._make_color(led_id, r, g, b)
                if hasattr(new_color, "a"):
                    new_color.a = 255
                copied.append(new_color)
            if copied:
                payload[device_id] = copied
        copy_total_elapsed = time.monotonic() - copy_total_start
        if copy_total_elapsed >= 0.005:
            logging.getLogger("bridge.timing").warning(
                "[WORKER] _copy_device_colors took %.1fms devices=%s leds=%s",
                copy_total_elapsed * 1000.0,
                len(device_ids_list),
                total_leds,
            )
        return payload

    def _ensure_direct_async_worker(self):
        with self._direct_async_lock:
            if (
                self._direct_async_thread is not None
                and self._direct_async_thread.is_alive()
            ):
                return
            self._direct_async_thread = threading.Thread(
                target=self._direct_async_loop,
                name="icue-direct-writer",
                daemon=True,
            )
            self._direct_async_thread.start()

    def _enqueue_direct_async(self, device_ids):
        # DIAG-8: Time main-thread direct async enqueue path.
        enqueue_start = time.monotonic()
        try:
            self._ensure_direct_async_worker()
            now = time.monotonic()
            with self._direct_async_lock:
                if self._direct_async_last_error:
                    err = self._direct_async_last_error
                    self._direct_async_last_error = None
                    return False, err
                if self._direct_async_inflight_since > 0:
                    elapsed = now - self._direct_async_inflight_since
                    # FIX-FREEZE-BLOCK-3: Coalesce busy async writes without waiting on iCUE.
                    if elapsed > self.perf_threshold:
                        self._perf_warn(
                            "direct_async_busy",
                            "[PERF] iCUE direct_async encore occupe %.3fs; la frame courante remplace la precedente",
                            elapsed,
                        )
                    if elapsed > self.direct_async_timeout:
                        return False, f"direct_async bloque depuis {elapsed:.1f}s"
                    self._direct_async_device_ids = device_ids
                    self._direct_async_event.set()
                    return True, None
                # FIX-FREEZE-1: Main thread only publishes ids and signals the worker.
                self._direct_async_device_ids = device_ids
                self._direct_async_event.set()
            return True, None
        finally:
            enqueue_elapsed = time.monotonic() - enqueue_start
            if enqueue_elapsed >= 0.002:
                logging.getLogger("bridge.timing").warning(
                    "[BLOCK] _enqueue_direct_async took %.1fms",
                    enqueue_elapsed * 1000.0,
                )

    def direct_async_elapsed(self):
        with self._direct_async_lock:
            if self._direct_async_inflight_since <= 0:
                return 0.0
            return time.monotonic() - self._direct_async_inflight_since

    def direct_async_blocked(self):
        elapsed = self.direct_async_elapsed()
        return elapsed > self.direct_async_timeout, elapsed

    def _direct_async_loop(self):
        while True:
            self._direct_async_event.wait()
            while True:
                with self._direct_async_lock:
                    device_ids = self._direct_async_device_ids
                    self._direct_async_device_ids = None
                    if device_ids is None:
                        self._direct_async_event.clear()
                        break
                # DIAG-11: Time full worker frame copy plus iCUE write.
                frame_start = time.monotonic()
                # FIX-FREEZE-1: Copy colors on the worker so UDP processing is not blocked.
                copy_start = time.monotonic()
                payload = self._copy_device_colors(device_ids)
                copy_elapsed = time.monotonic() - copy_start
                if copy_elapsed > self.perf_threshold:
                    self._perf_warn(
                        "direct_async_copy",
                        "[PERF] copie couleurs iCUE lente %.3fs devices=%s",
                        copy_elapsed,
                        len(device_ids or []),
                    )
                if not payload:
                    with self._direct_async_lock:
                        self._direct_async_last_error = "aucune couleur a envoyer"
                    continue
                with self._direct_async_lock:
                    self._direct_async_inflight_since = time.monotonic()
                    write_start = self._direct_async_inflight_since
                ok_any = False
                failures = []
                led_count = 0
                for device_id, colors in payload.items():
                    led_count += len(colors or [])
                    ok, err = self._try_set_direct(device_id, colors)
                    ok_any = ok_any or ok
                    if not ok:
                        failures.append(f"{device_id}:{err}")
                write_elapsed = time.monotonic() - write_start
                with self._direct_async_lock:
                    self._direct_async_inflight_since = 0.0
                    self._direct_async_last_error = (
                        None if ok_any else "; ".join(failures[:3])
                    )
                if write_elapsed > self.perf_threshold:
                    self._perf_warn(
                        "direct_async_write",
                        "[PERF] ecriture iCUE lente %.3fs devices=%s leds=%s ok=%s failures=%s",
                        write_elapsed,
                        len(payload),
                        led_count,
                        ok_any,
                        len(failures),
                    )
                frame_elapsed = time.monotonic() - frame_start
                if frame_elapsed >= 0.020:
                    logging.getLogger("bridge.timing").warning(
                        "[WORKER] frame: copy=%.1fms write=%.1fms total=%.1fms leds=%s",
                        copy_elapsed * 1000.0,
                        write_elapsed * 1000.0,
                        frame_elapsed * 1000.0,
                        led_count,
                    )
                # FIX-FREEZE-4: Soft-throttle successful writes to avoid saturating iCUE.
                if ok_any and write_elapsed < 0.004:
                    time.sleep(max(0.0, 0.004 - write_elapsed))

    def _try_set_buffer(self, device_id, colors):
        try:
            res = self.sdk.set_led_colors_buffer(device_id, colors)
        except Exception:
            return False, "exception"
        err = self._err_from_result(res)
        if self._is_no_control(err):
            return self._retry_after_control(
                device_id, colors, self.sdk.set_led_colors_buffer
            )
        return self._is_success(err), err

    def _try_set_direct(self, device_id, colors):
        # DIAG-7: Time worker-side iCUE set_led_colors call.
        set_start = time.monotonic()
        try:
            res = self.sdk.set_led_colors(device_id, colors)
        except Exception:
            set_elapsed = time.monotonic() - set_start
            if set_elapsed >= 0.010:
                logging.getLogger("bridge.timing").warning(
                    "[WORKER] set_led_colors(%s) took %.1fms",
                    device_id,
                    set_elapsed * 1000.0,
                )
            return False, "exception"
        set_elapsed = time.monotonic() - set_start
        if set_elapsed >= 0.010:
            logging.getLogger("bridge.timing").warning(
                "[WORKER] set_led_colors(%s) took %.1fms",
                device_id,
                set_elapsed * 1000.0,
            )
        err = self._err_from_result(res)
        if self._is_no_control(err):
            return self._retry_after_control(device_id, colors, self.sdk.set_led_colors)
        return self._is_success(err), err


def looks_like_ddp(data):
    if len(data) < 10:
        return False
    version = (data[0] & 0xC0) >> 6
    if version == 0:
        return False
    header_len = 14 if (data[0] & 0x10) else 10
    if len(data) < header_len:
        return False
    length = int.from_bytes(data[8:10], "big")
    if length == 0:
        return True
    payload_len = len(data) - header_len
    if length <= payload_len:
        return True
    # Certains emetteurs mettent le nombre de pixels au lieu d'octets.
    if length * 3 == payload_len:
        return True
    return False


def parse_ddp(data, frame_buffer):
    header_len = 14 if (data[0] & 0x10) else 10
    offset = int.from_bytes(data[4:8], "big")
    length = int.from_bytes(data[8:10], "big")
    payload = data[header_len:]
    if length and length * 3 == len(payload):
        length = len(payload)
    if length and len(payload) > length:
        payload = payload[:length]
    push = (data[0] & 0x01) != 0
    if offset < len(frame_buffer):
        end = min(len(frame_buffer), offset + len(payload))
        frame_buffer[offset:end] = payload[: end - offset]
    return push


def _clear_tail(frame_buffer, start):
    if start < len(frame_buffer):
        frame_buffer[start:] = b"\x00" * (len(frame_buffer) - start)


def parse_wled_or_raw(data, frame_buffer):
    if not data:
        return
    proto = data[0]
    if proto in (1, 2, 3, 4) and len(data) >= 2:
        if proto == 2 and (len(data) - 1) % 3 == 0:
            payload = data[1:]
            copy_len = min(len(frame_buffer), len(payload))
            frame_buffer[:copy_len] = payload[:copy_len]
            if len(payload) < len(frame_buffer):
                _clear_tail(frame_buffer, copy_len)
            return
        if len(data) < 2:
            return
        if proto == 1:
            payload = data[2:]
            for i in range(0, len(payload), 4):
                if i + 3 >= len(payload):
                    break
                idx = payload[i]
                offset = idx * 3
                if offset + 2 >= len(frame_buffer):
                    continue
                frame_buffer[offset] = payload[i + 1]
                frame_buffer[offset + 1] = payload[i + 2]
                frame_buffer[offset + 2] = payload[i + 3]
            return
        if proto == 2:
            payload = data[2:]
            copy_len = min(len(frame_buffer), len(payload))
            frame_buffer[:copy_len] = payload[:copy_len]
            if len(payload) < len(frame_buffer):
                _clear_tail(frame_buffer, copy_len)
            return
        if proto == 3:
            payload = data[2:]
            out = 0
            for i in range(0, len(payload), 4):
                if i + 2 >= len(payload) or out + 2 >= len(frame_buffer):
                    break
                frame_buffer[out] = payload[i]
                frame_buffer[out + 1] = payload[i + 1]
                frame_buffer[out + 2] = payload[i + 2]
                out += 3
            return
        if proto == 4:
            if len(data) < 4:
                return
            start = (data[2] << 8) | data[3]
            payload = data[4:]
            for i in range(0, len(payload), 3):
                idx = start + (i // 3)
                offset = idx * 3
                # FIX-4: Allow writes ending exactly at the frame buffer boundary.
                if i + 2 >= len(payload) or offset + 3 > len(frame_buffer):
                    break
                frame_buffer[offset] = payload[i]
                frame_buffer[offset + 1] = payload[i + 1]
                frame_buffer[offset + 2] = payload[i + 2]
            return

    if len(data) % 3 == 0:
        copy_len = min(len(frame_buffer), len(data))
        frame_buffer[:copy_len] = data[:copy_len]
        if len(data) < len(frame_buffer):
            _clear_tail(frame_buffer, copy_len)


def build_groups(cfg_groups, mapper, sdk, default_protocol, default_host, cfg=None):
    groups = []
    used_ids = set()
    if not cfg_groups:
        return groups
    mouse_types = set(parse_device_types(["CDT_Mouse"], sdk))
    mousemat_types = set(parse_device_types(["CDT_Mousemat"], sdk))
    cooler_types = set(parse_device_types(["CDT_Cooler"], sdk))
    pump_wb = parse_white_balance(cfg.get("aio_pump_white_balance")) if cfg else None
    for idx, grp in enumerate(cfg_groups):
        name = grp.get("name") or f"groupe_{idx+1}"
        keepalive_reapply_group = grp.get("keepalive_reapply")
        if keepalive_reapply_group is None and _normalize_name(name) in (
            "souris_tapis",
            "mouse_tapis",
            "mousemat_mouse",
        ):
            keepalive_reapply_group = False
        if "udp_port" not in grp:
            raise RuntimeError(f"Groupe '{name}' sans udp_port.")
        udp_port = int(grp["udp_port"])
        udp_host = grp.get("udp_host", default_host)
        protocol = normalize_protocol(grp.get("protocol"), default_protocol)

        device_ids = [s.lower() for s in _normalize_list(grp.get("device_ids"))]
        include_types = parse_device_types(grp.get("device_types_include"), sdk)
        exclude_types = parse_device_types(grp.get("device_types_exclude"), sdk)
        model_contains = _normalize_list(grp.get("model_contains") or grp.get("model"))
        serial_contains = _normalize_list(grp.get("serial_contains") or grp.get("serial"))

        selected = []
        for dev in mapper.devices:
            dev_id_str = str(dev.get("device_id_str") or dev.get("device_id")).lower()
            if device_ids and dev_id_str not in device_ids:
                continue
            if include_types and dev.get("device_type") not in include_types:
                continue
            if exclude_types and dev.get("device_type") in exclude_types:
                continue
            if model_contains and not _match_contains(dev.get("model"), model_contains):
                continue
            if serial_contains and not _match_contains(dev.get("serial"), serial_contains):
                continue
            selected.append(dev)

        sort_key = (grp.get("device_sort") or "").strip().lower()
        if cfg and cfg.get("ram_match_group_order", False):
            if _normalize_name(name) in ("ram", "memory"):
                sort_key = ""
            else:
                include_types_for_ram = parse_device_types(["CDT_MemoryModule"], sdk)
                if include_types_for_ram and include_types:
                    if any(t in include_types for t in include_types_for_ram):
                        sort_key = ""
        if sort_key:
            selected = sorted(selected, key=lambda d: _device_sort_key(d, sort_key))
        else:
            # For mixed mouse+mousemat groups, keep a deterministic order:
            # mousemat first, mouse last. This avoids stream index drift.
            has_mouse = any(dev.get("device_type") in mouse_types for dev in selected)
            has_mousemat = any(dev.get("device_type") in mousemat_types for dev in selected)
            if has_mouse and has_mousemat:
                mats = [dev for dev in selected if dev.get("device_type") in mousemat_types]
                mice = [dev for dev in selected if dev.get("device_type") in mouse_types]
                others = [
                    dev
                    for dev in selected
                    if dev.get("device_type") not in mousemat_types
                    and dev.get("device_type") not in mouse_types
                ]
                selected = mats + mice + others

        if not selected:
            print(f"Avertissement: groupe '{name}' sans appareil.")
            continue

        for dev in selected:
            dev_id_str = str(dev.get("device_id_str") or dev.get("device_id")).lower()
            if dev_id_str in used_ids:
                raise RuntimeError(
                    f"Appareil duplique entre groupes: {dev_id_str} ({name})"
                )
        for dev in selected:
            dev_id_str = str(dev.get("device_id_str") or dev.get("device_id")).lower()
            used_ids.add(dev_id_str)

        map_list = []
        device_ranges = {}
        device_ids_raw = []
        device_labels = []
        mouse_ids = set()
        mousemat_ids = []
        link_mouse_center = bool(grp.get("link_mouse_to_mousemat_center"))

        ram_layout = (grp.get("ram_group_layout") or cfg.get("ram_group_layout") if cfg else "").lower()
        is_ram_group = False
        if _normalize_name(name) in ("ram", "memory"):
            is_ram_group = True
        elif include_types:
            ram_types = set(parse_device_types(["CDT_MemoryModule"], sdk))
            if ram_types and all(t in ram_types for t in include_types):
                is_ram_group = True

        if is_ram_group and ram_layout == "rows":
            for dev in selected:
                device_id = dev["device_id"]
                device_ids_raw.append(device_id)
                label = dev.get("model") or dev.get("device_id_str") or str(device_id)
                device_labels.append(str(label))
            map_list = _build_ram_interleaved_map(selected, mapper)
            groups.append(
                {
                    "name": name,
                    "udp_host": udp_host,
                    "udp_port": udp_port,
                    "protocol": protocol,
                    "device_ids": device_ids_raw,
                    "map": map_list,
                    "led_count": len(map_list),
                    "device_labels": device_labels,
                    "update_mode": (
                        grp.get("update_mode")
                        or (cfg.get("icue_update_mode") if cfg else None)
                        or "direct_async"
                    ).lower(),
                    "keepalive_reapply": keepalive_reapply_group,
                    "idle_clear_disabled": bool(grp.get("idle_clear_disabled", False)),
                    "idle_clear_seconds": grp.get("idle_clear_seconds"),
                }
            )
            continue

        for dev in selected:
            device_id = dev["device_id"]
            device_ids_raw.append(device_id)
            colors = mapper.led_colors_by_device.get(device_id)
            if colors:
                order = None
                pump_split = False
                if cfg and dev.get("device_type") in cooler_types:
                    pump_split = bool(cfg.get("aio_pump_split")) or bool(
                        grp.get("pump_split")
                    )
                if pump_split:
                    start_side = cfg.get("aio_pump_angle_start") if cfg else "left"
                    orders = None
                    if cfg:
                        orders = _build_aio_cluster_orders(
                            mapper.positions_by_device.get(device_id),
                            cluster_count=cfg.get("aio_cluster_count", 3),
                            group_sort=cfg.get("aio_cluster_sort", "x"),
                            group_order=cfg.get("aio_cluster_order"),
                            start=cfg.get("aio_angle_start", "top"),
                            direction=cfg.get("aio_angle_direction", "clockwise"),
                            flip_x=cfg.get("aio_flip_x", False),
                            flip_y=cfg.get("aio_flip_y", False),
                            swap_xy=cfg.get("aio_swap_xy", False),
                            pump_first=True,
                        )
                    allowed_indices = orders[0] if orders else None
                    pairs = _build_pump_lr_pairs(
                        mapper.positions_by_device.get(device_id),
                        start=start_side or "left",
                        flip_x=cfg.get("aio_flip_x", False) if cfg else False,
                        flip_y=cfg.get("aio_flip_y", False) if cfg else False,
                        swap_xy=cfg.get("aio_swap_xy", False) if cfg else False,
                        allowed_indices=allowed_indices,
                    )
                    if pairs:
                        start_idx = len(map_list)
                        order = []
                        full_order = mapper.get_device_order(device_id)
                        allowed_set = set(allowed_indices) if allowed_indices else None
                        for pair in pairs:
                            if not pair:
                                continue
                            base_idx = len(map_list)
                            if pump_wb:
                                map_list.append((device_id, pair[0], None, pump_wb))
                            else:
                                map_list.append((device_id, pair[0]))
                            order.append(pair[0])
                            if len(pair) > 1:
                                if pump_wb:
                                    map_list.append(
                                        (device_id, pair[1], base_idx, pump_wb)
                                    )
                                else:
                                    map_list.append((device_id, pair[1], base_idx))
                                order.append(pair[1])
                        # Append remaining LEDs (ex: ventilos AIO) after the pump
                        for idx_led in full_order:
                            if allowed_set is not None and idx_led in allowed_set:
                                continue
                            map_list.append((device_id, idx_led))
                            order.append(idx_led)
                if order is None:
                    order = mapper.get_device_order(device_id)
                    start_idx = len(map_list)
                    for i in order:
                        map_list.append((device_id, i))
                if order:
                    # FIX-3: Store the real number of map entries added for this device.
                    device_ranges[device_id] = (start_idx, len(map_list) - start_idx)
            if dev.get("device_type") in mouse_types:
                mouse_ids.add(device_id)
            if dev.get("device_type") in mousemat_types:
                mousemat_ids.append(device_id)
            label = dev.get("model") or dev.get("device_id_str") or str(device_id)
            device_labels.append(str(label))

        if link_mouse_center and mousemat_ids and mouse_ids:
            mat_id = mousemat_ids[0]
            mat_range = device_ranges.get(mat_id)
            if mat_range:
                start_idx, length = mat_range
                if length > 0:
                    src_index = start_idx + (length // 2)
                    new_map = []
                    for entry in map_list:
                        device_id, led_idx = entry
                        if device_id in mouse_ids:
                            new_map.append((device_id, led_idx, src_index))
                        else:
                            new_map.append(entry)
                    map_list = new_map

        groups.append(
            {
                "name": name,
                "udp_host": udp_host,
                "udp_port": udp_port,
                "protocol": protocol,
                "device_ids": device_ids_raw,
                "map": map_list,
                "led_count": len(map_list),
                "device_labels": device_labels,
                "update_mode": (
                    grp.get("update_mode")
                    or (cfg.get("icue_update_mode") if cfg else None)
                    or "direct_async"
                ).lower(),
                "keepalive_reapply": keepalive_reapply_group,
                "idle_clear_disabled": bool(grp.get("idle_clear_disabled", False)),
                "idle_clear_seconds": grp.get("idle_clear_seconds"),
            }
        )
    return groups


def build_group_all(mapper, default_protocol, udp_host, group_port, cfg=None, sdk=None):
    device_labels = []
    for dev in mapper.devices:
        label = dev.get("model") or dev.get("device_id_str")
        if label:
            device_labels.append(str(label))
    ram_types = None
    ram_layout = None
    if cfg and sdk:
        ram_layout = (cfg.get("ram_group_layout") or "").lower()
        if ram_layout == "rows":
            ram_types = set(parse_device_types(["CDT_MemoryModule"], sdk))
    return [
        {
            "name": "groupe",
            "udp_host": udp_host,
            "udp_port": int(group_port),
            "protocol": default_protocol,
            "device_ids": list(mapper.led_colors_by_device.keys()),
            "map": _build_full_map_with_ram(mapper, ram_types=ram_types, ram_layout=ram_layout),
            "led_count": mapper.total_leds,
            "device_labels": device_labels,
            "update_mode": (
                cfg.get("icue_update_mode", "direct_async")
                if cfg
                else "direct_async"
            ).lower(),
        }
    ]


def build_group_fusion(cfg, mapper, sdk, default_protocol, udp_host, fusion_port):
    groups_cfg = cfg.get("groups") or []
    groups_by_name = {}
    for grp in groups_cfg:
        name = (grp.get("name") or "").strip().lower()
        if name:
            groups_by_name[name] = grp

    def pick_group(*names):
        for name in names:
            grp = groups_by_name.get(name)
            if grp is not None:
                return grp
        return None

    def select_devices(grp, force_types=None):
        device_ids = [s.lower() for s in _normalize_list(grp.get("device_ids"))] if grp else []
        include_types = parse_device_types(grp.get("device_types_include"), sdk) if grp else []
        if force_types:
            include_types = list(force_types)
        exclude_types = parse_device_types(grp.get("device_types_exclude"), sdk) if grp else []
        model_contains = _normalize_list(grp.get("model_contains") or grp.get("model")) if grp else []
        serial_contains = _normalize_list(grp.get("serial_contains") or grp.get("serial")) if grp else []
        selected = []
        for dev in mapper.devices:
            dev_id_str = str(dev.get("device_id_str") or dev.get("device_id")).lower()
            if device_ids and dev_id_str not in device_ids:
                continue
            if include_types and dev.get("device_type") not in include_types:
                continue
            if exclude_types and dev.get("device_type") in exclude_types:
                continue
            if model_contains and not _match_contains(dev.get("model"), model_contains):
                continue
            if serial_contains and not _match_contains(dev.get("serial"), serial_contains):
                continue
            selected.append(dev)
        sort_key = (grp.get("device_sort") or "").strip().lower() if grp else ""
        if sort_key:
            selected = sorted(selected, key=lambda d: _device_sort_key(d, sort_key))
        return selected

    def ram_axis_order(dev, prefer_axis="auto"):
        device_id = dev["device_id"]
        positions = mapper.positions_by_device.get(device_id) or []
        pts = []
        for idx, pos in enumerate(positions):
            x = pos.get("x")
            y = pos.get("y")
            if x is None or y is None:
                pts = []
                break
            pts.append((idx, float(x), float(y)))
        if not pts:
            return mapper.get_device_order(device_id)
        xs = [p[1] for p in pts]
        ys = [p[2] for p in pts]
        prefer_axis = (prefer_axis or "auto").lower()
        rx = max(xs) - min(xs)
        ry = max(ys) - min(ys)
        axis = prefer_axis
        if axis == "auto":
            axis = "x" if rx >= ry else "y"
        if axis == "x":
            pts.sort(key=lambda p: (p[1], p[2]))
        else:
            pts.sort(key=lambda p: (p[2], p[1]))
        return [p[0] for p in pts]

    keyboard_types = parse_device_types(["CDT_Keyboard"], sdk)
    mousemat_types = parse_device_types(["CDT_Mousemat"], sdk)
    mouse_types = parse_device_types(["CDT_Mouse"], sdk)
    ram_types = parse_device_types(["CDT_MemoryModule"], sdk)
    cooler_types = parse_device_types(["CDT_Cooler"], sdk)
    fan_types = parse_device_types(cfg.get("fan_device_types"), sdk)
    if not fan_types:
        fan_types = parse_device_types(["CDT_LedController", "CDT_Fan"], sdk)

    grp_keyboard = pick_group("clavier", "keyboard")
    grp_mousemat = pick_group("tapis", "mousemat")
    grp_mouse = pick_group("souris", "mouse")
    grp_mousemat_mouse = pick_group("souris_tapis", "mouse_tapis")
    grp_ventilos = pick_group("ventilos", "fans")
    grp_aio = pick_group("aio", "cooler")
    grp_ram = pick_group("ram", "memory")

    map_list = []
    device_ids_raw = []
    device_ids_set = set()
    device_labels = []
    label_set = set()
    used_leds = {}

    def ensure_device(device_id, label):
        if device_id not in device_ids_set:
            device_ids_set.add(device_id)
            device_ids_raw.append(device_id)
        if label and label not in label_set:
            label_set.add(label)
            device_labels.append(label)

    def add_device(device_id, label, indices, src_index=None, dedupe=False):
        ensure_device(device_id, label)
        used = used_leds.setdefault(device_id, set())
        if src_index is None:
            for i in indices:
                # FIX-7: Deduplicate against LEDs already recorded for this device.
                if dedupe:
                    if i in used:
                        continue
                    used.add(i)
                map_list.append((device_id, i))
                if not dedupe:
                    used.add(i)
        else:
            for i in indices:
                # FIX-7: Deduplicate against LEDs already recorded for this device.
                if dedupe:
                    if i in used:
                        continue
                    used.add(i)
                map_list.append((device_id, i, src_index))
                if not dedupe:
                    used.add(i)

    def add_devices(devices):
        for dev in devices:
            device_id = dev["device_id"]
            order = mapper.get_device_order(device_id)
            label = str(dev.get("model") or dev.get("device_id_str") or device_id)
            add_device(device_id, label, order)

    # 1) Clavier
    add_devices(select_devices(grp_keyboard, force_types=keyboard_types))

    # 2) Tapis (mousemat)
    link_mouse = False
    if grp_mousemat_mouse:
        link_mouse = bool(grp_mousemat_mouse.get("link_mouse_to_mousemat_center"))
    mousemat_devices = select_devices(grp_mousemat_mouse or grp_mousemat, force_types=mousemat_types)
    mousemat_start = len(map_list)
    mousemat_len = 0
    for dev in mousemat_devices:
        device_id = dev["device_id"]
        order = mapper.get_device_order(device_id)
        label = str(dev.get("model") or dev.get("device_id_str") or device_id)
        add_device(device_id, label, order)
        mousemat_len += len(order)

    # 3) Souris
    mouse_devices = select_devices(grp_mousemat_mouse or grp_mouse, force_types=mouse_types)
    src_index = None
    if link_mouse and mousemat_len > 0:
        src_index = mousemat_start + (mousemat_len // 2)
    for dev in mouse_devices:
        device_id = dev["device_id"]
        order = mapper.get_device_order(device_id)
        label = str(dev.get("model") or dev.get("device_id_str") or device_id)
        add_device(device_id, label, order, src_index=src_index)

    # 4) CPU cooler (pompe) + ventilos boitier avec insertion
    aio_devices = select_devices(grp_aio, force_types=cooler_types)
    aio_pump_orders = []
    aio_fan_orders = []
    aio_start = cfg.get("aio_angle_start", "top")
    aio_dir = cfg.get("aio_angle_direction", "clockwise")
    pump_start = cfg.get("aio_pump_angle_start") or aio_start
    pump_dir = cfg.get("aio_pump_angle_direction") or aio_dir
    for dev in aio_devices:
        device_id = dev["device_id"]
        positions = mapper.positions_by_device.get(device_id)
        orders_pump = _build_aio_cluster_orders(
            positions,
            cluster_count=cfg.get("aio_cluster_count", 3),
            group_sort=cfg.get("aio_cluster_sort", "x"),
            group_order=cfg.get("aio_cluster_order"),
            start=pump_start,
            direction=pump_dir,
            flip_x=cfg.get("aio_flip_x", False),
            flip_y=cfg.get("aio_flip_y", False),
            swap_xy=cfg.get("aio_swap_xy", False),
            pump_first=True,
        )
        orders_fans = _build_aio_cluster_orders(
            positions,
            cluster_count=cfg.get("aio_cluster_count", 3),
            group_sort=cfg.get("aio_cluster_sort", "x"),
            group_order=cfg.get("aio_cluster_order"),
            start=aio_start,
            direction=aio_dir,
            flip_x=cfg.get("aio_flip_x", False),
            flip_y=cfg.get("aio_flip_y", False),
            swap_xy=cfg.get("aio_swap_xy", False),
            pump_first=True,
        )
        orders = orders_pump or orders_fans
        if not orders:
            full_order = mapper.get_device_order(device_id)
            aio_pump_orders.append((dev, full_order, positions))
            continue
        aio_pump_orders.append((dev, (orders_pump or orders)[0], positions))
        fan_orders = orders_fans or orders
        if len(fan_orders) > 1:
            rest = []
            for chunk in fan_orders[1:]:
                rest.extend(chunk)
            if rest:
                aio_fan_orders.append((dev, rest))

    def add_aio_pump():
        pump_split = bool(cfg.get("aio_pump_split"))
        pump_wb = parse_white_balance(cfg.get("aio_pump_white_balance"))
        for dev, order, positions in aio_pump_orders:
            device_id = dev["device_id"]
            label = str(dev.get("model") or dev.get("device_id_str") or device_id)
            if pump_split:
                start_side = cfg.get("aio_pump_angle_start") or "left"
                pairs = _build_pump_lr_pairs(
                    positions,
                    start=start_side,
                    flip_x=cfg.get("aio_flip_x", False),
                    flip_y=cfg.get("aio_flip_y", False),
                    swap_xy=cfg.get("aio_swap_xy", False),
                    allowed_indices=(order if isinstance(order, (list, tuple)) else None),
                )
                if pairs:
                    ensure_device(device_id, label)
                    used = used_leds.setdefault(device_id, set())
                    for pair in pairs:
                        if not pair:
                            continue
                        base_idx = len(map_list)
                        if pump_wb:
                            map_list.append((device_id, pair[0], None, pump_wb))
                        else:
                            map_list.append((device_id, pair[0]))
                        # FIX-7: Record pump LEDs so later AIO fan chunks skip them.
                        used.add(pair[0])
                        if len(pair) > 1:
                            if pump_wb:
                                map_list.append(
                                    (device_id, pair[1], base_idx, pump_wb)
                                )
                            else:
                                map_list.append((device_id, pair[1], base_idx))
                            used.add(pair[1])
                    continue
            add_device(device_id, label, order)

    case_fan_devices = select_devices(grp_ventilos, force_types=fan_types)
    case_segments = []
    leds_per_fan = int(cfg.get("fan_outer_leds", 12)) + int(cfg.get("fan_inner_leds", 4))
    if leds_per_fan < 1:
        leds_per_fan = 0
    for dev in case_fan_devices:
        device_id = dev["device_id"]
        order = mapper.get_device_order(device_id)
        if leds_per_fan and order and len(order) >= leds_per_fan and len(order) % leds_per_fan == 0:
            for i in range(0, len(order), leds_per_fan):
                case_segments.append((dev, order[i : i + leds_per_fan]))
        else:
            case_segments.append((dev, order))

    insert_after = cfg.get("fusion_cpu_after_fan", 1)
    try:
        insert_after = int(insert_after)
    except Exception:
        insert_after = 1
    if insert_after < 0:
        insert_after = 0

    inserted = False
    count = 0
    for dev, segment in case_segments:
        if count == insert_after and not inserted:
            add_aio_pump()
            inserted = True
        device_id = dev["device_id"]
        label = str(dev.get("model") or dev.get("device_id_str") or device_id)
        add_device(device_id, label, segment)
        count += 1
    if not inserted:
        add_aio_pump()

    # 6) RAM (left->right)
    ram_devices = select_devices(grp_ram, force_types=ram_types)
    if ram_devices:
        if cfg.get("ram_match_group_order", False):
            ram_layout = (cfg.get("ram_group_layout") or "").lower()
            if ram_layout == "rows":
                for dev in ram_devices:
                    device_id = dev["device_id"]
                    label = str(dev.get("model") or dev.get("device_id_str") or device_id)
                    ensure_device(device_id, label)
                for device_id, led_idx in _build_ram_interleaved_map(ram_devices, mapper):
                    map_list.append((device_id, led_idx))
            else:
                for dev in ram_devices:
                    device_id = dev["device_id"]
                    order = mapper.get_device_order(device_id)
                    label = str(dev.get("model") or dev.get("device_id_str") or device_id)
                    add_device(device_id, label, order)
        else:
            ram_devices = sorted(
                ram_devices,
                key=lambda d: (
                    d.get("center_x") is None,
                    d.get("center_x", 0.0),
                    d.get("center_y", 0.0),
                ),
            )
            ram_mode = (cfg.get("fusion_ram_mode") or "sticks").lower()
            mirror = bool(cfg.get("fusion_ram_mirror", False))
            ram_axis = (cfg.get("fusion_ram_led_axis") or "auto").lower()
            if ram_mode == "rows":
                stick_orders = []
                for idx_dev, dev in enumerate(ram_devices):
                    order = ram_axis_order(dev, prefer_axis=ram_axis)
                    if mirror and (idx_dev % 2 == 1):
                        order = list(reversed(order))
                    stick_orders.append((dev, order))
                max_len = max((len(order) for _dev, order in stick_orders), default=0)
                for i in range(max_len):
                    for dev, order in stick_orders:
                        if i >= len(order):
                            continue
                        device_id = dev["device_id"]
                        label = str(dev.get("model") or dev.get("device_id_str") or device_id)
                        ensure_device(device_id, label)
                        map_list.append((device_id, order[i]))
            else:
                for idx_dev, dev in enumerate(ram_devices):
                    device_id = dev["device_id"]
                    order = ram_axis_order(dev, prefer_axis=ram_axis)
                    label = str(dev.get("model") or dev.get("device_id_str") or device_id)
                    add_device(device_id, label, order)

    # 7) Ventilos AIO (fin)
    for dev, order in aio_fan_orders:
        device_id = dev["device_id"]
        label = str(dev.get("model") or dev.get("device_id_str") or device_id)
        add_device(device_id, label, order, dedupe=True)

    if not map_list:
        raise RuntimeError("Mode fusion: aucun LED detecte.")

    return [
        {
            "name": "fusion",
            "udp_host": udp_host,
            "udp_port": int(fusion_port),
            "protocol": default_protocol,
            "device_ids": device_ids_raw,
            "map": map_list,
            "led_count": len(map_list),
            "device_labels": device_labels,
            "update_mode": (cfg.get("icue_update_mode", "direct_async") or "direct_async").lower(),
        }
    ]


def _build_full_map(mapper):
    map_list = []
    for device_id in mapper.led_colors_by_device.keys():
        order = mapper.get_device_order(device_id)
        for i in order:
            map_list.append((device_id, i))
    return map_list


def _build_ram_interleaved_map(devices, mapper):
    if not devices:
        return []
    stick_orders = []
    for dev in devices:
        device_id = dev["device_id"]
        order = mapper.get_device_order(device_id)
        stick_orders.append((dev, order))
    max_len = max((len(order) for _dev, order in stick_orders), default=0)
    map_list = []
    for i in range(max_len):
        for dev, order in stick_orders:
            if i >= len(order):
                continue
            map_list.append((dev["device_id"], order[i]))
    return map_list


def _build_full_map_with_ram(mapper, ram_types=None, ram_layout=None):
    if not ram_types or ram_layout != "rows":
        return _build_full_map(mapper)
    ram_devices = [dev for dev in mapper.devices if dev.get("device_type") in ram_types]
    if not ram_devices:
        return _build_full_map(mapper)
    ram_map = _build_ram_interleaved_map(ram_devices, mapper)
    ram_ids = {dev["device_id"] for dev in ram_devices}
    map_list = []
    inserted = False
    for dev in mapper.devices:
        device_id = dev["device_id"]
        if device_id in ram_ids:
            if not inserted:
                map_list.extend(ram_map)
                inserted = True
            continue
        order = mapper.get_device_order(device_id)
        for i in order:
            map_list.append((device_id, i))
    if not inserted:
        map_list.extend(ram_map)
    return map_list


def get_groups_for_mode(mode, cfg, mapper, sdk, default_protocol, args):
    if mode == "group":
        group_port = args.group_port or cfg.get("group_port", 34983)
        return build_group_all(
            mapper, default_protocol, cfg.get("udp_host", "0.0.0.0"), group_port, cfg=cfg, sdk=sdk
        )
    if mode == "fusion":
        fusion_port = args.fusion_port or cfg.get("fusion_port", 34984)
        return build_group_fusion(
            cfg,
            mapper,
            sdk,
            default_protocol,
            cfg.get("udp_host", "0.0.0.0"),
            fusion_port,
        )
    groups_cfg = cfg.get("groups") or []
    groups = build_groups(
        groups_cfg,
        mapper,
        sdk,
        default_protocol,
        cfg.get("udp_host", "0.0.0.0"),
        cfg=cfg,
    )
    if not groups:
        raise RuntimeError("Mode unique demande des groupes dans config.json.")
    return groups


def setup_runtime(groups):
    sel = selectors.DefaultSelector()
    bound = set()
    runtime_groups = []
    opened_groups = []
    try:
        for g in groups:
            if g.get("led_count", 0) <= 0:
                print(f"Avertissement: groupe '{g['name']}' sans LEDs, ignore.")
                continue
            host_port = (g["udp_host"], int(g["udp_port"]))
            if host_port in bound:
                raise RuntimeError(f"Port duplique: {host_port[0]}:{host_port[1]}")
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(host_port)
            sock.setblocking(False)
            g["sock"] = sock
            opened_groups.append(g)
            g["frame_buffer"] = bytearray(g["led_count"] * 3)
            now = time.monotonic()
            g["last_send"] = now
            g["first_packet"] = True
            g["pkt_count"] = 0
            g["byte_count"] = 0
            g["stats_ts"] = now
            g["last_packet_ts"] = 0.0
            g["fail_count"] = 0
            g["perf_last_udp_gap_warn"] = 0.0
            g["perf_last_apply_warn"] = 0.0
            g["perf_max_udp_gap"] = 0.0
            g["perf_max_apply_elapsed"] = 0.0
            g["idle_cleared"] = False
            g["idle_clear_disabled"] = bool(g.get("idle_clear_disabled", False))
            idle_clear_seconds = g.get("idle_clear_seconds")
            try:
                g["idle_clear_seconds"] = (
                    max(0.2, float(idle_clear_seconds))
                    if idle_clear_seconds is not None
                    else None
                )
            except Exception:
                g["idle_clear_seconds"] = None
            g["keepalive_interval"] = g.get("keepalive_interval")
            g["last_keepalive"] = now
            sel.register(sock, selectors.EVENT_READ, data=g)
            bound.add(host_port)
            runtime_groups.append(g)
    except Exception:
        for g in opened_groups:
            sock = g.get("sock")
            if not sock:
                continue
            try:
                sel.unregister(sock)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
            g["sock"] = None
        raise
    return sel, runtime_groups


def close_runtime(sel, runtime_groups):
    for g in runtime_groups:
        sock = g.get("sock")
        if sock:
            try:
                sel.unregister(sock)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
            g["sock"] = None


def wait_for_icue(sdk, device_type_mask, timeout_s=8.0):
    start = time.time()
    last_err = None
    while time.time() - start < timeout_s:
        try:
            if sdk.CorsairSessionState is not None:
                state, err = sdk.get_session_state()
                last_err = err
                if err == sdk.CorsairError.CE_Success and hasattr(
                    sdk.CorsairSessionState, "CSS_Connected"
                ):
                    if state == sdk.CorsairSessionState.CSS_Connected:
                        return True, last_err
        except Exception:
            pass

        try:
            _, err = sdk.get_devices(device_type_mask)
            last_err = err
            if err == sdk.CorsairError.CE_Success:
                return True, last_err
        except Exception:
            pass

        time.sleep(0.2)
    return False, last_err


def is_icue_connected(sdk, device_type_mask):
    # DIAG-4: Time watchdog SDK connectivity checks.
    timing_logger = logging.getLogger("bridge.timing")
    check_start = time.monotonic()

    def finish(result):
        elapsed = time.monotonic() - check_start
        timing_logger.warning(
            "[BLOCK] is_icue_connected took %.1fms result=%s",
            elapsed * 1000.0,
            result,
        )
        return result

    try:
        if sdk.CorsairSessionState is not None:
            state, err = sdk.get_session_state()
            if err == sdk.CorsairError.CE_Success and hasattr(
                sdk.CorsairSessionState, "CSS_Connected"
            ):
                return finish(state == sdk.CorsairSessionState.CSS_Connected)
    except Exception:
        pass
    try:
        _, err = sdk.get_devices(device_type_mask)
        return finish(err == sdk.CorsairError.CE_Success)
    except Exception:
        return finish(False)


def request_control_for_devices(sdk, mapper=None, device_ids=None, logger=None, context="request_control"):
    # DIAG-5: Time request_control batch and each per-device SDK call.
    timing_logger = logging.getLogger("bridge.timing")
    request_total_start = time.monotonic()
    targets = []
    seen = set()
    raw_ids = device_ids
    if raw_ids is None and mapper is not None:
        raw_ids = [dev.get("device_id") for dev in mapper.devices]
    for device_id in raw_ids or []:
        if not device_id or device_id in seen:
            continue
        seen.add(device_id)
        targets.append(device_id)

    failures = []
    ok_any = False
    if not targets:
        request_total_elapsed = time.monotonic() - request_total_start
        if request_total_elapsed >= 0.002:
            timing_logger.warning(
                "[BLOCK] request_control_for_devices took %.1fms devices=%s ok=%s",
                request_total_elapsed * 1000.0,
                len(targets),
                True,
            )
        return True

    for device_id in targets:
        request_start = time.monotonic()
        try:
            err = sdk.request_control(device_id)
        except Exception as exc:
            request_elapsed = time.monotonic() - request_start
            if request_elapsed >= 0.005:
                timing_logger.warning(
                    "[BLOCK] request_control(%s) took %.1fms",
                    device_id,
                    request_elapsed * 1000.0,
                )
            failures.append(f"{device_id}:{exc}")
            continue
        request_elapsed = time.monotonic() - request_start
        if request_elapsed >= 0.005:
            timing_logger.warning(
                "[BLOCK] request_control(%s) took %.1fms",
                device_id,
                request_elapsed * 1000.0,
            )
        if sdk.is_success(err):
            ok_any = True
        else:
            failures.append(f"{device_id}:{err}")

    if failures and logger is not None:
        sample = "; ".join(failures[:3])
        if len(failures) > 3:
            sample += f"; +{len(failures) - 3} autres"
        logger.warning("iCUE %s echoue sur %s appareil(s): %s", context, len(failures), sample)
    request_total_elapsed = time.monotonic() - request_total_start
    if request_total_elapsed >= 0.002:
        timing_logger.warning(
            "[BLOCK] request_control_for_devices took %.1fms devices=%s ok=%s",
            request_total_elapsed * 1000.0,
            len(targets),
            ok_any,
        )
    return ok_any


def build_mapper(cfg, sdk, device_type_mask, args):
    mapper = LedMapper(sdk, device_type_mask)
    try:
        mapper.direct_async_timeout = max(
            1.0, float(cfg.get("icue_direct_async_timeout", 10.0))
        )
    except Exception:
        mapper.direct_async_timeout = 10.0
    include_types = parse_device_types(cfg.get("device_types_include"), sdk)
    exclude_types = parse_device_types(cfg.get("device_types_exclude"), sdk)
    mapper.set_type_filters(include_types, exclude_types)
    mapper.set_debug(getattr(args, "debug_icue", False))
    perf_debug, perf_threshold, perf_log_cooldown = get_perf_options(cfg, args)
    mapper.set_perf_debug(
        perf_debug,
        logger=logging.getLogger("bridge"),
        threshold=perf_threshold,
        cooldown=perf_log_cooldown,
    )
    mapper.enumerate(cfg.get("clear_on_start", True))

    if cfg.get("keyboard_serpentine"):
        keyboard_types = parse_device_types(["CDT_Keyboard"], sdk)
        mapper.apply_serpentine(
            keyboard_types,
            row_tolerance=cfg.get("keyboard_serpentine_row_tolerance"),
            first_dir=cfg.get("keyboard_serpentine_first_dir", "left"),
            row_order=cfg.get("keyboard_serpentine_row_order", "top"),
            rows_count=cfg.get("keyboard_serpentine_rows"),
            flip_x=cfg.get("keyboard_serpentine_flip_x", False),
            flip_y=cfg.get("keyboard_serpentine_flip_y", False),
            swap_xy=cfg.get("keyboard_serpentine_swap_xy", False),
            mode=cfg.get("keyboard_serpentine_mode", "serpentine"),
        )
    ram_types = parse_device_types(["CDT_MemoryModule"], sdk)
    if cfg.get("ram_serpentine"):
        mapper.apply_serpentine(
            ram_types,
            row_tolerance=cfg.get("ram_serpentine_row_tolerance"),
            first_dir=cfg.get("ram_serpentine_first_dir", "left"),
            row_order=cfg.get("ram_serpentine_row_order", "bottom"),
            rows_count=cfg.get("ram_serpentine_rows"),
            flip_x=cfg.get("ram_serpentine_flip_x", False),
            flip_y=cfg.get("ram_serpentine_flip_y", False),
            swap_xy=cfg.get("ram_serpentine_swap_xy", False),
            mode=cfg.get("ram_serpentine_mode", "linear"),
        )
    ram_axis = (cfg.get("ram_order_axis") or "").lower()
    if ram_axis in ("auto", "x", "y"):
        mapper.apply_axis_order(ram_types, axis=ram_axis)
    if cfg.get("fan_ring"):
        fan_types = parse_device_types(cfg.get("fan_device_types"), sdk)
        mapper.apply_fan_ring(
            fan_types,
            outer_leds=cfg.get("fan_outer_leds", 12),
            inner_leds=cfg.get("fan_inner_leds", 4),
            fan_count=cfg.get("fan_count"),
            start=cfg.get("fan_start", "top"),
            direction=cfg.get("fan_direction", "clockwise"),
            group_sort=cfg.get("fan_group_sort", "x"),
            layout=cfg.get("fan_layout", "sequential"),
            group_order=cfg.get("fan_group_order"),
            ring_order=cfg.get("fan_ring_order", "index"),
            lock_to_first=cfg.get("fan_lock_to_first", False),
            inner_first=cfg.get("fan_inner_first", False),
            flip_x=cfg.get("fan_flip_x", False),
            flip_y=cfg.get("fan_flip_y", True),
            swap_xy=cfg.get("fan_swap_xy", False),
        )
    if cfg.get("aio_cluster"):
        aio_types = parse_device_types(["CDT_Cooler"], sdk)
        mapper.apply_aio_cluster(
            aio_types,
            cluster_count=cfg.get("aio_cluster_count", 3),
            group_sort=cfg.get("aio_cluster_sort", "x"),
            group_order=cfg.get("aio_cluster_order"),
            start=cfg.get("aio_angle_start", "top"),
            direction=cfg.get("aio_angle_direction", "clockwise"),
            flip_x=cfg.get("aio_flip_x", False),
            flip_y=cfg.get("aio_flip_y", False),
            swap_xy=cfg.get("aio_swap_xy", False),
            pump_first=cfg.get("aio_pump_first", False),
        )
    mat_types = parse_device_types(["CDT_Mousemat"], sdk)
    mousemat_mode = (cfg.get("mousemat_order_mode") or "serpentine").lower()
    if mousemat_mode == "index":
        mapper.apply_index_order(
            mat_types, reverse=cfg.get("mousemat_reverse", False)
        )
    elif mousemat_mode == "angle":
        mapper.apply_angle_order(
            mat_types,
            start=cfg.get("mousemat_angle_start", "left"),
            direction=cfg.get("mousemat_angle_direction", "clockwise"),
            flip_x=cfg.get("mousemat_serpentine_flip_x", False),
            flip_y=cfg.get("mousemat_serpentine_flip_y", False),
            swap_xy=cfg.get("mousemat_serpentine_swap_xy", False),
        )
        if cfg.get("mousemat_reverse", False):
            mapper.apply_reverse_order(mat_types)
    elif cfg.get("mousemat_serpentine"):
        mapper.apply_serpentine(
            mat_types,
            row_tolerance=cfg.get("mousemat_serpentine_row_tolerance"),
            first_dir=cfg.get("mousemat_serpentine_first_dir", "left"),
            row_order=cfg.get("mousemat_serpentine_row_order", "top"),
            rows_count=cfg.get("mousemat_serpentine_rows", 1),
            flip_x=cfg.get("mousemat_serpentine_flip_x", False),
            flip_y=cfg.get("mousemat_serpentine_flip_y", False),
            swap_xy=cfg.get("mousemat_serpentine_swap_xy", False),
            mode=cfg.get("mousemat_serpentine_mode", "linear"),
        )
        if cfg.get("mousemat_reverse", False):
            mapper.apply_reverse_order(mat_types)

    return mapper


def run_bridge(args):
    cfg_path = _resolve_config_path(args.config)
    try:
        cfg = load_config(cfg_path)
    except FileNotFoundError:
        print(
            f"Config introuvable: {cfg_path}. Copie config.example.json en config.json."
        )
        return 1
    global _I18N
    _I18N = i18n.get_i18n(cfg)
    logger = setup_logging(cfg)
    timing_logger = _configure_timing_logger()
    logger.info("Version: %s", APP_VERSION)
    perf_debug, perf_threshold, perf_log_cooldown = get_perf_options(cfg, args)
    if perf_debug:
        logger.info(
            "[PERF] Diagnostic actif: seuil=%.3fs cooldown=%.1fs",
            perf_threshold,
            perf_log_cooldown,
        )
    process_priority = str(cfg.get("process_priority", "high")).lower()
    try:
        if process_priority in ("normal", "high", "realtime"):
            if sys.platform == "win32":
                import ctypes

                priority_map = {
                    "normal": 0x00000020,   # NORMAL_PRIORITY_CLASS
                    "high": 0x00000080,     # HIGH_PRIORITY_CLASS
                    "realtime": 0x00000100, # REALTIME_PRIORITY_CLASS
                }
                priority_class = priority_map.get(process_priority)
                if priority_class is not None:
                    ctypes.windll.kernel32.SetPriorityClass(
                        ctypes.windll.kernel32.GetCurrentProcess(),
                        priority_class,
                    )
            else:
                if process_priority == "high":
                    os.nice(-10)
                elif process_priority == "realtime":
                    os.nice(-20)
    except Exception:
        pass
    cpu_affinity_core = cfg.get("cpu_affinity_core", -1)
    if cpu_affinity_core is not None:
        if not _HAS_PSUTIL:
            logger.warning("Affinite CPU demandee mais psutil indisponible.")
        else:
            try:
                p = psutil.Process()
                cpu_count = psutil.cpu_count()
                if not cpu_count:
                    raise RuntimeError("Nombre de coeurs indisponible.")
                all_cpus = list(range(cpu_count))
                try:
                    core_index = int(cpu_affinity_core)
                except Exception:
                    raise ValueError("cpu_affinity_core invalide.")
                if core_index == -1:
                    if len(all_cpus) < 4:
                        logger.info(
                            "Affinite CPU ignoree (moins de 4 coeurs disponibles)."
                        )
                    else:
                        dedicated_core = [all_cpus[-1]]
                        p.cpu_affinity(dedicated_core)
                        logger.info("Affinite CPU definie sur coeur %s", dedicated_core)
                else:
                    if core_index < 0 or core_index >= len(all_cpus):
                        raise ValueError(
                            f"cpu_affinity_core invalide (0..{len(all_cpus) - 1})."
                        )
                    dedicated_core = [core_index]
                    p.cpu_affinity(dedicated_core)
                    logger.info("Affinite CPU definie sur coeur %s", dedicated_core)
            except Exception as exc:
                logger.warning("Impossible de definir l'affinite CPU : %s", exc)

    update_enabled = bool(cfg.get("update_check_enabled", True))
    update_interval = float(cfg.get("update_check_interval_seconds", 3600))
    update_repo = str(cfg.get("update_repo") or "").strip()
    if update_interval < 10:
        update_interval = 10
    update_last_check = time.monotonic() - update_interval
    update_lock = threading.Lock()
    update_state = {"inflight": False, "pending": None, "last_notified": None}

    def is_repo_configured(value):
        return bool(value and "/" in value and value.lower() != "owner/repo")

    def start_update_check():
        if not update_enabled or not is_repo_configured(update_repo):
            return
        with update_lock:
            if update_state["inflight"]:
                return
            update_state["inflight"] = True

        def worker():
            try:
                info = _fetch_latest_release(update_repo)
                latest = info.get("version")
                if latest and _is_newer_version(latest, APP_VERSION):
                    with update_lock:
                        update_state["pending"] = info
            except Exception:
                pass
            finally:
                with update_lock:
                    update_state["inflight"] = False

        threading.Thread(target=worker, daemon=True).start()

    device_type_mask = None
    sdk = CueSdkWrapper()
    device_type_mask = parse_device_type_mask(cfg.get("device_type_mask"), sdk)
    reconnect_timeout = max(0.5, float(cfg.get("icue_reconnect_timeout", 8.0)))

    logger.info("Connexion iCUE SDK...")
    err = sdk.connect()
    if err != sdk.CorsairError.CE_Success:
        logger.error("Connexion iCUE SDK echouee: %s", err)
        print(f"Connexion iCUE SDK echouee: {err}")
        return 1
    ok, last_err = wait_for_icue(sdk, device_type_mask, timeout_s=reconnect_timeout)
    if not ok:
        msg = "iCUE non connecte (CE_NotConnected)"
        if last_err is not None and last_err != sdk.CorsairError.CE_Success:
            msg = f"iCUE non connecte: {last_err}"
        logger.error(msg)
        print(msg)
        print("Verifie que iCUE est lance, que le SDK est active,")
        print("et que le script est lance avec les memes droits (admin ou non).")
        return 1
    logger.info("iCUE connecte.")
    try:
        layer_priority = int(cfg.get("icue_layer_priority", 255))
        layer_priority = max(0, min(255, layer_priority))
        layer_err = sdk.set_layer_priority(layer_priority)
        if not sdk.is_success(layer_err):
            logger.warning("Priorite couche iCUE non appliquee: %s", layer_err)
        else:
            logger.info("Priorite couche iCUE definie sur %s.", layer_priority)
    except Exception as exc:
        logger.warning("Impossible de definir la priorite couche iCUE: %s", exc)

    mapper = build_mapper(cfg, sdk, device_type_mask, args)
    request_control_for_devices(
        sdk, mapper=mapper, logger=logger, context="startup_request_control"
    )
    default_protocol = normalize_protocol(cfg.get("protocol"), "drgb")
    mode = None
    groups = []
    sel = None
    runtime_groups = []

    def setup_runtime_with_retry(next_groups, context):
        delays = (0.0, 0.05, 0.2, 0.5)
        for attempt, delay in enumerate(delays, start=1):
            if delay > 0:
                time.sleep(delay)
            try:
                return setup_runtime(next_groups)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 10048 and attempt < len(delays):
                    logger.warning(
                        "%s: ports UDP encore occupes, tentative %s/%s",
                        context,
                        attempt + 1,
                        len(delays),
                    )
                    continue
                raise

    if args.list_devices:
        print(f"LED detectes: {mapper.total_leds}")
        logger.info("LED detectes: %s", mapper.total_leds)
        for dev in mapper.devices:
            info = dev.get("info")
            dtype = _get_attr(info, ("type", "device_type"))
            model = dev.get("model")
            serial = dev.get("serial")
            led_count = dev.get("leds")
            print(
                f"- id={dev['device_id_str']} leds={led_count} type={dtype} model={model} serial={serial}"
            )
        return 0

    lut = build_lut(cfg.get("brightness", 1.0), cfg.get("gamma", 1.0))
    if args.test or args.test_color:
        color = (255, 0, 0)
        if args.test_color:
            parsed = parse_rgb(args.test_color)
            if parsed is None:
                print("Couleur invalide. Exemple: --test-color 255,0,0")
                return 1
            color = parsed
        frame = bytearray(mapper.total_leds * 3)
        for i in range(0, len(frame), 3):
            frame[i] = color[0]
            frame[i + 1] = color[1]
            frame[i + 2] = color[2]
        mapper.apply_frame(frame, lut=lut)
        print(f"Test LEDs applique: {color[0]},{color[1]},{color[2]}")
        logger.info("Test LEDs applique: %s,%s,%s", color[0], color[1], color[2])
        return 0

    max_fps = float(cfg.get("max_fps", 60))
    min_interval = 1.0 / max_fps if max_fps > 0 else 0
    prompt_allowed = not (
        args.list_devices
        or args.list_groups
        or args.test
        or args.test_color
        or args.fan_sweep
        or args.fan_on
    )
    mode = choose_mode(cfg, args, prompt_allowed=prompt_allowed)

    try:
        groups = get_groups_for_mode(mode, cfg, mapper, sdk, default_protocol, args)
    except RuntimeError as exc:
        print(str(exc))
        print("Ajoute `groups` ou passe en mode groupe.")
        return 1

    if args.list_groups:
        for g in groups:
            labels = ", ".join(g.get("device_labels") or [])
            print(
                f"- {g['name']} port={g['udp_port']} leds={g['led_count']} proto={g['protocol']} devices={labels}"
            )
        return 0

    if args.fan_sweep or args.fan_on:
        target_name = str(args.fan_group or "ventilos").lower()
        target = None
        for g in groups:
            if str(g.get("name", "")).lower() == target_name:
                target = g
                break
        if target is None:
            print(f"Groupe '{target_name}' introuvable. Utilise --mode unique.")
            return 1
        leds_per_fan = int(cfg.get("fan_outer_leds", 12)) + int(cfg.get("fan_inner_leds", 4))
        if leds_per_fan <= 0:
            print("fan_outer_leds + fan_inner_leds invalide.")
            return 1
        total_leds = target.get("led_count", 0)
        if total_leds <= 0:
            print("Aucun LED detecte pour le groupe ventilos.")
            return 1
        if total_leds % leds_per_fan != 0:
            print(f"LEDs non multiple de {leds_per_fan} (total={total_leds}).")
        fan_count = int(cfg.get("fan_count") or max(1, total_leds // leds_per_fan))
        if args.fan_sweep:
            fan_index = int(args.fan_index)
            if fan_index < 1 or fan_index > fan_count:
                print(f"fan_index invalide (1..{fan_count}).")
                return 1
            start = (fan_index - 1) * leds_per_fan
            end = min(len(target["map"]), start + leds_per_fan)
            print(f"Sweep ventilo {fan_index}/{fan_count} (LEDs {start}-{end-1})")
            try:
                while True:
                    for i in range(start, end):
                        frame = bytearray(len(target["map"]) * 3)
                        frame[i * 3] = 255
                        frame[i * 3 + 1] = 255
                        frame[i * 3 + 2] = 255
                        mapper.apply_frame_map(
                            target["map"],
                            target["device_ids"],
                            frame,
                            lut=lut,
                            update_mode=target.get("update_mode", "direct_async"),
                        )
                        time.sleep(max(0.01, float(args.fan_speed)))
            except KeyboardInterrupt:
                frame = bytearray(len(target["map"]) * 3)
                mapper.apply_frame_map(
                    target["map"],
                    target["device_ids"],
                    frame,
                    lut=lut,
                    update_mode=target.get("update_mode", "direct_async"),
                )
                return 0
        else:
            indices = parse_int_list(args.fan_on)
            if not indices:
                print("fan_on invalide. Exemple: --fan-on 1,2")
                return 1
            color = parse_rgb(args.fan_color) or (255, 255, 255)
            frame = bytearray(len(target["map"]) * 3)
            for fan_index in indices:
                if fan_index < 1 or fan_index > fan_count:
                    continue
                start = (fan_index - 1) * leds_per_fan
                end = min(len(target["map"]), start + leds_per_fan)
                for i in range(start, end):
                    frame[i * 3] = color[0]
                    frame[i * 3 + 1] = color[1]
                    frame[i * 3 + 2] = color[2]
            mapper.apply_frame_map(
                target["map"],
                target["device_ids"],
                frame,
                lut=lut,
                update_mode=target.get("update_mode", "direct_async"),
            )
            print(f"Ventilos allumes: {indices} couleur={color[0]},{color[1]},{color[2]}")
            return 0

    debug_udp = args.debug_udp
    sel, runtime_groups = setup_runtime_with_retry(groups, "Initialisation runtime")
    if not runtime_groups:
        print("Aucun groupe valide. Verifie la config.")
        logger.error("Aucun groupe valide.")
        return 1

    print("Groupes actifs:")
    logger.info("Groupes actifs:")
    for g in runtime_groups:
        print(
            f"- {g['name']} -> {g['udp_host']}:{g['udp_port']} "
            f"(LEDs: {g['led_count']}, protocole: {g['protocol']})"
        )
        logger.info(
            "%s -> %s:%s (LEDs: %s, protocole: %s)",
            g["name"],
            g["udp_host"],
            g["udp_port"],
            g["led_count"],
            g["protocol"],
        )
    if _HAS_MSVCRT and prompt_allowed:
        print("Appuie sur M pour changer de mode.")

    watchdog_enabled = bool(cfg.get("icue_watchdog", True))
    watchdog_interval = float(cfg.get("icue_watchdog_interval", 5.0))
    watchdog_fail_threshold = int(cfg.get("icue_watchdog_fail_threshold", 3))
    reconnect_cooldown = float(cfg.get("icue_reconnect_cooldown", 15.0))
    watchdog_idle_only = bool(cfg.get("icue_watchdog_idle_only", False))
    skip_reconnect_when_idle = bool(cfg.get("icue_skip_reconnect_when_idle", True))
    watchdog_sdk_check = bool(cfg.get("icue_watchdog_sdk_check", False))
    restart_on_sdk_hang = bool(cfg.get("icue_restart_on_sdk_hang", True))
    keepalive_enabled = bool(cfg.get("icue_keepalive", True))
    keepalive_interval = float(cfg.get("icue_keepalive_interval", 30.0))
    keepalive_reapply = bool(cfg.get("icue_keepalive_reapply", True))
    keepalive_request_always = bool(cfg.get("icue_keepalive_request_always", False))
    try:
        request_control_interval = float(cfg.get("icue_request_control_interval", 0.0))
    except Exception:
        request_control_interval = 0.0
    if request_control_interval < 0:
        request_control_interval = 0.0
    request_control_reconnect = bool(
        cfg.get("icue_request_control_reconnect", False)
    )
    unique_idle_clear_enabled = mode == "unique" and bool(cfg.get("unique_idle_clear", True))
    unique_idle_clear_s = max(0.2, float(cfg.get("unique_idle_clear_seconds", 1.0)))
    apply_fail_threshold = int(cfg.get("icue_apply_fail_threshold", 6))
    request_control_fail_threshold = max(
        1, int(cfg.get("icue_request_control_fail_threshold", 2))
    )
    last_watchdog = time.monotonic()
    last_stats_log = time.monotonic()
    last_keepalive = time.monotonic()
    last_request_control = time.monotonic()
    last_reconnect = 0.0
    watchdog_fail_count = 0
    request_control_fail_count = 0

    def restart_process(reason):
        logger.error("[DEBUG] Redemarrage du processus requis: %s", reason)
        print(f"Redemarrage du bridge: {reason}")
        try:
            close_runtime(sel, runtime_groups)
        except Exception:
            pass
        try:
            _loop_alive.set()
        except Exception:
            pass
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
            except Exception:
                pass
        if getattr(sys, "frozen", False):
            cmd = [sys.executable] + sys.argv[1:]
        else:
            cmd = [sys.executable] + sys.argv
        if mode:
            cmd.extend(["--mode", mode])
        try:
            subprocess.Popen(cmd, cwd=str(pathlib.Path.cwd()), close_fds=False)
        except Exception as exc:
            logger.error("[DEBUG] Redemarrage impossible: %s", exc)
            print(f"Redemarrage impossible: {exc}")
            for handler in logging.getLogger().handlers:
                try:
                    handler.flush()
                except Exception:
                    pass
        os._exit(75)

    def attempt_reconnect(reason):
        logger.warning("[DEBUG] attempt_reconnect DEBUT raison=%s", reason)
        # FIX-FREEZE-2: Start reconnect work on a daemon thread so UDP polling continues.
        # FIX-FREEZE-BLOCK-4: Acquire reconnect guard before any reconnect SDK call.
        # DIAG-12: Log reconnect calls and lock state before non-blocking acquire.
        timing_logger.warning(
            "[BLOCK] attempt_reconnect called reason=%s lock_held=%s",
            reason,
            _reconnect_lock.locked(),
        )
        if not _reconnect_lock.acquire(blocking=False):
            logger.warning("[DEBUG] attempt_reconnect deja en cours raison=%s", reason)
            return

        def reconnect_worker():
            nonlocal last_keepalive, last_reconnect, last_request_control, last_watchdog
            nonlocal mapper, groups, sdk, sel, runtime_groups, watchdog_fail_count
            nonlocal request_control_fail_count
            try:
                if restart_on_sdk_hang:
                    blocked, elapsed = mapper.direct_async_blocked()
                    if blocked:
                        restart_process(f"appel iCUE direct bloque depuis {elapsed:.1f}s")
                        return
                if (time.monotonic() - last_reconnect) < reconnect_cooldown:
                    logger.warning("Reconnexion ignoree (cooldown) raison=%s", reason)
                    return
                last_reconnect = time.monotonic()
                logger.warning("Reconnexion iCUE (raison: %s)", reason)
                try:
                    sdk.release_control()
                    logger.debug("[DEBUG] release_control OK")
                except Exception as exc:
                    logger.warning("[DEBUG] release_control exception: %s", exc)
                try:
                    sdk.disconnect()
                    logger.debug("[DEBUG] disconnect OK")
                except Exception as exc:
                    logger.warning("[DEBUG] disconnect exception: %s", exc)
                try:
                    new_sdk = CueSdkWrapper()
                    logger.debug("[DEBUG] CueSdkWrapper OK")
                except Exception as exc:
                    logger.warning("[DEBUG] CueSdkWrapper exception: %s", exc)
                    logger.exception("Recreation du wrapper iCUE echoue: %s", exc)
                    return
                try:
                    err = new_sdk.connect()
                    logger.debug("[DEBUG] sdk.connect OK err=%s", err)
                except Exception as exc:
                    logger.warning("[DEBUG] sdk.connect exception: %s", exc)
                    logger.exception("Connexion iCUE echoue pendant reconnexion: %s", exc)
                    return
                if err != new_sdk.CorsairError.CE_Success:
                    logger.warning("[DEBUG] sdk.connect erreur err=%s", err)
                    logger.warning("Reconnexion iCUE echouee: %s", err)
                    return
                logger.debug("[DEBUG] wait_for_icue start")
                ok, last_err = wait_for_icue(
                    new_sdk, device_type_mask, timeout_s=reconnect_timeout
                )
                logger.debug("[DEBUG] wait_for_icue end ok=%s last_err=%s", ok, last_err)
                if not ok:
                    logger.warning("[DEBUG] wait_for_icue erreur last_err=%s", last_err)
                    logger.warning("iCUE pas pret apres reconnexion: %s", last_err)
                    return
                try:
                    layer_priority = int(cfg.get("icue_layer_priority", 255))
                    layer_priority = max(0, min(255, layer_priority))
                    layer_err = new_sdk.set_layer_priority(layer_priority)
                    if new_sdk.is_success(layer_err):
                        logger.debug(
                            "[DEBUG] set_layer_priority OK priority=%s",
                            layer_priority,
                        )
                    else:
                        logger.warning(
                            "[DEBUG] set_layer_priority erreur priority=%s err=%s",
                            layer_priority,
                            layer_err,
                        )
                except Exception as exc:
                    logger.warning("[DEBUG] set_layer_priority exception: %s", exc)
                try:
                    new_mapper = build_mapper(cfg, new_sdk, device_type_mask, args)
                    logger.debug(
                        "[DEBUG] build_mapper OK total_leds=%s",
                        getattr(new_mapper, "total_leds", None),
                    )
                    new_groups = get_groups_for_mode(
                        mode, cfg, new_mapper, new_sdk, default_protocol, args
                    )
                    logger.debug(
                        "[DEBUG] get_groups_for_mode OK group_count=%s",
                        len(new_groups),
                    )
                except Exception as exc:
                    logger.warning("[DEBUG] rebuild exception: %s", exc)
                    logger.exception("Rebuild apres reconnexion echoue: %s", exc)
                    print(f"Rebuild apres reconnexion echoue: {exc}")
                    return
                try:
                    request_control_for_devices(
                        new_sdk,
                        mapper=new_mapper,
                        logger=logger,
                        context="reconnect_request_control",
                    )
                    logger.debug("[DEBUG] request_control_for_devices OK")
                except Exception as exc:
                    logger.warning("[DEBUG] request_control_for_devices exception: %s", exc)

                # FIX-1: Keep old runtime state until the replacement is fully ready.
                old_sel = sel
                old_runtime_groups = runtime_groups
                try:
                    close_runtime(old_sel, old_runtime_groups)
                    logger.debug("[DEBUG] close_runtime OK")
                except Exception as exc:
                    logger.warning("[DEBUG] close_runtime exception: %s", exc)
                try:
                    new_sel, new_runtime_groups = setup_runtime_with_retry(
                        new_groups, "Reconnexion iCUE"
                    )
                    logger.debug(
                        "[DEBUG] setup_runtime_with_retry OK group_count=%s",
                        len(new_runtime_groups),
                    )
                except Exception as exc:
                    logger.warning("[DEBUG] setup_runtime_with_retry exception: %s", exc)
                    logger.exception("Activation runtime apres reconnexion echoue: %s", exc)
                    print(f"Activation runtime apres reconnexion echoue: {exc}")
                    sel = old_sel
                    runtime_groups = old_runtime_groups
                    return
                # FIX-FREEZE-2: Swap shared bridge state from the reconnect worker under lock.
                sdk = new_sdk
                mapper = new_mapper
                groups = new_groups
                sel = new_sel
                runtime_groups = new_runtime_groups
                last_keepalive = time.monotonic()
                last_request_control = last_keepalive
                last_watchdog = last_keepalive
                watchdog_fail_count = 0
                request_control_fail_count = 0
                logger.warning("[DEBUG] attempt_reconnect FIN OK")
                logger.info("Reconnexion iCUE OK.")
                print("Reconnexion iCUE OK.")
                print("Groupes actifs:")
                for gg in runtime_groups:
                    print(
                        f"- {gg['name']} -> {gg['udp_host']}:{gg['udp_port']} "
                        f"(LEDs: {gg['led_count']}, protocole: {gg['protocol']})"
                    )
                    logger.info(
                        "%s -> %s:%s (LEDs: %s, protocole: %s)",
                        gg["name"],
                        gg["udp_host"],
                        gg["udp_port"],
                        gg["led_count"],
                        gg["protocol"],
                    )
            finally:
                _reconnect_lock.release()

        try:
            threading.Thread(
                target=reconnect_worker,
                name="icue-reconnect",
                daemon=True,
            ).start()
        except Exception as exc:
            _reconnect_lock.release()
            logger.warning("[DEBUG] reconnect thread exception: %s", exc)
            logger.exception("Demarrage thread reconnexion echoue: %s", exc)

    def ensure_request_control(context, reconnect_reason):
        nonlocal request_control_fail_count
        try:
            ok = request_control_for_devices(
                sdk,
                mapper=mapper,
                logger=logger,
                context=context,
            )
        except Exception as exc:
            logger.warning("iCUE %s exception: %s", context, exc)
            ok = False
        if ok:
            request_control_fail_count = 0
            return True
        request_control_fail_count += 1
        logger.warning(
            "iCUE %s fail (%s/%s)",
            context,
            request_control_fail_count,
            request_control_fail_threshold,
        )
        if request_control_reconnect and (
            request_control_fail_count >= request_control_fail_threshold
        ):
            request_control_fail_count = 0
            attempt_reconnect(reconnect_reason)
        return False

    def apply_mode_change(new_mode):
        nonlocal mode, groups, sel, runtime_groups, mapper
        if not new_mode or new_mode == mode:
            return
        close_runtime(sel, runtime_groups)
        mode = new_mode
        try:
            groups = get_groups_for_mode(
                mode, cfg, mapper, sdk, default_protocol, args
            )
        except RuntimeError as exc:
            print(str(exc))
            print("Reste en mode actuel.")
            groups = get_groups_for_mode(
                "group", cfg, mapper, sdk, default_protocol, args
            )
            mode = "group"
        sel, runtime_groups = setup_runtime_with_retry(groups, "Changement de mode")
        print("Groupes actifs:")
        for gg in runtime_groups:
            print(
                f"- {gg['name']} -> {gg['udp_host']}:{gg['udp_port']} "
                f"(LEDs: {gg['led_count']}, protocole: {gg['protocol']})"
            )
            logger.info(
                "%s -> %s:%s (LEDs: %s, protocole: %s)",
                gg["name"],
                gg["udp_host"],
                gg["udp_port"],
                gg["led_count"],
                gg["protocol"],
            )
    _reconnect_lock = threading.Lock()
    _loop_alive = threading.Event()
    _last_loop_ts = time.monotonic()
    _perf_last_loop_warn = 0.0
    # FIX-FREEZE-BLOCK-1: Watchdog SDK checks run off the UDP/main thread.
    _watchdog_lock = threading.Lock()
    _watchdog_result = {"connected": True, "ts": 0.0, "inflight": False}
    # FIX-FREEZE-BLOCK-2: Periodic request_control runs off the UDP/main thread.
    _request_control_lock = threading.Lock()
    _request_control_inflight = [False]

    def _run_watchdog_check():
        try:
            result = is_icue_connected(sdk, device_type_mask)
        except Exception:
            result = False
        with _watchdog_lock:
            _watchdog_result["connected"] = result
            _watchdog_result["ts"] = time.monotonic()
            _watchdog_result["inflight"] = False

    def _run_request_control(context, reconnect_reason, success_message=None):
        try:
            ok = ensure_request_control(context, reconnect_reason)
            if ok and success_message:
                logger.info(success_message)
        finally:
            with _request_control_lock:
                _request_control_inflight[0] = False

    def _perf_group_warn(g, key, message, *values):
        if not perf_debug:
            return
        now_perf = time.monotonic()
        store_key = f"perf_last_{key}_warn"
        last_perf = g.get(store_key, 0.0)
        if now_perf - last_perf < perf_log_cooldown:
            return
        g[store_key] = now_perf
        logger.warning(message, *values)

    def _debug_loop_watchdog():
        while not _loop_alive.wait(3.0):
            delta = time.monotonic() - _last_loop_ts
            if delta <= 10.0:
                continue
            logger.error(
                "[DEBUG WATCHDOG] main loop bloque delta=%.3fs mode=%s sel=%s runtime_groups=%s",
                delta,
                mode,
                sel,
                len(runtime_groups),
            )
            thread_names = {
                thread.ident: thread.name for thread in threading.enumerate()
            }
            for thread_id, frame in sys._current_frames().items():
                stack = "".join(traceback.format_stack(frame))
                logger.error(
                    "[DEBUG WATCHDOG] thread=%s ident=%s stack:\n%s",
                    thread_names.get(thread_id, "unknown"),
                    thread_id,
                    stack,
                )

    def _gil_probe_loop():
        # DIAG-13: Probe scheduler/GIL stalls independently of bridge I/O.
        expected = 0.001
        while not _loop_alive.is_set():
            probe_start = time.monotonic()
            time.sleep(expected)
            actual = time.monotonic() - probe_start
            if actual - expected > 0.005:
                timing_logger.warning(
                    "[GIL] probe stall detected: expected=1ms actual=%.1fms",
                    actual * 1000.0,
                )

    _debug_watchdog_thread = threading.Thread(
        target=_debug_loop_watchdog,
        name="debug-loop-watchdog",
        daemon=True,
    )
    _debug_watchdog_thread.start()
    _gil_probe_thread = threading.Thread(
        target=_gil_probe_loop,
        name="gil-probe",
        daemon=True,
    )
    _gil_probe_thread.start()
    try:
        while True:
            now = time.monotonic()
            _loop_delta = now - _last_loop_ts
            _last_loop_ts = now
            # DIAG-1: Log main-loop tick duration regardless of perf_debug.
            if _loop_delta > 0.100:
                timing_logger.warning(
                    "[TICK] loop iter took %.1fms", _loop_delta * 1000.0
                )
            elif _loop_delta > 0.010:
                timing_logger.debug(
                    "[TICK] loop iter took %.1fms", _loop_delta * 1000.0
                )
            if _loop_delta > 0.200:
                # DIAG-14: Dump all thread stacks on long main-loop ticks.
                thread_names = {
                    thread.ident: thread.name for thread in threading.enumerate()
                }
                for thread_id, frame in sys._current_frames().items():
                    stack = "".join(traceback.format_stack(frame))
                    timing_logger.warning(
                        "[STACK] thread=%s ident=%s\n%s",
                        thread_names.get(thread_id, "unknown"),
                        thread_id,
                        stack,
                    )
            if perf_debug and _loop_delta > max(0.75, perf_threshold):
                if now - _perf_last_loop_warn >= perf_log_cooldown:
                    _perf_last_loop_warn = now
                    logger.warning(
                        "[PERF] boucle principale lente %.3fs mode=%s runtime_groups=%s",
                        _loop_delta,
                        mode,
                        len(runtime_groups),
                    )
            if _MODE_WINDOW is not None and _MODE_WINDOW.available:
                gui_mode = _MODE_WINDOW.poll()
                if gui_mode == _MODE_EXIT:
                    print("Arret demande via la fenetre.")
                    logger.info("Arret demande via la fenetre.")
                    return 0
                if gui_mode and gui_mode != mode:
                    apply_mode_change(gui_mode)
            select_start = time.monotonic()
            current_sel = sel
            try:
                # FIX-FREEZE-3: Poll in 50ms slices so idle paths are not delayed by select.
                # DIAG-2: Time selector wait every loop iteration.
                events = current_sel.select(timeout=0.05)
            except Exception as exc:
                # FIX-FREEZE-2: Swallow selector switchover errors during reconnect.
                if _reconnect_lock.locked():
                    logger.debug(
                        "[DEBUG] sel.select ignore pendant reconnexion sel=%s exc_type=%s exc=%s",
                        current_sel,
                        type(exc).__name__,
                        exc,
                    )
                else:
                    logger.warning(
                        "[DEBUG] sel.select exception sel=%s runtime_groups=%s exc_type=%s exc=%s",
                        current_sel,
                        len(runtime_groups),
                        type(exc).__name__,
                        exc,
                    )
                    time.sleep(0.05)
                events = []
            select_elapsed = time.monotonic() - select_start
            if select_elapsed >= 0.060:
                timing_logger.warning(
                    "[BLOCK] sel.select took %.1fms (events=%s)",
                    select_elapsed * 1000.0,
                    len(events),
                )
            else:
                timing_logger.debug(
                    "[BLOCK] sel.select took %.1fms (events=%s)",
                    select_elapsed * 1000.0,
                    len(events),
                )
            if select_elapsed > 2.0:
                try:
                    sel_map_len = len(current_sel.get_map())
                except Exception:
                    sel_map_len = "n/a"
                logger.warning(
                    "[DEBUG] sel.select lent elapsed=%.3fs sel=%s sel_map_len=%s runtime_groups=%s",
                    select_elapsed,
                    current_sel,
                    sel_map_len,
                    len(runtime_groups),
                )
            now = time.monotonic()
            if _loop_delta > 5.0:
                logger.warning(
                    "[DEBUG] main_loop delta=%.3fs mode=%s runtime_groups=%s events=%s",
                    _loop_delta,
                    mode,
                    len(runtime_groups),
                    len(events),
                )
            if update_enabled and is_repo_configured(update_repo):
                if (now - update_last_check) >= update_interval:
                    update_last_check = now
                    start_update_check()
                pending = None
                with update_lock:
                    pending = update_state.get("pending")
                    update_state["pending"] = None
                if pending:
                    latest = pending.get("version")
                    url = pending.get("url")
                    if latest and update_state.get("last_notified") != latest:
                        update_state["last_notified"] = latest
                        if _MODE_WINDOW is not None and _MODE_WINDOW.available:
                            _MODE_WINDOW.notify_update(latest, url)
                        else:
                            print(_I18N.t("update_available_console", version=latest, url=url))
                            logger.info(
                                _I18N.t("update_available_console", version=latest, url=url)
                            )
            if request_control_interval > 0 and (
                now - last_request_control
            ) >= request_control_interval:
                # FIX-FREEZE-BLOCK-2: Launch periodic request_control without blocking UDP.
                with _request_control_lock:
                    request_control_already = _request_control_inflight[0]
                    if not request_control_already:
                        _request_control_inflight[0] = True
                if not request_control_already:
                    try:
                        threading.Thread(
                            target=_run_request_control,
                            args=(
                                "periodic_request_control",
                                "periodic_request_control_no_control",
                            ),
                            daemon=True,
                            name="icue-request-control",
                        ).start()
                    except Exception as exc:
                        with _request_control_lock:
                            _request_control_inflight[0] = False
                        logger.warning(
                            "[DEBUG] request_control thread exception: %s", exc
                        )
                last_request_control = now
            any_active_recent = any(
                (g.get("last_packet_ts", 0.0) and (now - g.get("last_packet_ts", 0.0)) < (g.get("keepalive_interval") or keepalive_interval))
                for g in runtime_groups
            )
            if keepalive_enabled:
                due_groups = []
                for g in runtime_groups:
                    interval = g.get("keepalive_interval") or keepalive_interval
                    if now - g.get("last_keepalive", 0.0) < interval:
                        continue
                    g["last_keepalive"] = now
                    if g.get("keepalive_reapply", None) is False:
                        continue
                    last_pkt = g.get("last_packet_ts", 0.0)
                    if not last_pkt:
                        continue
                    if (now - last_pkt) < interval:
                        continue
                    if g.get("last_send", 0.0) <= 0:
                        continue
                    due_groups.append((g, interval))

                if due_groups and keepalive_request_always:
                    # FIX-FREEZE-BLOCK-2: Launch keepalive request_control without blocking UDP.
                    with _request_control_lock:
                        request_control_already = _request_control_inflight[0]
                        if not request_control_already:
                            _request_control_inflight[0] = True
                    if not request_control_already:
                        try:
                            threading.Thread(
                                target=_run_request_control,
                                args=(
                                    "keepalive_request_control",
                                    "keepalive_request_control_no_control",
                                    "iCUE keepalive: request_control",
                                ),
                                daemon=True,
                                name="icue-request-control",
                            ).start()
                        except Exception as exc:
                            with _request_control_lock:
                                _request_control_inflight[0] = False
                            logger.warning(
                                "[DEBUG] request_control thread exception: %s", exc
                            )

                if keepalive_reapply:
                    # DIAG-9: Time full keepalive reapply loop.
                    keepalive_loop_start = time.monotonic()
                    for g, interval in due_groups:
                        try:
                            # DIAG-3: Time main-thread keepalive apply_frame_map call.
                            apply_diag_start = time.monotonic()
                            ok = mapper.apply_frame_map(
                                g["map"],
                                g["device_ids"],
                                g["frame_buffer"],
                                lut=lut,
                                update_mode=g.get("update_mode", "direct_async"),
                            )
                            apply_diag_elapsed = time.monotonic() - apply_diag_start
                            if apply_diag_elapsed >= 0.010:
                                timing_logger.warning(
                                    "[BLOCK] apply_frame_map(keepalive) took %.1fms ok=%s",
                                    apply_diag_elapsed * 1000.0,
                                    ok,
                                )
                            else:
                                timing_logger.debug(
                                    "[BLOCK] apply_frame_map(keepalive) took %.1fms ok=%s",
                                    apply_diag_elapsed * 1000.0,
                                    ok,
                                )
                            if not ok:
                                g["fail_count"] = g.get("fail_count", 0) + 1
                                if g["fail_count"] >= apply_fail_threshold:
                                    g["fail_count"] = 0
                                    if skip_reconnect_when_idle and not any_active_recent:
                                        logger.warning(
                                            "Keepalive echec (idle), reconnexion ignoree."
                                        )
                                    else:
                                        attempt_reconnect("keepalive_no_success")
                        except Exception as exc:
                            # FIX-FREEZE-2: Ignore transient mapper errors while reconnect swaps state.
                            if _reconnect_lock.locked():
                                logger.debug(
                                    "[DEBUG] Keepalive apply_frame_map ignore pendant reconnexion: %s",
                                    exc,
                                )
                                continue
                            logger.exception("Keepalive apply_frame_map erreur: %s", exc)
                            if skip_reconnect_when_idle and not any_active_recent:
                                logger.warning(
                                    "Keepalive erreur (idle), reconnexion ignoree."
                                )
                            else:
                                attempt_reconnect("keepalive_error")
                    keepalive_loop_elapsed = time.monotonic() - keepalive_loop_start
                    if keepalive_loop_elapsed >= 0.010:
                        timing_logger.warning(
                            "[BLOCK] keepalive loop took %.1fms groups=%s",
                            keepalive_loop_elapsed * 1000.0,
                            len(due_groups),
                        )
            if unique_idle_clear_enabled and any_active_recent:
                for g in runtime_groups:
                    if g.get("idle_clear_disabled", False):
                        continue
                    clear_after = g.get("idle_clear_seconds")
                    if clear_after is None:
                        clear_after = unique_idle_clear_s
                    last_pkt = g.get("last_packet_ts", 0.0)
                    if not last_pkt:
                        continue
                    idle_for = now - last_pkt
                    if idle_for < clear_after:
                        continue
                    if g.get("idle_cleared", False):
                        continue
                    try:
                        frame = g.get("frame_buffer")
                        if frame is None:
                            continue
                        frame[:] = b"\x00" * len(frame)
                        # DIAG-3: Time main-thread idle_clear apply_frame_map call.
                        apply_diag_start = time.monotonic()
                        ok = mapper.apply_frame_map(
                            g["map"],
                            g["device_ids"],
                            frame,
                            lut=lut,
                            update_mode=g.get("update_mode", "direct_async"),
                        )
                        apply_diag_elapsed = time.monotonic() - apply_diag_start
                        if apply_diag_elapsed >= 0.010:
                            timing_logger.warning(
                                "[BLOCK] apply_frame_map(idle_clear) took %.1fms ok=%s",
                                apply_diag_elapsed * 1000.0,
                                ok,
                            )
                        else:
                            timing_logger.debug(
                                "[BLOCK] apply_frame_map(idle_clear) took %.1fms ok=%s",
                                apply_diag_elapsed * 1000.0,
                                ok,
                            )
                        if ok:
                            g["last_send"] = now
                            g["fail_count"] = 0
                            g["idle_cleared"] = True
                            logger.info(
                                "UDP[%s]: clear idle apres %.1fs",
                                g.get("name"),
                                idle_for,
                            )
                    except Exception as exc:
                        # FIX-FREEZE-2: Ignore transient mapper errors while reconnect swaps state.
                        if _reconnect_lock.locked():
                            logger.debug(
                                "[DEBUG] Idle clear ignore pendant reconnexion (%s): %s",
                                g.get("name"),
                                exc,
                            )
                            continue
                        logger.exception("Idle clear erreur (%s): %s", g.get("name"), exc)
            direct_blocked, direct_elapsed = mapper.direct_async_blocked()
            if direct_blocked and restart_on_sdk_hang:
                restart_process(
                    f"appel iCUE direct bloque depuis {direct_elapsed:.1f}s"
                )
                return 0
            if watchdog_enabled and now - last_watchdog >= watchdog_interval:
                # DIAG-10: Time main-thread watchdog decision block.
                watchdog_block_start = time.monotonic()
                last_watchdog = now
                active_recent = any(
                    (now - g.get("last_packet_ts", 0.0)) < watchdog_interval
                    for g in runtime_groups
                )
                if watchdog_idle_only and active_recent:
                    watchdog_fail_count = 0
                else:
                    if skip_reconnect_when_idle and not active_recent:
                        watchdog_fail_count = 0
                    elif direct_elapsed > 0:
                        watchdog_fail_count = 0
                    elif not watchdog_sdk_check:
                        watchdog_fail_count = 0
                    else:
                        # FIX-FREEZE-BLOCK-1: Main loop reads cached watchdog result only.
                        with _watchdog_lock:
                            watchdog_already_inflight = _watchdog_result["inflight"]
                            watchdog_last_result = _watchdog_result["connected"]
                            if not watchdog_already_inflight:
                                _watchdog_result["inflight"] = True
                        if not watchdog_already_inflight:
                            try:
                                threading.Thread(
                                    target=_run_watchdog_check,
                                    daemon=True,
                                    name="icue-watchdog-check",
                                ).start()
                            except Exception as exc:
                                with _watchdog_lock:
                                    _watchdog_result["inflight"] = False
                                logger.warning(
                                    "[DEBUG] watchdog thread exception: %s", exc
                                )
                        if not watchdog_last_result:
                            watchdog_fail_count += 1
                            logger.warning(
                                "iCUE check fail (%s/%s)", watchdog_fail_count, watchdog_fail_threshold
                            )
                            if watchdog_fail_count >= watchdog_fail_threshold:
                                watchdog_fail_count = 0
                                logger.warning("iCUE deconnecte, tentative de reconnexion...")
                                print("iCUE deconnecte, tentative de reconnexion...")
                                attempt_reconnect("watchdog")
                        else:
                            watchdog_fail_count = 0
                watchdog_block_elapsed = time.monotonic() - watchdog_block_start
                if watchdog_block_elapsed >= 0.002:
                    timing_logger.warning(
                        "[BLOCK] watchdog check took %.1fms",
                        watchdog_block_elapsed * 1000.0,
                    )
            if now - last_stats_log >= 10.0:
                last_stats_log = now
                for g in runtime_groups:
                    last_pkt = g.get("last_packet_ts", 0.0)
                    idle = None if not last_pkt else (now - last_pkt)
                    last_send = g.get("last_send", 0.0)
                    last_send_delta = None if not last_send else (now - last_send)
                    logger.info(
                        "UDP[%s]: idle %s, last_send=%s, leds=%s",
                        g.get("name"),
                        "never" if idle is None else f"{idle:.1f}s",
                        "never" if last_send_delta is None else f"{last_send_delta:.1f}s ago",
                        g.get("led_count"),
                    )
                    if perf_debug:
                        logger.info(
                            "[PERF] UDP[%s]: max_udp_gap=%.3fs max_apply=%.3fs",
                            g.get("name"),
                            g.get("perf_max_udp_gap", 0.0),
                            g.get("perf_max_apply_elapsed", 0.0),
                        )
                        g["perf_max_udp_gap"] = 0.0
                        g["perf_max_apply_elapsed"] = 0.0
            if debug_udp:
                for g in runtime_groups:
                    if now - g["stats_ts"] >= 1.0:
                        if g["pkt_count"] == 0:
                            print(f"UDP[{g['name']}]: 0 paquets/s")
                        else:
                            max_val = max(g["frame_buffer"]) if g["frame_buffer"] else 0
                            print(
                                f"UDP[{g['name']}]: {g['pkt_count']} paquets/s, "
                                f"{g['byte_count']} octets/s, max={max_val}"
                            )
                        g["pkt_count"] = 0
                        g["byte_count"] = 0
                        g["stats_ts"] = now

            for key, _ in events:
                sock = key.fileobj
                g = key.data
                try:
                    data, _ = sock.recvfrom(65535)
                except Exception:
                    continue

                if g["first_packet"]:
                    g["first_packet"] = False
                    proto_hex = f"0x{data[0]:02x}" if data else "n/a"
                    print(
                        f"Premier paquet UDP recu pour {g['name']}: "
                        f"{len(data)} octets, byte0={proto_hex}"
                    )
                    logger.info(
                        "Premier paquet UDP recu pour %s: %s octets, byte0=%s",
                        g["name"],
                        len(data),
                        proto_hex,
                    )

                if debug_udp:
                    g["pkt_count"] += 1
                    g["byte_count"] += len(data)
                prev_packet_ts = g.get("last_packet_ts", 0.0)
                if perf_debug and prev_packet_ts:
                    udp_gap = now - prev_packet_ts
                    g["perf_max_udp_gap"] = max(
                        g.get("perf_max_udp_gap", 0.0), udp_gap
                    )
                    if udp_gap > perf_threshold:
                        _perf_group_warn(
                            g,
                            "udp_gap",
                            "[PERF] UDP[%s] trou entre paquets %.3fs port=%s bytes=%s",
                            g.get("name"),
                            udp_gap,
                            g.get("udp_port"),
                            len(data),
                        )
                g["last_packet_ts"] = now
                g["idle_cleared"] = False

                protocol = g["protocol"]
                frame_buffer = g["frame_buffer"]
                ddp_like = looks_like_ddp(data)
                if protocol == "ddp" or (
                    protocol in ("auto", "wled") and ddp_like
                ):
                    if protocol == "wled" and ddp_like and not g.get("ddp_auto"):
                        g["ddp_auto"] = True
                        logger.info(
                            "DDP detecte sur %s (auto-detection activee pour wled).",
                            g.get("name"),
                        )
                    push = parse_ddp(data, frame_buffer)
                    if not push:
                        continue
                elif protocol == "wled":
                    parse_wled_or_raw(data, frame_buffer)
                elif protocol == "raw":
                    if len(data) % 3 == 0:
                        frame_buffer[: min(len(frame_buffer), len(data))] = data[
                            : len(frame_buffer)
                        ]
                    else:
                        continue
                else:
                    parse_wled_or_raw(data, frame_buffer)

                if min_interval and now - g["last_send"] < min_interval:
                    continue
                g["last_send"] = now
                try:
                    # DIAG-3: Time main-thread UDP apply_frame_map call.
                    apply_start = time.monotonic()
                    ok = mapper.apply_frame_map(
                        g["map"],
                        g["device_ids"],
                        frame_buffer,
                        lut=lut,
                        update_mode=g.get("update_mode", "direct_async"),
                    )
                    apply_elapsed = time.monotonic() - apply_start
                    if apply_elapsed >= 0.010:
                        timing_logger.warning(
                            "[BLOCK] apply_frame_map(udp) took %.1fms ok=%s",
                            apply_elapsed * 1000.0,
                            ok,
                        )
                    else:
                        timing_logger.debug(
                            "[BLOCK] apply_frame_map(udp) took %.1fms ok=%s",
                            apply_elapsed * 1000.0,
                            ok,
                        )
                    if perf_debug:
                        g["perf_max_apply_elapsed"] = max(
                            g.get("perf_max_apply_elapsed", 0.0), apply_elapsed
                        )
                        if apply_elapsed > perf_threshold:
                            _perf_group_warn(
                                g,
                                "apply",
                                "[PERF] bridge apply UDP[%s] lent %.3fs update_mode=%s ok=%s",
                                g.get("name"),
                                apply_elapsed,
                                g.get("update_mode", "direct_async"),
                                ok,
                            )
                    if ok:
                        g["fail_count"] = 0
                    else:
                        g["fail_count"] = g.get("fail_count", 0) + 1
                        if g["fail_count"] >= apply_fail_threshold:
                            g["fail_count"] = 0
                            attempt_reconnect("apply_no_success")
                except Exception as exc:
                    # FIX-FREEZE-2: Ignore transient mapper errors while reconnect swaps state.
                    if _reconnect_lock.locked():
                        logger.debug(
                            "[DEBUG] apply_frame_map ignore pendant reconnexion: %s",
                            exc,
                        )
                        continue
                    logger.exception("apply_frame_map erreur: %s", exc)
                    attempt_reconnect("apply_frame_map_error")
            if _HAS_MSVCRT and prompt_allowed and msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in ("m", "M"):
                    if _MODE_WINDOW is None or not _MODE_WINDOW.available:
                        apply_mode_change(
                            prompt_mode(mode)
                        )
    except KeyboardInterrupt:
        _loop_alive.set()
        _debug_watchdog_thread.join(timeout=1.0)
        print("Arret.")
        logger.info("Arret.")
        close_runtime(sel, runtime_groups)
        return 0
