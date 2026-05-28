#!/usr/bin/env python3
"""
CaliFlip Analyzer v8.0 — анализ недвижимости для флипа в Riverside County, CA

Новое в v8.0:
  - Интерактивный калькулятор Flip Score прямо в отчёте.
  - Ввод listing price (Zillow/Redfin) и стоимости ремонта вручную.
  - Финансовая логика: 70 очков из 100 от финансов, 20 от характеристик, 10 от рисков.
  - Расчёт по правилу 70% сразу видно в отчёте.

Использование:
  python3 califlip.py "123 Main St, Riverside, CA 92501"
  python3 califlip.py "https://www.zillow.com/homedetails/..."
"""

import sys
import re
import json
import time
import webbrowser
from pathlib import Path
from datetime import datetime

try:
    import requests
except ImportError:
    print("❌ Не установлена библиотека requests.")
    print("   Установи командой: pip3 install requests --break-system-packages")
    sys.exit(1)


# ---------- КОНФИГ ----------

TARGET_BUDGET_MIN = 250_000
TARGET_BUDGET_MAX = 500_000

# ARV (After Repair Value) множители для пересчёта assessed → market.
# КАЛИБРОВАНО ДЛЯ CALIFORNIA 2026 (recalibrated 2026-05-27):
# По Prop 13 assessed value сильно отстаёт от market — особенно в Inland Empire
# где много owner-occupied десятилетиями. Из реальных Zillow vs Assessor сверок
# в Banning/Hemet/SB разрыв 1.4-1.7x норма.
#   Pessimistic 1.2 — для свежепроданных домов (assessed ≈ market близко)
#   Realistic   1.5 — средний случай California 2026
#   Optimistic  1.8 — для старых owner-occupied (Prop 13 frozen)
# Если есть Zillow Zestimate — он используется напрямую (это уже market estimate
# от Zillow с их огромной data база — точнее наших multipliers).
ARV_MULT_PESSIMISTIC = 1.2
ARV_MULT_REALISTIC = 1.5
ARV_MULT_OPTIMISTIC = 1.8

# Правило N% от ARV — сколько максимум платить за fixer.
# Calibrated для California 2026 (где markets горячее чем в pre-2020 учебниках).
# Inland Empire (Riverside, SB) — 75% (slow exit, нужен запас).
# Coastal LA / OC — 80% (быстрый exit, можно тонкая маржа).
FLIP_RULE_PERCENT = 0.75  # default — Inland Empire mode

# Правильный endpoint (проверено через web fetch)
ASSESSOR_BASE = "https://gis.countyofriverside.us/arcgis_mapping/rest/services/OpenData/Assessor/MapServer"
LAYER_PARCELS = 40
TABLE_GENERAL = 70
TABLE_PROPERTY_CHAR = 80
TABLE_RECORDED_BOOK = 90
TABLE_TAXYEAR = 100

NOMINATIM = "https://nominatim.openstreetmap.org/search"
# US Census Geocoder — бесплатный fallback с лучшим покрытием house-numbers в США
# (Nominatim/OSM часто не знает mobile home parks, сельские адреса, новые subdivisions).
CENSUS_GEOCODER = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
FEMA_FLOOD = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"

# Браузерный UA — критично для прохождения Cloudflare
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")

REQUEST_TIMEOUT = 60
MAX_RETRIES = 2


# ---------- УТИЛИТЫ ----------

def log(msg, level="INFO"):
    """Цветное логирование с таймштампом."""
    colors = {"INFO": "\033[36m", "OK": "\033[32m", "WARN": "\033[33m", "ERR": "\033[31m"}
    reset = "\033[0m"
    color = colors.get(level, "")
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{color}[{ts}] {level:4} {msg}{reset}")


def _headers(referer="https://gis.countyofriverside.us/"):
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "Connection": "keep-alive",
    }


def api_query(layer_id, params, post=True):
    """
    Универсальный запрос к ArcGIS REST API Riverside County.
    Возвращает features (list) или [].
    """
    url = f"{ASSESSOR_BASE}/{layer_id}/query"
    params_full = {"f": "json", **params}

    for attempt in range(MAX_RETRIES + 1):
        try:
            if post:
                headers = {**_headers(), "Content-Type": "application/x-www-form-urlencoded"}
                r = requests.post(url, data=params_full, headers=headers, timeout=REQUEST_TIMEOUT)
            else:
                r = requests.get(url, params=params_full, headers=_headers(), timeout=REQUEST_TIMEOUT)

            if r.status_code != 200:
                log(f"HTTP {r.status_code} от layer {layer_id}", "WARN")
                if attempt < MAX_RETRIES:
                    time.sleep(2)
                    continue
                return []

            data = r.json()
            if "error" in data:
                log(f"API error: {data['error'].get('message', data['error'])}", "WARN")
                return []
            return data.get("features", [])

        except requests.Timeout:
            log(f"Таймаут (попытка {attempt+1}/{MAX_RETRIES+1})", "WARN")
        except requests.RequestException as e:
            log(f"Ошибка запроса (попытка {attempt+1}): {e}", "WARN")
        except json.JSONDecodeError:
            log(f"Не JSON-ответ от layer {layer_id}", "WARN")
            return []

        if attempt < MAX_RETRIES:
            time.sleep(2 * (attempt + 1))

    return []


def http_get_json(url, params=None):
    """Простой GET для внешних API (Nominatim, FEMA)."""
    try:
        r = requests.get(url, params=params, headers=_headers(), timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            return r.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        log(f"GET ошибка для {url[:60]}...: {e}", "WARN")
    return None


# ---------- ШАГ 1: ИЗВЛЕЧЕНИЕ АДРЕСА ----------

def extract_address(user_input):
    """Из URL Zillow/Redfin/Realtor или из чистого текста."""
    s = user_input.strip()

    if not s.startswith("http"):
        return s

    log(f"Парсю URL...", "INFO")

    # Zillow
    m = re.search(r"/homedetails/([^/]+?)/(?:\d+_zpid|b)", s)
    if m:
        addr = m.group(1).replace("-", " ")
        addr = re.sub(r"\s+CA\s+(\d{5})", r", CA \1", addr)
        # Вычищаем суффиксы юнитов — Nominatim/Census их не понимают и
        # геокод падает (актуально для кондо, квартир, mobile home parks).
        addr = re.sub(r"\s+(SPACE|UNIT|APT|STE|#)\s*\S+", "", addr, flags=re.IGNORECASE)
        log(f"Адрес из Zillow: {addr}", "OK")
        return addr

    # Redfin
    m = re.search(r"/CA/([^/]+)/([^/]+?)-(\d{5})/", s)
    if m:
        city, street, zipc = m.groups()
        addr = f"{street.replace('-', ' ')}, {city.replace('-', ' ')}, CA {zipc}"
        log(f"Адрес из Redfin: {addr}", "OK")
        return addr

    # Realtor.com
    m = re.search(r"realestateandhomes-detail/([^/]+)", s)
    if m:
        parts = m.group(1).split("_")
        if len(parts) >= 4:
            addr = f"{parts[0].replace('-', ' ')}, {parts[1].replace('-', ' ')}, {parts[2]} {parts[3]}"
            log(f"Адрес из Realtor: {addr}", "OK")
            return addr

    log("Не смог извлечь адрес из URL. Введи адрес напрямую.", "ERR")
    return None


# ---------- ШАГ 2: ГЕОКОДИРОВАНИЕ ----------

def _geocode_nominatim(address):
    """Геокод через OpenStreetMap Nominatim. Хорош в городах, плох в сельской местности."""
    data = http_get_json(NOMINATIM, params={
        "q": address,
        "format": "json",
        "limit": 5,
        "addressdetails": 1,
        "countrycodes": "us",
    })
    if not data or not isinstance(data, list) or len(data) == 0:
        return None

    # Предпочитаем класс "building" или "place" с типом "house", "residential", и т.п.
    # Избегаем класс "highway" (это дорога) — иначе попадаем в служебный parcel.
    preferred = None
    for item in data:
        cls = item.get("class", "")
        typ = item.get("type", "")
        if cls == "building":
            preferred = item
            break
        if cls == "place" and typ in ("house", "residential"):
            preferred = item
            break
        if cls == "highway":
            continue
        if preferred is None:
            preferred = item

    if preferred is None:
        preferred = data[0]

    return {
        "lat": float(preferred["lat"]),
        "lon": float(preferred["lon"]),
        "display_name": preferred.get("display_name", address),
        "class": preferred.get("class", ""),
        "type": preferred.get("type", ""),
        "source": "Nominatim",
    }


def _geocode_census(address):
    """
    US Census Bureau Geocoder — fallback с лучшим покрытием house-numbers в США.
    Бесплатный, без API key, хорошо знает mobile home parks и сельские адреса.
    """
    data = http_get_json(CENSUS_GEOCODER, params={
        "address": address,
        "benchmark": "Public_AR_Current",
        "format": "json",
    })
    if not data:
        return None
    matches = data.get("result", {}).get("addressMatches", [])
    if not matches:
        return None
    m = matches[0]
    coord = m.get("coordinates", {})
    lat = coord.get("y")
    lon = coord.get("x")
    if lat is None or lon is None:
        return None
    return {
        "lat": float(lat),
        "lon": float(lon),
        "display_name": m.get("matchedAddress", address),
        "class": "address",
        "type": "match",
        "source": "Census",
    }


def geocode(address):
    """
    Координаты через каскад: сначала Nominatim, потом US Census Geocoder.
    Census знает house-numbers которых нет в OSM (mobile home parks, сельская
    недвижимость, новые subdivisions).
    """
    log(f"Геокодирую: {address}", "INFO")

    coords = _geocode_nominatim(address)
    if coords:
        log(f"Координаты: {coords['lat']:.5f}, {coords['lon']:.5f} "
            f"(источник: {coords['source']}, тип: {coords['class']}/{coords['type']})", "OK")
        return coords

    log("Nominatim не нашёл — пробую US Census Geocoder...", "WARN")
    coords = _geocode_census(address)
    if coords:
        log(f"Координаты: {coords['lat']:.5f}, {coords['lon']:.5f} "
            f"(источник: {coords['source']})", "OK")
        return coords

    log("Адрес не найден ни в OpenStreetMap, ни в US Census Geocoder", "WARN")
    return None


# ---------- ШАГ 3: ПОИСК PARCEL ПО КООРДИНАТАМ ----------

# Флаги parcel, которые нужно игнорировать — это не дома, а служебные участки
SKIP_FLAGS = {"RW", "RD", "ST", "HW", "FW"}  # right-of-way, road, street, highway, freeway


def _is_residential_apn(attrs):
    """Проверяет, что parcel это жилой объект, а не дорога/общественная земля."""
    flag = (attrs.get("FLAG") or "").strip().upper()
    apn = (attrs.get("APN") or "").strip().upper()
    # APN, состоящий из букв вместо цифр (RW, RD) — это служебный
    if not apn or apn in SKIP_FLAGS:
        return False
    if flag in SKIP_FLAGS:
        return False
    # Нормальный APN — 9 цифр (Riverside формат XXX-XXX-XXX или XXXXXXXXX)
    if not any(c.isdigit() for c in apn):
        return False
    return True


STREET_TYPE_MAP = {
    "ST": "ST", "STREET": "ST",
    "AVE": "AVE", "AV": "AVE", "AVENUE": "AVE",
    "BLVD": "BLVD", "BL": "BLVD", "BOULEVARD": "BLVD",
    "DR": "DR", "DRIVE": "DR",
    "RD": "RD", "ROAD": "RD",
    "LN": "LN", "LANE": "LN",
    "CT": "CT", "COURT": "CT",
    "PL": "PL", "PLACE": "PL",
    "WAY": "WAY",
    "CIR": "CIR", "CIRCLE": "CIR",
    "TER": "TER", "TERRACE": "TER",
    "PKWY": "PKWY", "PARKWAY": "PKWY",
    "HWY": "HWY", "HIGHWAY": "HWY",
}
PREDIR_VALID = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}


