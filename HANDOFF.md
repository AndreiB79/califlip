# CaliFlip — Project Handoff

> **Цель этого файла:** при старте новой сессии с Claude — отдать ему этот файл, и он сразу будет в курсе проекта без лишних объяснений.
>
> **Последнее обновление:** 2026-05-29

---

## 🎯 Что это

Инструмент для **массового скрининга real-estate fix-and-flip кандидатов** в California. Для **Андрея** — флиппер из Culver City, бюджет до $500k, радиус 2 часа.

**Не оракул, а screener.** Сужает 200-300 листингов в ZIPе до 5-10 кандидатов за минуту. Дальше — ехать смотреть глазами.

---

## 🚀 Quick start для новой сессии

```bash
cd ~/Desktop/califlip
git pull
cat HANDOFF.md       # этот файл
cat CLAUDE.md        # правила работы с Андреем
git log --oneline -15  # последние коммиты — что менялось
```

Первое сообщение в новой сессии: «*Открой HANDOFF.md и продолжим с того где остановились. Кратко: что сделано, что в работе.*»

---

## 🌐 Где живёт код / приложение

| Что | URL / Путь |
|---|---|
| GitHub repo | https://github.com/AndreiB79/califlip |
| Локальный код | `~/Desktop/califlip/` |
| Streamlit Cloud (production) | https://califlip.streamlit.app |
| Streamlit Cloud admin | https://share.streamlit.io/ |
| Локальный URL (LaunchAgent) | `http://localhost:8501` |

**Cloud auto-deploy** триггерится **на каждый push в `main`**. Если cache залип — touch `requirements.txt` + push.

---

## 🧱 Стек

- **Python 3.13** (Homebrew, `/opt/homebrew/bin/python3.13`)
  - ⚠️ Apple Xcode Python `/usr/bin/python3` **НЕ работает** под LaunchAgent — TCC sandbox блокирует `os.getcwd()`. Используй только brew Python.
- **Streamlit 1.58+** — UI
- **pandas** — таблицы
- **requests** — HTTP
- **RapidAPI Zillow Property Data API** ("rabbitapi" provider, **Pro plan $4.99/мес**, 1000 req/мес). Ключ в `.env` (gitignored).
- **Riverside County Assessor REST API** — county data (APN, tax history, characteristics, comps)
- **US Census Geocoder** — fallback для адресов которых нет в OpenStreetMap
- **FEMA NFHL** — flood zones

---

## 📁 Структура файлов

| Файл | Что внутри |
|---|---|
| `califlip.py` | Legacy v8.0 single-URL CLI. Используется как **module** — `extract_address`, `parse_us_address`, `find_parcel_by_address`, `get_property_char`, `get_taxyear_history`, county lookup, tech score, ARV constants. |
| `califlip_api.py` | **Главный backend.** Bulk Zillow fetch, Quick Score, Fixer Score, sold comps, realistic offer, profit estimate, `flip_calculator_2026`, `RENOVATION_PRESETS`, ZIP recommendations, preset bundles. |
| `califlip_app.py` | Streamlit UI. 3 режима: Простой / Pro / Single URL. |
| `diagnose.py` | Диагностика API (запускается отдельно для troubleshooting). |
| `requirements.txt` | `requests`, `streamlit`, `pandas` |
| `run_screener.command` | Двойной клик в Finder → запуск streamlit вручную (легаси, теперь есть LaunchAgent). |
| `.env` | `RAPIDAPI_KEY`, `RAPIDAPI_HOST` (gitignored, в репо НЕТ). |
| `~/Library/LaunchAgents/com.andrei.califlip.plist` | LaunchAgent для auto-start localhost:8501 при логине. |

---

## ✅ Что работает (фичи)

### 3 режима в Streamlit UI

