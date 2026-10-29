"""Trip-plan helpers: demo plans, photo lookup, map coordinates and the route map drawing."""
import copy
import hashlib
import json
import os
import re

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

with open(os.path.join(DATA_DIR, "demo_plans.json"), encoding="utf-8") as f:
    DEMO_PLANS = json.load(f)

with open(os.path.join(DATA_DIR, "lookups.json"), encoding="utf-8") as f:
    _LOOKUPS = json.load(f)

POPULAR_PRESETS = _LOOKUPS["popular_presets"]
GENERIC_PLACEHOLDERS = _LOOKUPS["generic_placeholders"]
CITY_CENTERS = _LOOKUPS["city_centers"]
LANDMARK_COORDS = _LOOKUPS["landmark_coords"]


# Preset cities shown on the setup page, grouped like the original UI.
PRESET_GROUPS = [
    ("🇮🇳 India", ["Jaipur", "Mumbai", "Delhi", "Goa", "Udaipur", "Manali", "Rishikesh", "Varanasi",
                  "Amritsar", "Leh", "Jodhpur", "Darjeeling", "Mysuru", "Agra", "Kochi", "Hampi"]),
    ("🌏 Asia & Middle East", ["Bali", "Bangkok", "Singapore", "Seoul", "Dubai", "Tokyo", "Kyoto"]),
    ("🇪🇺 Europe", ["Paris", "Barcelona", "London", "Amsterdam", "Istanbul", "Prague", "Rome", "Florence"]),
    ("🌎 Americas", ["New York", "Rio de Janeiro", "Cusco"]),
]


# ---------------------------------------------------------------- demo plans

def find_demo_template(destination):
    """Return the pre-loaded plan whose key matches the destination, or None."""
    wanted = destination.lower().replace(" city", "").strip()
    if not wanted:
        return None
    for key, plan in DEMO_PLANS.items():
        if key in wanted or wanted in key:
            return plan
    return None


def _scale_numbers(text, factor):
    def repl(m):
        value = int(m.group(0).replace(",", ""))
        return f"{round(value * factor):,}"
    return re.sub(r"[\d,]*\d", repl, text)


def adapt_demo_plan(template, duration, budget, style):
    """Fit a pre-loaded plan to the requested duration, budget class and style."""
    plan = copy.deepcopy(template)
    plan["duration"] = duration
    plan["budget"] = budget
    plan["style"] = style

    days = plan.get("itinerary") or []
    if days:
        if len(days) >= duration:
            plan["itinerary"] = days[:duration]
        else:
            original = len(days)
            for i in range(original, duration):
                extra = copy.deepcopy(days[i % original])
                extra["day"] = i + 1
                days.append(extra)

    hotels = plan.get("hotels") or []
    overview = plan.setdefault("overview", {})
    if budget == "Budget":
        overview["daily_cost"] = _scale_numbers(overview.get("daily_cost", ""), 0.5)
        hotels = hotels[1:2] or hotels[:1]
    elif budget == "Luxury":
        overview["daily_cost"] = _scale_numbers(overview.get("daily_cost", ""), 4)
        hotels = hotels[2:3] or hotels[:1]
    else:
        hotels = hotels[:1]
    plan["hotels"] = hotels
    return plan


# ---------------------------------------------------------------- photos

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_HEADERS = {"User-Agent": "VoyageAI-TravelPlanner/1.0 (https://github.com/kolambekarpratik123-source/Travel-planner)"}
_STOPWORDS = {"the", "and", "with", "from", "visit", "tour", "local", "lunch", "dinner", "breakfast",
              "evening", "morning", "walk", "explore", "market", "hotel", "city", "day", "trip", "at", "of", "in"}
_wiki_cache = {}


# Gemini can't look up photos, so links it gives to stock-photo sites are guesses
# (a beach for a fort, the Taj Mahal for Jaipur). Those are never shown.
STOCK_PHOTO_HOSTS = ("unsplash.com", "pexels.com", "pixabay.com", "shutterstock.com", "istockphoto.com")


def valid_image(url, _fallback_query=None):
    """The image URL if it can be trusted to show the right place, otherwise "".
    An empty result tells the template to draw a placeholder instead of a wrong photo."""
    if not url or any(p in url for p in GENERIC_PLACEHOLDERS) or any(h in url for h in STOCK_PHOTO_HOSTS):
        return ""
    return url


def _keywords(text):
    return {w for w in re.findall(r"[a-z]+", (text or "").lower()) if len(w) > 3 and w not in _STOPWORDS}


def wiki_photo(name, city="", timeout=6):
    """Photo of a real place from Wikipedia, or "" when no matching article has one.
    The article title must share a word with the place name, so "Breakfast at hotel"
    doesn't come back with a photo of something unrelated."""
    import requests

    key = (name or "").strip().lower(), (city or "").strip().lower()
    if not key[0]:
        return ""
    if key in _wiki_cache:
        return _wiki_cache[key]
    url = ""
    try:
        r = requests.get(WIKI_API, headers=WIKI_HEADERS, timeout=timeout, params={
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": f"{name} {city}".strip(), "gsrlimit": 3,
            "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 800,
        })
        pages = sorted((r.json().get("query") or {}).get("pages", {}).values(),
                       key=lambda p: p.get("index", 99))
        wanted = _keywords(name)
        for page in pages:
            thumb = (page.get("thumbnail") or {}).get("source")
            if thumb and wanted & _keywords(page.get("title")):
                url = thumb
                break
    except (requests.RequestException, ValueError):
        return ""  # don't cache network failures
    _wiki_cache[key] = url
    return url


