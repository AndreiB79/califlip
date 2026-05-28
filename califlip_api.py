#!/usr/bin/env python3
"""
CaliFlip API — модуль bulk screening листингов через RapidAPI Zillow.

Two-stage scoring:
  Stage 1 (Quick): только Zillow data → score 0-100 для всех 200-300 листингов
                   (price-to-Zestimate gap, $/sqft, days on market, year, beds)
  Stage 2 (Deep):  county data + Flip Score для TOP-N quick-кандидатов
                   (медленно ~5 сек/дом, делаем только для шорт-листа)

Используется из califlip_app.py (Streamlit UI).
"""

import os
import re
import time
import json
import requests
import urllib.parse
from pathlib import Path
from datetime import datetime

# Импортируем готовые функции из основного скрипта
from califlip import (
    parse_us_address, find_parcel_by_address,
    get_property_char, get_taxyear_history, get_subject_assessed_value,
    find_neighbor_apns, get_comps_data, get_flood_zone,
    calc_tech_score, estimate_repair_cost,
    ARV_MULT_PESSIMISTIC, ARV_MULT_REALISTIC, ARV_MULT_OPTIMISTIC,
    FLIP_RULE_PERCENT,
    log,
)


# ---------- ENV ----------

def _load_env():
    env = {}
    # Локальная разработка — читаем .env файл
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    # Streamlit Cloud / production — читаем переменные окружения
    for key in ("RAPIDAPI_KEY", "RAPIDAPI_HOST"):
        if key not in env and os.environ.get(key):
            env[key] = os.environ[key]
    return env