1. **🔍 Простой** — bulk скрин ZIPов, sort by «Нужен ремонт» по умолчанию. Для browse рынка.
2. **🧮 Pro** — bulk + расчёт прибыли + фильтр «только прибыльные» (default ON, min $20k).
3. **📋 Single URL** — вставляешь Zillow ссылку → анализ района + характеристики дома + sold comps таблица. Без флипперской математики.

### Под капотом

- **Bulk Zillow fetch** по ZIP (RapidAPI, до 2-5 страниц)
- **Multi-location** — можно сразу 3-5 ZIPов через многострочный input
- **Quick Score 0-100** — Zestimate gap, $/sqft, days on market, year, beds
- **Fixer Score 0-100** — price cut, days, дисконт к Zestimate, $/sqft, год + premium highlights penalty
- **Sold comps fetch** (~6 мес) — реальные продажи, фильтр по похожему sqft ±25%
- **Real ARV** = медиана $/sqft sold × наш sqft (а не Zestimate-гадание)
- **Realistic offer** — −3% до −25% от listing в зависимости от days/price cuts (НЕ учебниковые 70% rule)
- **Flip Score sensitivity** — 3 ARV scenarios (×1.2 / ×1.5 / ×1.8)
- **Net profit + ROI** — после ремонта, hard money, NAR commission, carrying costs
- **17 курированных ZIPов** для Culver City flipper (Riverside/SB/LA/Antelope Valley/High Desert)
- **6 preset bundles** (Топ-3, Best Riverside, SB Valley, etc) с автоподстановкой фильтров
- **County detection** (Riverside / LA / OC / SB / SD) — для Riverside full county data, для остальных Zillow-only
- **Smart filters auto-apply** при вводе known ZIP
- **LaunchAgent** для 24/7 локального доступа

---

## 🔥 Что добавил Андрей сам (между моими сессиями)

**Не переделывать без обсуждения!**

### `RENOVATION_PRESETS` в `califlip_api.py:1187`
Реальные цены Inland Empire 2026:
- 💄 **cosmetic** — $30/sqft, 2 мес (краска, ламинат, fixtures)
- 🔧 **medium** — $55/sqft, 4 мес (кухня + ванная update)
- 🏗 **heavy** — $85/sqft, 6 мес (HVAC, сантехника, электрика + отделка)
- 🔨 **gut** — $130/sqft, 9 мес (всё под ноль)

### `flip_calculator_2026` в `califlip_api.py:1215`
Комплексный калькулятор учитывающий:
- **Hard money loan** (11% rate, 2 points, 80% LTV — default)
- **Contingency** 15% к ремонту (по умолчанию)
- **NAR post-settlement commissions** (6.5% selling cost)
- **Custom repair total** (можно вводить руками вместо preset)
- **Hold months** (default 6)

Используется в Single URL mode через **кнопку «Калькулятор»**.

### Другие изменения Андрея
- Hardcoded `RAPIDAPI_HOST` (избегает Streamlit secrets formatting issues)
- Manual data entry если дом не в active listings (введёшь характеристики сам)
- Fix ZIP extraction (match после CA state, не house number)

---

## ⚠️ Архитектурные решения — НЕ ПЕРЕДЕЛЫВАТЬ