def parse_us_address(address):
    """
    "1081 N San Gorgonio Ave, Banning, CA 92220" →
    {number: '1081', predir: 'N', name: 'SAN GORGONIO', type: 'AVE',
     city: 'BANNING', zip: '92220'}
    Возвращает None если не смогли распарсить.
    """
    if not address:
        return None
    # Нормализация: убираем запятые, лишние пробелы, апоск (для O'Brien etc — оставляем)
    s = re.sub(r"\s+", " ", address.replace(",", " ").strip())
    parts = s.split()

    # Zip — последнее число (5 цифр опц + 4 цифры опц)
    zip_idx = None
    for i in range(len(parts) - 1, -1, -1):
        if re.fullmatch(r"\d{5}(-\d{4})?", parts[i]):
            zip_idx = i
            break
    if zip_idx is None:
        return None
    zip_code = parts[zip_idx][:5]

    # CA / California — обычно прямо перед zip
    state_idx = zip_idx - 1
    if state_idx < 0 or parts[state_idx].upper() not in ("CA", "CALIFORNIA"):
        return None

    # Number — первый токен, должен быть номер дома (опционально с буквой суффиксом)
    if not re.match(r"^\d+[A-Za-z]?$", parts[0]):
        return None
    number = re.match(r"^(\d+)", parts[0]).group(1)

    # Optional predirectional (N/S/E/W) — второй токен если он в whitelist
    cur = 1
    predir = ""
    if cur < state_idx and parts[cur].upper() in PREDIR_VALID:
        predir = parts[cur].upper()
        cur += 1

    # Street type — ищем известный тип в токенах между cur и state_idx
    # (street type обычно последний или предпоследний токен перед city)
    street_type = ""
    street_type_idx = None
    for i in range(state_idx - 1, cur - 1, -1):
        tok_clean = parts[i].rstrip(".").upper()
        if tok_clean in STREET_TYPE_MAP:
            street_type = STREET_TYPE_MAP[tok_clean]
            street_type_idx = i
            break

    if street_type_idx is None:
        # Нет известного типа улицы — попробуем без него
        street_type_idx = state_idx  # city — весь остаток до конца

    # City — между street_type_idx (exclusive) и state_idx (exclusive)
    city_tokens = parts[street_type_idx + 1:state_idx]
    if not city_tokens:
        return None
    city = " ".join(city_tokens).upper()

    # Street name — между cur (inclusive) и street_type_idx (exclusive)
    name_tokens = parts[cur:street_type_idx]
    if not name_tokens:
        return None
    street_name = " ".join(name_tokens).upper()

    return {
        "number": number,
        "predir": predir,
        "name": street_name,
        "type": street_type,
        "city": city,
        "zip": zip_code,
    }


def find_parcel_by_address(parsed):
    """
    Прямой поиск parcel по structured address в TABLE_GENERAL.

    ⚠️  В Riverside Assessor STREET_NUMBER НЕ индексирован — нельзя ставить в WHERE
    (API возвращает 'Unable to complete operation'). Поэтому стратегия:
    1. WHERE по STREET_NAME LIKE + CITY [+ PREDIR] — получаем все дома на улице.
    2. Фильтруем по STREET_NUMBER в Python.

    Возвращает (apn, matched_address_str, nearby_numbers) где:
      - apn / matched: если точный номер найден
      - nearby_numbers: list соседних номеров (для диагностики если точный не найден)
    """
    if not parsed:
        return None, None, []

    log(f"Ищу parcel по адресу: {parsed['number']} {parsed['predir']} {parsed['name']} "
        f"{parsed['type']} в {parsed['city']}...", "INFO")

    name_safe = parsed["name"].replace("'", "''")
    city_safe = parsed["city"].replace("'", "''")
    where = f"STREET_NAME LIKE '%{name_safe}%' AND CITY='{city_safe}'"
    if parsed["predir"]:
        where += f" AND STREET_PREDIRECTIONAL='{parsed['predir']}'"

    features = api_query(TABLE_GENERAL, {
        "where": where,
        "outFields": "PIN,STREET_NUMBER,STREET_PREDIRECTIONAL,STREET_NAME,STREET_TYPE,CITY,POSTAL_CD",
        "returnGeometry": "false",
        "resultRecordCount": 500,
    })

    if not features:
        log(f"На улице {parsed['name']} в {parsed['city']} parcels не нашлось", "WARN")
        return None, None, []

    log(f"Получено {len(features)} parcels на улице — фильтрую по номеру {parsed['number']}...", "INFO")

    # Точный номер
    exact = [f for f in features if str(f["attributes"].get("STREET_NUMBER", "")).strip() == parsed["number"]]
    if exact:
        # Если несколько с одним номером — предпочитаем по совпадающему STREET_TYPE
        best = exact[0]
        if parsed["type"]:
            for f in exact:
                if (f["attributes"].get("STREET_TYPE") or "").upper() == parsed["type"]:
                    best = f
                    break
        a = best["attributes"]
        apn = a.get("PIN")
        matched = (f"{a.get('STREET_NUMBER')} {a.get('STREET_PREDIRECTIONAL') or ''} "
                   f"{a.get('STREET_NAME')} {a.get('STREET_TYPE') or ''}, "
                   f"{a.get('CITY')} {a.get('POSTAL_CD')}").replace("  ", " ").strip()
        log(f"✅ Точный match: APN {apn} ({matched})", "OK")
        return apn, matched, []

    # Не нашли точный — возвращаем 10 ближайших по номеру (для диагностики)
    try:
        target = int(parsed["number"])
        with_nums = []
        for f in features:
            try:
                n = int(re.sub(r"\D.*$", "", str(f["attributes"].get("STREET_NUMBER", "0"))))
                with_nums.append((abs(n - target), n))
            except (ValueError, TypeError):
                continue
        with_nums.sort()
        nearby = sorted({n for _, n in with_nums[:10]})
    except ValueError:
        nearby = []

    nearby_str = ", ".join(str(n) for n in nearby[:10])
    log(f"Точного номера {parsed['number']} нет в Assessor. Ближайшие на этой улице: {nearby_str}", "WARN")
    return None, None, nearby


def find_parcel(coords):
    """
    Geometry query → возвращает APN жилого parcel.
    Если попадаем в right-of-way (дорогу), ищем ближайший жилой в радиусе 50м.
    """
    log("Ищу parcel в Riverside County...", "INFO")

    # Сначала пробуем точное попадание
    features = api_query(LAYER_PARCELS, {
        "geometry": f"{coords['lon']},{coords['lat']}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "APN,FLAG,OBJECTID",
        "returnGeometry": "false",
    })

    # Фильтруем — оставляем только жилые
    residential = [f["attributes"] for f in features if _is_residential_apn(f["attributes"])]

    if residential:
        attrs = residential[0]
        log(f"Parcel найден. APN: {attrs.get('APN')}", "OK")
        return attrs

    # Не повезло — точка попала в дорогу или общественную землю
    if features:
        bad_flag = features[0]["attributes"].get("FLAG", "") or features[0]["attributes"].get("APN", "")
        log(f"Точка попала в служебный участок (FLAG={bad_flag}). Ищу ближайший жилой...", "WARN")

    # Расширенный поиск с буфером ~30 метров
    features = api_query(LAYER_PARCELS, {
        "geometry": f"{coords['lon']},{coords['lat']}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": 30,
        "units": "esriSRUnit_Meter",
        "outFields": "APN,FLAG,OBJECTID",
        "returnGeometry": "false",
        "resultRecordCount": 20,
    })

    residential = [f["attributes"] for f in features if _is_residential_apn(f["attributes"])]

    if residential:
        attrs = residential[0]
        log(f"Найден жилой parcel рядом. APN: {attrs.get('APN')}", "OK")
        return attrs

    log("Жилой parcel не найден. Возможно, адрес не в Riverside County.", "WARN")
    return None


