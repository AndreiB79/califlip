#!/usr/bin/env python3
"""
CaliFlip — Zillow-like UI для скрининга fix-and-flip кандидатов в Калифорнии.
Источники: Zillow (RapidAPI), Redfin (бесплатно), Propwire CSV (загрузка).
"""

import streamlit as st
import pandas as pd
from califlip_api import (
    fetch_zillow_listings, fetch_redfin_listings, parse_propwire_csv,
    quick_score, deep_analysis, apply_client_filters,
    detect_county, has_full_county_support,
    fixer_score, ZIP_RECOMMENDATIONS, PRESET_BUNDLES, get_smart_filters,
    fetch_sold_comps, estimate_profit_quick,
    RENOVATION_PRESETS, flip_calculator_2026,
)
from califlip import ARV_MULT_PESSIMISTIC, ARV_MULT_REALISTIC, ARV_MULT_OPTIMISTIC, extract_address
import re as _re


st.set_page_config(page_title="CaliFlip", page_icon="🏠", layout="wide")


# ──────────────────────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ — рендер карточки дома
# ──────────────────────────────────────────────────────────────

def _render_property_card(s, idx):
    """Карточка одного дома в стиле Zillow."""
    l = s["listing"]
    source = l.get("_source", "zillow")
    price = l.get("price") or 0
    fixer = s.get("fixer_score", 0)
    profit = s.get("estimated_profit")
    vs = s.get("vs_market_pct")

    source_badges = {"zillow": "🟡 Zillow", "redfin": "🔵 Redfin", "propwire": "🟣 Propwire"}
    source_label = source_badges.get(source, source.title())

    with st.container(border=True):
        # Фото (только Zillow даёт URL)
        if l.get("photo_url"):
            st.image(l["photo_url"], use_container_width=True)

        # Индекс + источник
        st.caption(f"#{idx + 1}  ·  {source_label}")

        # Цена — главная цифра
        st.markdown(f"### ${price:,}")

        # Адрес
        st.write(f"**{l.get('address_street', '')}**")

        # Характеристики
        parts = []
        if l.get("beds") is not None:
            parts.append(f"🛏 {l['beds']}")
        if l.get("baths") is not None:
            parts.append(f"🚿 {l['baths']}")
        if l.get("sqft"):
            parts.append(f"📐 {l['sqft']:,} sqft")
        if l.get("year_built"):
            parts.append(f"📅 {l['year_built']}")
        if parts:
            st.write("  ·  ".join(parts))

        # Дней на рынке
        dom = l.get("days_on_market")
        if dom:
            if dom > 90:
                dom_label = f"🔥 {dom} дней — долго стоит"
            elif dom > 30:
                dom_label = f"⏱ {dom} дней на рынке"
            else:
                dom_label = f"🆕 {dom} дней"
            st.caption(dom_label)

        # Нужен ли ремонт
        if fixer >= 65:
            st.write(f"🔨 **Нужен ремонт: {fixer}/100** ← интересно")
        elif fixer >= 40:
            st.write(f"🔨 Нужен ремонт: {fixer}/100")
        else:
            st.write(f"⚪ Выглядит готовым: {fixer}/100")

        # Vs рынок
        if vs is not None:
            if vs < -5:
                st.caption(f"📉 {vs:+.1f}% vs медиана — дешевле района")
            elif vs > 10:
                st.caption(f"📈 {vs:+.1f}% vs медиана — дороже района")
            else:
                st.caption(f"➡️ {vs:+.1f}% vs медиана — в рынке")

        # Прибыль
        if profit is not None:
            if profit >= 20000:
                st.markdown(f"**💰 Прибыль: +${profit:,}**")
            elif profit >= 5000:
                st.markdown(f"**🟡 Прибыль: +${profit:,}**")
            else:
                st.markdown(f"**❌ Убыток: ${profit:,}**")

        # Кнопка открыть
        url = l.get("zillow_url")
        if url:
            label = "↗ Открыть на Redfin" if source == "redfin" else "↗ Открыть на Zillow"
            st.link_button(label, url, use_container_width=True)


# ──────────────────────────────────────────────────────────────
# DEEP DIVE (county data + полный flip score)
# ──────────────────────────────────────────────────────────────