1. **API провайдер — rabbitapi на RapidAPI** ($4.99/мес Pro). Tested 10+ альтернатив, этот лучший. Не менять.
2. **Zillow direct scraping не работает** — PerimeterX блокирует. Пробовали: `requests`, `curl_cffi` с Chrome TLS impersonation, Playwright headless. Все 403/CAPTCHA. Не трогать.
3. **Riverside Assessor `STREET_NUMBER` не индексирован** — `WHERE STREET_NUMBER='X'` возвращает "Unable to complete operation". Поиск ТОЛЬКО через `STREET_NAME LIKE` + Python filter.
4. **Address-search FIRST, geo-fallback SECOND** в `find_parcel` — иначе скрипт молча подменяет соседями (вот так мы потеряли час диагностируя «не тот дом»).
5. **ARV multipliers 1.2/1.5/1.8** — калибровано для California 2026. Не учебниковые 1.0/1.25/1.4 (те для slow markets post-2008).
6. **75% rule для Inland Empire, 80% для LA** — calibrated для current market. Учебниковые 70% дают −50% дисконт = нереально.
7. **Apple Python `/usr/bin/python3` НЕ работает с LaunchAgent** — TCC sandbox. Use `/opt/homebrew/bin/python3.13`.
8. **Многострочный HTML в `st.markdown(unsafe_allow_html=True)` глючит** — Streamlit рендерит сырые теги. Использовать native components (`st.metric`, `st.table`, `st.dataframe`, `st.success/warning/error`).
9. **Sold comps кэшируются по ZIP** в `st.session_state.sold_cache` — НЕ фетчить дважды для одного ZIP.
10. **API quirk для location:** Banning работает с `City, CA ZIP`, Van Nuys (LA neighborhood) работает ТОЛЬКО с `ZIP` (только 5 цифр). Парсер должен fallback'нуть.

---

## 📋 TODO / Roadmap

### Высокий приоритет
- [ ] **Школьные рейтинги** — интеграция с **GreatSchools API** (free 200/day, требует регистрации Андреем). Колонка 🏫 в таблице.
- [ ] **San Bernardino County API** — добавить full county data для SB ZIPов (Андрей давал URL: `https://arcportal.sbcounty.gov/arcgis/rest/services/PAT_Public/SBC_PAT_Public_Report/MapServer`)
- [ ] **Redfin** — второй источник данных, +20-30% дополнительного inventory которого нет на Zillow
- [ ] **Auto-refresh stale Streamlit Cloud cache** — sometimes redeploy не подхватывается, нужен manual touch requirements.txt + push

### Средний приоритет
- [ ] **LA County / Orange County / San Diego County** API интеграции (миллионы parcels, тяжело)
- [ ] **Off-market deals** — direct mail integration / PropStream подключение когда у Андрея будет $99/mo бюджет
- [ ] **Email уведомления** при появлении новых high-Fixer-Score deals в watched ZIPах
- [ ] **Сохранение favorites** между сессиями (через Streamlit session_state на disk)

### Низкий приоритет / отложено
- ~~Полный 75% rule wholesale calculator~~ → заменён на `flip_calculator_2026` с realistic offer
- ~~Single deep_analysis для each house~~ → заменён на batch с кэшем sold comps
- Auction.com / HUD / HomePath — нет API, manual review

---

## 🐛 Известные баги / лимитации

- **Free RapidAPI tier = 20 calls/мес** (не 50 как иногда указано). Pro $4.99 = 1000.
- **Mobile home parks** (SPACE NN в адресе) не имеют отдельного APN — обходим через `homeType=houses` filter.
- **Streamlit Cloud cache** иногда держит старую версию даже после push. Fix: touch `requirements.txt` + push.
- **LA neighborhoods quirk** — нужен только ZIP, не City+ZIP (см. п. 10 архитектурных решений).
- **Off-market дома** — не появляются через `listingStatus=For_Sale`. Можно вытащить через `listingStatus=Sold` (но 2545 records, нужна пагинация).
- **Days on market > 730** — глюк API, capping до None.

---

## 🛠 Команды управления

### LaunchAgent (auto-start локально)

```bash
# Статус
launchctl print gui/$(id -u)/com.andrei.califlip | head -10

# Перезапустить (после изменений кода)
launchctl kickstart -k gui/$(id -u)/com.andrei.califlip

# Остановить
launchctl bootout gui/$(id -u)/com.andrei.califlip

# Запустить заново после bootout
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.andrei.califlip.plist

# Логи в реальном времени
tail -f ~/Desktop/califlip/streamlit.log
```

### Deploy на Streamlit Cloud

```bash
cd ~/Desktop/califlip
git add <files>
git commit -m "..."
git push origin main
# Auto-deploy за 2-5 минут
```

