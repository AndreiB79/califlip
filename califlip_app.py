#!/usr/bin/env python3
"""
CaliFlip Streamlit UI — bulk screener для флип-кандидатов.

Запуск:
  streamlit run califlip_app.py
"""

import streamlit as st
import pandas as pd
from califlip_api import (fetch_zillow_listings, quick_score, deep_analysis,
                          apply_client_filters, detect_county, has_full_county_support,
                          fixer_score, ZIP_RECOMMENDATIONS, PRESET_BUNDLES, get_smart_filters,
                          fetch_sold_comps, estimate_profit_quick,
                          RENOVATION_PRESETS, flip_calculator_2026)
from califlip import ARV_MULT_PESSIMISTIC, ARV_MULT_REALISTIC, ARV_MULT_OPTIMISTIC, extract_address
import re as _re


st.set_page_config(page_title="CaliFlip — Bulk Screener", page_icon="🏠", layout="wide")


def _render_deep(deep):
    """Полный анализ с county data."""
    listing = deep["listing"]

    county = deep.get("county_name", "Unknown")
    match = deep["county_match"]

    if match == "exact":
        st.success(f"✅ **Riverside County** · точный match: APN **{deep['county_apn']}** "
                   f"({deep.get('county_matched_address', '')}) · полные county данные ниже")
    elif match == "nearest":
        st.warning("⚠️ **Riverside County** · точного адреса нет, взят ближайший parcel")
    elif match == "zillow_only":
        st.info(f"📊 **{county} County** · Zillow-only анализ "
                f"(deep county integration пока только для Riverside; "
                f"для {county} используем Zestimate как ARV). "
                f"Quick Score + Flip Score sensitivity всё равно работают.")
    else:
        st.error(f"⚠️ Не смог достать данные для этого адреса (county: {county}). "
                 f"Если адрес выглядит правильно — пришли скриншот, разберёмся.")
        return

    # Cross-check Zillow ↔ County — только если есть county data
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
        st.warning("Недостаточно данных для Flip Score (нет comps или цены)")
        return

    # ===== РЕАЛЬНЫЕ ДАННЫЕ — sold comps + realistic offer =====
    meta = scores.get("_meta", {})
    listing_price = meta.get("price", 0)
    repair = meta.get("repair", 0)

    # NEW: реальные числа из sold comps и realistic torgа
    real_arv = deep.get("real_arv")
    arv_source_text = deep.get("arv_source_text", "")
    realistic_offer = deep.get("realistic_offer", 0)
    offer_discount = deep.get("offer_discount_pct", 0)
    offer_reasoning = deep.get("offer_reasoning", "")
    sold_comps = deep.get("sold_comps", [])

    # Если real_arv не получилось — fallback на Zestimate
    if not real_arv:
        real_arv = scores.get("realistic", {}).get("arv", 0)
        arv_source_text = "оценка Zillow (sold comps не нашлись)"

    # Считаем прибыль для двух сценариев на РЕАЛЬНОМ ARV
    from califlip_api import _estimate_flip_economics
    profit_listing = _estimate_flip_economics(real_arv, listing_price, repair)
    profit_offer = _estimate_flip_economics(real_arv, realistic_offer, repair)
    net_listing = int(profit_listing.get("net_profit", 0))
    net_offer = int(profit_offer.get("net_profit", 0))
    costs_listing = profit_listing.get("costs_breakdown", {})
    costs_offer = profit_offer.get("costs_breakdown", {})

    # ====== ГЛАВНЫЙ КВАДРАТ — реальная прибыль при реалистичном offer ======
    if net_offer > 30000:
        verdict_emoji, verdict_text, verdict_type = "🟢", "Стоит съездить посмотреть", "success"
    elif net_offer > 5000:
        verdict_emoji, verdict_text, verdict_type = "🟡", "На грани — только если очень понравится глазами", "warning"
    else:
        verdict_emoji, verdict_text, verdict_type = "🔴", "Пропускай — на retail listing'е не окупится", "error"

    big_block = (
        f"# {verdict_emoji} В карман {'+' if net_offer >= 0 else '−'}${abs(net_offer):,}\n\n"
        f"если предложишь **${realistic_offer:,}** "
        f"(это −{offer_discount*100:.0f}% от ${listing_price:,} — реалистичный торг)  \n"
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

    # ====== SOLD COMPS ТАБЛИЦА — что реально продавалось ======
    st.markdown("## 🏘 Что реально продавалось рядом (последние ~6 мес)")
    if sold_comps:
        sold_df = pd.DataFrame([
            {
                "Адрес": s["address"],
                "Цена продажи": f"${s['sold_price']:,}",
                "$/sqft": f"${s['price_per_sqft']}",
                "Sqft": s["sqft"],
                "Beds": s["beds"],
                "Baths": s["baths"],
                "Год": s["year"],
            }
            for s in sold_comps
        ])
        st.dataframe(sold_df, hide_index=True, use_container_width=True)
        st.caption(f"✅ Найдено **{len(sold_comps)} проданных аналогов** (отфильтровано по похожему sqft ±25%). "
                   f"Медиана **${deep.get('median_psqft', 0)}/sqft** — это основа расчёта ARV.")
    else:
        st.warning("Не нашлось sold comps для этого ZIP/sqft. ARV рассчитан по Zestimate "
                   "(менее точно — можем промахнуться ±15%).")

    st.markdown("## 🧾 Подробный расчёт — два сценария")

    def _scenario_table(buy_price, repair_cost, sell_price, costs, net):
        """Native Streamlit: pandas DataFrame через st.table — рендерится надёжно."""
        carrying = int(costs.get("carrying", 0))
        selling_fee = int(costs.get("selling", 0))
        buying_fee = int(costs.get("buying", 0))
        total_spent = int(buy_price) + int(repair_cost) + carrying + selling_fee + buying_fee

        # Чек как простая таблица. Сумма со знаком, всё выровнено по правому краю.
        df = pd.DataFrame({
            "Статья": [
                "🏠 Купил дом",
                "🔧 Сделал ремонт",
                "📋 Оформление покупки",
                "💸 Налог + страховка + utilities (5 мес)",
                "👔 Заплатил риелтору при продаже",
                "━━━━━━━━━━━━━━━━━━━━━━━━",
                "📤 ВСЕГО ПОТРАТИЛ",
                "💰 Продал отремонтированный дом",
                "━━━━━━━━━━━━━━━━━━━━━━━━",
                "🎯 В КАРМАН",
            ],
            "Сумма": [
                f"−${int(buy_price):,}",
                f"−${int(repair_cost):,}",
                f"−${buying_fee:,}",
                f"−${carrying:,}",
                f"−${selling_fee:,}",
                "",
                f"−${total_spent:,}",
                f"+${int(sell_price):,}",
                "",
                f"{'+' if net >= 0 else '−'}${abs(int(net)):,}",
            ],
        })
        st.table(df.set_index("Статья"))

    col_a, col_b = st.columns(2)
    with col_a:
        if net_listing > 5000:
            st.success(f"### ✅ Купить по цене Zillow (${int(listing_price):,})")
        elif net_listing > -5000:
            st.warning(f"### ⚠️ Купить по цене Zillow (${int(listing_price):,})")
        else:
            st.error(f"### ❌ Купить по цене Zillow (${int(listing_price):,})")
        _scenario_table(listing_price, repair, real_arv, costs_listing, net_listing)

    with col_b:
        if net_offer > 30000:
            st.success(f"### ✅ Купить за ${realistic_offer:,} (реалистичный торг −{offer_discount*100:.0f}%)")
        elif net_offer > 5000:
            st.warning(f"### 🟡 Купить за ${realistic_offer:,} (торг −{offer_discount*100:.0f}%)")
        else:
            st.error(f"### ❌ Купить за ${realistic_offer:,} (даже с торгом)")
        _scenario_table(realistic_offer, repair, real_arv, costs_offer, net_offer)

    # Маленький technical info внизу — для тех кто хочет deep dive
    with st.expander("ℹ Откуда я взял эти цифры (для тех кому интересна математика)"):
        zest = meta.get("zestimate", 0)
        sold_n = len(sold_comps)
        st.markdown(f"""
- **Цена после ремонта (ARV)** = ${real_arv:,.0f} — {arv_source_text}
  - Reference: Zillow Zestimate был ${zest:,.0f}
  - Считаем по **реальным продажам** соседей (n={sold_n}) — это надёжнее Zestimate
- **Стоимость ремонта** = ${repair:,.0f} — оценка по дому {meta.get('sqft', 0)} sqft × $60 ({meta.get('repair_label', '')})
- **Реалистичная цена торга** = ${realistic_offer:,.0f} — это −{offer_discount*100:.0f}% от listing
  - Логика: {offer_reasoning}
- **Расходы покупки/продажи/удержания** — средние California 2026 (tax 1.15%, риелтор 7%, страховка $150/мес)

⚠️ Эти числа — **прогноз**. Реальный ремонт может стоить дороже, реальная продажная цена — другая. Это **screener**, не оракул. Едь смотри глазами.
        """)



def _render_flip_calculator(target, sold_comps):
    """Новый упрощённый калькулятор флипа 2026. Язык: вложил/потратил/в кармане."""
    sqft = target.get("sqft") or 0
    purchase_price = target.get("price") or 0
    zestimate = target.get("zestimate") or 0

    if not sqft or not purchase_price:
        st.warning("Нет данных о цене или площади — калькулятор недоступен.")
        return

    # ARV из sold comps
    similar = [s for s in sold_comps
               if s.get("sqft") and sqft * 0.75 <= s["sqft"] <= sqft * 1.25
               and s.get("price_per_sqft")]
    psqfts = sorted([s["price_per_sqft"] for s in similar])
    if psqfts:
        median_psqft = psqfts[len(psqfts) // 2]
        arv_auto = int(median_psqft * sqft)
        arv_source = f"медиана ${median_psqft}/sqft × {sqft:,} sqft ({len(similar)} аналогов в ZIP)"
    elif zestimate:
        arv_auto = int(zestimate)
        arv_source = "Zestimate от Zillow (sold comps не нашлись)"
    else:
        st.warning("Нет данных для расчёта ARV — нужны sold comps или Zestimate.")
        return

    st.markdown("## 🧮 Калькулятор флипа")

    # ARV — пользователь может скорректировать
    col_arv, col_arv_info = st.columns([1, 2])
    with col_arv:
        arv = st.number_input(
            "💰 Продашь после ремонта (ARV) $",
            value=arv_auto, min_value=50000, max_value=5000000, step=5000,
            key="flip_arv",
        )
    with col_arv_info:
        st.caption(f"Авто-расчёт: {arv_source}")
        if arv != arv_auto:
            st.caption(f"✏️ Изменено вручную (авто было ${arv_auto:,})")

    st.markdown("---")

    # Уровень ремонта
    st.markdown("#### 🔧 Уровень ремонта (твоя оценка после просмотра)")
    preset_keys = list(RENOVATION_PRESETS.keys())

    reno_labels = []
    for k in preset_keys:
        p = RENOVATION_PRESETS[k]
        cost = p["cost_per_sqft"] * sqft
        reno_labels.append(f"{p['label']}  ·  ${p['cost_per_sqft']}/sqft = ~${cost:,.0f}")

    reno_idx = st.radio(
        "Состояние дома:",
        options=list(range(len(preset_keys))),
        format_func=lambda i: reno_labels[i],
        index=1,
        horizontal=False,
        key="flip_reno_idx",
    )
    reno_type = preset_keys[reno_idx]
    preset = RENOVATION_PRESETS[reno_type]
    st.caption(f"_{preset['description']}_")

    custom_reno = st.number_input(
        "Или введи свою сумму ремонта $ (оставь 0 чтобы использовать пресет выше)",
        min_value=0, max_value=1000000, value=0, step=5000,
        key="flip_custom_reno",
    )

    contingency_pct = st.slider(
        "Запас на неожиданности %",
        min_value=0, max_value=30, value=15, step=5,
        help="Реальные флипперы закладывают 15-20% — всегда что-то вылезает.",
        key="flip_contingency",
    )

    st.markdown("---")

    # Финансирование
    st.markdown("#### 💳 Как покупаешь")
    financing = st.radio(
        "Источник денег:",
        options=["hard_money", "cash"],
        format_func=lambda x: (
            "🏦 Hard money  ·  11% годовых + 2 points (самый распространённый у флипперов)"
            if x == "hard_money"
            else "💵 Наличные / своя ипотека  ·  без процентов по займу"
        ),
        horizontal=False,
        key="flip_financing",
    )

    col_hold, col_rate = st.columns(2)
    with col_hold:
        default_months = preset["default_reno_months"] + 2
        hold_months = st.slider(
            "Месяцев держишь (ремонт + продажа)",
            min_value=2, max_value=18, value=default_months, step=1,
            help="В Riverside сейчас ~49 дней до продажи после листинга (+2 мес). Не занижай.",
            key="flip_hold",
        )
    with col_rate:
        if financing == "hard_money":
            hm_rate = st.slider(
                "Ставка hard money %/год",
                min_value=8.0, max_value=15.0, value=11.0, step=0.5,
                key="flip_hm_rate",
            )
        else:
            hm_rate = 11.0
            st.caption("Без hard money — экономишь на процентах.")

    # Считаем
    calc = flip_calculator_2026(
        purchase_price=purchase_price,
        sqft=sqft,
        arv=arv,
        renovation_type=reno_type,
        custom_repair_total=custom_reno if custom_reno > 0 else None,
        contingency_pct=contingency_pct / 100,
        financing=financing,
        hard_money_rate=hm_rate / 100,
        hold_months=hold_months,
    )
    if not calc:
        return

    net = calc["net_profit"]
    st.markdown("---")
    st.markdown("## 📊 Результат")

    if calc["verdict"] == "green":
        msg = f"### Результат: В КАРМАН +${int(net):,}"
        st.success(msg + "\n\nСтоит съездить посмотреть глазами")
    elif calc["verdict"] == "yellow":
        msg = f"### Результат: В КАРМАН +${int(net):,}"
        st.warning(msg + "\n\nНа грани — только если очень понравится вживую")
    else:
        sign = "-" if net < 0 else "+"
        msg = "### Результат: В КАРМАН " + sign + f"${abs(int(net)):,}"
        st.error(msg + "\n\nПо этой цене денег нет")

    # Чек
    rows = [
        ("🏠 Купишь дом", f"−${int(purchase_price):,}"),
    ]
    if custom_reno > 0:
        rows.append((f"🔧 Ремонт (свой ввод) + {contingency_pct}% запас",
                     f"−${int(calc['total_repair']):,}"))
    else:
        rows.append((f"🔧 Ремонт ({preset['label']}, ${preset['cost_per_sqft']}/sqft) + {contingency_pct}% запас",
                     f"−${int(calc['total_repair']):,}"))

    if financing == "hard_money":
        rows.append((f"📋 Оформление покупки  (hard money {calc['loan_amount']:,.0f} × 2pts + escrow)",
                     f"−${int(calc['buying_costs']):,}"))
        rows.append((f"⏱ Держишь {hold_months} мес  (проценты ${calc['monthly_interest']:,.0f}/мес + налог/страховка/утилиты)",
                     f"−${int(calc['total_carrying']):,}"))
    else:
        rows.append(("📋 Оформление покупки  (escrow + title + inspection)",
                     f"−${int(calc['buying_costs']):,}"))
        rows.append((f"⏱ Держишь {hold_months} мес  (налог + страховка + утилиты)",
                     f"−${int(calc['total_carrying']):,}"))

    rows.append((f"👔 Продажа с агентами (6.5% от ${int(arv):,})",
                 f"−${int(calc['selling_costs']):,}"))
    rows.append(("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", ""))
    rows.append(("📤 ИТОГО ПОТРАТИШЬ", f"−${int(calc['total_spent']):,}"))
    rows.append(("💰 Продашь после ремонта", f"+${int(arv):,}"))
    rows.append(("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", ""))
    _s = "-" if net < 0 else "+"
    rows.append(("🎯 В КАРМАНЕ", _s + f"${abs(int(net)):,}"))

    df_check = pd.DataFrame(rows, columns=["Статья", "Сумма"])
    st.table(df_check.set_index("Статья"))

    # MAO блок
    mao = calc["mao"]
    st.markdown("### 💡 Максимальная цена покупки (MAO)")
    if mao > 0:
        mao_col1, mao_col2 = st.columns(2)
        with mao_col1:
            st.metric("Нужно купить не дороже (65% правило IE)", f"${int(mao):,}")
            st.metric("Zillow просит", f"${int(purchase_price):,}")
        with mao_col2:
            if calc["discount_needed"] > 0:
                st.metric(
                    "Нужен торг",
                    f"−${int(calc['discount_needed']):,}",
                    delta=f"−{calc['discount_pct']*100:.0f}% от цены",
                    delta_color="inverse",
                )
                if calc["discount_pct"] > 0.25:
                    st.error("Нужен торг >25% — реально только на distressed/probate/аукционе.")
                elif calc["discount_pct"] > 0.12:
                    st.warning("Торг 12-25% — сложно, но возможно при 90+ дней на рынке.")
                else:
                    st.success("Торг <12% — вполне реальный на текущем рынке.")
            else:
                st.success(f"✅ Цена уже ниже MAO — можно покупать по листингу!")
                st.metric("Запас прочности", f"+${int(-calc['discount_needed']):,}")

        if calc["profit_at_mao"] > 0:
            st.info(f"💬 Если купишь за MAO ${int(mao):,} — в кармане будет **${int(calc['profit_at_mao']):,}**")

    # Ссылки на off-market источники по этому ZIP
    zip_code = target.get("zip") or ""
    if zip_code:
        with st.expander("🏛 Найти этот дом дешевле — off-market источники"):
            st.markdown(f"""
**Аукционы (Foreclosure / REO) — самый большой дисконт:**
- [Auction.com — ZIP {zip_code}](https://www.auction.com/residential/search?zip={zip_code})
- [Hubzu — аукционы банк-owned](https://www.hubzu.com/)
- [RealtyTrac CA](https://www.realtytrac.com/ca/foreclosure/auction/)

**Probate / Pre-Foreclosure данные:**
- [Foreclosure.com — ZIP {zip_code}](https://www.foreclosure.com/listing/detail/?zip={zip_code})
- [US Probate Leads](https://www.usprobateleads.com/)

**Investor платформы (платные, но мощные):**
- [PropStream $99/мес](https://www.propstream.com/) — 160M+ объектов, 165 фильтров
- [PropertyRadar $119/мес](https://www.propertyradar.com/) — лучшее покрытие CA, NOD в реальном времени
            """)


# ---------- HEADER ----------
st.title("🏠 CaliFlip — массовый скрининг флипов в Калифорнии")
st.caption("Два этапа: (1) Quick Score по Zillow данным для всех листингов · "
           "(2) Deep dive с county data + Flip Score для топ-N")


# ---------- SIDEBAR ----------
with st.sidebar:
    # ====== РЕЖИМ РАБОТЫ ======
    st.header("🎚 Режим работы")
    mode = st.radio(
        "Что делаем",
        options=[
            "🔍 Простой — смотрю рынок",
            "🧮 Pro — расчёт прибыли по формуле",
            "📋 Один дом по ссылке Zillow",
        ],
        index=0,
        help=(
            "🔍 Простой: bulk скрин ZIPов, список домов + sort по 'нужен ремонт'.\n\n"
            "🧮 Pro: bulk скрин + точный расчёт прибыли с фильтром.\n\n"
            "📋 Один дом: вставляешь URL с Zillow → анализ района (sold comps, медиана) "
            "+ характеристики этого дома, без флипперской математики."
        ),
    )
    is_pro_mode = "Pro" in mode
    is_single_url_mode = "Один дом" in mode

    st.markdown("---")

    # ====== SINGLE URL MODE — отдельный простой UI ======
    if is_single_url_mode:
        st.header("📋 Анализ одного дома")
        single_url = st.text_input(
            "Вставь ссылку на Zillow",
            placeholder="https://www.zillow.com/homedetails/...",
            help="Полная ссылка на конкретное объявление"
        )
        analyze_single_btn = st.button("🔬 Анализировать", type="primary", use_container_width=True)
        clear_single_btn = st.button("🗑 Очистить", use_container_width=True)
        # Дефолты для bulk-only переменных чтобы main код не падал
        locations_raw, price_min, price_max, bed_min, year_max = "", 0, 9999999, 0, 2026
        max_pages, profitable_only, min_profit = 1, False, 0
        fixer_only, fixer_threshold = False, 0
        sort_by = "Default"
        run_btn = False
    else:
        single_url, analyze_single_btn, clear_single_btn = "", False, False
        # === Старый bulk UI идёт ниже ===

    # ====== PRESET SELECTOR (для Culver City flipper) ======
    if not is_single_url_mode:
        st.header("🎯 Готовый маршрут (для bulk-режимов)")
    else:
        st.caption("ℹ Bulk-настройки ниже — для других режимов. В Single mode используются URL input выше.")
    if True:  # keep indentation for existing code
        pass
    preset_options = ["— Свой выбор —"] + list(PRESET_BUNDLES.keys())
    chosen_preset = st.selectbox(
        "Curated bundle ZIPов",
        options=preset_options,
        index=0,
        help="Список курированных рынков для Culver City flipper. "
             "Выбираешь bundle — автоматически заполнятся локации и оптимальные фильтры."
    )

    # Если выбран preset — заполнить session_state значениями
    if chosen_preset != "— Свой выбор —":
        zips = PRESET_BUNDLES[chosen_preset]
        # Строим текст: "City, CA ZIP" для каждого ZIP в bundle
        loc_lines = []
        for z in zips:
            info = ZIP_RECOMMENDATIONS.get(z, {})
            city = info.get("city", "")
            loc_lines.append(f"{city}, CA {z}" if city else z)
        st.session_state["_preset_locations"] = "\n".join(loc_lines)

        # Берём фильтры из ПЕРВОГО ZIP bundle как baseline
        first_info = ZIP_RECOMMENDATIONS.get(zips[0], {})
        if first_info:
            st.session_state["_preset_price_min"] = first_info["price_min"]
            st.session_state["_preset_price_max"] = first_info["price_max"]
            st.session_state["_preset_year_max"] = first_info["year_max"]
            st.session_state["_preset_bed_min"] = first_info["bed_min"]

        # Показать info про preset
        with st.expander(f"ℹ Что в этом bundle ({len(zips)} ZIP)", expanded=False):
            for z in zips:
                info = ZIP_RECOMMENDATIONS.get(z, {})
                if info:
                    st.markdown(
                        f"**{info['city']} ({z})** · {info['drive_minutes']} мин от Culver · "
                        f"${info['price_min']:,}-{info['price_max']:,} · до {info['year_max']}г  \n"
                        f"_{info['notes']}_"
                    )

    st.markdown("---")
    st.header("📍 Локации")
    default_loc = st.session_state.get("_preset_locations", "Banning, CA 92220")
    locations_raw = st.text_area(
        "Локации (одна на строку)",
        value=default_loc,
        height=120,
        help="Можно несколько городов сразу — каждый на своей строке. Или один ZIP."
    )

    # Smart filter button — если в первой строке known ZIP, предложить применить
    first_line = (locations_raw.splitlines() or [""])[0]
    smart = get_smart_filters(first_line)
    if smart:
        if st.button(f"🧠 Применить умные фильтры для {smart['city']}", use_container_width=True):
            st.session_state["_preset_price_min"] = smart["price_min"]
            st.session_state["_preset_price_max"] = smart["price_max"]
            st.session_state["_preset_year_max"] = smart["year_max"]
            st.session_state["_preset_bed_min"] = smart["bed_min"]
            st.rerun()
        st.caption(f"💡 _{smart['notes']}_")

    st.header("💰 Фильтры")
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

    max_pages = st.slider(
        "Глубина (страниц)",
        min_value=1, max_value=5, value=2,
        help="200 листингов на страницу. 2 страницы хватит для small city (Banning). "
             "Каждая страница = 1 RapidAPI запрос."
    )

    st.markdown("---")
    if is_pro_mode:
        st.markdown("💰 **Только прибыльные дома** (Pro mode фильтр)")
        profitable_only = st.toggle(
            "Показать только дома где есть деньги",
            value=True,
            help="Прячет дома где прибыль меньше threshold даже после реалистичного торга."
        )
        min_profit = st.slider(
            "Минимальная прибыль ($)",
            min_value=0, max_value=100000, value=20000, step=5000,
            disabled=not profitable_only,
        )
    else:
        profitable_only = False
        min_profit = 0
        st.caption("💡 В Простом режиме показываются все дома + статистика рынка. "
                   "Переключись на Pro mode для фильтра по прибыли.")

    st.markdown("---")
    st.markdown("🔨 **Только fixer-uppers**")
    fixer_only = st.toggle(
        "Скрыть move-in ready",
        value=False,
        help="Прячет дома которые выглядят свежеотремонтированными."
    )
    fixer_threshold = st.slider(
        "Минимальный Fixer Score",
        min_value=30, max_value=90, value=55, step=5,
        disabled=not fixer_only,
    )

    if is_pro_mode:
        sort_options = ["💰 Прибыль (default)", "🔨 Нужен ремонт", "Quick Score",
                       "Days on market (motivated)", "vs Рынок (дешевле первыми)",
                       "Цена (от дешёвых)", "$/sqft (недооценённые)", "Год (новые)"]
    else:
        # Simple mode: ремонт-кандидаты сверху, потом дешёвые относительно рынка
        sort_options = ["🔨 Нужен ремонт (default)", "vs Рынок (дешевле первыми)",
                       "Days on market (motivated)", "Цена (от дешёвых)",
                       "$/sqft (недооценённые)", "Год (новые)", "Quick Score"]
    sort_by = st.selectbox("Сортировать по", options=sort_options, index=0)

    st.markdown("---")
    run_btn = st.button("🚀 Скринить!", type="primary", use_container_width=True)
    clear_btn = st.button("🗑 Очистить результаты", use_container_width=True)

    st.markdown("---")
    st.caption("**RapidAPI cost:** ~$0.005 за ZIP-проход на Pro plan. "
               "1000 запросов/мес = ~500 ZIP-проходов или ~100 multi-location скринов.")


# ---------- SESSION STATE ----------
if "listings" not in st.session_state:
    st.session_state.listings = None
if "scored" not in st.session_state:
    st.session_state.scored = None
if "deep_cache" not in st.session_state:
    st.session_state.deep_cache = {}

if clear_btn:
    st.session_state.listings = None
    st.session_state.scored = None
    st.session_state.deep_cache = {}
    st.rerun()

if "single_result" not in st.session_state:
    st.session_state.single_result = None
if "single_deep" not in st.session_state:
    st.session_state.single_deep = None

if clear_single_btn:
    st.session_state.single_result = None
    st.session_state.single_deep = None
    st.rerun()


# ====== SINGLE URL MODE — главная логика ======
def _run_single_url_analysis(url):
    """Анализирует один Zillow URL — район + характеристики дома."""
    # Извлекаем адрес
    address = extract_address(url)
    if not address:
        st.error("Не смог извлечь адрес из URL. Проверь что это правильная Zillow ссылка.")
        return None

    # Извлекаем ZIP — ищем после "CA " чтобы не спутать с номером дома (12494 ≠ ZIP)
    zip_match = _re.search(r"\bCA\s+(\d{5})\b", address)
    if not zip_match:
        zip_match = _re.search(r"\b(9\d{4})\b", address)  # fallback: CA ZIP начинается с 9
    if not zip_match:
        st.error(f"Не нашёл ZIP в адресе '{address}'.")
        return None
    zip_code = zip_match.group(1)

    # Достаём ZPID из URL для точного match'а
    zpid_match = _re.search(r"/(\d+)_zpid", url)
    target_zpid = int(zpid_match.group(1)) if zpid_match else None

    with st.status("🔍 Анализирую...", expanded=True) as status:
        st.write(f"📍 Адрес: {address}")
        st.write(f"🔢 ZIP: {zip_code}" + (f", ZPID: {target_zpid}" if target_zpid else ""))

        # 1. Fetch active listings в ZIP (попытаемся найти наш дом)
        st.write(f"📄 Фетчу активные листинги в ZIP {zip_code}...")
        try:
            active_listings = fetch_zillow_listings(zip_code, max_pages=2)
        except Exception as e:
            st.error(f"❌ {e}")
            return None

        # 2. Ищем наш дом по ZPID или по адресу
        target_house = None
        if target_zpid:
            for l in active_listings:
                if l.get("zpid") == target_zpid:
                    target_house = l
                    break
        if not target_house:
            # Fallback — поиск по street address keyword
            addr_lower = address.lower()
            for l in active_listings:
                street = (l.get("address_street") or "").lower()
                if street and street.split(",")[0] in addr_lower:
                    target_house = l
                    break

        if target_house:
            st.write(f"✅ Дом найден в active: {target_house['address_street']}")
        else:
            st.write(f"⚠️ Дом не в active listings (может off-market/sold). Покажу только статистику района.")

        # 3. Sold comps для market stats
        st.write(f"📊 Фетчу проданные дома за последние 6 мес...")
        sold_comps = fetch_sold_comps(zip_code, max_results=50)
        st.write(f"✅ Найдено {len(sold_comps)} продаж в ZIP {zip_code}")

        status.update(label="✅ Готово!", state="complete")

    return {
        "url": url,
        "address": address,
        "zip": zip_code,
        "target_house": target_house,
        "sold_comps": sold_comps,
    }


if analyze_single_btn and single_url:
    result = _run_single_url_analysis(single_url.strip())
    if result:
        st.session_state.single_result = result
        st.session_state.scored = None  # очистить bulk результаты чтоб не мешали


# ====== SINGLE URL MODE — рендер ======
def _render_single_result(result):
    """Чистый отчёт по одному дому + статистика района."""
    address = result["address"]
    zip_code = result["zip"]
    target = result["target_house"]
    sold_comps = result["sold_comps"]

    st.markdown(f"# 🏠 {address}")
    st.markdown(f"[↗ Открыть на Zillow]({result['url']})")

    # ===== СТАТИСТИКА РАЙОНА =====
    st.markdown(f"## 📊 Что продавалось в ZIP {zip_code} (~6 мес)")
    if sold_comps:
        prices = sorted([s["sold_price"] for s in sold_comps if s.get("sold_price")])
        psqfts = sorted([s["price_per_sqft"] for s in sold_comps if s.get("price_per_sqft")])
        median_price = prices[len(prices) // 2] if prices else 0
        median_psqft = psqfts[len(psqfts) // 2] if psqfts else 0
        min_price = prices[0] if prices else 0
        max_price = prices[-1] if prices else 0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Продано всего", f"{len(sold_comps)}", help="Количество продаж за последние ~6 мес")
        c2.metric("Медиана цены", f"${median_price:,}")
        c3.metric("Медиана $/sqft", f"${median_psqft}")
        c4.metric("Диапазон", f"${min_price//1000}k–${max_price//1000}k")

        # ===== ХАРАКТЕРИСТИКИ ДОМА =====
        if target:
            st.markdown("## 🏠 Этот дом")
            cols = st.columns([1, 2])
            with cols[0]:
                if target.get("photo_url"):
                    st.image(target["photo_url"], width="stretch")
            with cols[1]:
                target_price = target.get("price") or 0
                target_psqft = target.get("price_per_sqft") or 0
                vs_market = (target_psqft - median_psqft) / median_psqft * 100 if median_psqft and target_psqft else 0
                vs_market_str = f"{'+' if vs_market >= 0 else ''}{vs_market:.1f}%"
                vs_market_color = "🟢" if vs_market < -5 else ("🟡" if vs_market < 10 else "🔴")

                m1, m2 = st.columns(2)
                m1.metric("Цена", f"${target_price:,}")
                m2.metric(f"{vs_market_color} vs медиана района", vs_market_str,
                         help="Цена $/sqft этого дома относительно медианы ZIP. "
                              "Отрицательное = дешевле района = вероятно нужен ремонт или distressed.")
                m3, m4 = st.columns(2)
                m3.metric("Площадь", f"{target.get('sqft', '—')} sqft")
                m4.metric("$/sqft", f"${target_psqft}")
                m5, m6, m7 = st.columns(3)
                m5.metric("Спален", target.get("beds", "—"))
                m6.metric("Ванных", target.get("baths", "—"))
                m7.metric("Год", target.get("year_built", "—"))

                # Highlights с Zillow (price cut, days on market)
                highlights = target.get("highlights") or []
                if highlights:
                    st.markdown("**🏷 Из объявления:**")
                    for h in highlights:
                        st.markdown(f"- {h}")
        else:
            st.info(f"⚠️ Этот дом не нашёлся среди active listings ZIP {zip_code}. "
                    "Возможно он off-market, pending, или recently sold. "
                    "Статистика района ниже покажет картину рынка.")

        # ===== КАЛЬКУЛЯТОР ФЛИПА (инлайн, без кнопки) =====
        if target:
            st.markdown("---")
            _render_flip_calculator(target, sold_comps)

        # ===== ТАБЛИЦА ВСЕХ ПРОДАЖ =====
        st.markdown(f"## 🏘 Все {len(sold_comps)} проданных домов за 6 мес")
        sold_df = pd.DataFrame([
            {
                "Адрес": s["address"],
                "Цена продажи": f"${s['sold_price']:,}",
                "$/sqft": f"${s['price_per_sqft']}",
                "Sqft": s["sqft"],
                "Beds": s["beds"],
                "Baths": s["baths"],
                "Год": s["year"],
            }
            for s in sorted(sold_comps, key=lambda x: x.get("sold_price") or 0, reverse=True)
        ])
        st.dataframe(sold_df, hide_index=True, use_container_width=True, height=400)
    else:
        st.warning(f"Не нашлось продаж в ZIP {zip_code}. "
                   "Возможно ZIP слишком новый/маленький или API не имеет данных.")


# Рендер: если в single mode и есть результат
if is_single_url_mode:
    if st.session_state.single_result:
        _render_single_result(st.session_state.single_result)
    else:
        st.info("👈 Слева вставь ссылку с Zillow и нажми **Анализировать**")
        st.markdown("""
### Что покажет:
- 📊 **Статистика района по ZIP** — сколько домов продано за 6 мес, медиана цены, $/sqft, диапазон
- 🏠 **Характеристики этого дома** (если активный листинг) — фото, цена, площадь, год, насколько дёшево/дорого относительно района
- 🏘 **Таблица всех проданных аналогов** — адреса, цены, sqft, beds/baths
- Без флипперской математики, profit calculations — просто данные для понимания района
        """)
    st.stop()  # skip всю bulk-логику ниже


# ---------- RUN ----------
if run_btn:
    st.session_state.deep_cache = {}  # сбрасываем кеш deep dive при новом запуске

    # Parse multi-location input
    locations = [l.strip() for l in locations_raw.splitlines() if l.strip()]
    if not locations:
        st.error("Введи хотя бы одну локацию.")
        st.stop()

    all_listings = []
    progress = st.progress(0, text="Подключаюсь к Zillow API...")

    try:
        for loc_idx, loc in enumerate(locations):
            def on_progress(page, total_pages, count, _loc=loc, _idx=loc_idx, _total=len(locations)):
                # Прогресс по локациям + страницам внутри
                loc_pct = _idx / _total
                page_pct = page / max(total_pages, 1) / _total
                progress.progress(
                    min(1.0, loc_pct + page_pct),
                    text=f"📍 {_loc} · page {page}/{total_pages} — собрано {count}"
                )

            loc_listings = fetch_zillow_listings(
                loc,
                price_min=price_min, price_max=price_max,
                bed_min=bed_min if bed_min > 0 else None,
                year_max=year_max,
                max_pages=max_pages,
                on_progress=on_progress,
            )
            # Tag listings with their source location (для отображения)
            for l in loc_listings:
                l["_source_location"] = loc
            all_listings.extend(loc_listings)

    except Exception as e:
        progress.empty()
        st.error(f"❌ {e}")
        st.stop()

    progress.empty()

    # Client-side фильтрация — API игнорирует max-параметры
    pre_filter_count = len(all_listings)
    all_listings, filter_stats = apply_client_filters(
        all_listings,
        price_min=price_min, price_max=price_max,
        bed_min=bed_min if bed_min > 0 else None,
        year_max=year_max,
    )
    post_filter_count = len(all_listings)

    if not all_listings:
        # Подробная диагностика — какой именно фильтр всё отрезал
        msg = (f"API вернул **{pre_filter_count}** листингов в {len(locations)} локациях, "
               f"но **0 прошли** твои фильтры:\n\n"
               f"- ❌ {filter_stats['dropped_price_min']} отвалились по `Цена от ${price_min:,}`\n"
               f"- ❌ {filter_stats['dropped_price_max']} отвалились по `Цена до ${price_max:,}`\n"
               f"- ❌ {filter_stats['dropped_bed_min']} отвалились по `Спален от {bed_min}`\n"
               f"- ❌ {filter_stats['dropped_year_max']} отвалились по `Год не позже {year_max}`\n\n"
               f"**Рекомендация:** посмотри какой счётчик самый большой — это и есть слишком жёсткий фильтр. "
               f"Для LA County рынков обычно нужен Год ≥ 2000, цена $500k+. Для Inland Empire — Год ≥ 1985 норм.")
        st.warning(msg)
        st.stop()

    # Quick scoring + Fixer scoring
    scored = []
    for l in all_listings:
        qscore, qreasons = quick_score(l)
        fscore, freasons = fixer_score(l)
        scored.append({
            "score": qscore, "reasons": qreasons,
            "fixer_score": fscore, "fixer_reasons": freasons,
            "listing": l,
            "estimated_profit": None,  # заполнится ниже если profitable_only=True
        })

    # ===== SOLD COMPS — нужны в ОБОИХ режимах (для market stats и для profit calc) =====
    unique_zips = sorted({l.get("zip") for l in all_listings if l.get("zip")})
    sold_cache = {}
    sold_progress = st.progress(0, text=f"📊 Фетчу sold comps для {len(unique_zips)} ZIPов...")
    for i, z in enumerate(unique_zips):
        try:
            sold_cache[z] = fetch_sold_comps(z, max_results=50)
        except Exception:
            sold_cache[z] = []
        sold_progress.progress((i + 1) / len(unique_zips),
                                 text=f"📊 Sold comps {i+1}/{len(unique_zips)} (ZIP {z})")
    sold_progress.empty()
    st.session_state.sold_cache = sold_cache

    # ===== MARKET STATS по каждому ZIP (медиана $/sqft, медиана цены, count) =====
    market_stats = {}
    for z, sold in sold_cache.items():
        prices = sorted([s["sold_price"] for s in sold if s.get("sold_price")])
        psqfts = sorted([s["price_per_sqft"] for s in sold if s.get("price_per_sqft")])
        if prices and psqfts:
            market_stats[z] = {
                "n_sold": len(sold),
                "median_price": prices[len(prices) // 2],
                "median_psqft": psqfts[len(psqfts) // 2],
                "min_price": prices[0],
                "max_price": prices[-1],
            }
    st.session_state.market_stats = market_stats

    # ===== vs_market% и estimate_profit для каждого дома =====
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

        # Profit считается всегда (для Pro mode фильтра + полезно знать)
        est = estimate_profit_quick(l, sold_comps_cache=sold_cache)
        s["estimated_profit"] = est["estimated_profit"]
        s["realistic_offer"] = est["realistic_offer"]
        s["real_arv"] = est["real_arv"]
        s["arv_source"] = est["arv_source"]

    # ===== PROFIT FILTER (только в Pro mode) =====
    if profitable_only:
        before = len(scored)
        scored = [s for s in scored if (s.get("estimated_profit") or 0) >= min_profit]
        st.session_state.profit_filter_dropped = before - len(scored)
        st.session_state.profit_filter_pre = before
    else:
        st.session_state.profit_filter_dropped = 0
        st.session_state.profit_filter_pre = 0

    # Fixer-only filter (после profitability)
    if fixer_only:
        before = len(scored)
        scored = [s for s in scored if s["fixer_score"] >= fixer_threshold]
        st.session_state.fixer_filter_dropped = before - len(scored)

    # Sort by selected criterion
    if "Прибыль" in sort_by:
        scored.sort(key=lambda x: (x.get("estimated_profit") or -999999), reverse=True)
    elif "Нужен ремонт" in sort_by or "Fixer Score" in sort_by:
        scored.sort(key=lambda x: x["fixer_score"], reverse=True)
    elif "vs Рынок" in sort_by:
        # Самые недооценённые первыми (most negative = most below market)
        scored.sort(key=lambda x: (x.get("vs_market_pct") if x.get("vs_market_pct") is not None else 999))
    elif sort_by.startswith("Quick Score"):
        scored.sort(key=lambda x: x["score"], reverse=True)
    elif sort_by.startswith("Days"):
        scored.sort(key=lambda x: (x["listing"].get("days_on_market") or 0), reverse=True)
    elif sort_by.startswith("Цена"):
        scored.sort(key=lambda x: (x["listing"].get("price") or 0))
    elif sort_by.startswith("$/sqft"):
        scored.sort(key=lambda x: (x["listing"].get("price_per_sqft") or 9999))
    elif sort_by.startswith("Год"):
        scored.sort(key=lambda x: (x["listing"].get("year_built") or 0), reverse=True)

    st.session_state.listings = all_listings
    st.session_state.scored = scored
    st.session_state.pre_filter_count = pre_filter_count
    st.session_state.post_filter_count = post_filter_count
    st.session_state.locations_searched = locations
    st.session_state.is_pro_mode = is_pro_mode


# ---------- RESULTS ----------
if st.session_state.scored is not None:
    scored = st.session_state.scored
    pre = st.session_state.get("pre_filter_count", len(scored))
    post = st.session_state.get("post_filter_count", len(scored))
    locs = st.session_state.get("locations_searched", [])
    profit_dropped = st.session_state.get("profit_filter_dropped", 0)
    profit_pre = st.session_state.get("profit_filter_pre", 0)

    # CASE 1: всё отфильтровалось profitable_only filter'ом
    if profit_dropped > 0 and len(scored) == 0:
        st.error(f"## 💔 Прибыльных деалов не нашлось\n\n"
                 f"Из **{profit_pre}** активных листингов в {len(locs)} локациях "
                 f"**0** прошли фильтр прибыли (минимум считалась $20k+).\n\n"
                 f"### Что попробовать:\n"
                 f"- 👈 В сайдбаре **снизь «Минимальная прибыль»** до $10,000 или $5,000\n"
                 f"- 👈 Или **выключи toggle «Показать только дома где есть деньги»** — увидишь все листинги, среди них пометки 🟡 и 🔴\n"
                 f"- 👈 Или поменяй локации — например preset **«🔥 ВСЁ что я бы скринил еженедельно»**\n\n"
                 f"⚠️ На текущем California рынке 0 прибыльных deals в одном небольшом ZIP — норма. "
                 f"Реальные deals 1-3% от inventory. Чтобы найти 5 deals нужно сканить 200-500 листингов.")
        st.stop()

    # CASE 2: после bulk fetch получили 0 (это уже обработано выше через st.warning + st.stop)
    if len(scored) == 0:
        st.warning("Список пустой. Нажми Скринить заново или измени фильтры в сайдбаре.")
        st.stop()

    # CASE 3: всё ок — показываем результаты
    is_pro_render = st.session_state.get("is_pro_mode", False)
    if profit_dropped:
        st.success(f"💰 **{len(scored)} прибыльных deals** найдено из {profit_pre} активных листингов "
                   f"в {len(locs)} локациях. {profit_dropped} убыточных скрыто.")
    else:
        st.success(f"✅ Скрин по **{len(locs)}** локациям. Активных листингов: **{post}**.")

    # ===== MARKET STATS блок — статистика продаж за 6 мес по ZIPам =====
    market_stats = st.session_state.get("market_stats", {})
    if market_stats:
        st.markdown("### 📊 Что продавалось в этих районах (~6 мес)")
        cols = st.columns(min(len(market_stats), 4))
        for i, (z, stats) in enumerate(market_stats.items()):
            if i >= 4: break
            with cols[i]:
                st.metric(
                    label=f"ZIP {z}",
                    value=f"${stats['median_price']:,}",
                    help=f"Медианная цена продажи за последние 6 мес. "
                         f"Продано: {stats['n_sold']}. "
                         f"Медиана $/sqft: ${stats['median_psqft']}. "
                         f"Диапазон: ${stats['min_price']:,}–${stats['max_price']:,}"
                )
                st.caption(f"📈 ${stats['median_psqft']}/sqft · n={stats['n_sold']}")

    # Сводная таблица top-N
    rows = []
    for i, s in enumerate(scored[:30]):
        l = s["listing"]
        county = detect_county(l.get("city"), l.get("zip"))
        county_badge = f"🏛 {county}" if has_full_county_support(county) else county

        # Колонки разные для Pro и Simple mode
        row = {"#": i + 1}
        if is_pro_render:
            row["💰 Прибыль"] = s.get("estimated_profit")
            row["Цель торга"] = s.get("realistic_offer")
        row["🔨 Нужен ремонт"] = s["fixer_score"]
        row["vs Рынок %"] = s.get("vs_market_pct")
        row["🏫 Школа"] = "—"  # placeholder v2 — нужен GreatSchools API
        row["Город"] = l.get("city") or l.get("_source_location") or ""
        row["County"] = county_badge
        row["Адрес"] = l["address_street"]
        rows.append(row)
        # Добавляю остальные стандартные поля в той же итерации:
        rows[-1].update({
            "Цена": l["price"],
            "$/sqft": l["price_per_sqft"],
            "Beds": l["beds"],
            "Baths": l["baths"],
            "sqft": l["sqft"],
            "Год": l["year_built"],
            "Days": l["days_on_market"],
            "Zestimate": l["zestimate"],
            "Δ Zest": int(((l["zestimate"] or 0) - (l["price"] or 0))) if l["zestimate"] and l["price"] else None,
            "Zillow": l["zillow_url"],
        })

    df = pd.DataFrame(rows)
    st.dataframe(
        df,
        column_config={
            "💰 Прибыль": st.column_config.NumberColumn(
                "💰 Прибыль", format="$%d",
                help="Расчётная чистая прибыль после реалистичного торга, ремонта, "
                     "всех расходов. На основе sold comps. Зелёное = в плюсе."),
            "Цель торга": st.column_config.NumberColumn(
                "Цель торга", format="$%d",
                help="Реалистичная цена торга — listing минус 3-25% в зависимости "
                     "от days on market и price cuts."),
            "🔨 Нужен ремонт": st.column_config.ProgressColumn(
                "🔨 Нужен ремонт", min_value=0, max_value=100, format="%d",
                help="0 = move-in ready (свежеотремонтированный), 100 = явный fixer. "
                     "Сигналы: price cut, days on market, дисконт к Zestimate, "
                     "$/sqft низкое, старый год."),
            "vs Рынок %": st.column_config.NumberColumn(
                "vs Рынок %", format="%+.1f%%",
                help="Цена $/sqft этого дома относительно медианы района за 6 мес. "
                     "Отрицательное = дешевле рынка (потенциальная недооценка), "
                     "положительное = дороже рынка (вряд ли торг)."),
            "🏫 Школа": st.column_config.TextColumn(
                "🏫 Школа",
                help="Рейтинг школ в районе. В разработке (нужна интеграция с GreatSchools API)."),
            "Цена": st.column_config.NumberColumn("Цена", format="$%d"),
            "Zestimate": st.column_config.NumberColumn("Zestimate", format="$%d"),
            "Δ Zest": st.column_config.NumberColumn(
                "Δ Zest", format="$%d",
                help="Сколько Zestimate выше цены. Положительное = недооценка."),
            "$/sqft": st.column_config.NumberColumn("$/sqft", format="$%d"),
            "Zillow": st.column_config.LinkColumn("Zillow", display_text="↗ Open"),
        },
        hide_index=True,
        use_container_width=True,
        height=600,
    )

    # ---------- DEEP DIVE ----------
    st.markdown("---")
    st.subheader("🔍 Deep dive — Flip Score с county data")
    st.caption("Раскрой карточку и нажми «Полный анализ» — подтянем Riverside Assessor "
               "(характеристики, налоговая история, comps, FEMA) и посчитаем Flip Score "
               "с sensitivity к ARV.")

    top_n = st.slider("Сколько верхних показать для deep dive", 5, 30, 10)

    for i, s in enumerate(scored[:top_n]):
        l = s["listing"]
        cache_key = str(l.get("zpid") or l.get("address_full"))
        has_deep = cache_key in st.session_state.deep_cache

        days_str = f"{l['days_on_market']}d" if l.get("days_on_market") else "—"
        title = (f"#{i+1} · **{l['address_street']}** · "
                 f"Quick **{s['score']}**/100 · ${l['price']:,} · "
                 f"{l['beds']}/{l['baths']} · {l['sqft']}sqft · {l['year_built']}г · "
                 f"{days_str} on market"
                 f"{'  ✅ analyzed' if has_deep else ''}")

        # AUTO-EXPAND если уже есть deep результат
        with st.expander(title, expanded=has_deep):
            colA, colB = st.columns([1, 2])
            with colA:
                if l.get("photo_url"):
                    st.image(l["photo_url"], width="stretch")
                if l.get("zillow_url"):
                    st.markdown(f"[↗ Открыть на Zillow]({l['zillow_url']})")
            with colB:
                # Highlights с Zillow (price cut, days on market, pool, etc)
                highlights = l.get("highlights") or []
                if highlights:
                    st.markdown("**🏷 Zillow highlights:** " + " · ".join(f"`{h}`" for h in highlights))

                tab1, tab2 = st.tabs(["Quick Score", f"🔨 Fixer Score {s['fixer_score']}/100"])
                with tab1:
                    for r in s["reasons"]:
                        st.markdown(f"- {r}")
                with tab2:
                    if s["fixer_reasons"]:
                        for r in s["fixer_reasons"]:
                            st.markdown(f"- {r}")
                    else:
                        st.caption("Нейтральный — нет ярких сигналов ни в сторону fixer, ни move-in.")

                btn_label = "🔄 Перезапустить анализ" if has_deep else "🔬 Полный анализ с county data"
                key_btn = f"deep_btn_{i}"
                if st.button(btn_label, key=key_btn, type="primary" if not has_deep else "secondary"):
                    try:
                        with st.spinner("Подключаюсь к Riverside Assessor + FEMA + RapidAPI..."):
                            st.session_state.deep_cache[cache_key] = deep_analysis(l)
                        st.rerun()  # Принудительный rerun чтобы expander раскрылся с результатом
                    except Exception as e:
                        st.error(f"❌ Анализ упал: {e}")
                        import traceback
                        st.code(traceback.format_exc(), language="python")

            # Если deep подгружен — рендерим
            if has_deep:
                st.markdown("---")
                _render_deep(st.session_state.deep_cache[cache_key])

else:
    # Empty state
    st.info("👈 Слева выбери локацию(и) и фильтры, потом жми **Скринить!**")
    st.markdown("""
### Как работает CaliFlip Screener

**Шаг 1 — Quick Score (моментально, по Zillow данным — РАБОТАЕТ ДЛЯ ЛЮБОГО ZIP США):**
- Цена vs Zestimate (дисконт = score+)
- $/sqft (низкое = недооценка)
- Days on market (60+ дней = motivated seller, торгуйся)
- Год постройки (1950-1985 = sweet spot для флипа)
- Спален (3+ = легче продать)

**Шаг 2 — Deep dive:**

🏛 **Riverside County** (Banning, Riverside, Hemet, Corona, Murrieta, Temecula, Palm Springs, ...) — **full county data:**
- Riverside Assessor (APN, налоговая история, characteristics)
- Comps по 50 соседям в радиусе 400м
- FEMA flood zone
- Flip Score sensitivity к 3 ARV (pessimistic / realistic / optimistic)

📊 **LA, San Bernardino, Orange, San Diego counties** — Zillow-only анализ:
- Цены, Zestimate, beds/baths/sqft/year, days on market
- FEMA flood zone (по координатам)
- Flip Score sensitivity (ARV из Zestimate)
- Tech Score из Zillow характеристик
- **Without** comps по соседям (county API не интегрирован ещё)

**На будущее:** San Bernardino County API уже на радаре (Андрей дал URL).
LA / Orange / SD County — добавим если будут активно использоваться.

**Не тратишь время на:**
- Ручной ввод адреса (всё из Zillow API)
- Открытие 200 вкладок (всё в одной таблице)
- Анализ заведомо слабых сделок (Quick Score < 40 — пропускаем)
""")