_ENV = _load_env()
RAPIDAPI_KEY = _ENV.get("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = "zillow-property-data-api1.p.rapidapi.com"


# ---------- РЕКОМЕНДОВАННЫЕ РЫНКИ ДЛЯ CULVER CITY FLIPPER ----------
# Курировано как реальным флиппером: радиус 1-2 часа, бюджет < $500k,
# фокус на старый фонд и distressed inventory.

ZIP_RECOMMENDATIONS = {
    # === Hemet area (Riverside County) — sweet spot для bulk fixer screening ===
    "92543": {
        "city": "Hemet", "county": "Riverside", "drive_minutes": 100,
        "price_min": 250000, "price_max": 450000, "year_max": 1990, "bed_min": 3,
        "notes": "🥇 SWEET SPOT. Много 70-80х домов, retirees продают estate properties. "
                 "Низкая конкуренция от других флипперов. ARV resale 4-5 мес.",
    },
    "92545": {
        "city": "Hemet", "county": "Riverside", "drive_minutes": 100,
        "price_min": 280000, "price_max": 480000, "year_max": 1995, "bed_min": 3,
        "notes": "Восточный Hemet, чуть новее фонд. Та же стратегия.",
    },
    # === Banning / Beaumont ===
    "92220": {
        "city": "Banning", "county": "Riverside", "drive_minutes": 100,
        "price_min": 250000, "price_max": 450000, "year_max": 1990, "bed_min": 2,
        "notes": "Маленький старый город, ~320 active. Mobile home parks обходим "
                 "(fixer-filter их режет, плюс county data на них не работает).",
    },
    "92223": {
        "city": "Beaumont", "county": "Riverside", "drive_minutes": 100,
        "price_min": 350000, "price_max": 500000, "year_max": 2000, "bed_min": 3,
        "notes": "Беднее Banning, новее фонд (2000-х много). Меньше vintage fixers.",
    },
    # === San Bernardino city ===
    "92401": {
        "city": "San Bernardino", "county": "San Bernardino", "drive_minutes": 90,
        "price_min": 280000, "price_max": 450000, "year_max": 1990, "bed_min": 3,
        "notes": "🥈 Самая высокая плотность distressed/REO/probate в SoCal. "
                 "⚠️ Sketchy зоны — обязательно проверь crime map перед визитом. "
                 "ARV длинный (6-8 мес) из-за reputation города.",
    },
    "92404": {
        "city": "San Bernardino", "county": "San Bernardino", "drive_minutes": 90,
        "price_min": 280000, "price_max": 450000, "year_max": 1990, "bed_min": 3,
        "notes": "Северная часть SB, чуть лучше reputation чем downtown.",
    },
    # === Perris / Moreno Valley ===
    "92570": {
        "city": "Perris", "county": "Riverside", "drive_minutes": 80,
        "price_min": 350000, "price_max": 500000, "year_max": 2000, "bed_min": 3,
        "notes": "Ближе к LA, рост населения. Sweet spot для buy-and-hold + flip "
                 "(можно сдать пока flip готовится).",
    },
    "92551": {
        "city": "Moreno Valley", "county": "Riverside", "drive_minutes": 75,
        "price_min": 400000, "price_max": 550000, "year_max": 1995, "bed_min": 3,
        "notes": "Большой market (500+ active обычно). Высокая конкуренция но и inventory.",
    },
    "92557": {
        "city": "Moreno Valley", "county": "Riverside", "drive_minutes": 75,
        "price_min": 400000, "price_max": 550000, "year_max": 2000, "bed_min": 3,
        "notes": "Восточный Moreno Valley.",
    },
    # === Lake Elsinore ===
    "92530": {
        "city": "Lake Elsinore", "county": "Riverside", "drive_minutes": 70,
        "price_min": 380000, "price_max": 530000, "year_max": 1995, "bed_min": 3,
        "notes": "Озеро = STR (short-term rental) потенциал на exit. "
                 "Старые cabins и старые SFH у воды.",
    },
    # === Rialto / Fontana (SB County, premium edge) ===
    "92376": {
        "city": "Rialto", "county": "San Bernardino", "drive_minutes": 80,
        "price_min": 400000, "price_max": 550000, "year_max": 1995, "bed_min": 3,
        "notes": "Лучше reputation чем SB city. Retail rebound сильный.",
    },
    "92335": {
        "city": "Fontana", "county": "San Bernardino", "drive_minutes": 75,
        "price_min": 420000, "price_max": 580000, "year_max": 1995, "bed_min": 3,
        "notes": "Растущий market, retail сильный. На верхней границе бюджета — "
                 "берёшь fixer за $420, ARV $600+.",
    },
    # === Antelope Valley (LA County north) ===
    "93534": {
        "city": "Lancaster", "county": "Los Angeles", "drive_minutes": 110,
        "price_min": 280000, "price_max": 450000, "year_max": 1990, "bed_min": 3,
        "notes": "Aerospace town, много старого фонда от 70-80х. "
                 "Часто distressed после foreclosure wave 2008. Дальняя дорога.",
    },
    "93535": {
        "city": "Lancaster", "county": "Los Angeles", "drive_minutes": 115,
        "price_min": 300000, "price_max": 470000, "year_max": 1995, "bed_min": 3,
        "notes": "Восточный Lancaster, чуть свежее.",
    },
    "93550": {
        "city": "Palmdale", "county": "Los Angeles", "drive_minutes": 100,
        "price_min": 320000, "price_max": 500000, "year_max": 1995, "bed_min": 3,
        "notes": "Ближе к LA чем Lancaster. Growing commuter market.",
    },
    # === High Desert ===
    "92392": {
        "city": "Victorville", "county": "San Bernardino", "drive_minutes": 100,
        "price_min": 280000, "price_max": 450000, "year_max": 1995, "bed_min": 3,
        "notes": "Cheap inventory, но slower resale (6-9 мес). ARV consideration критичен.",
    },
    "92344": {
        "city": "Hesperia", "county": "San Bernardino", "drive_minutes": 110,
        "price_min": 300000, "price_max": 470000, "year_max": 1995, "bed_min": 3,
        "notes": "Та же тема что Victorville. Дальняя дорога.",
    },
}

# Preset-bundles — выбираешь один dropdown'ом, автоматом 5-6 ZIPов
PRESET_BUNDLES = {
    "🎯 Топ-3 для дебюта (легче начать)": ["92543", "92220", "92401"],
    "🥇 Best Riverside fixers (60-100 мин)": ["92543", "92545", "92220", "92223", "92570", "92551", "92530"],
    "🥈 San Bernardino Valley (75-90 мин)": ["92401", "92404", "92376", "92335"],
    "🌄 Antelope Valley (Lancaster/Palmdale)": ["93534", "93535", "93550"],
    "🏜 High Desert (дёшево, дальше)": ["92392", "92344"],
    "🔥 ВСЁ что я бы скринил еженедельно (вся карта)": list(ZIP_RECOMMENDATIONS.keys()),
}


def get_smart_filters(location_input):
    """
    Вернёт recommended filters если location_input содержит known ZIP.
    Принимает '92543' или 'Hemet, CA 92543' — извлекает ZIP regex'ом.
    """
    if not location_input:
        return None
    m = re.search(r"\b(\d{5})\b", str(location_input))
    if not m:
        return None
    return ZIP_RECOMMENDATIONS.get(m.group(1))


# ---------- COUNTY DETECTION ----------

# Карта city → county для наиболее частых California cities.
# Используется для решения: делать ли deep county lookup или Zillow-only.
CITY_TO_COUNTY = {
    # Riverside County (наш fully supported county — есть deep county data)
    "BANNING": "Riverside", "BEAUMONT": "Riverside", "CHERRY VALLEY": "Riverside",
    "RIVERSIDE": "Riverside", "CORONA": "Riverside", "MORENO VALLEY": "Riverside",
    "HEMET": "Riverside", "PALM SPRINGS": "Riverside", "INDIO": "Riverside",
    "MURRIETA": "Riverside", "TEMECULA": "Riverside", "PERRIS": "Riverside",
    "LAKE ELSINORE": "Riverside", "MENIFEE": "Riverside", "WILDOMAR": "Riverside",
    "JURUPA VALLEY": "Riverside", "EASTVALE": "Riverside", "NORCO": "Riverside",
    "DESERT HOT SPRINGS": "Riverside", "CATHEDRAL CITY": "Riverside", "COACHELLA": "Riverside",
    "LA QUINTA": "Riverside", "RANCHO MIRAGE": "Riverside", "PALM DESERT": "Riverside",
    "BLYTHE": "Riverside", "IDYLLWILD": "Riverside", "WINCHESTER": "Riverside",
    # Los Angeles County
    "LOS ANGELES": "Los Angeles", "VAN NUYS": "Los Angeles", "CULVER CITY": "Los Angeles",
    "BEVERLY HILLS": "Los Angeles", "SANTA MONICA": "Los Angeles", "PASADENA": "Los Angeles",
    "LONG BEACH": "Los Angeles", "GLENDALE": "Los Angeles", "BURBANK": "Los Angeles",
    "INGLEWOOD": "Los Angeles", "TORRANCE": "Los Angeles", "COMPTON": "Los Angeles",
    "WEST HOLLYWOOD": "Los Angeles", "HOLLYWOOD": "Los Angeles", "SHERMAN OAKS": "Los Angeles",
    "STUDIO CITY": "Los Angeles", "WOODLAND HILLS": "Los Angeles", "ENCINO": "Los Angeles",
    "NORTH HOLLYWOOD": "Los Angeles", "RESEDA": "Los Angeles", "TARZANA": "Los Angeles",
    "DOWNEY": "Los Angeles", "EL MONTE": "Los Angeles", "WHITTIER": "Los Angeles",
    "LANCASTER": "Los Angeles", "PALMDALE": "Los Angeles", "POMONA": "Los Angeles",
    "WEST COVINA": "Los Angeles", "NORWALK": "Los Angeles", "CARSON": "Los Angeles",
    "SANTA CLARITA": "Los Angeles", "MALIBU": "Los Angeles", "MANHATTAN BEACH": "Los Angeles",
    "REDONDO BEACH": "Los Angeles", "HERMOSA BEACH": "Los Angeles", "ALHAMBRA": "Los Angeles",
    "MONTEREY PARK": "Los Angeles", "ARCADIA": "Los Angeles", "ROSEMEAD": "Los Angeles",
    # San Bernardino County
    "SAN BERNARDINO": "San Bernardino", "FONTANA": "San Bernardino", "ONTARIO": "San Bernardino",
    "RANCHO CUCAMONGA": "San Bernardino", "REDLANDS": "San Bernardino", "CHINO": "San Bernardino",
    "UPLAND": "San Bernardino", "VICTORVILLE": "San Bernardino", "HESPERIA": "San Bernardino",
    "APPLE VALLEY": "San Bernardino", "BIG BEAR LAKE": "San Bernardino", "HIGHLAND": "San Bernardino",
    "YUCAIPA": "San Bernardino", "COLTON": "San Bernardino", "RIALTO": "San Bernardino",
    "CHINO HILLS": "San Bernardino", "MONTCLAIR": "San Bernardino", "BARSTOW": "San Bernardino",
    # Orange County
    "ANAHEIM": "Orange", "IRVINE": "Orange", "SANTA ANA": "Orange", "HUNTINGTON BEACH": "Orange",
    "GARDEN GROVE": "Orange", "FULLERTON": "Orange", "ORANGE": "Orange", "COSTA MESA": "Orange",
    "MISSION VIEJO": "Orange", "NEWPORT BEACH": "Orange", "LAGUNA BEACH": "Orange",
    "LAGUNA NIGUEL": "Orange", "ALISO VIEJO": "Orange", "LAKE FOREST": "Orange",
    "TUSTIN": "Orange", "YORBA LINDA": "Orange", "BREA": "Orange", "BUENA PARK": "Orange",
    "CYPRESS": "Orange", "DANA POINT": "Orange", "FOUNTAIN VALLEY": "Orange",
    "PLACENTIA": "Orange", "SAN CLEMENTE": "Orange", "WESTMINSTER": "Orange",
    # San Diego County
    "SAN DIEGO": "San Diego", "CHULA VISTA": "San Diego", "OCEANSIDE": "San Diego",
    "ESCONDIDO": "San Diego", "CARLSBAD": "San Diego", "EL CAJON": "San Diego",
    "VISTA": "San Diego", "SAN MARCOS": "San Diego", "ENCINITAS": "San Diego",
    "POWAY": "San Diego", "LA MESA": "San Diego", "SANTEE": "San Diego",
    "NATIONAL CITY": "San Diego", "DEL MAR": "San Diego", "RAMONA": "San Diego",
}

# Fallback по ZIP-range (если city не в карте). Approximate, есть overlaps между counties.
ZIP_RANGES_TO_COUNTY = [
    # Riverside County
    ((92201, 92276), "Riverside"),
    ((92501, 92599), "Riverside"),
    ((92860, 92883), "Riverside"),
    # Los Angeles County
    ((90001, 90899), "Los Angeles"),
    ((91040, 91609), "Los Angeles"),
    ((93510, 93599), "Los Angeles"),
    # San Bernardino County
    ((91708, 91792), "San Bernardino"),
    ((92301, 92345), "San Bernardino"),
    ((92350, 92408), "San Bernardino"),
    # Orange County
    ((90620, 90680), "Orange"),
    ((92602, 92899), "Orange"),
    # San Diego County
    ((91901, 92199), "San Diego"),
]


def detect_county(city, zip_code):
    """
    Возвращает имя California county по city/zip.
    Используется чтобы решить, делать ли deep county lookup или Zillow-only.
    """
    if city:
        county = CITY_TO_COUNTY.get(city.strip().upper())
        if county:
            return county
    # Fallback на ZIP range
    if zip_code:
        try:
            z = int(str(zip_code)[:5])
            for (lo, hi), county in ZIP_RANGES_TO_COUNTY:
                if lo <= z <= hi:
                    return county
        except (ValueError, TypeError):
            pass
    return "Unknown"


def has_full_county_support(county):
    """Только Riverside у нас сейчас имеет deep county API integration."""
    return county == "Riverside"


# ---------- BULK ZILLOW FETCH ----------

def fetch_zillow_listings(location, price_min=None, price_max=None,
                          bed_min=None, year_max=None, max_pages=3,
                          on_progress=None):
    """
    Получает все active listings в указанной локации через RapidAPI.

    location: "Banning, CA 92220" или "Riverside, CA" или просто "92220"
    price_min/max: optional фильтр $
    bed_min: optional фильтр спален
    year_max: optional фильтр года постройки (для флипа — старше = лучше)
    on_progress: callback(page, total_pages, count) для UI прогресс-бара

    Returns: list[dict] — нормализованные listings с полями:
        zpid, address, price, zestimate, beds, baths, sqft, year, days_on_market,
        property_type, lot_sqft, tax_assessed, photo_url, zillow_url
    """
    if not RAPIDAPI_KEY:
        raise RuntimeError("RAPIDAPI_KEY не найден в .env. Запусти setup.")

    base_params = {
        "location": location,
        "listingStatus": "For_Sale",
        "homeType": "houses",  # отсекает mobile/condos/apartments автоматически
    }
    if price_min or price_max:
        lo = price_min or 0
        hi = price_max or 9999999
        base_params["listPriceRange"] = f"{lo}-{hi}"
    if bed_min:
        base_params["bed_min"] = bed_min
    if year_max:
        base_params["built_max"] = year_max

    headers = {
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key": RAPIDAPI_KEY,
    }
    url = f"https://{RAPIDAPI_HOST}/api/zillow/search/byaddress"

    listings = []
    total_pages_known = None

    for page in range(1, max_pages + 1):
        params = dict(base_params, page=page)
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 429:
                time.sleep(2)
                r = requests.get(url, params=params, headers=headers, timeout=30)
            data = r.json() if r.text else {}
        except (requests.RequestException, json.JSONDecodeError) as e:
            log(f"Ошибка fetch page {page}: {e}", "WARN")
            raise RuntimeError(f"Сетевая ошибка при запросе к RapidAPI: {e}")

        # Явная проверка на quota / rate limit / auth errors
        msg = (data.get("message") or "")
        if "exceeded" in msg.lower() and "quota" in msg.lower():
            raise RuntimeError(
                "🚫 Исчерпана месячная квота RapidAPI на тарифе BASIC (50 запросов/мес). "
                "Нужно апгрейдить план — см. инструкцию в чате. "
                "Квота сбрасывается 1-го числа каждого месяца, "
                "ИЛИ можно подписаться на PRO за $10/мес для 3000+ запросов."
            )
        if r.status_code == 429:
            raise RuntimeError(f"Rate limit 429 от RapidAPI: {msg}")
        if r.status_code == 401 or r.status_code == 403:
            raise RuntimeError(f"Auth error {r.status_code} — проверь RAPIDAPI_KEY в .env")
        if r.status_code != 200:
            raise RuntimeError(f"RapidAPI HTTP {r.status_code}: {msg or 'неизвестная ошибка'}")

        page_results = data.get("searchResults", [])
        if not page_results:
            break

        for r in page_results:
            normalized = _normalize_zillow_listing(r.get("property", {}))
            if normalized:
                listings.append(normalized)

        total_pages_known = data.get("pagesInfo", {}).get("totalPages", 1)
        if on_progress:
            on_progress(page, total_pages_known, len(listings))

        if page >= total_pages_known:
            break

        time.sleep(1.2)  # rate limit: BASIC plan = 1 req/sec

    return listings


def _extract_highlights(prop):
    """Достаёт Zillow highlights из listCardRecommendation.flexFieldRecommendations."""
    recs = (prop.get("listCardRecommendation") or {}).get("flexFieldRecommendations") or []
    return [r.get("displayString", "") for r in recs if r.get("displayString")]


def fixer_score(listing):
    """
    Score 0-100 — насколько дом похож на fixer-upper (для investor flip),
    а не на move-in-ready (для retail buyer).

    Сигналы (description у нас нет, обходимся косвенными):
      + Price cut в highlights (motivated seller)
      + Days on market high
      + Price < Zestimate (дисконт за состояние)
      + $/sqft низкое
      + Старый дом (1950-1980)
      − Premium highlights (3D tour, pool, новый ремонт)

    Returns: (score 0-100, list[reasons])
    """
    score = 50  # neutral baseline
    reasons = []

    price = listing.get("price") or 0
    zest = listing.get("zestimate") or 0
    psqft = listing.get("price_per_sqft") or 0
    days = listing.get("days_on_market") or 0
    year = listing.get("year_built") or 0
    highlights = listing.get("highlights") or []

    # Price cut — ОЧЕНЬ сильный fixer/motivated сигнал
    has_price_cut = any("price cut" in h.lower() or "price reduced" in h.lower() for h in highlights)
    if has_price_cut:
        score += 25
        cut_text = next((h for h in highlights if "price cut" in h.lower()), "Price cut")
        reasons.append(f"🔥 {cut_text} — motivated seller (+25)")

    # Days on market — высокий = долго не могут продать = что-то не так
    if days and days >= 90:
        score += 20
        reasons.append(f"{days}d on market — серьёзно засиделся (+20)")
    elif days and days >= 45:
        score += 12
        reasons.append(f"{days}d on market — долговато (+12)")
    elif days and days >= 21:
        score += 5
        reasons.append(f"{days}d on market (+5)")
    elif days and days <= 7:
        score -= 10
        reasons.append(f"Только {days}d — горячий новый листинг, fixer не успел бы (−10)")

    # Цена vs Zestimate — дисконт обычно за состояние
    if price and zest:
        gap_pct = (zest - price) / zest * 100
        if gap_pct >= 15:
            score += 20
            reasons.append(f"Цена на {gap_pct:.0f}% ниже Zestimate — дисконт за состояние (+20)")
        elif gap_pct >= 5:
            score += 10
            reasons.append(f"Цена на {gap_pct:.0f}% ниже Zestimate (+10)")
        elif gap_pct <= -10:
            score -= 15
            reasons.append(f"Цена на {abs(gap_pct):.0f}% выше Zestimate — переоценка, не fixer (−15)")

    # $/sqft — низкое = вероятно condition discount
    if psqft and psqft < 200:
        score += 10
        reasons.append(f"${psqft}/sqft — низко для CA, condition discount? (+10)")
    elif psqft and psqft > 500:
        score -= 15
        reasons.append(f"${psqft}/sqft — высоко, это move-in ready (−15)")

    # Год постройки — старые дома чаще нужен ремонт
    if year and 1950 <= year <= 1980:
        score += 8
        reasons.append(f"{year} — старый дом, оригинальные кухня/ванные? (+8)")
    elif year and year >= 2010:
        score -= 15
        reasons.append(f"{year} — новый дом, ремонтировать нечего (−15)")

    # Premium highlights — отрицательные сигналы
    NEG_KEYWORDS = ["3d tour", "sparkling pool", "remodel", "renovated", "updated",
                    "new kitchen", "new bath", "new roof", "new hvac", "turnkey",
                    "move-in", "move in ready", "open house", "pride of ownership"]
    POS_KEYWORDS = ["price cut", "price reduced", "as-is", "as is", "fixer",
                    "needs work", "tlc", "investor", "estate sale", "probate",
                    "foreclosure", "bank owned", "reo", "short sale", "cash only"]

    for h in highlights:
        hl = h.lower()
        for kw in POS_KEYWORDS:
            if kw in hl and not any(kw in r.lower() for r in reasons):
                score += 10
                reasons.append(f"Highlight: '{h}' — fixer indicator (+10)")
                break
        for kw in NEG_KEYWORDS:
            if kw in hl:
                score -= 8
                reasons.append(f"Highlight: '{h}' — premium, не fixer (−8)")
                break

    return max(0, min(100, score)), reasons


def _normalize_zillow_listing(prop):
    """Превращает raw Zillow API response в плоский dict."""
    if not prop:
        return None
    addr = prop.get("address") or {}
    price = prop.get("price") or {}
    est = prop.get("estimates") or {}
    listing = prop.get("listing") or {}
    tax = prop.get("taxAssessment") or {}
    lot = prop.get("lotSizeWithUnit") or {}
    media = prop.get("media") or {}
    photos = media.get("propertyPhotoLinks") or {}

    zpid = prop.get("zpid")
    street = addr.get("streetAddress", "")
    city = addr.get("city", "")
    state = addr.get("state", "")
    zipc = addr.get("zipcode", "")

    # Sanity-cap для days_on_market — API иногда возвращает 3000+ (явный глюк
    # их парсера; реальный максимум "висения" листинга на Zillow ~2 года).
    days = prop.get("daysOnZillow")
    if days and days > 730:
        days = None  # лучше показать "—" чем абсурд

    return {
        "zpid": zpid,
        "address_street": street,
        "address_full": f"{street}, {city}, {state} {zipc}".strip(", "),
        "city": city,
        "state": state,
        "zip": zipc,
        "price": price.get("value"),
        "price_per_sqft": price.get("pricePerSquareFoot"),
        "zestimate": est.get("zestimate"),
        "rent_zestimate": est.get("rentZestimate"),
        "beds": prop.get("bedrooms"),
        "baths": prop.get("bathrooms"),
        "sqft": prop.get("livingArea"),
        "year_built": prop.get("yearBuilt"),
        "lot_sqft": lot.get("lotSize") if lot.get("lotSizeUnit") == "squareFeet" else None,
        "property_type": prop.get("propertyType"),
        "days_on_market": days,
        "tax_assessed": tax.get("taxAssessedValue"),
        "listing_status": listing.get("listingStatus"),
        "photo_url": photos.get("mediumSizeLink"),
        "zillow_url": f"https://www.zillow.com/homedetails/{zpid}_zpid/" if zpid else None,
        "lat": (prop.get("location") or {}).get("latitude"),
        "lon": (prop.get("location") or {}).get("longitude"),
        "highlights": _extract_highlights(prop),
    }


def apply_client_filters(listings, price_min=None, price_max=None,
                          bed_min=None, year_max=None):
    """
    Client-side фильтрация после API fetch — потому что RapidAPI игнорирует
    наши max-параметры (price_max, year_max). Фильтруем здесь.

    Returns: (filtered_list, stats_dict)
    stats_dict содержит счётчик «сколько отвалилось каждым фильтром»
    для диагностики «почему 0 осталось».
    """
    stats = {
        "input_count": len(listings),
        "dropped_price_min": 0,
        "dropped_price_max": 0,
        "dropped_bed_min": 0,
        "dropped_year_max": 0,
        "kept": 0,
    }
    out = []
    for l in listings:
        price = l.get("price") or 0
        if price_min and price < price_min:
            stats["dropped_price_min"] += 1
            continue
        if price_max and price > price_max:
            stats["dropped_price_max"] += 1
            continue
        beds = l.get("beds") or 0
        if bed_min and beds < bed_min:
            stats["dropped_bed_min"] += 1
            continue
        year = l.get("year_built") or 0
        if year_max and year > year_max:
            stats["dropped_year_max"] += 1
            continue
        out.append(l)
    stats["kept"] = len(out)
    return out, stats


# ---------- STAGE 1: QUICK SCORE (только Zillow data, no county) ----------

def quick_score(listing):
    """
    Считает 0-100 score только по Zillow данным — без county lookup.
    Используется для ранжирования всех 200-300 листингов перед deep-dive.

    Сигналы (как реально оценивают флипперы):
      1. Цена vs Zestimate — under = дисконт (max 25)
      2. $/sqft — низкое = недооценка (max 20)
      3. Days on market — high = motivated seller (max 20)
      4. Год постройки — sweet spot 1950-1985 = больше equity потенциал (max 20)
      5. Беды — 3+ продаётся легче (max 15)
    """
    score = 0
    reasons = []

    price = listing.get("price") or 0
    zest = listing.get("zestimate") or 0
    psqft = listing.get("price_per_sqft") or 0
    days = listing.get("days_on_market") or 0
    year = listing.get("year_built") or 0
    beds = listing.get("beds") or 0

    # 1. Zestimate gap
    if price and zest:
        gap_pct = (zest - price) / zest * 100
        if gap_pct >= 15:
            score += 25
            reasons.append(f"Цена на {gap_pct:.0f}% ниже Zestimate (+25)")
        elif gap_pct >= 5:
            score += 15
            reasons.append(f"Цена на {gap_pct:.0f}% ниже Zestimate (+15)")
        elif gap_pct >= -5:
            score += 5
            reasons.append(f"Цена ≈ Zestimate (+5)")
        else:
            reasons.append(f"Переоценка {abs(gap_pct):.0f}% к Zestimate (0)")

    # 2. $/sqft (для California 35-55$/sqft — норм, <250 = дисконт)
    if psqft:
        if psqft < 200:
            score += 20
            reasons.append(f"${psqft}/sqft — низко для CA (+20)")
        elif psqft < 300:
            score += 12
            reasons.append(f"${psqft}/sqft — норм (+12)")
        elif psqft < 400:
            score += 5
            reasons.append(f"${psqft}/sqft — средне (+5)")

    # 3. Days on market — motivated seller signal
    if days >= 60:
        score += 20
        reasons.append(f"{days} дней на рынке — продавец устал (+20)")
    elif days >= 30:
        score += 12
        reasons.append(f"{days} дней — есть place торговаться (+12)")
    elif days >= 14:
        score += 5
        reasons.append(f"{days} дней (+5)")

    # 4. Год постройки (sweet spot для флипа)
    if year:
        if 1950 <= year <= 1985:
            score += 20
            reasons.append(f"{year} — sweet spot для флипа (+20)")
        elif 1986 <= year <= 2000:
            score += 10
            reasons.append(f"{year} — средний возраст (+10)")
        elif year < 1950:
            score += 8
            reasons.append(f"{year} — старый, проверить фундамент (+8)")

    # 5. Bedrooms (sellability)
    if beds and beds >= 3:
        score += 15
        reasons.append(f"{beds} спален — продаётся легко (+15)")
    elif beds == 2:
        score += 5
        reasons.append(f"2 спальни — узкий рынок (+5)")

    return max(0, min(100, score)), reasons


# ---------- STAGE 2: DEEP DIVE с county data ----------

def _zillow_only_tech_score(listing):
    """Tech score из Zillow данных (для не-Riverside где нет county data)."""
    # Адаптируем calc_tech_score: он ожидает county char структуру,
    # переименовываем поля под него.
    fake_char = {
        "YEAR_BUILT": listing.get("year_built"),
        "LIVING_AREA": listing.get("sqft"),
        "BEDROOM_COUNT": listing.get("beds"),
        "BATH_COUNT": listing.get("baths"),
    }
    return calc_tech_score(fake_char)


def deep_analysis(listing):
    """
    Полный анализ одного дома.

    Для Riverside County → deep с county Assessor (APN, comps, tax history, FEMA).
    Для остальных California → Zillow-only анализ (Zestimate как ARV, beds/sqft/year из Zillow).
    """
    full_addr = listing.get("address_full") or ""
    parsed = parse_us_address(full_addr)
    county = detect_county(listing.get("city"), listing.get("zip"))

    # Recent sold comps + realistic offer — это самое важное для real ARV
    sold_comps = fetch_sold_comps(
        zip_code=listing.get("zip"),
        beds=listing.get("beds"),
        sqft=listing.get("sqft"),
        max_results=15,
    )
    real_arv, median_psqft, arv_source_text = compute_arv_from_sold(sold_comps, listing.get("sqft"))
    offer_price, offer_discount_pct, offer_reasoning = realistic_offer_price(listing)

    result = {
        "listing": listing,
        "parsed_address": parsed,
        "county_name": county,
        "county_apn": None,
        "county_match": "none",  # exact / nearest / none / zillow_only
        "county_char": {},
        "county_tax_history": [],
        "county_subject_assessed": None,
        "comps": [],
        "comps_median": None,
        "comps_q75": None,
        "flood": {"zone": "?", "risk": "low"},
        "tech_score": 0,
        "tech_breakdown": [],
        "flip_scores": {},
        # NEW: real-world data
        "sold_comps": sold_comps,
        "real_arv": real_arv,
        "median_psqft": median_psqft,
        "arv_source_text": arv_source_text,
        "realistic_offer": offer_price,
        "offer_discount_pct": offer_discount_pct,
        "offer_reasoning": offer_reasoning,
    }

    # ---- Branch 1: не Riverside → Zillow-only ----
    if not has_full_county_support(county):
        tech_score, tech_breakdown = _zillow_only_tech_score(listing)
        result["tech_score"] = tech_score
        result["tech_breakdown"] = tech_breakdown
        result["county_match"] = "zillow_only"

        # FEMA flood можем достать по координатам из Zillow
        if listing.get("lat") and listing.get("lon"):
            coords = {"lat": listing["lat"], "lon": listing["lon"]}
            result["flood"] = get_flood_zone(coords)

        # ARV для Zillow-only = Zestimate (если есть) или taxAssessed
        arv_base = listing.get("zestimate") or listing.get("tax_assessed") or 0
        # Для Zillow-only multipliers около 1.0 (Zestimate уже = market estimate,
        # tax_assessed обычно ниже market по Prop 13)
        if listing.get("zestimate"):
            # Zestimate ≈ market → используем меньшие multipliers
            arv_for_scores = listing["zestimate"]
            multiplier_offset = 1.0  # Zestimate уже market
        else:
            # taxAssessed → Prop 13 gap, нужны higher multipliers
            arv_for_scores = listing.get("tax_assessed") or 0
            multiplier_offset = ARV_MULT_REALISTIC

        result["flip_scores"] = _compute_flip_scores(
            listing.get("price") or 0,
            listing,
            arv_for_scores * multiplier_offset,
            tech_score,
            result["flood"]["risk"],
            county=county,
        )
        return result

    # ---- Branch 2: Riverside → full county data ----
    if not parsed:
        return result

    apn, matched, _nearby = find_parcel_by_address(parsed)
    if apn:
        result["county_apn"] = apn
        result["county_matched_address"] = matched
        result["county_match"] = "exact"
    else:
        result["county_match"] = "none"
        return result

    result["county_char"] = get_property_char(apn)
    result["county_tax_history"] = get_taxyear_history(apn)
    subj, _ = get_subject_assessed_value(result["county_tax_history"])
    result["county_subject_assessed"] = subj

    coords = None
    if listing.get("lat") and listing.get("lon"):
        coords = {"lat": listing["lat"], "lon": listing["lon"]}
    if coords:
        neighbors = find_neighbor_apns(coords)
        comps = get_comps_data(neighbors, apn)
        result["comps"] = comps
        if comps:
            values = sorted(c["total_value"] for c in comps if c.get("total_value"))
            result["comps_median"] = values[len(values) // 2] if values else None
            result["comps_q75"] = values[int(len(values) * 0.75)] if len(values) >= 4 else result["comps_median"]
        result["flood"] = get_flood_zone(coords)

    tech_score, tech_breakdown = calc_tech_score(result["county_char"])
    result["tech_score"] = tech_score
    result["tech_breakdown"] = tech_breakdown

    result["flip_scores"] = _compute_flip_scores(
        listing.get("price") or 0,
        listing,
        result["comps_q75"] or result["comps_median"] or 0,
        tech_score,
        result["flood"]["risk"],
        county=county,
    )

    return result


def realistic_offer_price(listing):
    """
    Реальная цель торга для active Zillow listing.
    Использует days_on_market + price cut signals из Zillow highlights.

    Возвращает (offer_price, discount_pct, reasoning).
    """
    listing_price = listing.get("price") or 0
    days = listing.get("days_on_market") or 0
    highlights = listing.get("highlights") or []

    has_price_cut = any("price cut" in h.lower() or "price reduced" in h.lower() for h in highlights)
    cut_count = sum(1 for h in highlights if "price cut" in h.lower())

    if days >= 120 and (cut_count >= 1 or has_price_cut):
        discount = 0.25
        reason = f"📉 {days}d + было снижение цены — distressed signals → можешь предложить −25%"
    elif days >= 90 and has_price_cut:
        discount = 0.18
        reason = f"📉 {days}d + price cut → разумно предложить −18%"
    elif days >= 90:
        discount = 0.15
        reason = f"📉 {days}d на рынке (без price cut) → продавец устал, −15% реалистично"
    elif days >= 60:
        discount = 0.12
        reason = f"⏰ {days}d — застойный листинг, −12% попробуй"
    elif days >= 30:
        discount = 0.08
        reason = f"⏰ {days}d — некоторая гибкость, −8% приличный shot"
    elif days >= 14:
        discount = 0.05
        reason = f"🆕 {days}d — относительно новый, −5% максимум"
    else:
        discount = 0.03
        reason = f"🔥 Только {days}d на рынке — горячий, больше −3-5% не получишь"

    offer = listing_price * (1 - discount)
    return int(offer), discount, reason


def estimate_profit_quick(listing, sold_comps_cache=None):
    """
    Быстрая оценка прибыли БЕЗ полного deep_analysis (для bulk filtering).
    Использует sold_comps из кэша (или Zestimate fallback) + realistic offer.

    Returns dict {estimated_profit, realistic_offer, real_arv, arv_source}
    """
    listing_price = listing.get("price") or 0
    sqft = listing.get("sqft") or 0
    zestimate = listing.get("zestimate") or 0
    zip_code = listing.get("zip")

    if not listing_price or not sqft:
        return {"estimated_profit": 0, "realistic_offer": 0, "real_arv": 0, "arv_source": "no data"}

    # ARV из sold_comps (если в кэше) или из Zestimate
    real_arv = 0
    arv_source = ""
    if sold_comps_cache and zip_code in sold_comps_cache:
        all_sold = sold_comps_cache[zip_code]
        # Filter по похожему sqft ±25% и беды ±1
        similar = [
            s for s in all_sold
            if s.get("sqft") and sqft * 0.75 <= s["sqft"] <= sqft * 1.25
        ]
        if similar:
            psqfts = sorted([s["price_per_sqft"] for s in similar if s.get("price_per_sqft")])
            if psqfts:
                median_psqft = psqfts[len(psqfts) // 2]
                real_arv = int(median_psqft * sqft)
                arv_source = f"sold comps (n={len(similar)}, median ${median_psqft}/sqft)"

    if not real_arv and zestimate:
        real_arv = zestimate
        arv_source = "Zestimate (sold comps не нашлись)"

    if not real_arv:
        return {"estimated_profit": 0, "realistic_offer": 0, "real_arv": 0, "arv_source": "нет данных"}

    # Realistic offer
    offer_price, _, _ = realistic_offer_price(listing)

    # Repair estimate
    repair, _ = estimate_repair_cost("", sqft)

    # Net profit
    econ = _estimate_flip_economics(real_arv, offer_price, repair)

    return {
        "estimated_profit": int(econ.get("net_profit", 0)),
        "realistic_offer": offer_price,
        "real_arv": real_arv,
        "arv_source": arv_source,
        "repair": repair,
    }


def fetch_sold_comps(zip_code, beds=None, sqft=None, max_results=20):
    """
    Recent sold comps в радиусе ZIP'а за последние 6-12 мес.
    Используется как realistic ARV proxy (вместо Zestimate-гаданий).

    Returns: list of dicts {address, sold_date, sold_price, sqft, beds, baths, $/sqft}
    """
    if not RAPIDAPI_KEY or not zip_code:
        return []
    headers = {"x-rapidapi-host": RAPIDAPI_HOST, "x-rapidapi-key": RAPIDAPI_KEY}
    url = f"https://{RAPIDAPI_HOST}/api/zillow/search/byaddress"

    params = {
        "location": str(zip_code),
        "listingStatus": "Sold",
        "homeType": "houses",
    }
    if beds:
        params["bed_min"] = max(beds - 1, 1)
        params["bed_max"] = beds + 1

    try:
        r = requests.get(url, params=params, headers=headers, timeout=30)
        if r.status_code != 200:
            log(f"Sold comps fetch failed: HTTP {r.status_code}", "WARN")
            return []
        data = r.json()
        if data.get("message") and "exceeded" in data["message"].lower():
            log("RapidAPI quota exceeded — sold comps skipped", "WARN")
            return []
    except Exception as e:
        log(f"Sold comps error: {e}", "WARN")
        return []

    sold = []
    for r in data.get("searchResults", [])[:max_results * 3]:  # filter potential later
        prop = r.get("property", {})
        sold_p = (prop.get("price") or {}).get("value")
        sf = prop.get("livingArea")
        if not sold_p or not sf:
            continue
        sold.append({
            "zpid": prop.get("zpid"),
            "address": (prop.get("address") or {}).get("streetAddress", ""),
            "sold_price": sold_p,
            "sqft": sf,
            "beds": prop.get("bedrooms"),
            "baths": prop.get("bathrooms"),
            "year": prop.get("yearBuilt"),
            "price_per_sqft": int(sold_p / sf) if sf else None,
        })

    # Если есть наш sqft — оставляем только похожие (±25%)
    if sqft:
        sqft_min = sqft * 0.75
        sqft_max = sqft * 1.25
        sold = [s for s in sold if sqft_min <= s["sqft"] <= sqft_max]

    sold.sort(key=lambda s: s["price_per_sqft"] or 0)
    return sold[:max_results]


def compute_arv_from_sold(sold_comps, our_sqft):
    """
    Реальный ARV на основе recent sold comps.
    Возвращает (arv, median_psqft, source_text).
    """
    if not sold_comps or not our_sqft:
        return None, None, "Нет sold comps — используем Zestimate"
    psqfts = sorted([s["price_per_sqft"] for s in sold_comps if s.get("price_per_sqft")])
    if not psqfts:
        return None, None, "Sold comps без цен"
    median_psqft = psqfts[len(psqfts) // 2]
    arv = median_psqft * our_sqft
    return int(arv), median_psqft, f"медиана ${median_psqft}/sqft × {our_sqft} sqft (n={len(sold_comps)} продаж)"


def _estimate_flip_economics(arv, purchase_price, repair_cost, hold_months=5):
    """
    Считает реальную чистую прибыль флипа после ВСЕХ расходов.

    Расходы включены в 75% rule, но юзеру важно видеть конкретные $$$ —
    "если куплю за X и продам за ARV, в карман возьму Y".

    Расходы:
      - Buying costs (~1.5% ARV): inspection, appraisal, title insurance
      - Carrying costs (5 мес дефолт): tax 1.15%/year, insurance, utilities
      - Selling costs (7% ARV): agent 5-6%, title/escrow 1%, transfer tax
    """
    if not arv or not purchase_price:
        return {"net_profit": 0, "roi_total": 0, "roi_annualized": 0, "costs_breakdown": {}}

    # Buying costs (one-time)
    buying = purchase_price * 0.015

    # Carrying costs (по месяцам)
    monthly_tax = purchase_price * 0.0115 / 12  # CA property tax
    monthly_insurance = 150
    monthly_utilities = 250
    carrying = (monthly_tax + monthly_insurance + monthly_utilities) * hold_months

    # Selling costs
    selling = arv * 0.07

    total_costs = buying + carrying + selling
    total_invested = purchase_price + repair_cost + total_costs
    net_profit = arv - total_invested

    roi_total = (net_profit / total_invested * 100) if total_invested > 0 else 0
    roi_annualized = roi_total * (12 / hold_months) if hold_months > 0 else 0

    return {
        "net_profit": net_profit,
        "roi_total": roi_total,
        "roi_annualized": roi_annualized,
        "total_invested": total_invested,
        "costs_breakdown": {
            "buying": buying,
            "carrying": carrying,
            "selling": selling,
            "total_costs": total_costs,
        },
        "hold_months": hold_months,
    }


def _compute_flip_scores(price, listing, arv_base_comps, tech_score, flood_risk, county=None):
    """
    Compute 3-scenario Flip Score (pessimistic/realistic/optimistic).

    Key logic:
    - ARV base = Zestimate (если есть, более точно) OR comps × multiplier (fallback)
    - Flip rule = 75% (Inland Empire default) OR 80% (LA County coastal)
    - Возвращает FULL breakdown для каждого сценария — чтобы UI показал
      пошаговую формулу «откуда взялась эта цифра».
    """
    if not price:
        return {}

    sqft = listing.get("sqft") or 0
    repair, repair_label = estimate_repair_cost("", sqft)
    zestimate = listing.get("zestimate") or 0

    # Choose flip rule by county (LA = быстрее exit = можно 80%)
    if county == "Los Angeles":
        rule_pct = 0.80
        rule_label = "80% правило (LA / coastal — быстрый exit)"
    else:
        rule_pct = FLIP_RULE_PERCENT  # 0.75 для Inland Empire default
        rule_label = "75% правило (Inland Empire / slow exit)"

    # Confidence score — насколько доверять расчёту
    confidence = 50  # baseline
    confidence_reasons = []

    if zestimate and price:
        confidence += 25
        confidence_reasons.append("✓ Есть Zestimate от Zillow (+25)")
    else:
        confidence_reasons.append("✗ Нет Zestimate — полагаемся только на assessed comps (−)")
    if arv_base_comps and arv_base_comps > 0:
        confidence += 15
        confidence_reasons.append("✓ Есть assessed comps по соседям (+15)")
    if listing.get("days_on_market") is not None:
        confidence += 5
        confidence_reasons.append("✓ Известны days on market (+5)")
    if tech_score >= 15:
        confidence += 5
        confidence_reasons.append("✓ Полные характеристики дома (+5)")
    confidence = max(0, min(100, confidence))

    scores = {"_meta": {
        "repair": repair,
        "repair_label": repair_label,
        "sqft": sqft,
        "rule_pct": rule_pct,
        "rule_label": rule_label,
        "zestimate": zestimate,
        "arv_base_comps": arv_base_comps,
        "confidence": confidence,
        "confidence_reasons": confidence_reasons,
        "price": price,
    }}

    for label, mult in [("pessimistic", ARV_MULT_PESSIMISTIC),
                        ("realistic",   ARV_MULT_REALISTIC),
                        ("optimistic",  ARV_MULT_OPTIMISTIC)]:
        # ARV calculation — Zestimate primary, comps fallback
        if zestimate:
            # Use Zestimate как baseline. Multipliers применяются как "discount/premium"
            # к Zestimate потому что Zestimate тоже не идеален.
            #   pessimistic 1.2: ARV = Zestimate × (1.2/1.5) = 0.8× Zestimate (consrv)
            #   realistic   1.5: ARV = Zestimate                 (как есть)
            #   optimistic  1.8: ARV = Zestimate × (1.8/1.5) = 1.2× Zestimate (после top reno)
            zest_factor = mult / ARV_MULT_REALISTIC  # 0.8 / 1.0 / 1.2
            arv = zestimate * zest_factor
            arv_source = f"Zestimate × {zest_factor:.2f}"
        elif arv_base_comps:
            arv = arv_base_comps * mult
            arv_source = f"Comps × {mult}"
        else:
            arv = 0
            arv_source = "—"

        if arv <= 0:
            scores[label] = {"score": 0, "arv": 0, "max_buy": 0, "margin": 0,
                            "arv_source": "Нет данных", "breakdown": []}
            continue

        max_buy = arv * rule_pct - repair
        margin = max_buy - price
        margin_pct = (margin / price * 100) if price else 0

        # Финансовый score
        fin = 0
        fin_reasons = []
        if margin >= 0:
            fin += 50
            fin_reasons.append(f"✅ Margin +${margin:,.0f} (+50) — сделка проходит")
        elif margin >= -price * 0.10:
            fin += 25
            fin_reasons.append(f"⚠️ Margin -${abs(margin):,.0f} ({margin_pct:.0f}%) — близко, нужен торг (+25)")
        elif margin >= -price * 0.20:
            fin += 10
            fin_reasons.append(f"⚠️ Margin -${abs(margin):,.0f} ({margin_pct:.0f}%) — нужен сильный торг (+10)")
        else:
            fin_reasons.append(f"❌ Margin -${abs(margin):,.0f} ({margin_pct:.0f}%) — формула не сходится (+0)")

        # Discount vs Zestimate (если есть)
        if zestimate:
            discount = (zestimate - price) / zestimate * 100
            if discount >= 15:
                fin += 20
                fin_reasons.append(f"💎 Цена на {discount:.0f}% ниже Zestimate (+20)")
            elif discount >= 5:
                fin += 12
                fin_reasons.append(f"Цена на {discount:.0f}% ниже Zestimate (+12)")
            elif discount >= -5:
                fin += 5
                fin_reasons.append(f"Цена ≈ Zestimate (+5)")
            else:
                fin_reasons.append(f"⚠️ Цена выше Zestimate на {abs(discount):.0f}% (+0)")

        # Flood risk
        risk = -10 if flood_risk == "high" else 5
        risk_reason = ("🌊 HIGH flood zone (−10)" if flood_risk == "high"
                       else "✓ Низкий риск затопления (+5)")

        total = max(0, min(100, fin + tech_score + risk))

        # Прибыль в 2 сценариях покупки:
        # 1. Купил по текущему listing price (что Андрей увидит сразу)
        # 2. Купил по max_buy (target price если торгуешься правильно)
        profit_at_listing = _estimate_flip_economics(arv, price, repair)
        profit_at_max_buy = _estimate_flip_economics(arv, max(max_buy, 1), repair)

        scores[label] = {
            "score": total,
            "arv": arv,
            "arv_source": arv_source,
            "max_buy": max_buy,
            "margin": margin,
            "margin_pct": margin_pct,
            "profit_at_listing": profit_at_listing,
            "profit_at_max_buy": profit_at_max_buy,
            "breakdown": [
                {"label": "Финансы (правило + дисконт)", "value": fin, "max": 70, "reasons": fin_reasons},
                {"label": "Характеристики дома (tech)", "value": tech_score, "max": 20, "reasons": ["См. ниже Tech Score breakdown"]},
                {"label": "Риск (flood)", "value": risk, "max": 5, "reasons": [risk_reason]},
            ],
        }

    # Legacy fields for backwards compat
    scores["repair_label"] = repair_label
    return scores


# ---------- RENOVATION PRESETS (Inland Empire 2026) ----------

RENOVATION_PRESETS = {
    "cosmetic": {
        "label": "💄 Косметика",
        "cost_per_sqft": 30,
        "description": "Краска, ламинат, fixtures, уборка. Кухня/ванная не трогаем.",
        "default_reno_months": 2,
    },
    "medium": {
        "label": "🔧 Средний",
        "cost_per_sqft": 55,
        "description": "Кухня + ванная update, бытовая техника, ландшафт.",
        "default_reno_months": 4,
    },
    "heavy": {
        "label": "🏗 Тяжёлый",
        "cost_per_sqft": 85,
        "description": "HVAC, сантехника, электрика + полная отделка.",
        "default_reno_months": 6,
    },
    "gut": {
        "label": "🔨 Капитальный",
        "cost_per_sqft": 130,
        "description": "Всё под ноль, всё новое. Fire damage, очень старый дом.",
        "default_reno_months": 9,
    },
}


def flip_calculator_2026(
    purchase_price,
    sqft,
    arv,
    renovation_type="medium",
    custom_repair_total=None,
    contingency_pct=0.15,
    financing="hard_money",
    hard_money_rate=0.11,
    hard_money_points=2.0,
    hard_money_ltv=0.80,
    hold_months=6,
    selling_costs_pct=0.065,
):
    """
    Реальный калькулятор флипа 2026 Inland Empire California.
    Учитывает hard money, contingency, NAR post-settlement commissions.
    Язык: вложил / потратил / в кармане.
    """
    if not purchase_price or not arv or not sqft:
        return {}

    preset = RENOVATION_PRESETS.get(renovation_type, RENOVATION_PRESETS["medium"])

    # Ремонт
    if custom_repair_total is not None and custom_repair_total > 0:
        base_repair = float(custom_repair_total)
    else:
        base_repair = preset["cost_per_sqft"] * sqft

    contingency = base_repair * contingency_pct
    total_repair = base_repair + contingency

    # Расходы на покупку
    if financing == "hard_money":
        loan_amount = purchase_price * hard_money_ltv
        points_cost = loan_amount * (hard_money_points / 100)
        buying_costs = points_cost + 1500 + 3500
    else:
        loan_amount = 0
        points_cost = 0
        buying_costs = 3500

    # Holding costs
    monthly_tax = purchase_price * 0.0125 / 12
    monthly_insurance = 150
    monthly_utilities = 250
    monthly_non_loan = monthly_tax + monthly_insurance + monthly_utilities
    monthly_interest = (loan_amount * hard_money_rate / 12) if financing == "hard_money" else 0
    monthly_carrying = monthly_non_loan + monthly_interest
    total_carrying = monthly_carrying * hold_months

    # Selling costs (NAR 2024: ~3% listing + ~2.5% buyer + ~1% escrow/title)
    selling_costs = arv * selling_costs_pct

    total_spent = purchase_price + total_repair + buying_costs + total_carrying + selling_costs
    net_profit = arv - total_spent

    # MAO — 65% rule Inland Empire 2026
    mao = arv * 0.65 - total_repair
    discount_needed = max(0, purchase_price - mao)
    discount_pct = discount_needed / purchase_price if purchase_price else 0

    if mao > 0:
        if financing == "hard_money":
            bc_mao = mao * hard_money_ltv * (hard_money_points / 100) + 1500 + 3500
            cc_mao = (mao * hard_money_ltv * hard_money_rate / 12 + monthly_non_loan) * hold_months
        else:
            bc_mao = 3500
            cc_mao = monthly_non_loan * hold_months
        profit_at_mao = arv - mao - total_repair - bc_mao - cc_mao - selling_costs
    else:
        profit_at_mao = 0

    verdict = "green" if net_profit >= 30000 else ("yellow" if net_profit >= 10000 else "red")

    return {
        "purchase_price": purchase_price,
        "arv": arv,
        "sqft": sqft,
        "financing": financing,
        "hold_months": hold_months,
        "renovation_type": renovation_type,
        "preset": preset,
        "base_repair": base_repair,
        "contingency": contingency,
        "total_repair": total_repair,
        "loan_amount": loan_amount,
        "points_cost": points_cost,
        "buying_costs": buying_costs,
        "monthly_interest": monthly_interest,
        "monthly_non_loan": monthly_non_loan,
        "monthly_carrying": monthly_carrying,
        "total_carrying": total_carrying,
        "selling_costs": selling_costs,
        "total_spent": total_spent,
        "net_profit": net_profit,
        "mao": mao,
        "discount_needed": discount_needed,
        "discount_pct": discount_pct,
        "profit_at_mao": profit_at_mao,
        "verdict": verdict,
    }