# ---------- ШАГ 4: ДЕТАЛИ ПО APN ----------

def normalize_apn(apn):
    """Возвращает варианты APN — с дефисами и без — для поиска по PIN."""
    if not apn:
        return []
    no_dash = str(apn).replace("-", "").strip()
    variants = [no_dash]
    # формат XXX-XXX-XXX (стандартный Riverside)
    if len(no_dash) == 9:
        variants.append(f"{no_dash[:3]}-{no_dash[3:6]}-{no_dash[6:9]}")
    return variants


def query_by_pin(table_id, apn, order_by=None, max_records=10):
    """Запрос таблицы по PIN (он же APN)."""
    variants = normalize_apn(apn)
    if not variants:
        return []
    where = " OR ".join(f"PIN='{v}'" for v in variants)
    params = {
        "where": where,
        "outFields": "*",
        "returnGeometry": "false",
        "resultRecordCount": max_records,
    }
    if order_by:
        params["orderByFields"] = order_by
    return api_query(table_id, params)


def get_property_char(apn):
    """Характеристики дома: год постройки, площадь, спальни, ванные."""
    log("Запрос характеристик дома...", "INFO")
    features = query_by_pin(TABLE_PROPERTY_CHAR, apn, max_records=5)
    if features:
        log(f"Характеристики получены ({len(features)} записей)", "OK")
        return features[0]["attributes"]
    log("Характеристики не найдены", "WARN")
    return {}


def get_taxyear_history(apn):
    """История налоговых лет → assessed values."""
    log("Запрос налоговой истории...", "INFO")
    features = query_by_pin(
        TABLE_TAXYEAR, apn, order_by="TAX_YEAR DESC", max_records=20
    )
    if features:
        log(f"Налоговая история получена ({len(features)} лет)", "OK")
        return [f["attributes"] for f in features]
    log("Налоговая история не найдена", "WARN")
    return []


def get_recorded_book(apn):
    """Записи передачи прав (история продаж по датам)."""
    log("Запрос истории передачи прав...", "INFO")
    features = query_by_pin(TABLE_RECORDED_BOOK, apn, max_records=20)
    if features:
        log(f"Передачи прав получены ({len(features)} записей)", "OK")
        return [f["attributes"] for f in features]
    return []


# ---------- ШАГ 4b: ZILLOW LISTING SCRAPE ----------

# Типы недвижимости Zillow которые НЕ подходят для нашего инструмента
# (нет отдельного APN, продаются как personal property, county data неприменим).
NON_FLIP_HOME_TYPES = {
    "MANUFACTURED", "MOBILE_MANUFACTURED", "MOBILE",
    "APARTMENT", "CONDO", "TOWNHOUSE",  # тоже спорные — кондо требуют HOA анализ
}


def fetch_zillow_listing(url):
    """
    Скачивает страницу Zillow, извлекает цену + описание + тип + характеристики.

    ⚠️  СЕЙЧАС ЭТО НЕ РАБОТАЕТ — Zillow защищён PerimeterX, который блокирует
    requests, curl_cffi (TLS impersonation), и даже headless Playwright.
    Чтобы реально работало нужен один из:
      - RapidAPI Zillow API (~$10/мес, требует API key) — РЕКОМЕНДУЕТСЯ
      - ScrapingBee / ScraperAPI (~$30/мес, прокси-сервис)
      - playwright-stealth + non-headless Chrome (бесплатно но хрупко)

    Пока функция оставлена как scaffold — при апгрейде заменить body, остальной
    pipeline (auto-prefill repair, sensitivity panel, отчёт) уже готов.
    """
    log("Zillow scraping недоступен (PerimeterX) — пропускаю", "INFO")
    return None
    # --- Legacy fallback code, may work for non-Zillow listing sites in future ---
    log("Запрос данных листинга с Zillow...", "INFO")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        r = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        if r.status_code != 200:
            log(f"Zillow вернул HTTP {r.status_code} — fallback на ручной ввод", "WARN")
            return None
        html = r.text
        low = html[:5000].lower()
        if "px-captcha" in low or "perimeterx" in low or ("captcha" in low and "challenge" in low):
            log("Zillow показал CAPTCHA — fallback на ручной ввод", "WARN")
            return None

        result = {}

        # Listing price — несколько вариантов pattern (структура Zillow часто меняется)
        for pattern in [
            r'"price"\s*:\s*(\d{5,8})\b',
            r'"unformattedPrice"\s*:\s*"?(\d{5,8})"?',
            r'<meta\s+property="product:price:amount"\s+content="(\d+)"',
        ]:
            m = re.search(pattern, html)
            if m:
                try:
                    val = int(m.group(1))
                    if 50_000 <= val <= 5_000_000:  # sanity check
                        result["price"] = val
                        break
                except ValueError:
                    continue

        # Тип жилья — для отсечения mobile homes / кондо
        m = re.search(r'"homeType"\s*:\s*"([^"]+)"', html)
        if m:
            result["home_type"] = m.group(1).upper()

        # Описание — для оценки ремонта
        m = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.){20,2000}?)"', html)
        if m:
            result["description"] = m.group(1)[:1500]

        # Bedrooms / bathrooms / sqft — для cross-check с county data
        for key, pat in [
            ("beds", r'"bedrooms"\s*:\s*(\d+)'),
            ("baths", r'"bathrooms"\s*:\s*([\d.]+)'),
            ("sqft", r'"livingArea"\s*:\s*(\d+)'),
            ("year_built", r'"yearBuilt"\s*:\s*(\d+)'),
            ("zestimate", r'"zestimate"\s*:\s*(\d+)'),
        ]:
            m = re.search(pat, html)
            if m:
                result[key] = m.group(1)

        if not result.get("price"):
            log("Не нашёл цену в HTML Zillow — fallback на ручной ввод", "WARN")
            return None

        log(f"Zillow: цена ${result['price']:,}, тип {result.get('home_type', '?')}, "
            f"{result.get('beds', '?')}/{result.get('baths', '?')}, {result.get('sqft', '?')} sqft", "OK")
        return result

    except requests.RequestException as e:
        log(f"Ошибка fetch Zillow: {e}", "WARN")
        return None
    except Exception as e:
        log(f"Парсинг Zillow упал: {e}", "WARN")
        return None


def estimate_repair_cost(description, living_area_sqft):
    """
    Эвристика стоимости ремонта по ключевым словам в описании листинга.
    Возвращает (cost_dollars, label_human_readable).
    """
    if not living_area_sqft or living_area_sqft <= 0:
        living_area_sqft = 1500

    HEAVY_PATTERNS = [
        r"\bas[- ]is\b", r"\bTLC\b", r"\bfixer[- ]?upper\b", r"\bneeds?\s+work\b",
        r"\binvestor\s+special\b", r"\bcash\s+only\b", r"\bmajor\s+repairs?\b",
        r"\bfoundation\s+(?:issues?|problems?)\b", r"\bgut\s+(?:job|rehab)\b",
        r"\bteardown\b", r"\brehab\b", r"\bdistressed\b", r"\bhandyman\s+special\b",
    ]
    LIGHT_PATTERNS = [
        r"\bmove[- ]in\s+ready\b", r"\bturn[- ]key\b",
        r"\brecently\s+remodel(?:ed|ing)?\b", r"\brecently\s+updated\b",
        r"\bnewer?\s+(?:roof|HVAC|kitchen|bath|windows)\b",
        r"\bfully\s+renovated\b", r"\bnew\s+construction\b", r"\bjust\s+remodeled\b",
    ]

    if not description:
        return int(living_area_sqft * 60), "средний (нет описания — оценка по умолчанию)"

    heavy_hits = sum(1 for p in HEAVY_PATTERNS if re.search(p, description, re.IGNORECASE))
    light_hits = sum(1 for p in LIGHT_PATTERNS if re.search(p, description, re.IGNORECASE))

    if heavy_hits > light_hits and heavy_hits >= 1:
        return int(living_area_sqft * 100), f"серьёзный (~$100/sqft — в описании TLC/fixer/needs work)"
    elif light_hits > heavy_hits and light_hits >= 1:
        return int(living_area_sqft * 30), f"лёгкий (~$30/sqft — в описании updated/move-in ready)"
    else:
        return int(living_area_sqft * 60), f"средний (~$60/sqft — нейтральное описание, default)"


def get_general(apn):
    """Общая info — владелец."""
    log("Запрос данных владельца...", "INFO")
    features = query_by_pin(TABLE_GENERAL, apn, max_records=5)
    if features:
        log(f"Данные владельца получены", "OK")
        return features[0]["attributes"]
    return {}


# ---------- ШАГ 5: COMPS — соседи и их assessed values ----------

def find_neighbor_apns(coords, radius_meters=400):
    """
    Соседние parcels в радиусе ~400m (1/4 мили).
    Меньше радиуса = быстрее запрос и более релевантные comps.
    """
    log(f"Ищу соседей в радиусе {radius_meters}м...", "INFO")
    features = api_query(LAYER_PARCELS, {
        "geometry": f"{coords['lon']},{coords['lat']}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": radius_meters,
        "units": "esriSRUnit_Meter",
        "outFields": "APN",
        "returnGeometry": "false",
        "resultRecordCount": 100,
    })
    apns = [f["attributes"].get("APN") for f in features if f["attributes"].get("APN")]
    log(f"Найдено {len(apns)} соседних parcels", "OK")
    return apns