Если cache залип:
```bash
echo "# rebuild: $(date)" >> requirements.txt
git commit -am "Force cache invalidation"
git push
```

### Local dev (если LaunchAgent остановлен)

```bash
cd ~/Desktop/califlip
/opt/homebrew/bin/python3.13 -m streamlit run califlip_app.py
```

---

## 🔑 Полезные deep-links

- **RapidAPI Zillow provider:** https://rapidapi.com/rabbitapi-rabbitapi-default/api/zillow-property-data-api1
- **RapidAPI pricing:** https://rapidapi.com/rabbitapi-rabbitapi-default/api/zillow-property-data-api1/pricing
- **RapidAPI keys management:** https://rapidapi.com/developer/security
- **GitHub repo:** https://github.com/AndreiB79/califlip
- **GitHub commits:** https://github.com/AndreiB79/califlip/commits/main
- **Streamlit Cloud:** https://share.streamlit.io/
- **GreatSchools API signup (TODO):** https://www.greatschools.org/api/

---

## 🧑‍💼 Контекст про Андрея (важно для нового Claude'а)

- **Не программист, не маркетолог.** Plain Russian, no jargon. Без «commit/deploy/refactor/API/endpoint» без перевода.
- **Sole user** — это **его** инструмент для **его** workflow (Culver City flipper, 1-2 часа в день).
- **Работает с разными AI** (Claude, Gemini, ChatGPT в табах) — между сессиями может добавлять код сам. **Спросить** что добавил перед тем как трогать.
- **Декларации > вопросы.** Не «А/Б/В на выбор», а одна рекомендация с обоснованием.
- **Прибыль/убыток в долларах**, не Score/Margin/ROI. Андрей не должен учить флипперский жаргон.
- **Если что-то можно сделать самому через CLI** — делаю сам, не прошу кликать (§10d AGENTS.md).

Подробнее в `CLAUDE.md` + `WORKING-WITH-ANDREI.md`.

---

## 📊 Session state в Streamlit (для понимания state flow)

| Ключ | Что хранит |
|---|---|
| `listings` | raw bulk fetched listings |
| `scored` | scored + filtered listings (для таблицы) |
| `sold_cache` | dict `zip → sold comps list` (кэш) |
| `market_stats` | dict `zip → {median_price, median_psqft, n_sold}` |
| `deep_cache` | dict `cache_key → deep_analysis result` |
| `single_result` | результат для Single URL mode |
| `is_pro_mode` | какой mode был активен на последнем Скрин |
| `profit_filter_dropped` | сколько отфильтровалось profit-фильтром (для UX-сообщения) |

При смене mode session state НЕ очищается автоматически — пользователь может переключаться между bulk и single без потерь.

---

## 🧪 Smoke test (когда сомневаешься что всё работает)

```bash
cd ~/Desktop/califlip
/opt/homebrew/bin/python3.13 -c "
import ast
for f in ['califlip.py', 'califlip_api.py', 'califlip_app.py']:
    ast.parse(open(f).read())
    print(f'✅ {f}')

from califlip_api import (
    fetch_zillow_listings, quick_score, deep_analysis,
    fixer_score, RENOVATION_PRESETS, flip_calculator_2026,
    fetch_sold_comps, estimate_profit_quick, realistic_offer_price,
    ZIP_RECOMMENDATIONS, PRESET_BUNDLES,
)
print(f'✅ Все imports OK')
print(f'   ZIP_RECOMMENDATIONS: {len(ZIP_RECOMMENDATIONS)} ZIPов')
print(f'   PRESET_BUNDLES: {len(PRESET_BUNDLES)} bundles')
print(f'   RENOVATION_PRESETS: {list(RENOVATION_PRESETS.keys())}')
"
```

Должно вывести `✅ Все imports OK` и счётчики.

---

**End of HANDOFF. Дальше — git log + текущий код.**