def attach_photos(plan, timeout=6):
    """Fill in missing photos for the destination, attractions and itinerary stops from
    Wikipedia. Hotels are left alone: a search for a hotel name too often finds a
    different building, and a placeholder is better than a wrong picture."""
    from concurrent.futures import ThreadPoolExecutor

    city = (plan.get("destination") or "").split(",")[0].strip()
    jobs = []  # (holder, key, name, city)
    if not valid_image(plan.get("hero_image")):
        jobs.append((plan, "hero_image", city, ""))
    for spot in plan.get("spots") or []:
        if not valid_image(spot.get("image")):
            jobs.append((spot, "image", spot.get("name"), city))
    for day in plan.get("itinerary") or []:
        for stop in day.get("schedule") or []:
            if not valid_image(stop.get("image")):
                jobs.append((stop, "image", stop.get("spot"), city))
    if not jobs:
        return plan
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda j: wiki_photo(j[2], j[3], timeout), jobs))
    for (holder, key, _n, _c), url in zip(jobs, results):
        if url:
            holder[key] = url
    return plan


# ---------------------------------------------------------------- coordinates

def _stable_offset(name):
    """Small, repeatable offset so unknown places don't all stack on the city centre."""
    h = hashlib.md5(name.encode("utf-8")).digest()
    return (h[0] / 255 - 0.5) * 0.03, (h[1] / 255 - 0.5) * 0.03


def ensure_coordinates(plan):
    """Give every hotel, spot and schedule stop a latitude/longitude."""
    dest = (plan.get("destination") or "").lower()
    center = (20.5937, 78.9629)  # centre of India
    for city, coords in CITY_CENTERS.items():
        if city in dest:
            center = tuple(coords)
            break

    def resolve(name):
        lower = (name or "").lower().strip()
        for key, coords in LANDMARK_COORDS.items():
            if key in lower:
                return coords
        d_lat, d_lng = _stable_offset(lower)
        return [center[0] + d_lat, center[1] + d_lng]

    def fill(item, name_key):
        if not isinstance(item.get("latitude"), (int, float)) or not isinstance(item.get("longitude"), (int, float)):
            item["latitude"], item["longitude"] = resolve(item.get(name_key))

    for h in plan.get("hotels") or []:
        fill(h, "name")
    for s in plan.get("spots") or []:
        fill(s, "name")
    for day in plan.get("itinerary") or []:
        for stop in day.get("schedule") or []:
            fill(stop, "spot")
    return plan


def route_map(plan, schedule, width=420, height=260, pad=28):
    """Points for a schematic route map (hotel -> each stop -> hotel), scaled into an SVG box."""
    hotels = plan.get("hotels") or []
    if not schedule:
        return None
    if hotels:
        base = {"lat": hotels[0]["latitude"], "lng": hotels[0]["longitude"], "label": hotels[0]["name"]}
    else:
        base = {"lat": schedule[0]["latitude"], "lng": schedule[0]["longitude"], "label": "Hotel base"}
    stops = [{"lat": s["latitude"], "lng": s["longitude"], "label": s["spot"], "time": s.get("time", "")}
             for s in schedule]

    pts = [base] + stops
    lats = [p["lat"] for p in pts]
    lngs = [p["lng"] for p in pts]
    min_lat, max_lat = min(lats), max(lats)
    min_lng, max_lng = min(lngs), max(lngs)
    span_lat = max(max_lat - min_lat, 1e-4)
    span_lng = max(max_lng - min_lng, 1e-4)

    def project(p):
        x = pad + (p["lng"] - min_lng) / span_lng * (width - 2 * pad)
        y = pad + (max_lat - p["lat"]) / span_lat * (height - 2 * pad)
        return round(x, 1), round(y, 1)

    base["x"], base["y"] = project(base)
    for s in stops:
        s["x"], s["y"] = project(s)
    line = [(base["x"], base["y"])] + [(s["x"], s["y"]) for s in stops] + [(base["x"], base["y"])]

    waypoints = "/".join(f"{p['lat']},{p['lng']}" for p in [base] + stops)
    return {
        "width": width,
        "height": height,
        "base": base,
        "stops": stops,
        "polyline": " ".join(f"{x},{y}" for x, y in line),
        "google_maps_url": f"https://www.google.com/maps/dir/{waypoints}",
    }


# ---------------------------------------------------------------- AI image check

def drop_broken_images(plan, timeout=5):
    """Gemini often invents Unsplash links that 404. Without JavaScript the page can't swap a
    broken image after it fails, so check each link once on the server and blank the bad ones;
    the page then falls back to a known-good photo for that place."""
    import requests
    from concurrent.futures import ThreadPoolExecutor

    holders = []  # (dict, key) pairs that hold an image URL
    if plan.get("hero_image"):
        holders.append((plan, "hero_image"))
    for group in ("hotels", "spots"):
        for item in plan.get(group) or []:
            if item.get("image"):
                holders.append((item, "image"))
    for day in plan.get("itinerary") or []:
        for stop in day.get("schedule") or []:
            if stop.get("image"):
                holders.append((stop, "image"))

    urls = {h[k] for h, k in holders}

    def works(url):
        if not url.startswith("https://"):
            return url, False
        try:
            r = requests.head(url, timeout=timeout, allow_redirects=True)
            return url, r.ok and r.headers.get("Content-Type", "").startswith("image/")
        except requests.RequestException:
            return url, False

    with ThreadPoolExecutor(max_workers=8) as pool:
        ok = dict(pool.map(works, urls))
    for holder, key in holders:
        if not ok.get(holder[key]):
            holder[key] = ""
    return plan