def get_comps_data(neighbor_apns, subject_apn, current_year=None):
    """
    Для каждого соседа получаем последний tax year + property char.
    Это и есть наши comps — assessed values похожих домов.
    """
    if current_year is None:
        current_year = datetime.now().year

    if not neighbor_apns:
        return []

    # Уберём наш дом из списка соседей
    subject_clean = str(subject_apn).replace("-", "")
    neighbors = [a for a in neighbor_apns if str(a).replace("-", "") != subject_clean]

    # Ограничим до 50 для скорости
    neighbors = neighbors[:50]
    if not neighbors:
        return []

    log(f"Запрашиваю assessed values для {len(neighbors)} соседей...", "INFO")

    # Подготовим WHERE для batch-запроса (только последний год)
    pins_clause = ",".join(f"'{n.replace('-', '')}'" for n in neighbors)
    pins_clause_dashed = ",".join(f"'{n}'" for n in neighbors)
    where = (f"(PIN IN ({pins_clause}) OR PIN IN ({pins_clause_dashed})) "
             f"AND TAX_YEAR >= {current_year - 1}")

    features = api_query(TABLE_TAXYEAR, {
        "where": where,
        "outFields": "PIN,TAX_YEAR,LAND,STRUCTURES,LIVING_IMPROVEMENTS",
        "returnGeometry": "false",
        "resultRecordCount": 200,
    })

    log(f"Получено {len(features)} налоговых записей соседей", "OK")

    # Собираем по PIN — берём самый свежий tax year
    by_pin = {}
    for f in features:
        a = f["attributes"]
        pin = a.get("PIN")
        if not pin:
            continue
        if pin not in by_pin or a.get("TAX_YEAR", 0) > by_pin[pin].get("TAX_YEAR", 0):
            by_pin[pin] = a

    comps = []
    for pin, attrs in by_pin.items():
        land = attrs.get("LAND") or 0
        structures = attrs.get("STRUCTURES") or 0
        improvements = attrs.get("LIVING_IMPROVEMENTS") or 0
        total = (land or 0) + (structures or 0) + (improvements or 0)
        if total > 50_000:  # отсеиваем явный мусор
            comps.append({
                "pin": pin,
                "tax_year": attrs.get("TAX_YEAR"),
                "land": land,
                "structures": structures,
                "improvements": improvements,
                "total_value": total,
            })

    # Сортировка по total_value
    comps.sort(key=lambda c: c["total_value"], reverse=True)
    return comps


def get_subject_assessed_value(taxyear_history):
    """Из истории берём самый свежий tax year и считаем total."""
    if not taxyear_history:
        return None, None
    latest = max(taxyear_history, key=lambda t: t.get("TAX_YEAR", 0))
    land = latest.get("LAND") or 0
    structures = latest.get("STRUCTURES") or 0
    improvements = latest.get("LIVING_IMPROVEMENTS") or 0
    total = land + structures + improvements
    return total, latest.get("TAX_YEAR")


# ---------- ШАГ 6: FEMA FLOOD ZONE ----------

def get_flood_zone(coords):
    """FEMA flood hazard zone."""
    log("Запрос FEMA flood zone...", "INFO")
    data = http_get_json(FEMA_FLOOD, params={
        "f": "json",
        "geometry": f"{coords['lon']},{coords['lat']}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,ZONE_SUBTY",
        "returnGeometry": "false",
    })
    if not data or not data.get("features"):
        return {"zone": "X", "subtype": "минимальный риск", "risk": "low"}
    attrs = data["features"][0]["attributes"]
    zone = attrs.get("FLD_ZONE", "X")
    sub = attrs.get("ZONE_SUBTY", "") or ""
    high_risk = zone in ("A", "AE", "AH", "AO", "V", "VE")
    log(f"Flood zone: {zone} {sub}", "OK")
    return {
        "zone": zone,
        "subtype": sub.strip(),
        "risk": "high" if high_risk else "low",
    }


# ---------- ШАГ 7: ТЕХНИЧЕСКИЙ СКОР ДОМА (0-20 очков) ----------

def calc_tech_score(char):
    """
    Считает только "технические" 20 очков из 100 — это просто характеристики дома.
    Финансовый скор (80 очков) считается в JS в браузере после ввода listing price.
    """
    score = 0
    breakdown = []

    # Год постройки (max 8)
    year_built = char.get("YEAR_BUILT")
    if year_built:
        try:
            yb = int(year_built)
            if 1950 <= yb <= 1985:
                pts, note = 8, f"{yb} — оптимальный возраст для флипа (нужен апгрейд кухни/ванных)"
            elif 1986 <= yb <= 2000:
                pts, note = 5, f"{yb} — средний возраст, есть пространство для улучшений"
            elif yb > 2000:
                pts, note = 2, f"{yb} — современный дом, мало места для апсайда"
            else:
                pts, note = 4, f"{yb} — очень старый, проверь фундамент и системы"
            score += pts
            breakdown.append(("Год постройки", pts, note))
        except (ValueError, TypeError):
            pass

    # Площадь (max 7)
    living_area = char.get("LIVING_AREA")
    if living_area:
        try:
            sf = int(float(living_area))
            if 1200 <= sf <= 2000:
                pts, note = 7, f"{sf} sqft — оптимальный размер для быстрого флипа"
            elif 2000 < sf <= 2800:
                pts, note = 5, f"{sf} sqft — больше работ, но и больше прибыли"
            elif sf < 1200:
                pts, note = 3, f"{sf} sqft — маленький дом, ограниченный потолок"
            else:
                pts, note = 3, f"{sf} sqft — большой дом, дорогой ремонт"
            score += pts
            breakdown.append(("Площадь", pts, note))
        except (ValueError, TypeError):
            pass

    # Конфигурация (max 5)
    beds = char.get("BEDROOM_COUNT")
    baths = char.get("BATH_COUNT")
    if beds and baths:
        try:
            b, ba = int(beds), int(baths)
            if b >= 3 and ba >= 2:
                pts, note = 5, f"{b}/{ba} — целевой формат для семейного покупателя"
            elif b == 2 or ba < 2:
                pts, note = 2, f"{b}/{ba} — нестандартный формат, сложнее продать"
            else:
                pts, note = 3, f"{b}/{ba}"
            score += pts
            breakdown.append(("Конфигурация", pts, note))
        except (ValueError, TypeError):
            pass

    return score, breakdown


# ---------- ШАГ 8: HTML-ОТЧЁТ (v8 с интерактивным расчётом) ----------

def _match_source_banner(match_source, matched_address, requested_address,
                         nearby_numbers=None, requested_number=None):
    """Баннер прозрачности: сообщаем юзеру exact / nearest / none status подбора parcel."""
    if match_source == "exact":
        return (f'<div style="background:#dcfce7; border-left:4px solid #22c55e; padding:14px 16px;'
                f'border-radius:8px; margin-bottom:12px; font-size:14px; color:#14532d;">'
                f'✅ <strong>Точное совпадение в Riverside Assessor:</strong> '
                f'{matched_address or requested_address}. Все данные ниже — именно про этот дом.'
                f'</div>')
    if match_source == "nearest":
        nearby_hint = ""
        if nearby_numbers and requested_number:
            nums_str = ", ".join(str(n) for n in nearby_numbers[:8])
            nearby_hint = (f'<br><br>📍 <strong>Адреса {requested_number} нет в Riverside Assessor.</strong> '
                          f'Ближайшие номера на этой улице: <code>{nums_str}</code>. '
                          f'Возможно, на Zillow указан неточный house number — попробуй один из этих '
                          f'(скопируй URL ↗ Zillow, замени номер в адресной части).')
        return (f'<div style="background:#fee2e2; border-left:4px solid #dc2626; padding:14px 16px;'
                f'border-radius:8px; margin-bottom:12px; font-size:14px; color:#7f1d1d;">'
                f'⚠️ <strong>Точного адреса в Riverside Assessor нет.</strong> '
                f'Скрипт взял <strong>ближайший жилой parcel</strong> по координатам — '
                f'это может быть СОСЕД, а не твой дом. Сверь характеристики (год, площадь, '
                f'спальни) с Zillow прежде чем считать Score.{nearby_hint}'
                f'</div>')
    return ""


def fmt_money(v):
    if v is None:
        return "—"
    try:
        return f"${float(v):,.0f}"
    except (ValueError, TypeError):
        return "—"


def fmt_yesno(v):
    if v is None:
        return "—"
    s = str(v).upper()
    if s in ("Y", "YES", "TRUE", "1"):
        return "Да"
    if s in ("N", "NO", "FALSE", "0"):
        return "Нет"
    return str(v)


