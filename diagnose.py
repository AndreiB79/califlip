#!/usr/bin/env python3
"""
diagnose.py — проверка доступности API Riverside County Assessor.

Запуск:
  python3 diagnose.py
"""

import time
import requests

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://gis.countyofriverside.us/",
}

BASE = "https://gis.countyofriverside.us/arcgis_mapping/rest/services/OpenData/Assessor/MapServer"

TEST_LON = -117.37369
TEST_LAT = 33.97804


def test(name, fn):
    print(f"\n{'='*60}\n  {name}\n{'='*60}")
    t0 = time.time()
    try:
        result = fn()
        print(f"  ⏱ Время: {time.time()-t0:.2f}s")
        return result
    except Exception as e:
        print(f"  ❌ ОШИБКА после {time.time()-t0:.2f}s: {type(e).__name__}: {e}")
        return None


def t_root():
    r = requests.get(f"{BASE}?f=json", headers=HEADERS, timeout=30)
    print(f"  HTTP {r.status_code}, {len(r.content)} bytes")
    if r.status_code == 200:
        d = r.json()
        print(f"  Service: {d.get('mapName')}")
        print(f"  Layers: {[(l['id'], l['name']) for l in d.get('layers', [])]}")
        print(f"  Tables: {[(t['id'], t['name']) for t in d.get('tables', [])]}")
        return d


def t_layer(layer_id, expected):
    r = requests.get(f"{BASE}/{layer_id}?f=json", headers=HEADERS, timeout=30)
    print(f"  HTTP {r.status_code}, {len(r.content)} bytes")
    if r.status_code == 200:
        d = r.json()
        print(f"  Layer {layer_id}: {d.get('name')} (ожидалось {expected})")
        fields = d.get('fields', [])
        print(f"  Полей: {len(fields)}")
        for f in fields[:10]:
            print(f"    - {f['name']} ({f['type']})")
        return d


def t_spatial_post():
    """Поиск parcel по координатам через POST."""
    params = {
        "f": "json",
        "geometry": f"{TEST_LON},{TEST_LAT}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "APN,FLAG",
        "returnGeometry": "false",
    }
    headers = {**HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(f"{BASE}/40/query", data=params, headers=headers, timeout=60)
    print(f"  HTTP {r.status_code}, {len(r.content)} bytes")
    if r.status_code == 200:
        d = r.json()
        features = d.get('features', [])
        print(f"  Найдено parcels: {len(features)}")
        if features:
            attrs = features[0]['attributes']
            for k, v in attrs.items():
                print(f"    {k}: {v}")
            return attrs


def t_pin_lookup(apn, table_id, table_name):
    """Lookup в таблице по PIN (он же APN)."""
    if not apn:
        print("  ⏭  Skip: APN не получен на предыдущем шаге")
        return None
    apn_no_dash = str(apn).replace("-", "")
    params = {
        "f": "json",
        "where": f"PIN='{apn_no_dash}' OR PIN='{apn}'",
        "outFields": "*",
        "returnGeometry": "false",
    }
    headers = {**HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(f"{BASE}/{table_id}/query", data=params, headers=headers, timeout=60)
    print(f"  HTTP {r.status_code}, {len(r.content)} bytes")
    if r.status_code == 200:
        d = r.json()
        features = d.get('features', [])
        print(f"  {table_name}: найдено записей {len(features)}")
        if features:
            attrs = features[0]['attributes']
            for k, v in list(attrs.items())[:15]:
                print(f"    {k}: {v}")
            return attrs


def main():
    print("🔍 DIAGNOSE v2: проверяю Riverside County Assessor API")
    print(f"   Тест: 4080 Lemon St, Riverside ({TEST_LAT}, {TEST_LON})")

    test("Тест 1: ROOT — структура сервиса", t_root)
    test("Тест 2: LAYER 40 PARCELS — метаданные", lambda: t_layer(40, "PARCELS"))
    test("Тест 3: TABLE 80 CREST_PROPERTY_CHAR — метаданные",
         lambda: t_layer(80, "CREST_PROPERTY_CHAR"))
    test("Тест 4: TABLE 100 CREST_TAXYEAR — метаданные",
         lambda: t_layer(100, "CREST_TAXYEAR"))

    parcel = test("Тест 5: SPATIAL QUERY — поиск parcel по координатам", t_spatial_post)
    apn = parcel.get("APN") if parcel else None

    if apn:
        print(f"\n   ✅ APN получен: {apn}")
        test("Тест 6: CREST_PROPERTY_CHAR по APN",
             lambda: t_pin_lookup(apn, 80, "CREST_PROPERTY_CHAR"))
        test("Тест 7: CREST_TAXYEAR по APN",
             lambda: t_pin_lookup(apn, 100, "CREST_TAXYEAR"))
        test("Тест 8: CREST_GENERAL по APN",
             lambda: t_pin_lookup(apn, 70, "CREST_GENERAL"))

    print(f"\n{'='*60}\n  ГОТОВО\n{'='*60}")
    print("Скопируй весь вывод и пришли Claude.")


if __name__ == "__main__":
    main()