def _render_deep(deep):
    """Полный анализ с county data."""
    listing = deep["listing"]
    county = deep.get("county_name", "Unknown")
    match = deep["county_match"]

    if match == "exact":
        st.success(f"✅ **Riverside County** · APN **{deep['county_apn']}** "
                   f"({deep.get('county_matched_address', '')}) · полные county данные")
    elif match == "nearest":
        st.warning("⚠️ **Riverside County** · точного адреса нет, взят ближайший parcel")
    elif match == "zillow_only":
        st.info(f"📊 **{county} County** · Zillow-only анализ "
                f"(deep county data только для Riverside). "
                f"Quick Score + Flip Score всё равно работают.")
    else:
        st.error(f"⚠️ Не смог достать county данные. "
                 f"Если адрес выглядит правильно — пришли скриншот.")
        return

    # Cross-check Zillow ↔ County
    if match in ("exact", "nearest") and deep["county_char"]:
        char = deep["county_char"]
        st.markdown("##### 🔄 Cross-check Zillow ↔ County")
        cmp_data = pd.DataFrame([
            {"поле": "Спален",  "Zillow": listing.get("beds"),       "County": char.get("BEDROOM_COUNT")},
            {"поле": "Ванных",  "Zillow": listing.get("baths"),      "County": char.get("BATH_COUNT")},
            {"поле": "sqft",    "Zillow": listing.get("sqft"),       "County": char.get("LIVING_AREA")},
            {"поле": "Год",     "Zillow": listing.get("year_built"), "County": char.get("YEAR_BUILT")},
        ])
        st.dataframe(cmp_data, hide_index=True, use_container_width=False)

    scores = deep["flip_scores"]
    if not scores or not isinstance(scores, dict):
        st.warning("Недостаточно данных для Flip Score")
        return

    meta = scores.get("_meta", {})
    listing_price = meta.get("price", 0)
    repair = meta.get("repair", 0)
    real_arv = deep.get("real_arv")
    arv_source_text = deep.get("arv_source_text", "")
    realistic_offer = deep.get("realistic_offer", 0)
    offer_discount = deep.get("offer_discount_pct", 0)
    offer_reasoning = deep.get("offer_reasoning", "")
    sold_comps = deep.get("sold_comps", [])

    if not real_arv:
        real_arv = scores.get("realistic", {}).get("arv", 0)
        arv_source_text = "оценка Zillow (sold comps не нашлись)"

    from califlip_api import _estimate_flip_economics
    profit_listing = _estimate_flip_economics(real_arv, listing_price, repair)
    profit_offer = _estimate_flip_economics(real_arv, realistic_offer, repair)
    net_listing = int(profit_listing.get("net_profit", 0))
    net_offer = int(profit_offer.get("net_profit", 0))
    costs_listing = profit_listing.get("costs_breakdown", {})
    costs_offer = profit_offer.get("costs_breakdown", {})

    if net_offer > 30000:
        verdict_emoji, verdict_text, verdict_type = "🟢", "Стоит съездить посмотреть", "success"
    elif net_offer > 5000:
        verdict_emoji, verdict_text, verdict_type = "🟡", "На грани — только если понравится глазами", "warning"
    else:
        verdict_emoji, verdict_text, verdict_type = "🔴", "Пропускай — на retail цене не окупится", "error"

    big_block = (
        f"# {verdict_emoji} В карман {'+' if net_offer >= 0 else '−'}${abs(net_offer):,}\n\n"
        f"если предложишь **${realistic_offer:,}** "
        f"(это −{offer_discount*100:.0f}% от ${listing_price:,})  \n"
        f"и продашь после ремонта за **${real_arv:,}** ({arv_source_text})\n\n"
        f"### ➡ {verdict_text}"
    )
    if verdict_type == "success":
        st.success(big_block)
    elif verdict_type == "warning":
        st.warning(big_block)
    else:
        st.error(big_block)

    st.info(f"💬 **Почему такой торг:** {offer_reasoning}")

    st.markdown("## 🏘 Что реально продавалось рядом (~6 мес)")
    if sold_comps:
        sold_df = pd.DataFrame([
            {"Адрес": s["address"], "Цена": f"${s['sold_price']:,}",
             "$/sqft": f"${s['price_per_sqft']}", "Sqft": s["sqft"],
             "Beds": s["beds"], "Baths": s["baths"], "Год": s["year"]}
            for s in sold_comps
        ])
        st.dataframe(sold_df, hide_index=True, use_container_width=True)
        st.caption(f"✅ {len(sold_comps)} аналогов · медиана **${deep.get('median_psqft', 0)}/sqft**")
    else:
        st.warning("Sold comps не нашлись. ARV рассчитан по Zestimate (менее точно).")

    st.markdown("## 🧾 Два сценария")

    def _scenario_table(buy_price, repair_cost, sell_price, costs, net):
        carrying = int(costs.get("carrying", 0))
        selling_fee = int(costs.get("selling", 0))
        buying_fee = int(costs.get("buying", 0))
        total_spent = int(buy_price) + int(repair_cost) + carrying + selling_fee + buying_fee
        df = pd.DataFrame({"Статья": [
            "🏠 Купил", "🔧 Ремонт", "📋 Оформление покупки",
            "💸 Налог + страховка + utilities", "👔 Риелтор при продаже",
            "━━━━━━━━━━━━━━━━", "📤 ИТОГО ПОТРАТИЛ",
            "💰 Продал после ремонта", "━━━━━━━━━━━━━━━━", "🎯 В КАРМАН",
        ], "Сумма": [
            f"−${int(buy_price):,}", f"−${int(repair_cost):,}",
            f"−${buying_fee:,}", f"−${carrying:,}", f"−${selling_fee:,}",
            "", f"−${total_spent:,}", f"+${int(sell_price):,}",
            "", f"{'+' if net >= 0 else '−'}${abs(int(net)):,}",
        ]})
        st.table(df.set_index("Статья"))

    col_a, col_b = st.columns(2)
    with col_a:
        label = f"По цене Zillow (${int(listing_price):,})"
        if net_listing > 5000:
            st.success(f"### ✅ {label}")
        elif net_listing > -5000:
            st.warning(f"### ⚠️ {label}")
        else:
            st.error(f"### ❌ {label}")
        _scenario_table(listing_price, repair, real_arv, costs_listing, net_listing)

    with col_b:
        label = f"Реалистичный торг ${realistic_offer:,} (−{offer_discount*100:.0f}%)"
        if net_offer > 30000:
            st.success(f"### ✅ {label}")
        elif net_offer > 5000:
            st.warning(f"### 🟡 {label}")
        else:
            st.error(f"### ❌ {label}")
        _scenario_table(realistic_offer, repair, real_arv, costs_offer, net_offer)

    with st.expander("ℹ Откуда цифры"):
        zest = meta.get("zestimate", 0)
        st.markdown(f"""
- **ARV ${real_arv:,.0f}** — {arv_source_text} (Zestimate был ${zest:,.0f})
- **Ремонт ${repair:,.0f}** — {meta.get('sqft', 0)} sqft × $60 ({meta.get('repair_label', '')})
- **Торг −{offer_discount*100:.0f}%** — {offer_reasoning}
- **Расходы** — средние California 2026 (tax 1.15%, риелтор 7%, страховка $150/мес)

⚠️ Это **прогноз**, не гарантия. Едь смотреть глазами.
        """)


# ──────────────────────────────────────────────────────────────
# SINGLE URL MODE — анализ одного дома
# ──────────────────────────────────────────────────────────────

def _run_single_url_analysis(url):
    address = extract_address(url)
    if not address:
        st.error("Не смог извлечь адрес из URL.")
        return None

    zip_match = _re.search(r"\bCA\s+(\d{5})\b", address)
    if not zip_match:
        zip_match = _re.search(r"\b(9\d{4})\b", address)
    if not zip_match:
        st.error(f"Не нашёл ZIP в адресе '{address}'.")
        return None
    zip_code = zip_match.group(1)

    zpid_match = _re.search(r"/(\d+)_zpid", url)
    target_zpid = int(zpid_match.group(1)) if zpid_match else None

    with st.status("🔍 Анализирую...", expanded=True) as status:
        st.write(f"📍 {address} · ZIP {zip_code}")
        try:
            active_listings = fetch_zillow_listings(zip_code, max_pages=2)
        except Exception as e:
            st.error(f"❌ {e}")
            return None

        target_house = None
        if target_zpid:
            for l in active_listings:
                if l.get("zpid") == target_zpid:
                    target_house = l
                    break
        if not target_house:
            addr_lower = address.lower()
            for l in active_listings:
                street = (l.get("address_street") or "").lower()
                if street and street.split(",")[0] in addr_lower:
                    target_house = l
                    break

        st.write(f"✅ Дом {'найден' if target_house else 'не в active listings'}")
        st.write("📊 Фетчу проданные дома (~6 мес)...")
        sold_comps = fetch_sold_comps(zip_code, max_results=50)
        st.write(f"✅ {len(sold_comps)} продаж в ZIP {zip_code}")
        status.update(label="✅ Готово!", state="complete")

    return {
        "url": url, "address": address, "zip": zip_code,
        "target_house": target_house, "sold_comps": sold_comps,
    }