def render_html(address, coords, parcel, char, general, taxyear_history,
                comps, flood, subject_total, tech_score, tech_breakdown, prefill=None,
                match_source=None, matched_address=None,
                nearby_numbers=None, requested_number=None):

    apn = parcel.get("APN", "—")

    # Характеристики дома для отображения
    year_built = char.get("YEAR_BUILT", "—") or "—"
    living_area = char.get("LIVING_AREA")
    living_area_str = f"{int(float(living_area))}" if living_area else "—"
    living_area_num = int(float(living_area)) if living_area else 0
    beds = char.get("BEDROOM_COUNT", "—") or "—"
    baths = char.get("BATH_COUNT", "—") or "—"
    stories = char.get("NUMBER_OF_STORIES", "—") or "—"
    garage = char.get("GARAGE_TYPE", "—") or "—"
    garage_size = char.get("GARAGE_SIZE")
    if garage_size:
        try:
            garage = f"{garage} ({int(float(garage_size))} sqft)"
        except (ValueError, TypeError):
            pass
    pool = fmt_yesno(char.get("HAS_POOL"))
    fireplace = fmt_yesno(char.get("HAS_FIREPLACE"))
    cooling = fmt_yesno(char.get("CENTRAL_COOLING"))
    heating = fmt_yesno(char.get("CENTRAL_HEATING"))

    # Считаем медиану comps для JS
    comp_values = sorted([c["total_value"] for c in comps if c.get("total_value")])
    if comp_values:
        median_comp = comp_values[len(comp_values) // 2]
        q75_comp = comp_values[int(len(comp_values) * 0.75)] if len(comp_values) >= 4 else median_comp
    else:
        median_comp = 0
        q75_comp = 0

    # Comps таблица (только последний tax year)
    comps_html = ""
    for c in comps[:15]:
        comps_html += f"""
            <tr>
                <td>{c['pin']}</td>
                <td>{c.get('tax_year', '—')}</td>
                <td style="text-align: right;">{fmt_money(c['land'])}</td>
                <td style="text-align: right;">{fmt_money((c.get('structures') or 0) + (c.get('improvements') or 0))}</td>
                <td style="text-align: right; font-weight: 600;">{fmt_money(c['total_value'])}</td>
            </tr>
        """
    if not comps_html:
        comps_html = '<tr><td colspan="5" style="text-align:center; color:#94a3b8;">Соседи не найдены</td></tr>'

    # Tax history
    tax_history_html = ""
    for t in sorted(taxyear_history, key=lambda x: x.get("TAX_YEAR", 0), reverse=True)[:8]:
        land = t.get("LAND") or 0
        structures = t.get("STRUCTURES") or 0
        improvements = t.get("LIVING_IMPROVEMENTS") or 0
        total = land + structures + improvements
        tax_history_html += f"""
            <tr>
                <td>{t.get('TAX_YEAR', '—')}</td>
                <td style="text-align: right;">{fmt_money(land)}</td>
                <td style="text-align: right;">{fmt_money(structures + improvements)}</td>
                <td style="text-align: right; font-weight: 600;">{fmt_money(total)}</td>
            </tr>
        """
    if not tax_history_html:
        tax_history_html = '<tr><td colspan="4" style="text-align:center; color:#94a3b8;">Нет данных</td></tr>'

    # Технические факторы (показываем сразу)
    tech_breakdown_html = ""
    for factor, pts, note in tech_breakdown:
        tech_breakdown_html += f"""
            <tr>
                <td>{factor}</td>
                <td style="text-align: center; color: #22c55e; font-weight: 700;">+{pts}</td>
                <td style="color: #475569;">{note}</td>
            </tr>
        """

    # Подсказка для оценки ремонта
    if living_area_num > 0:
        repair_light = int(living_area_num * 30)   # косметика
        repair_medium = int(living_area_num * 60)  # средний
        repair_heavy = int(living_area_num * 100)  # серьёзный
    else:
        repair_light, repair_medium, repair_heavy = 30000, 60000, 100000

    # Данные для JS
    js_data = json.dumps({
        "median_comp": median_comp,
        "q75_comp": q75_comp,
        "tech_score": tech_score,
        "subject_assessed": subject_total or 0,
        "flood_risk": flood["risk"],
        "living_area": living_area_num,
        "comps_count": len(comps),
        "arv_mult_pessimistic": ARV_MULT_PESSIMISTIC,
        "arv_mult_realistic": ARV_MULT_REALISTIC,
        "arv_mult_optimistic": ARV_MULT_OPTIMISTIC,
        "prefill": prefill or {},
    })

    # Баннер: 3 состояния — full prefill / repair-only prefill / no prefill
    zillow_btn = ""
    if prefill and prefill.get("zillow_url"):
        zillow_btn = (f'<a href="{prefill["zillow_url"]}" target="_blank" rel="noopener" '
                      f'style="display:inline-block; padding:8px 14px; background:#006aff; color:white; '
                      f'text-decoration:none; border-radius:6px; font-weight:600; font-size:13px; '
                      f'margin-left:8px;">↗ Открыть на Zillow</a>')

    if prefill and prefill.get("listing_price"):
        zest_str = f" · Zestimate: {fmt_money(prefill['zestimate'])}" if prefill.get('zestimate') else ""
        prefill_banner = f"""
<div style="background:#dcfce7; border-left:4px solid #22c55e; padding:14px 16px;
            border-radius:8px; margin-bottom:16px; font-size:14px; color:#14532d;">
🤖 <strong>Listing price и ремонт подставлены автоматически</strong>{zest_str}.
Ремонт: <strong>{prefill['repair_label']}</strong>. Можешь поменять руками — расчёт обновится мгновенно.
</div>"""
    elif prefill and prefill.get("repair_cost"):
        prefill_banner = f"""
<div style="background:#dbeafe; border-left:4px solid #3b82f6; padding:14px 16px;
            border-radius:8px; margin-bottom:16px; font-size:14px; color:#1e3a8a;">
🤖 <strong>Ремонт подставлен автоматически</strong>: {prefill['repair_label']} = <strong>${prefill['repair_cost']:,}</strong>
(на {living_area_num} sqft из county data). <br>
👉 <strong>Введи только listing price</strong> (одна цифра) — Flip Score рассчитается сразу.
{zillow_btn}
</div>"""
    else:
        prefill_banner = """
<div style="background:#fef3c7; border-left:4px solid #f59e0b; padding:14px 16px;
            border-radius:8px; margin-bottom:16px; font-size:14px; color:#78350f;">
⚠️ <strong>Автоматический prefill не сработал</strong>. Введи listing price и оценку ремонта руками.
</div>"""

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>CaliFlip — {address}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
       margin: 0; padding: 0; background: #f8fafc; color: #0f172a; }}
.container {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
header {{ background: linear-gradient(135deg, #1e3a8a, #3b82f6); color: white;
          padding: 32px 24px; margin: -24px -24px 24px -24px; border-radius: 0 0 16px 16px; }}
h1 {{ margin: 0 0 8px 0; font-size: 24px; }}
.address {{ font-size: 16px; opacity: 0.9; }}

/* Калькулятор */
.calculator {{ background: white; border-radius: 12px; padding: 24px; margin-bottom: 24px;
              box-shadow: 0 1px 3px rgba(0,0,0,0.08); border: 2px solid #3b82f6; }}
.calc-title {{ font-size: 18px; font-weight: 700; color: #1e3a8a; margin: 0 0 8px 0; }}
.calc-hint {{ color: #64748b; font-size: 14px; margin-bottom: 16px; }}
.calc-row {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 16px; }}
.calc-field {{ flex: 1; min-width: 240px; }}
.calc-field label {{ display: block; font-weight: 600; margin-bottom: 6px; font-size: 14px; color: #334155; }}
.calc-field input {{ width: 100%; padding: 12px; font-size: 16px; border: 1px solid #cbd5e1;
                     border-radius: 8px; font-family: inherit; }}
.calc-field input:focus {{ outline: none; border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59,130,246,0.15); }}
.calc-presets {{ display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }}
.calc-preset {{ padding: 4px 10px; background: #f1f5f9; border: 1px solid #cbd5e1;
                border-radius: 6px; font-size: 12px; cursor: pointer; }}
.calc-preset:hover {{ background: #e0e7ff; }}

/* Score card (после ввода) */
.score-card {{ display: flex; align-items: center; gap: 24px; background: white;
               border-radius: 12px; padding: 24px; margin-bottom: 24px;
               box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
.score-circle {{ width: 120px; height: 120px; border-radius: 50%;
                 color: white; display: flex; align-items: center; justify-content: center;
                 font-size: 42px; font-weight: 700; flex-shrink: 0;
                 transition: background 0.3s; }}
.score-label {{ font-size: 20px; font-weight: 600; margin-bottom: 8px; }}
.score-desc {{ color: #64748b; }}
.score-empty {{ background: #94a3b8; }}

/* Деал расчёт */
.deal-box {{ background: white; border-radius: 12px; padding: 20px; margin-bottom: 24px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
.deal-row {{ display: flex; justify-content: space-between; padding: 10px 0;
            border-bottom: 1px solid #f1f5f9; }}
.deal-row:last-child {{ border-bottom: none; padding-top: 16px; font-size: 18px; font-weight: 700; }}
.deal-good {{ color: #22c55e; }}
.deal-bad {{ color: #ef4444; }}

.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }}
.card {{ background: white; border-radius: 12px; padding: 20px;
         box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
.card h2 {{ margin: 0 0 16px 0; font-size: 16px; color: #1e3a8a;
            padding-bottom: 8px; border-bottom: 2px solid #e2e8f0; }}
.kv {{ display: flex; justify-content: space-between; padding: 6px 0;
       border-bottom: 1px solid #f1f5f9; }}
.kv:last-child {{ border-bottom: none; }}
.kv-key {{ color: #64748b; }}
.kv-val {{ font-weight: 600; color: #0f172a; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th {{ text-align: left; padding: 10px 8px; background: #f1f5f9;
      color: #475569; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }}
td {{ padding: 10px 8px; border-bottom: 1px solid #f1f5f9; }}
.warn {{ background: #fef3c7; border-left: 4px solid #f59e0b;
         padding: 12px; border-radius: 8px; margin-top: 16px; }}
.info {{ background: #dbeafe; border-left: 4px solid #3b82f6;
         padding: 12px; border-radius: 8px; font-size: 13px; color: #1e3a8a; margin-top: 12px; }}
.footer {{ text-align: center; color: #94a3b8; padding: 24px; font-size: 13px; }}
.full-width {{ grid-column: 1 / -1; }}
.hidden {{ display: none; }}
</style>
</head>
<body>
<div class="container">

<header>
<h1>🏠 CaliFlip Analysis</h1>
<div class="address">{address}</div>
<div style="font-size: 13px; opacity: 0.8; margin-top: 8px;">
Сгенерировано {datetime.now().strftime('%d %b %Y, %H:%M')} · Riverside County, CA · APN {apn}
</div>
</header>

{_match_source_banner(match_source, matched_address, address, nearby_numbers, requested_number)}

{prefill_banner}

<!-- КАЛЬКУЛЯТОР -->
<div class="calculator">
<div class="calc-title">💰 Listing price и оценка ремонта</div>
<div class="calc-hint">
Открой объект на <strong>Zillow</strong> или <strong>Redfin</strong>, скопируй текущую цену объявления.
Затем оцени стоимость ремонта (см. подсказки ниже).
<strong>Без этих двух цифр Flip Score не может быть рассчитан</strong> — assessed value ≠ market price.
</div>

<div class="calc-row">
<div class="calc-field">
<label>Listing price (текущая цена с Zillow/Redfin)</label>
<input type="number" id="listPrice" placeholder="например, 425000" />
<div class="calc-presets">
<span class="calc-preset" onclick="setVal('listPrice', {int(median_comp * ARV_MULT_REALISTIC)})">≈ market медианы (× {ARV_MULT_REALISTIC})</span>
<span class="calc-preset" onclick="setVal('listPrice', {int(subject_total or 0)})">= assessed</span>
</div>
</div>

<div class="calc-field">
<label>Оценка ремонта</label>
<input type="number" id="repairCost" placeholder="например, 50000" />
<div class="calc-presets">
<span class="calc-preset" onclick="setVal('repairCost', {repair_light})">Лёгкий ~${repair_light//1000}k</span>
<span class="calc-preset" onclick="setVal('repairCost', {repair_medium})">Средний ~${repair_medium//1000}k</span>
<span class="calc-preset" onclick="setVal('repairCost', {repair_heavy})">Серьёзный ~${repair_heavy//1000}k</span>
</div>
</div>
</div>

<div class="calc-hint">
<strong>Подсказки по ремонту:</strong> $30/sqft — косметика (краска, полы, чистка) ·
$60/sqft — средний (кухня + ванные + окна) · $100/sqft — серьёзный (плюс крыша, HVAC, перепланировка)
</div>
</div>

<!-- SCORE CARD -->
<div class="score-card">
<div id="scoreCircle" class="score-circle score-empty">?</div>
<div style="flex: 1;">
<div id="scoreLabel" class="score-label" style="color: #94a3b8;">Введи listing price и ремонт выше</div>
<div id="scoreDesc" class="score-desc">
После ввода цифр Flip Score рассчитается автоматически на основе финансовой логики
(правило 70%, ARV, дисконт к рынку) плюс факторов дома и риска.
</div>
</div>
</div>

<!-- ЧУВСТВИТЕЛЬНОСТЬ К ARV -->
<div id="sensitivityBox" class="deal-box hidden">
<h2 style="margin: 0 0 12px 0; font-size: 16px; color: #1e3a8a;">📉 Чувствительность Flip Score к ARV-предположению</h2>
<div style="color:#64748b; font-size:13px; margin-bottom:16px;">
ARV (стоимость после ремонта) — главное допущение. Assessed value по Prop 13 может быть в 1.0×-3× ниже market price в зависимости от того, когда соседи покупали свои дома. Главное число выше — реалистичный сценарий. Если три значения сильно расходятся (>30 очков) — значит сделка очень чувствительна к рынку и стоит проверить active listings соседей на Zillow перед предложением.
</div>
<div style="display:grid; grid-template-columns: repeat(3, 1fr); gap: 12px;">
<div style="text-align:center; padding:14px 8px; background:#fef3c7; border-radius:8px;">
<div style="font-size:11px; color:#92400e; text-transform:uppercase; font-weight:700;">Пессимистично × {ARV_MULT_PESSIMISTIC}</div>
<div style="font-size:12px; color:#78350f; margin:4px 0 2px 0;">ARV ≈ <span id="arvPVal">—</span></div>
<div style="font-size:34px; font-weight:700; color:#92400e; margin-top:6px;" id="scoreP">—</div>
</div>
<div style="text-align:center; padding:14px 8px; background:#dbeafe; border-radius:8px; border:2px solid #3b82f6;">
<div style="font-size:11px; color:#1e3a8a; text-transform:uppercase; font-weight:700;">Реалистично × {ARV_MULT_REALISTIC}</div>
<div style="font-size:12px; color:#1e3a8a; margin:4px 0 2px 0;">ARV ≈ <span id="arvRVal">—</span></div>
<div style="font-size:34px; font-weight:700; color:#1e3a8a; margin-top:6px;" id="scoreR">—</div>
</div>
<div style="text-align:center; padding:14px 8px; background:#dcfce7; border-radius:8px;">
<div style="font-size:11px; color:#15803d; text-transform:uppercase; font-weight:700;">Оптимистично × {ARV_MULT_OPTIMISTIC}</div>
<div style="font-size:12px; color:#14532d; margin:4px 0 2px 0;">ARV ≈ <span id="arvOVal">—</span></div>
<div style="font-size:34px; font-weight:700; color:#15803d; margin-top:6px;" id="scoreO">—</div>
</div>
</div>
</div>

<!-- РАСЧЁТ СДЕЛКИ -->
<div id="dealBox" class="deal-box hidden">
<h2 style="margin: 0 0 16px 0; font-size: 16px; color: #1e3a8a;">📊 Расчёт сделки (правило 70%)</h2>
<div class="deal-row">
<span>ARV (After Repair Value, ~медиана соседей)</span>
<span id="arvVal">—</span>
</div>
<div class="deal-row">
<span>× 0.70 (правило 70%)</span>
<span id="arv70">—</span>
</div>
<div class="deal-row">
<span>− Стоимость ремонта</span>
<span id="repairOut">—</span>
</div>
<div class="deal-row">
<span><strong>Макс. цена покупки (по правилу)</strong></span>
<span id="maxBuy">—</span>
</div>
<div class="deal-row">
<span>Текущий listing price</span>
<span id="listOut">—</span>
</div>
<div class="deal-row" id="verdictRow">
<span>Запас / переплата</span>
<span id="verdict">—</span>
</div>
<div class="info" id="dealHint" style="margin-top:16px;"></div>
</div>

<div class="grid">

<div class="card">
<h2>📋 Характеристики дома</h2>
<div class="kv"><span class="kv-key">APN</span><span class="kv-val">{apn}</span></div>
<div class="kv"><span class="kv-key">Год постройки</span><span class="kv-val">{year_built}</span></div>
<div class="kv"><span class="kv-key">Жилая площадь</span><span class="kv-val">{living_area_str} sqft</span></div>
<div class="kv"><span class="kv-key">Спальни</span><span class="kv-val">{beds}</span></div>
<div class="kv"><span class="kv-key">Ванные</span><span class="kv-val">{baths}</span></div>
<div class="kv"><span class="kv-key">Этажей</span><span class="kv-val">{stories}</span></div>
<div class="kv"><span class="kv-key">Гараж</span><span class="kv-val">{garage}</span></div>
<div class="kv"><span class="kv-key">Бассейн</span><span class="kv-val">{pool}</span></div>
<div class="kv"><span class="kv-key">Камин</span><span class="kv-val">{fireplace}</span></div>
<div class="kv"><span class="kv-key">Кондиционер</span><span class="kv-val">{cooling}</span></div>
<div class="kv"><span class="kv-key">Центр. отопление</span><span class="kv-val">{heating}</span></div>
</div>

<div class="card">
<h2>💰 Оценочная стоимость (county)</h2>
<div class="kv"><span class="kv-key">Total assessed value</span><span class="kv-val">{fmt_money(subject_total)}</span></div>
<div class="kv"><span class="kv-key">Медиана соседей</span><span class="kv-val">{fmt_money(median_comp) if median_comp else '—'}</span></div>
<div class="kv"><span class="kv-key">75-й перцентиль (топ-25%)</span><span class="kv-val">{fmt_money(q75_comp) if q75_comp else '—'}</span></div>
<div class="info">⚠️ Это <strong>assessed values</strong>, не listing prices. По Prop 13 реальная рыночная цена обычно <strong>выше</strong>, особенно для давних владельцев.</div>
</div>

<div class="card">
<h2>🌊 Риски</h2>
<div class="kv"><span class="kv-key">Flood zone (FEMA)</span><span class="kv-val">{flood['zone']}</span></div>
<div class="kv"><span class="kv-key">Подтип</span><span class="kv-val">{flood['subtype'] or '—'}</span></div>
<div class="kv"><span class="kv-key">Уровень</span><span class="kv-val" style="color: {'#ef4444' if flood['risk']=='high' else '#22c55e'};">{flood['risk'].upper()}</span></div>
{('<div class="warn">⚠️ Высокий риск затопления. Страховка обязательна и дорогая.</div>' if flood['risk']=='high' else '')}
</div>

<div class="card full-width">
<h2>📈 История assessed values</h2>
<table>
<thead><tr><th>Год</th><th style="text-align:right;">Земля</th><th style="text-align:right;">Строения</th><th style="text-align:right;">Итого</th></tr></thead>
<tbody>{tax_history_html}</tbody>
</table>
</div>

<div class="card full-width">
<h2>📊 Соседи в радиусе 400м — последний tax year</h2>
<table>
<thead><tr><th>APN соседа</th><th>Год</th><th style="text-align:right;">Земля</th><th style="text-align:right;">Строения</th><th style="text-align:right;">Итого</th></tr></thead>
<tbody>{comps_html}</tbody>
</table>
</div>

<div class="card full-width">
<h2>🏗️ Технический скор дома (до 20 очков)</h2>
<div style="color:#64748b; font-size:13px; margin-bottom:12px;">
Это только характеристики самого дома. Главное (финансы, до 80 очков) рассчитается выше после ввода listing price.
</div>
<table>
<thead><tr><th>Фактор</th><th style="text-align:center; width:80px;">Очки</th><th>Объяснение</th></tr></thead>
<tbody>{tech_breakdown_html}</tbody>
</table>
<div style="text-align:right; margin-top:12px; padding-top:12px; border-top:2px solid #e2e8f0; font-weight:700;">
Итого технический скор: <span style="color:#22c55e;">+{tech_score}</span>
</div>
</div>

<div class="card full-width">
<h2>📚 Что делать дальше</h2>
<ol style="line-height: 1.8; color: #475569;">
<li><strong>Введи listing price и ремонт сверху</strong> — получишь честный Flip Score.</li>
<li><strong>Если Score ≥ 80</strong> — съезди посмотри вживую. Это редкость, надо хватать.</li>
<li><strong>Если Score 60-79</strong> — на грани, торгуйся вниз. Сделка работает только при скидке.</li>
<li><strong>Если Score < 60</strong> — пропускай или жди снижения цены. Математика не сходится.</li>
<li><strong>Проверь permits</strong> в City/County Building Department — был ли неавторизованный ремонт.</li>
<li><strong>Проезжай в разное время</strong> — вечер пятницы, утро понедельника. Соседство решает.</li>
</ol>
</div>

</div>

<div class="footer">
CaliFlip v8.0 · Данные: Riverside County Assessor (Open Data), FEMA, OpenStreetMap<br>
⚠️ Образовательный инструмент. Не финансовый или юридический совет.
</div>
</div>

<script>
const DATA = {js_data};

function setVal(id, val) {{
    document.getElementById(id).value = val;
    recalc();
}}

function fmt(n) {{
    if (!isFinite(n) || n === null) return '—';
    return '$' + Math.round(n).toLocaleString('en-US');
}}

// Расчёт Flip Score для заданного ARV (без breakdown — для сценариев чувствительности).
function calcScore(arvMarket, listPrice, repair) {{
    const maxBuyByRule = arvMarket * 0.70 - repair;
    const margin = maxBuyByRule - listPrice;

    let finScore = 0;
    if (margin >= 0) finScore += 50;
    else if (margin >= -listPrice * 0.10) finScore += 20;
    else if (margin >= -listPrice * 0.20) finScore += 5;

    const discount = (DATA.median_comp - listPrice) / DATA.median_comp * 100;
    if (discount >= 20) finScore += 20;
    else if (discount >= 5) finScore += 10;
    else if (discount >= -10) finScore += 3;

    const riskScore = DATA.flood_risk === 'high' ? -10 : 5;
    return Math.max(0, Math.min(100, finScore + DATA.tech_score + riskScore));
}}

function recalc() {{
    const listPrice = parseFloat(document.getElementById('listPrice').value);
    const repair = parseFloat(document.getElementById('repairCost').value);

    const scoreCircle = document.getElementById('scoreCircle');
    const scoreLabel = document.getElementById('scoreLabel');
    const scoreDesc = document.getElementById('scoreDesc');
    const dealBox = document.getElementById('dealBox');
    const sensitivityBox = document.getElementById('sensitivityBox');

    if (!listPrice || !repair) {{
        scoreCircle.className = 'score-circle score-empty';
        scoreCircle.textContent = '?';
        scoreLabel.textContent = 'Введи listing price и ремонт выше';
        scoreLabel.style.color = '#94a3b8';
        scoreDesc.textContent = 'После ввода цифр Flip Score рассчитается автоматически.';
        dealBox.classList.add('hidden');
        sensitivityBox.classList.add('hidden');
        return;
    }}

    // ARV: оценка после ремонта. Используем 75-й перцентиль соседей (или медиану если нет),
    // потому что после качественного флипа дом продаётся выше медианы района.
    const arv = DATA.q75_comp > 0 ? DATA.q75_comp : DATA.median_comp;
    if (!arv || arv <= 0) {{
        scoreLabel.textContent = 'Недостаточно данных по соседям для расчёта';
        scoreDesc.textContent = 'Нужны comps в радиусе 400м, но их в API не нашлось.';
        return;
    }}

    // Применяем коэффициент к assessed value, чтобы примерно соотнести с market.
    // Реалистичный множитель — основной сценарий. Панель «чувствительность» ниже
    // показывает что было бы при пессимистичном/оптимистичном допущении.
    const arvMarket = arv * DATA.arv_mult_realistic;

    const maxBuyByRule = arvMarket * 0.70 - repair;
    const margin = maxBuyByRule - listPrice;
    const marginPct = (margin / listPrice) * 100;

    // ----- FINANCIAL SCORE (до 70 очков) -----
    let finScore = 0;
    let finBreakdown = [];

    // 1. ARV vs (Purchase + Repair) — главное, до 50 очков
    if (margin >= 0) {{
        finScore += 50;
        finBreakdown.push(`Сделка проходит по правилу 70% (+50). Запас $${{Math.round(margin).toLocaleString()}}.`);
    }} else if (margin >= -listPrice * 0.10) {{
        finScore += 20;
        finBreakdown.push(`Сделка близка к правилу 70%, переплата ${{Math.abs(marginPct).toFixed(1)}}% (+20). Нужен торг.`);
    }} else if (margin >= -listPrice * 0.20) {{
        finScore += 5;
        finBreakdown.push(`Переплата ${{Math.abs(marginPct).toFixed(1)}}% — рискованно (+5). Только при сильном торге.`);
    }} else {{
        finScore += 0;
        finBreakdown.push(`Переплата ${{Math.abs(marginPct).toFixed(1)}}% — математика не сходится (0).`);
    }}

    // 2. Дисконт к рынку (listing vs медиана) — до 20 очков
    const discount = (DATA.median_comp - listPrice) / DATA.median_comp * 100;
    if (discount >= 20) {{
        finScore += 20;
        finBreakdown.push(`Listing на ${{discount.toFixed(0)}}% ниже медианы соседей (+20) — отличный дисконт.`);
    }} else if (discount >= 5) {{
        finScore += 10;
        finBreakdown.push(`Listing на ${{discount.toFixed(0)}}% ниже медианы (+10) — небольшой дисконт.`);
    }} else if (discount >= -10) {{
        finScore += 3;
        finBreakdown.push(`Listing около медианы (${{discount >= 0 ? '+' : ''}}${{discount.toFixed(0)}}%) — нет дисконта (+3).`);
    }} else {{
        finScore += 0;
        finBreakdown.push(`Listing на ${{Math.abs(discount).toFixed(0)}}% выше медианы — переоценён (0).`);
    }}

    // ----- TECH SCORE (предрасcчитан Python, до 20 очков) -----
    const techScore = DATA.tech_score;

    // ----- RISK (до 10 очков, может уйти в минус) -----
    let riskScore = 0;
    if (DATA.flood_risk === 'high') {{
        riskScore = -10;
        finBreakdown.push('Flood zone HIGH (-10) — обязательная страховка.');
    }} else {{
        riskScore = 5;
        finBreakdown.push('Flood zone X — минимальный риск (+5).');
    }}

    // ----- TOTAL -----
    let totalScore = finScore + techScore + riskScore;
    totalScore = Math.max(0, Math.min(100, totalScore));

    // Покраска
    let color, label;
    if (totalScore >= 80) {{
        color = '#22c55e';
        label = '✅ Сильный кандидат — рассмотри серьёзно';
    }} else if (totalScore >= 60) {{
        color = '#f59e0b';
        label = '⚠️ На грани — работает только с жёстким торгом';
    }} else if (totalScore >= 40) {{
        color = '#f97316';
        label = '⛔ Слабая сделка — пропускай или жди снижения';
    }} else {{
        color = '#ef4444';
        label = '🚫 Не флип — математика не сходится';
    }}

    scoreCircle.style.background = color;
    scoreCircle.className = 'score-circle';
    scoreCircle.textContent = totalScore;
    scoreLabel.textContent = label;
    scoreLabel.style.color = color;
    scoreDesc.innerHTML = '<strong>Финансы:</strong> ' + finScore + '/70 · <strong>Дом:</strong> ' + techScore + '/20 · <strong>Риски:</strong> ' + (riskScore >= 0 ? '+' : '') + riskScore + '<br><br>' + finBreakdown.map(s => '• ' + s).join('<br>');

    // Deal box
    dealBox.classList.remove('hidden');
    document.getElementById('arvVal').textContent = fmt(arvMarket) + ' (× ' + DATA.arv_mult_realistic + ' к assessed, реалистичный сценарий)';

    // Панель чувствительности — три ARV-сценария с разными множителями.
    const arvP = arv * DATA.arv_mult_pessimistic;
    const arvO = arv * DATA.arv_mult_optimistic;
    document.getElementById('arvPVal').textContent = fmt(arvP);
    document.getElementById('arvRVal').textContent = fmt(arvMarket);
    document.getElementById('arvOVal').textContent = fmt(arvO);
    document.getElementById('scoreP').textContent = calcScore(arvP, listPrice, repair);
    document.getElementById('scoreR').textContent = calcScore(arvMarket, listPrice, repair);
    document.getElementById('scoreO').textContent = calcScore(arvO, listPrice, repair);
    sensitivityBox.classList.remove('hidden');
    document.getElementById('arv70').textContent = fmt(arvMarket * 0.70);
    document.getElementById('repairOut').textContent = fmt(repair);
    document.getElementById('maxBuy').textContent = fmt(maxBuyByRule);
    document.getElementById('listOut').textContent = fmt(listPrice);

    const verdictEl = document.getElementById('verdict');
    const verdictRow = document.getElementById('verdictRow');
    if (margin >= 0) {{
        verdictEl.textContent = '+' + fmt(margin) + ' запас';
        verdictEl.className = 'deal-good';
    }} else {{
        verdictEl.textContent = fmt(margin) + ' переплата';
        verdictEl.className = 'deal-bad';
    }}

    const hint = document.getElementById('dealHint');
    if (margin >= 0) {{
        hint.innerHTML = '<strong>💡 Сделка по правилу 70% работает.</strong> Это означает, что при ARV ' + fmt(arvMarket) + ' и ремонте ' + fmt(repair) + ', цена ' + fmt(listPrice) + ' оставляет нормальную маржу. Это редкая ситуация на сильном рынке — стоит съездить посмотреть.';
    }} else {{
        const targetPrice = maxBuyByRule;
        hint.innerHTML = '<strong>💡 Чтобы сделка заработала</strong>, тебе нужно купить за ' + fmt(targetPrice) + ' или дешевле. Это на ' + fmt(listPrice - targetPrice) + ' (' + Math.abs(marginPct).toFixed(0) + '%) ниже текущего listing. Реалистично ли торговаться на такую скидку — зависит от мотивации продавца (days on market, price reductions).';
    }}
}}

document.getElementById('listPrice').addEventListener('input', recalc);
document.getElementById('repairCost').addEventListener('input', recalc);

// Auto-prefill: repair всегда (по county sqft), listing — если Zillow scrape сработал.
if (DATA.prefill) {{
    if (DATA.prefill.repair_cost) {{
        document.getElementById('repairCost').value = DATA.prefill.repair_cost;
    }}
    if (DATA.prefill.listing_price) {{
        document.getElementById('listPrice').value = DATA.prefill.listing_price;
        recalc();
    }} else {{
        // Auto-focus listing price input — единственное что нужно ввести
        document.getElementById('listPrice').focus();
    }}
}}
</script>

</body>
</html>"""



def render_error_html(address, error_msg, extras=None):
    extras_html = ""
    if extras:
        for k, v in extras.items():
            extras_html += f'<div class="kv"><span>{k}</span><span><strong>{v}</strong></span></div>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>CaliFlip — ошибка</title>
<style>body{{font-family:system-ui;max-width:700px;margin:40px auto;padding:24px;}}
.warn{{background:#fef3c7;border-left:4px solid #f59e0b;padding:16px;border-radius:8px;}}
.kv{{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #eee;}}</style>
</head><body>
<h1>⚠️ Не получилось собрать полные данные</h1>
<div class="warn"><strong>Адрес:</strong> {address}<br><strong>Причина:</strong> {error_msg}</div>
{f'<h3>Что удалось собрать:</h3>{extras_html}' if extras_html else ''}
<h3>Что попробовать:</h3>
<ul>
<li>Проверь, что адрес в Riverside County (а не LA/SB/Orange/SD)</li>
<li>Формат: "123 Main St, Riverside, CA 92501"</li>
<li>Запусти diagnose.py для проверки доступности API</li>
</ul>
</body></html>"""


# ---------- ГЛАВНАЯ ----------

def analyze(user_input):
    print()
    log("🚀 CaliFlip v8.0 — анализ начат", "OK")
    log(f"Запрос: {user_input}", "INFO")
    print()

    raw_input = user_input.strip()
    is_zillow_url = raw_input.lower().startswith("http") and "zillow.com" in raw_input.lower()

    address = extract_address(raw_input)
    if not address:
        log("Прерываю — нет адреса", "ERR")
        return None

    # Если это Zillow URL — пробуем сразу выкачать listing data
    zillow = fetch_zillow_listing(raw_input) if is_zillow_url else None

    # Ранний выход для mobile homes / кондо — county data на них не работает
    if zillow and zillow.get("home_type") in NON_FLIP_HOME_TYPES:
        ht = zillow["home_type"]
        log(f"⚠️  Тип объекта: {ht} — не подходит для нашего инструмента", "ERR")
        return save_and_open(
            render_error_html(address,
                f"Это {ht} — наш инструмент работает только с обычными single-family homes.",
                {"Тип жилья (с Zillow)": ht,
                 "Цена объявления": fmt_money(zillow.get("price")),
                 "Почему не подходит": "Mobile/manufactured homes продаются как personal property "
                                       "(не real estate), у них нет отдельного APN в county Assessor. "
                                       "Кондо требуют отдельного HOA-анализа который мы пока не делаем."}),
            address)

    # PRIMARY path: точный address-search в Assessor (rooftop-accurate).
    parsed = parse_us_address(address)
    apn = None
    matched_address = None
    match_source = None  # 'exact' | 'nearest' | 'none'
    nearby_numbers = []

    if parsed:
        apn, matched_address, nearby_numbers = find_parcel_by_address(parsed)
        if apn:
            match_source = "exact"

    # FALLBACK: геокод + поиск parcel по geometry (если address-search не нашёл).
    coords = None
    if not apn:
        coords = geocode(address)
        if not coords:
            return save_and_open(render_error_html(address,
                f"Адрес не найден ни в Assessor по STREET_NUMBER+NAME ({parsed['number'] if parsed else '?'} "
                f"{parsed['name'] if parsed else '?'}), ни через геокод."), address)
        parcel = find_parcel(coords)
        if not parcel:
            flood = get_flood_zone(coords)
            return save_and_open(
                render_error_html(address,
                    "Parcel не найден в Riverside County. Это другой округ?",
                    {"Координаты": f"{coords['lat']:.5f}, {coords['lon']:.5f}",
                     "Flood zone": flood['zone']}),
                address)
        apn = parcel.get("APN")
        match_source = "nearest"
    else:
        # Для address-match нам всё равно нужны coords для FEMA flood + comps.
        # Берём через геокод (или можно из parcel geometry, но это лишний запрос).
        coords = geocode(address)
        if not coords:
            log("⚠️  Адрес нашли в Assessor, но геокод упал — flood/comps будут пустые", "WARN")
            coords = {"lat": 0, "lon": 0, "display_name": address, "class": "", "type": "", "source": "none"}

    parcel = {"APN": apn}

    # Параллельный сбор данных
    char = get_property_char(apn)
    general = get_general(apn)
    taxyear_history = get_taxyear_history(apn)
    flood = get_flood_zone(coords)

    # Comps
    neighbor_apns = find_neighbor_apns(coords)
    comps = get_comps_data(neighbor_apns, apn)

    # Subject assessed
    subject_total, subject_year = get_subject_assessed_value(taxyear_history)

    # Технический скор дома (только 20 очков из 100; финансы считаются в JS отчёта)
    tech_score, tech_breakdown = calc_tech_score(char)

    # Prefill: repair ВСЕГДА (по sqft из county data), listing — только если Zillow scrape сработал
    living_area = char.get("LIVING_AREA")
    try:
        living_area_int = int(float(living_area)) if living_area else (
            int(zillow.get("sqft")) if zillow and zillow.get("sqft") else 0)
    except (ValueError, TypeError):
        living_area_int = 0

    repair_cost, repair_label = estimate_repair_cost(
        zillow.get("description", "") if zillow else "", living_area_int)

    prefill = {
        "repair_cost": repair_cost,
        "repair_label": repair_label,
        "zillow_url": raw_input if is_zillow_url else None,
    }
    if zillow and zillow.get("price"):
        prefill["listing_price"] = zillow["price"]
        prefill["zestimate"] = int(zillow["zestimate"]) if zillow.get("zestimate") else None
        log(f"💰 Prefill: listing ${zillow['price']:,} + repair ${repair_cost:,} ({repair_label})", "OK")
    else:
        log(f"💰 Prefill: repair ${repair_cost:,} ({repair_label}). Listing price — введёшь вручную.", "OK")

    print()
    log(f"📊 Технический скор дома: {tech_score}/20", "OK")

    html = render_html(address, coords, parcel, char, general, taxyear_history,
                       comps, flood, subject_total, tech_score, tech_breakdown, prefill,
                       match_source=match_source, matched_address=matched_address,
                       nearby_numbers=nearby_numbers, requested_number=parsed["number"] if parsed else None)
    return save_and_open(html, address)


def save_and_open(html, address):
    safe = re.sub(r"[^\w\s-]", "", address).strip().replace(" ", "_")[:50]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path.home() / "califlip_reports"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"{safe}_{ts}.html"
    out_path.write_text(html, encoding="utf-8")
    log(f"💾 Отчёт сохранён: {out_path}", "OK")
    try:
        webbrowser.open(f"file://{out_path}")
        log("🌐 Открыто в браузере", "OK")
    except Exception as e:
        log(f"Не открылось автоматически: {e}", "WARN")
    return out_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print('\nПример:\n  python3 califlip.py "4080 Lemon St, Riverside, CA 92501"')
        sys.exit(1)
    try:
        analyze(" ".join(sys.argv[1:]))
    except KeyboardInterrupt:
        log("Прервано пользователем", "WARN")
    except Exception as e:
        log(f"Ошибка: {e}", "ERR")
        import traceback
        traceback.print_exc()