def _render_single_result(result):
    address = result["address"]
    zip_code = result["zip"]
    target = result["target_house"]
    sold_comps = result["sold_comps"]

    st.markdown(f"# 🏠 {address}")
    st.markdown(f"[↗ Открыть на Zillow]({result['url']})")

    st.markdown(f"## 📊 Рынок ZIP {zip_code} (~6 мес)")
    if sold_comps:
        prices = sorted([s["sold_price"] for s in sold_comps if s.get("sold_price")])
        psqfts = sorted([s["price_per_sqft"] for s in sold_comps if s.get("price_per_sqft")])
        median_price = prices[len(prices) // 2] if prices else 0
        median_psqft = psqfts[len(psqfts) // 2] if psqfts else 0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Продано всего", f"{len(sold_comps)}")
        c2.metric("Медиана цены", f"${median_price:,}")
        c3.metric("Медиана $/sqft", f"${median_psqft}")
        c4.metric("Диапазон", f"${prices[0]//1000}k–${prices[-1]//1000}k" if prices else "—")

        if target:
            st.markdown("## 🏠 Этот дом")
            cols = st.columns([1, 2])
            with cols[0]:
                if target.get("photo_url"):
                    st.image(target["photo_url"], use_container_width=True)
            with cols[1]:
                target_price = target.get("price") or 0
                target_psqft = target.get("price_per_sqft") or 0
                vs_market = (target_psqft - median_psqft) / median_psqft * 100 if median_psqft and target_psqft else 0

                m1, m2 = st.columns(2)
                m1.metric("Цена", f"${target_price:,}")
                m2.metric("vs медиана района", f"{vs_market:+.1f}%")
                m3, m4 = st.columns(2)
                m3.metric("Площадь", f"{target.get('sqft', '—')} sqft")
                m4.metric("$/sqft", f"${target_psqft}")
                m5, m6, m7 = st.columns(3)
                m5.metric("Спален", target.get("beds", "—"))
                m6.metric("Ванных", target.get("baths", "—"))
                m7.metric("Год", target.get("year_built", "—"))

                highlights = target.get("highlights") or []
                if highlights:
                    st.markdown("**🏷 Из объявления:**")
                    for h in highlights:
                        st.markdown(f"- {h}")
        else:
            st.warning("Дом не в active listings. Введи данные вручную:")
            mc1, mc2, mc3 = st.columns(3)
            with mc1:
                m_price = st.number_input("Цена $", min_value=50000, max_value=5000000,
                                          value=300000, step=5000, key="m_price")
                m_sqft = st.number_input("Площадь sqft", min_value=300,
                                         max_value=10000, value=1200, step=50, key="m_sqft")
            with mc2:
                m_beds = st.number_input("Спален", min_value=1, max_value=10,
                                         value=3, step=1, key="m_beds")
                m_baths = st.number_input("Ванных", min_value=1, max_value=10,
                                          value=2, step=1, key="m_baths")
            with mc3:
                m_year = st.number_input("Год постройки", min_value=1900,
                                         max_value=2026, value=1980, step=1, key="m_year")
                m_zest = st.number_input("Zestimate $ (или 0)", min_value=0,
                                         max_value=5000000, value=0, step=5000, key="m_zest")
            target = {
                "address_street": address, "address_full": address, "zip": zip_code,
                "price": m_price, "sqft": m_sqft, "beds": m_beds, "baths": m_baths,
                "year_built": m_year, "zestimate": m_zest if m_zest > 0 else None,
                "price_per_sqft": int(m_price / m_sqft) if m_sqft else None,
                "days_on_market": None, "highlights": [], "photo_url": None,
            }

        # Flip calculator
        if target:
            st.markdown("---")
            _render_flip_calculator_inline(target, sold_comps)

        st.markdown(f"## 🏘 Все {len(sold_comps)} проданных домов")
        sold_df = pd.DataFrame([
            {"Адрес": s["address"], "Цена": f"${s['sold_price']:,}",
             "$/sqft": f"${s['price_per_sqft']}", "Sqft": s["sqft"],
             "Beds": s["beds"], "Baths": s["baths"], "Год": s["year"]}
            for s in sorted(sold_comps, key=lambda x: x.get("sold_price") or 0, reverse=True)
        ])
        st.dataframe(sold_df, hide_index=True, use_container_width=True, height=400)
    else:
        st.warning(f"Не нашлось продаж в ZIP {zip_code}.")


def _render_flip_calculator_inline(target, sold_comps):
    """Калькулятор флипа внутри анализа одного дома."""
    sqft = target.get("sqft") or 0
    purchase_price = target.get("price") or 0
    zestimate = target.get("zestimate") or 0

    if not sqft or not purchase_price:
        st.warning("Нет данных о цене или площади — калькулятор недоступен.")
        return

    similar = [s for s in sold_comps
               if s.get("sqft") and sqft * 0.75 <= s["sqft"] <= sqft * 1.25
               and s.get("price_per_sqft")]
    psqfts = sorted([s["price_per_sqft"] for s in similar])
    if psqfts:
        median_psqft = psqfts[len(psqfts) // 2]
        arv_auto = int(median_psqft * sqft)
        arv_source = f"медиана ${median_psqft}/sqft × {sqft:,} sqft ({len(similar)} аналогов)"
    elif zestimate:
        arv_auto = int(zestimate)
        arv_source = "Zestimate от Zillow"
    else:
        st.warning("Нет данных для ARV — нужны sold comps или Zestimate.")
        return

    st.markdown("## 🧮 Калькулятор флипа")

    col_arv, col_arv_info = st.columns([1, 2])
    with col_arv:
        arv = st.number_input("💰 Продам после ремонта (ARV) $",
                              value=arv_auto, min_value=50000, max_value=5000000,
                              step=5000, key="flip_arv")
    with col_arv_info:
        st.caption(f"Авто-расчёт: {arv_source}")

    st.markdown("---")
    st.markdown("#### 🔧 Ремонт")
    preset_keys = list(RENOVATION_PRESETS.keys())
    reno_labels = [
        f"{RENOVATION_PRESETS[k]['label']}  ·  ${RENOVATION_PRESETS[k]['cost_per_sqft']}/sqft"
        f" = ~${RENOVATION_PRESETS[k]['cost_per_sqft'] * sqft:,.0f}"
        for k in preset_keys
    ]
    reno_idx = st.radio("Состояние дома:", range(len(preset_keys)),
                        format_func=lambda i: reno_labels[i], index=1,
                        horizontal=False, key="flip_reno_idx")
    reno_type = preset_keys[reno_idx]
    preset = RENOVATION_PRESETS[reno_type]
    st.caption(f"_{preset['description']}_")

    custom_reno = st.number_input("Или своя сумма ремонта $ (0 = пресет выше)",
                                  min_value=0, max_value=1000000, value=0,
                                  step=5000, key="flip_custom_reno")
    contingency_pct = st.slider("Запас на неожиданности %", 0, 30, 15, 5,
                                 key="flip_contingency")

    st.markdown("---")
    st.markdown("#### 💳 Финансирование")
    financing = st.radio("Источник денег:", ["hard_money", "cash"],
                         format_func=lambda x: (
                             "🏦 Hard money · 11% + 2 points"
                             if x == "hard_money" else "💵 Наличные"
                         ), horizontal=False, key="flip_financing")

    col_hold, col_rate = st.columns(2)
    with col_hold:
        hold_months = st.slider("Месяцев держишь", 2, 18,
                                preset["default_reno_months"] + 2, 1, key="flip_hold")
    with col_rate:
        hm_rate = st.slider("Ставка %/год", 8.0, 15.0, 11.0, 0.5,
                             key="flip_hm_rate") if financing == "hard_money" else 11.0

    calc = flip_calculator_2026(
        purchase_price=purchase_price, sqft=sqft, arv=arv,
        renovation_type=reno_type,
        custom_repair_total=custom_reno if custom_reno > 0 else None,
        contingency_pct=contingency_pct / 100,
        financing=financing, hard_money_rate=hm_rate / 100,
        hold_months=hold_months,
    )
    if not calc:
        return

    net = calc["net_profit"]
    st.markdown("---")
    st.markdown("## 📊 Результат")

    if calc["verdict"] == "green":
        st.success(f"### В КАРМАН +${int(net):,}\nСтоит съездить посмотреть глазами")
    elif calc["verdict"] == "yellow":
        st.warning(f"### В КАРМАН +${int(net):,}\nНа грани — только если понравится вживую")
    else:
        sign = "−" if net < 0 else "+"
        st.error(f"### В КАРМАН {sign}${abs(int(net)):,}\nПо этой цене денег нет")

    rows = [("🏠 Купишь дом", f"−${int(purchase_price):,}")]
    if custom_reno > 0:
        rows.append((f"🔧 Ремонт (свой ввод) + {contingency_pct}%",
                     f"−${int(calc['total_repair']):,}"))
    else:
        rows.append((f"🔧 Ремонт ({preset['label']}) + {contingency_pct}%",
                     f"−${int(calc['total_repair']):,}"))
    rows.append((f"📋 Оформление покупки", f"−${int(calc['buying_costs']):,}"))
    rows.append((f"⏱ Держишь {hold_months} мес", f"−${int(calc['total_carrying']):,}"))
    rows.append((f"👔 Продажа с агентами (6.5%)", f"−${int(calc['selling_costs']):,}"))
    rows.append(("━━━━━━━━━━━━━━━━━━━━━", ""))
    rows.append(("📤 ИТОГО ПОТРАТИШЬ", f"−${int(calc['total_spent']):,}"))
    rows.append(("💰 Продашь после ремонта", f"+${int(arv):,}"))
    rows.append(("━━━━━━━━━━━━━━━━━━━━━", ""))
    _s = "−" if net < 0 else "+"
    rows.append(("🎯 В КАРМАНЕ", f"{_s}${abs(int(net)):,}"))

    st.table(pd.DataFrame(rows, columns=["Статья", "Сумма"]).set_index("Статья"))

    mao = calc["mao"]
    if mao > 0:
        st.markdown("### 💡 Максимальная цена покупки (MAO)")
        mc1, mc2 = st.columns(2)
        with mc1:
            st.metric("MAO (65% правило IE)", f"${int(mao):,}")
            st.metric("Zillow просит", f"${int(purchase_price):,}")
        with mc2:
            if calc["discount_needed"] > 0:
                st.metric("Нужен торг",
                          f"−${int(calc['discount_needed']):,}",
                          delta=f"−{calc['discount_pct']*100:.0f}%",
                          delta_color="inverse")
                if calc["discount_pct"] > 0.25:
                    st.error("Торг >25% — реально только distressed/аукцион.")
                elif calc["discount_pct"] > 0.12:
                    st.warning("Торг 12-25% — возможно при 90+ дней на рынке.")
                else:
                    st.success("Торг <12% — реальный на текущем рынке.")
            else:
                st.success(f"✅ Цена уже ниже MAO — можно брать по листингу!")


# ──────────────────────────────────────────────────────────────
# ВКЛАДКА "КАЛЬКУЛЯТОР" — standalone, без листинга
# ──────────────────────────────────────────────────────────────

def _render_standalone_calculator():
    st.markdown("### Введи параметры сделки")
    st.caption("Для расчёта не нужен конкретный листинг — просто вбей цифры.")

    col1, col2 = st.columns(2)
    with col1:
        calc_price = st.number_input("🏠 Цена покупки $",
                                     min_value=50000, max_value=5000000,
                                     value=350000, step=5000, key="sa_price")
        calc_sqft = st.number_input("📐 Площадь sqft",
                                    min_value=300, max_value=10000,
                                    value=1400, step=50, key="sa_sqft")
    with col2:
        calc_arv = st.number_input("💰 Продам после ремонта (ARV) $",
                                   min_value=50000, max_value=5000000,
                                   value=480000, step=5000, key="sa_arv")
        st.caption("ARV = цена после ремонта. Смотри проданные аналоги в ZIPе "
                   "через вкладку «Поиск домов».")

    st.markdown("---")
    st.markdown("#### 🔧 Ремонт")
    preset_keys = list(RENOVATION_PRESETS.keys())
    reno_labels = [
        f"{RENOVATION_PRESETS[k]['label']}  —  "
        f"${RENOVATION_PRESETS[k]['cost_per_sqft']}/sqft  ≈  "
        f"${RENOVATION_PRESETS[k]['cost_per_sqft'] * calc_sqft:,.0f}"
        for k in preset_keys
    ]
    reno_idx = st.radio("Уровень ремонта:", range(len(preset_keys)),
                        format_func=lambda i: reno_labels[i], index=1, key="sa_reno")
    reno_type = preset_keys[reno_idx]
    preset = RENOVATION_PRESETS[reno_type]
    st.caption(f"_{preset['description']}_")

    custom_reno = st.number_input("Или своя сумма ремонта $ (0 = пресет)",
                                  min_value=0, max_value=1000000,
                                  value=0, step=5000, key="sa_custom_reno")
    contingency = st.slider("Запас на неожиданности %", 0, 30, 15, 5, key="sa_contingency")

    st.markdown("---")
    st.markdown("#### 💳 Финансирование")
    financing = st.radio("Источник денег:", ["hard_money", "cash"],
                         format_func=lambda x: (
                             "🏦 Hard money  ·  11%/год + 2 points"
                             if x == "hard_money" else "💵 Наличные / своя ипотека"
                         ), horizontal=False, key="sa_financing")

    default_hold = preset["default_reno_months"] + 2
    col_h, col_r = st.columns(2)
    with col_h:
        hold_months = st.slider("Месяцев держишь", 2, 18, default_hold, 1, key="sa_hold")
    with col_r:
        hm_rate = (
            st.slider("Ставка %/год", 8.0, 15.0, 11.0, 0.5, key="sa_hm_rate")
            if financing == "hard_money" else 11.0
        )

    # Считаем в реальном времени (без кнопки)
    calc = flip_calculator_2026(
        purchase_price=calc_price, sqft=calc_sqft, arv=calc_arv,
        renovation_type=reno_type,
        custom_repair_total=custom_reno if custom_reno > 0 else None,
        contingency_pct=contingency / 100,
        financing=financing, hard_money_rate=hm_rate / 100,
        hold_months=hold_months,
    )
    if not calc:
        return

    net = calc["net_profit"]
    st.markdown("---")
    st.markdown("## 📊 Результат")

    if calc["verdict"] == "green":
        st.success(f"### ✅ В КАРМАН +${int(net):,}")
    elif calc["verdict"] == "yellow":
        st.warning(f"### 🟡 В КАРМАН +${int(net):,}")
    else:
        sign = "−" if net < 0 else "+"
        st.error(f"### ❌ В КАРМАН {sign}${abs(int(net)):,}")

    # Детальный чек
    rows = [("🏠 Купишь дом", f"−${int(calc_price):,}")]
    if custom_reno > 0:
        rows.append((f"🔧 Ремонт (свой) + {contingency}%", f"−${int(calc['total_repair']):,}"))
    else:
        rows.append((f"🔧 Ремонт ({preset['label']}, ${preset['cost_per_sqft']}/sqft) + {contingency}%",
                     f"−${int(calc['total_repair']):,}"))
    rows.append(("📋 Оформление покупки", f"−${int(calc['buying_costs']):,}"))
    rows.append((f"⏱ Держишь {hold_months} мес", f"−${int(calc['total_carrying']):,}"))
    rows.append(("👔 Продажа с агентами (6.5%)", f"−${int(calc['selling_costs']):,}"))
    rows.append(("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", ""))
    rows.append(("📤 ИТОГО ПОТРАТИШЬ", f"−${int(calc['total_spent']):,}"))
    rows.append(("💰 Продашь после ремонта", f"+${int(calc_arv):,}"))
    rows.append(("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", ""))
    _s = "−" if net < 0 else "+"
    rows.append(("🎯 В КАРМАНЕ", f"{_s}${abs(int(net)):,}"))
    st.table(pd.DataFrame(rows, columns=["Статья", "Сумма"]).set_index("Статья"))

    # MAO блок
    mao = calc["mao"]
    if mao > 0:
        st.markdown("### 💡 Максимальная цена покупки (MAO = 65% правило)")
        mc1, mc2 = st.columns(2)
        with mc1:
            st.metric("Нужно купить не дороже", f"${int(mao):,}")
            st.metric("Ты указал цену", f"${int(calc_price):,}")
        with mc2:
            if calc["discount_needed"] > 0:
                st.metric("Нужен торг",
                          f"−${int(calc['discount_needed']):,}",
                          delta=f"−{calc['discount_pct']*100:.0f}%",
                          delta_color="inverse")
                if calc["discount_pct"] > 0.25:
                    st.error("Нужен торг >25% — реально только distressed/аукцион.")
                elif calc["discount_pct"] > 0.12:
                    st.warning("Торг 12-25% — возможно при 90+ дней на рынке.")
                else:
                    st.success("Торг <12% — реальный на текущем рынке.")
            else:
                st.success(f"✅ Цена уже ниже MAO — выгодная сделка!")
                st.metric("Запас прочности", f"+${int(-calc['discount_needed']):,}")
        if calc["profit_at_mao"] > 0:
            st.info(f"💬 Если купишь за MAO ${int(mao):,} — в кармане будет **${int(calc['profit_at_mao']):,}**")


# ──────────────────────────────────────────────────────────────
# SIDEBAR
# ──────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("🏠 CaliFlip")
    st.caption("Screener флип-кандидатов в Калифорнии")
    st.divider()

    # === ИСТОЧНИКИ ДАННЫХ ===
    st.subheader("📊 Источники данных")
    use_zillow = st.toggle("🟡 Zillow (через RapidAPI)", value=True,
                           help="Основной источник. Требует ключ в .env. $4.99/мес Pro план.")
    use_redfin = st.toggle("🔵 Redfin (бесплатно)", value=True,
                           help="Бесплатный источник. +20-30% уникальных листингов. "
                                "Нет фото и Zestimate.")
    st.caption("🟣 **Propwire CSV** — загрузи файл ниже:")
    propwire_file = st.file_uploader(
        "Propwire CSV (distressed / pre-foreclosure)",
        type=["csv"],
        help="Скачай список на propwire.com (бесплатно до 10k/мес) и загрузи сюда. "
             "Добавит distressed объекты которых нет на Zillow/Redfin."
    )
    if not use_zillow and not use_redfin and propwire_file is None:
        st.error("Выбери хотя бы один источник!")

    st.divider()

    # === PRESET BUNDLES ===
    st.subheader("🎯 Готовый маршрут")
    preset_options = ["— Свой выбор —"] + list(PRESET_BUNDLES.keys())
    chosen_preset = st.selectbox("Куrated bundle ZIPов", options=preset_options, index=0)

    if chosen_preset != "— Свой выбор —":
        zips = PRESET_BUNDLES[chosen_preset]
        loc_lines = []
        for z in zips:
            info = ZIP_RECOMMENDATIONS.get(z, {})
            city = info.get("city", "")
            loc_lines.append(f"{city}, CA {z}" if city else z)
        st.session_state["_preset_locations"] = "\n".join(loc_lines)
        first_info = ZIP_RECOMMENDATIONS.get(zips[0], {})
        if first_info:
            st.session_state["_preset_price_min"] = first_info["price_min"]
            st.session_state["_preset_price_max"] = first_info["price_max"]
            st.session_state["_preset_year_max"] = first_info["year_max"]
            st.session_state["_preset_bed_min"] = first_info["bed_min"]
        with st.expander(f"ℹ {len(zips)} ZIP в bundle", expanded=False):
            for z in zips:
                info = ZIP_RECOMMENDATIONS.get(z, {})
                if info:
                    st.markdown(
                        f"**{info['city']} ({z})** · {info['drive_minutes']} мин  \n"
                        f"_{info['notes']}_"
                    )

    st.divider()

    # === ЛОКАЦИИ ===
    st.subheader("📍 Локации")
    default_loc = st.session_state.get("_preset_locations", "Banning, CA 92220")
    locations_raw = st.text_area(
        "Одна на строку", value=default_loc, height=100,
        help="'Hemet, CA 92543' или просто '92543'. Несколько — каждый на новой строке."
    )

    first_line = (locations_raw.splitlines() or [""])[0]
    smart = get_smart_filters(first_line)
    if smart:
        if st.button(f"🧠 Умные фильтры для {smart['city']}", use_container_width=True):
            st.session_state["_preset_price_min"] = smart["price_min"]
            st.session_state["_preset_price_max"] = smart["price_max"]
            st.session_state["_preset_year_max"] = smart["year_max"]
            st.session_state["_preset_bed_min"] = smart["bed_min"]
            st.rerun()
        st.caption(f"_{smart['notes']}_")

    st.divider()

    # === ФИЛЬТРЫ ===
    st.subheader("💰 Фильтры")
    c1, c2 = st.columns(2)
    with c1:
        price_min = st.number_input("Цена от $",
                                    value=st.session_state.get("_preset_price_min", 250000),
                                    step=10000, format="%d")
        bed_min = st.number_input("Спален от",
                                   value=st.session_state.get("_preset_bed_min", 2),
                                   step=1, min_value=0, max_value=10)
    with c2:
        price_max = st.number_input("Цена до $",
                                    value=st.session_state.get("_preset_price_max", 500000),
                                    step=10000, format="%d")
        year_max = st.number_input("Год не позже",
                                   value=st.session_state.get("_preset_year_max", 1985),
                                   step=5, min_value=1900, max_value=2026)

    max_pages = st.slider("Глубина (страниц Zillow)", 1, 5, 2,
                          help="1 стр ≈ 200 листингов. Каждая стр = 1 RapidAPI запрос.")

    st.divider()

    # === ДОПОЛНИТЕЛЬНО ===
    st.subheader("🔧 Дополнительно")
    profitable_only = st.toggle("Только прибыльные (≥ $20k)", value=False,
                                 help="Скрывает дома где прибыль меньше порога.")
    min_profit = st.slider("Минимальная прибыль $", 0, 100000, 20000, 5000,
                           disabled=not profitable_only)

    fixer_only = st.toggle("Только fixer-uppers", value=False)
    fixer_threshold = st.slider("Минимальный Fixer Score", 30, 90, 55, 5,
                                 disabled=not fixer_only)

    sort_options = [
        "🔨 Нужен ремонт", "💰 Прибыль", "📉 Дешевле рынка",
        "⏱ Дни на рынке", "💵 Цена (дешевле)", "📐 $/sqft", "📅 Год постройки"
    ]
    sort_by = st.selectbox("Сортировать по", options=sort_options, index=0)

    st.divider()

    col_run, col_clr = st.columns(2)
    with col_run:
        run_btn = st.button("🚀 Скринить!", type="primary", use_container_width=True)
    with col_clr:
        clear_btn = st.button("🗑 Очистить", use_container_width=True)

    st.caption("RapidAPI: ~$0.005/ZIP · 1000 запросов/мес на Pro плане.")


# ──────────────────────────────────────────────────────────────
# SESSION STATE
# ──────────────────────────────────────────────────────────────

for key in ("listings", "scored", "deep_cache", "single_result", "market_stats",
            "sold_cache", "redfin_errors"):
    if key not in st.session_state:
        st.session_state[key] = {} if key in ("deep_cache", "sold_cache") else None

if clear_btn:
    for key in ("listings", "scored", "deep_cache", "single_result", "market_stats",
                "sold_cache", "redfin_errors"):
        st.session_state[key] = {} if key in ("deep_cache", "sold_cache") else None
    st.rerun()


# ──────────────────────────────────────────────────────────────
# MAIN TABS
# ──────────────────────────────────────────────────────────────

tab_search, tab_calc = st.tabs(["🔍 Поиск домов", "🧮 Калькулятор рентабельности"])

with tab_calc:
    _render_standalone_calculator()

with tab_search:

    # === ОДИН ДОМ ПО ССЫЛКЕ ===
    with st.expander("🔗 Анализ одного дома по ссылке Zillow", expanded=False):
        single_url = st.text_input(
            "Вставь ссылку",
            placeholder="https://www.zillow.com/homedetails/...",
            key="single_url_input"
        )
        col_ana, col_clr2 = st.columns(2)
        with col_ana:
            analyze_single_btn = st.button("🔬 Анализировать", type="primary",
                                           use_container_width=True)
        with col_clr2:
            if st.button("🗑 Сбросить", use_container_width=True):
                st.session_state.single_result = None
                st.rerun()

    if analyze_single_btn and single_url:
        result = _run_single_url_analysis(single_url.strip())
        if result:
            st.session_state.single_result = result
            st.session_state.scored = None

    if st.session_state.single_result:
        _render_single_result(st.session_state.single_result)
        st.stop()

    # ──────────────────────────────────────────────────────────
    # BULK SEARCH — fetch + score
    # ──────────────────────────────────────────────────────────

    if run_btn:
        st.session_state.deep_cache = {}
        locations = [l.strip() for l in locations_raw.splitlines() if l.strip()]
        if not locations:
            st.error("Введи хотя бы одну локацию.")
            st.stop()
        if not use_zillow and not use_redfin and propwire_file is None:
            st.error("Выбери хотя бы один источник данных в сайдбаре.")
            st.stop()

        all_listings = []
        redfin_errors = []

        progress = st.progress(0, text="Подключаюсь...")

        # --- Zillow ---
        if use_zillow:
            try:
                for loc_idx, loc in enumerate(locations):
                    def on_progress_z(page, total_pages, count,
                                      _loc=loc, _idx=loc_idx, _total=len(locations)):
                        loc_pct = _idx / _total
                        page_pct = page / max(total_pages, 1) / _total
                        progress.progress(min(0.45, loc_pct * 0.45 + page_pct),
                                          text=f"🟡 Zillow · {_loc} · стр {page}/{total_pages} · {count} листингов")

                    loc_listings = fetch_zillow_listings(
                        loc, price_min=price_min, price_max=price_max,
                        bed_min=bed_min if bed_min > 0 else None,
                        year_max=year_max, max_pages=max_pages,
                        on_progress=on_progress_z,
                    )
                    for l in loc_listings:
                        l["_source_location"] = loc
                    all_listings.extend(loc_listings)
            except Exception as e:
                st.error(f"❌ Zillow: {e}")

        # --- Redfin ---
        if use_redfin:
            redfin_total = 0
            for loc_idx, loc in enumerate(locations):
                progress.progress(
                    0.45 + (loc_idx / len(locations)) * 0.35,
                    text=f"🔵 Redfin · {loc}..."
                )
                rf_listings, rf_err = fetch_redfin_listings(
                    loc, price_min=price_min, price_max=price_max,
                    bed_min=bed_min if bed_min > 0 else None,
                    year_max=year_max, max_pages=1,
                )
                if rf_err:
                    redfin_errors.append(f"{loc}: {rf_err}")
                else:
                    for l in rf_listings:
                        l["_source_location"] = loc
                    all_listings.extend(rf_listings)
                    redfin_total += len(rf_listings)

            st.session_state.redfin_errors = redfin_errors

        # --- Propwire CSV ---
        if propwire_file is not None:
            progress.progress(0.82, text="🟣 Парсю Propwire CSV...")
            pw_listings, pw_err = parse_propwire_csv(propwire_file.read())
            if pw_err:
                st.warning(f"⚠️ Propwire CSV: {pw_err}")
            else:
                for l in pw_listings:
                    # Применяем фильтры к Propwire данным
                    if price_min and (l.get("price") or 0) < price_min:
                        continue
                    if price_max and (l.get("price") or 0) > price_max:
                        continue
                    if bed_min and (l.get("beds") or 0) < bed_min:
                        continue
                    if year_max and (l.get("year_built") or 9999) > year_max:
                        continue
                    all_listings.append(l)

        progress.progress(0.85, text="Применяю фильтры...")

        # --- Client-side фильтрация (Zillow-only, Redfin уже фильтрован) ---
        zillow_only = [l for l in all_listings if l.get("_source") == "zillow"]
        other = [l for l in all_listings if l.get("_source") != "zillow"]
        filtered_zillow, filter_stats = apply_client_filters(
            zillow_only, price_min=price_min, price_max=price_max,
            bed_min=bed_min if bed_min > 0 else None, year_max=year_max,
        )
        all_listings = filtered_zillow + other

        progress.progress(0.88, text="Скорю...")

        if not all_listings:
            progress.empty()
            st.warning(f"Листингов не нашлось. Попробуй расширить фильтры или другие ZIPы.")
            st.stop()

        # --- Scoring ---
        scored = []
        for l in all_listings:
            qscore, qreasons = quick_score(l)
            fscore, freasons = fixer_score(l)
            scored.append({
                "score": qscore, "reasons": qreasons,
                "fixer_score": fscore, "fixer_reasons": freasons,
                "listing": l,
                "estimated_profit": None,
            })

        # --- Sold comps ---
        unique_zips = sorted({l.get("zip") for l in all_listings if l.get("zip")})
        sold_cache = {}
        for i, z in enumerate(unique_zips):
            progress.progress(
                0.88 + (i / max(len(unique_zips), 1)) * 0.10,
                text=f"📊 Sold comps ZIP {z} ({i+1}/{len(unique_zips)})..."
            )
            try:
                sold_cache[z] = fetch_sold_comps(z, max_results=50)
            except Exception:
                sold_cache[z] = []
        st.session_state.sold_cache = sold_cache

        # --- Market stats ---
        market_stats = {}
        for z, sold in sold_cache.items():
            prices = sorted([s["sold_price"] for s in sold if s.get("sold_price")])
            psqfts = sorted([s["price_per_sqft"] for s in sold if s.get("price_per_sqft")])
            if prices and psqfts:
                market_stats[z] = {
                    "n_sold": len(sold),
                    "median_price": prices[len(prices) // 2],
                    "median_psqft": psqfts[len(psqfts) // 2],
                    "min_price": prices[0], "max_price": prices[-1],
                }
        st.session_state.market_stats = market_stats

        # --- Profit + vs market для каждого дома ---
        for s in scored:
            l = s["listing"]
            z = l.get("zip")
            stats = market_stats.get(z, {})
            median_psqft = stats.get("median_psqft")
            psqft = l.get("price_per_sqft")
            if median_psqft and psqft:
                s["vs_market_pct"] = round((psqft - median_psqft) / median_psqft * 100, 1)
            else:
                s["vs_market_pct"] = None
            est = estimate_profit_quick(l, sold_comps_cache=sold_cache)
            s["estimated_profit"] = est["estimated_profit"]
            s["realistic_offer"] = est["realistic_offer"]
            s["real_arv"] = est["real_arv"]

        # --- Фильтры ---
        if profitable_only:
            before = len(scored)
            scored = [s for s in scored if (s.get("estimated_profit") or 0) >= min_profit]
            st.session_state["profit_filter_dropped"] = before - len(scored)
        else:
            st.session_state["profit_filter_dropped"] = 0

        if fixer_only:
            scored = [s for s in scored if s["fixer_score"] >= fixer_threshold]

        # --- Сортировка ---
        if "Прибыль" in sort_by:
            scored.sort(key=lambda x: (x.get("estimated_profit") or -999999), reverse=True)
        elif "Нужен ремонт" in sort_by:
            scored.sort(key=lambda x: x["fixer_score"], reverse=True)
        elif "Дешевле рынка" in sort_by:
            scored.sort(key=lambda x: (x.get("vs_market_pct") if x.get("vs_market_pct") is not None else 999))
        elif "Дни" in sort_by:
            scored.sort(key=lambda x: (x["listing"].get("days_on_market") or 0), reverse=True)
        elif "Цена" in sort_by:
            scored.sort(key=lambda x: (x["listing"].get("price") or 0))
        elif "sqft" in sort_by:
            scored.sort(key=lambda x: (x["listing"].get("price_per_sqft") or 9999))
        elif "Год" in sort_by:
            scored.sort(key=lambda x: (x["listing"].get("year_built") or 0))

        progress.empty()

        st.session_state.listings = all_listings
        st.session_state.scored = scored
        st.session_state.locations_searched = locations
        st.session_state.pre_filter_count = len(all_listings)

    # ──────────────────────────────────────────────────────────
    # RESULTS — показываем карточки
    # ──────────────────────────────────────────────────────────

    if st.session_state.scored is not None:
        scored = st.session_state.scored
        locs = st.session_state.get("locations_searched", [])
        profit_dropped = st.session_state.get("profit_filter_dropped", 0)
        redfin_errors = st.session_state.get("redfin_errors") or []

        if redfin_errors:
            with st.expander(f"⚠️ Redfin: {len(redfin_errors)} ошибок", expanded=False):
                for e in redfin_errors:
                    st.caption(e)
                st.caption("Redfin может блокировать автоматические запросы. "
                           "Zillow и Propwire данные всё равно загружены.")

        if len(scored) == 0:
            if profit_dropped > 0:
                st.error(f"💔 Прибыльных deals не нашлось. "
                         f"Попробуй снизить порог прибыли или отключи фильтр 'Только прибыльные'.")
            else:
                st.warning("Список пустой. Расширь фильтры или выбери другие ZIPы.")
            st.stop()

        # Статистика источников
        all_list = st.session_state.listings or []
        sources = {}
        for l in all_list:
            src = l.get("_source", "zillow")
            sources[src] = sources.get(src, 0) + 1

        total_str = f"**{len(scored)}** домов"
        src_parts = []
        if sources.get("zillow"):
            src_parts.append(f"🟡 Zillow: {sources['zillow']}")
        if sources.get("redfin"):
            src_parts.append(f"🔵 Redfin: {sources['redfin']}")
        if sources.get("propwire"):
            src_parts.append(f"🟣 Propwire: {sources['propwire']}")
        st.success(f"✅ Найдено {total_str} в {len(locs)} локациях  ·  {' · '.join(src_parts)}")

        # Market stats
        market_stats = st.session_state.get("market_stats", {})
        if market_stats:
            st.markdown("### 📊 Рынок по ZIPам (~6 мес продаж)")
            cols = st.columns(min(len(market_stats), 4))
            for i, (z, stats) in enumerate(market_stats.items()):
                if i >= 4:
                    break
                with cols[i]:
                    st.metric(f"ZIP {z}", f"${stats['median_price']:,}",
                              help=(f"Медиана продаж за 6 мес. "
                                    f"n={stats['n_sold']} · ${stats['median_psqft']}/sqft · "
                                    f"${stats['min_price']:,}–${stats['max_price']:,}"))
                    st.caption(f"${stats['median_psqft']}/sqft · {stats['n_sold']} продаж")

        st.markdown("---")

        # Сколько карточек показывать
        n_show = st.slider("Показать карточек", 5, min(50, len(scored)), min(20, len(scored)), 5)

        # Карточки — 2 в ряд (на мобиле автоматически 1)
        shown = scored[:n_show]
        for row_start in range(0, len(shown), 2):
            cols = st.columns(2, gap="medium")
            for j in range(2):
                idx = row_start + j
                if idx < len(shown):
                    with cols[j]:
                        _render_property_card(shown[idx], idx)

        # ── DEEP DIVE ──
        st.markdown("---")
        st.subheader("🔍 Детальный анализ — county data + Flip Score")
        st.caption("Раскрой карточку и нажми «Полный анализ» — подтянем Riverside Assessor, "
                   "comps, FEMA и посчитаем точный Flip Score.")

        top_n = st.slider("Сколько домов для deep dive", 3, 20, 8)
        for i, s in enumerate(scored[:top_n]):
            l = s["listing"]
            cache_key = str(l.get("zpid") or l.get("address_full"))
            has_deep = cache_key in st.session_state.deep_cache

            days_str = f"{l['days_on_market']}d" if l.get("days_on_market") else "—"
            source_badge = {"zillow": "🟡", "redfin": "🔵", "propwire": "🟣"}.get(
                l.get("_source", "zillow"), "")
            title = (f"#{i+1} {source_badge} · **{l['address_street']}** · "
                     f"${l['price']:,} · {l.get('beds')}/{l.get('baths')} · "
                     f"{l.get('sqft')}sqft · {l.get('year_built')}г · {days_str}"
                     f"{'  ✅' if has_deep else ''}")

            with st.expander(title, expanded=has_deep):
                colA, colB = st.columns([1, 2])
                with colA:
                    if l.get("photo_url"):
                        st.image(l["photo_url"], use_container_width=True)
                    if l.get("zillow_url"):
                        src = l.get("_source", "zillow")
                        link_label = "↗ Redfin" if src == "redfin" else "↗ Zillow"
                        st.markdown(f"[{link_label}]({l['zillow_url']})")
                with colB:
                    highlights = l.get("highlights") or []
                    if highlights:
                        st.markdown("**🏷 Highlights:** " + " · ".join(f"`{h}`" for h in highlights))

                    tab1, tab2 = st.tabs(["Quick Score", f"🔨 Fixer {s['fixer_score']}/100"])
                    with tab1:
                        for r in s["reasons"]:
                            st.markdown(f"- {r}")
                    with tab2:
                        if s["fixer_reasons"]:
                            for r in s["fixer_reasons"]:
                                st.markdown(f"- {r}")
                        else:
                            st.caption("Нет ярких fixer-сигналов.")

                    btn_label = "🔄 Обновить анализ" if has_deep else "🔬 Полный анализ с county data"
                    if st.button(btn_label, key=f"deep_btn_{i}",
                                 type="primary" if not has_deep else "secondary"):
                        try:
                            with st.spinner("Подключаюсь к Riverside Assessor + FEMA..."):
                                st.session_state.deep_cache[cache_key] = deep_analysis(l)
                            st.rerun()
                        except Exception as e:
                            st.error(f"❌ Анализ упал: {e}")
                            import traceback
                            st.code(traceback.format_exc(), language="python")

                if has_deep:
                    st.markdown("---")
                    _render_deep(st.session_state.deep_cache[cache_key])

    else:
        # Empty state — подсказка
        st.info("👈 Выбери локации и фильтры в сайдбаре, нажми **Скринить!**")
        st.markdown("""
### Как пользоваться

**Источники данных:**
- 🟡 **Zillow** — основной, нужен ключ RapidAPI ($4.99/мес). Фото + Zestimate.
- 🔵 **Redfin** — бесплатно, +20-30% уникальных домов которых нет на Zillow
- 🟣 **Propwire** — загрузи CSV с propwire.com (бесплатно). Pre-foreclosure, absentee owner, vacant properties

**Что показывают карточки:**
- Цена · площадь · год · дни на рынке
- 🔨 **Нужен ремонт** (0-100) — сигналы price cut, дисконт к рынку, старый дом
- 💰 **Расчётная прибыль** после реалистичного торга и ремонта

**Deep dive** — для топ-N домов подтягивает Riverside County Assessor:
налоговая история, характеристики, FEMA зоны, точный Flip Score

**Вкладка 🧮 Калькулятор** — считай рентабельность любой сделки вручную
        """)
