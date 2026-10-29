import html
import os
import re
import time
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
CHAT_ID = os.environ['TELEGRAM_CHAT_ID']

VOX_BASE_URL = 'https://egy.voxcinemas.com/showtimes?c=city-centre-almaza'
TAZKARTI_URL = 'https://www.tazkarti.com/#/matches'
TAZKARTI_MATCH_SELECTOR = 'div.content.matches div.match'
SENT_FILE = 'sent.txt'
MONITOR_ERRORS = []

EGYPT_TZ = ZoneInfo("Africa/Cairo")

HALL_NAME_DISPLAY = {
    "gold": "GOLD",
    "imax": "IMAX",
    "max": "MAX",
    "4dx": "4DX",
    "standard": "Standard",
    "kids": "Kids",
    "vip": "VIP",
}


def normalize_hall_name(raw_name):
    key = raw_name.strip().lower()
    return HALL_NAME_DISPLAY.get(key, raw_name.strip())


def now_egypt():
    return datetime.now(EGYPT_TZ)


def today_egypt():
    return now_egypt().date()


def date_label(d):
    diff = (d - today_egypt()).days
    if diff == 0:
        return "Today"
    if diff == 1:
        return "Tomorrow"
    return d.strftime('%a %d %b')


def vox_url_for_date(d):
    if d == today_egypt():
        return VOX_BASE_URL
    return f"{VOX_BASE_URL}&d={d.strftime('%Y%m%d')}"


def parse_showtime_dt(target_date, time_text):
    try:
        t = datetime.strptime(re.search(r"\d{1,2}:\d{2}\s*[ap]m", time_text.lower())[0].replace(" ", ""), "%I:%M%p").time()
    except (ValueError, TypeError, AttributeError):
        return None
    return datetime.combine(target_date, t, tzinfo=EGYPT_TZ)


def load_sent_items():
    if not os.path.exists(SENT_FILE):
        return set()
    with open(SENT_FILE, 'r', encoding='utf-8') as f:
        return set(line.strip() for line in f if line.strip())


def save_sent_item(item):
    with open(SENT_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{item}\n")


def send_telegram_message(message, button_text=None, button_url=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'HTML',
               'disable_web_page_preview': True}
    if button_text and button_url:
        payload['reply_markup'] = {'inline_keyboard': [[
            {'text': button_text, 'url': button_url}]]}
    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, timeout=20)
            data = response.json()
            if response.ok and data.get('ok') is True:
                print('Telegram delivery confirmed.')
                return True
            if response.status_code == 429:
                delay = min(60, max(1, int(data.get('parameters', {}).get('retry_after', 3))))
            elif response.status_code >= 500:
                delay = 2 ** attempt
            else:
                print(f'Telegram rejected delivery (HTTP {response.status_code}).')
                break
        except (requests.RequestException, ValueError, TypeError):
            # Do not print request URLs: they contain the bot token.
            print('Telegram request failed; no notification history was recorded.')
            delay = 2 ** attempt
        if attempt < 2:
            time.sleep(delay)
    MONITOR_ERRORS.append('Telegram delivery failed')
    return False


def new_browser():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=chrome_options)
    driver.set_page_load_timeout(30)
    return driver


def save_debug_snapshot(driver, name):
    try:
        driver.save_screenshot(f"debug_{name}.png")
        with open(f"debug_{name}.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        print(f"Saved debug snapshot for {name}")
    except Exception as e:
        print(f"Could not save debug snapshot for {name}: {e}")


def load_vox_page(driver, url, timeout=25, retries=2, pause_between=3):
    last_error = None
    for attempt in range(1, retries + 2):
        try:
            driver.get(url)
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "article.movie-compare"))
            )
            return True
        except Exception as e:
            last_error = e
            print(f"     Attempt {attempt} failed for {url}: {e.__class__.__name__}")
            time.sleep(pause_between)
    print(f"     Giving up on {url}. Last error: {last_error}")
    return False


def discover_open_dates(driver):
    dates = [today_egypt()]
    nav_links = driver.find_elements(By.CSS_SELECTOR, "nav.date-filter li a[href]")
    for link in nav_links:
        href = link.get_attribute("href") or ""
        match = re.search(r'[?&]d=(\d{8})', href)
        if match:
            d = datetime.strptime(match.group(1), '%Y%m%d').date()
            dates.append(d)
    return sorted(set(dates))


def extract_movies(driver):
    movies = []
    articles = driver.find_elements(By.CSS_SELECTOR, "article.movie-compare")

    for article in articles:
        try:
            title_el = article.find_elements(By.TAG_NAME, "h2")
            title = title_el[0].text.strip() if title_el else "Unknown Movie"

            rating_el = article.find_elements(By.CSS_SELECTOR, "span.classification")
            rating = rating_el[0].text.strip() if rating_el else ""

            tag_els = article.find_elements(By.CSS_SELECTOR, "span.tag")
            tags = [t.text.strip() for t in tag_els if t.text.strip()]
            language = tags[0] if len(tags) > 0 else ""
            duration = tags[1] if len(tags) > 1 else ""

            info_link_el = article.find_elements(By.CSS_SELECTOR, "a.read-more")
            info_link = info_link_el[0].get_attribute("href") if info_link_el else ""

            halls = []
            hall_groups = article.find_elements(By.CSS_SELECTOR, "div.dates ol.showtimes > li")
            for hall in hall_groups:
                strong_el = hall.find_elements(By.TAG_NAME, "strong")
                raw_hall_name = strong_el[0].text.strip() if strong_el else "Standard"
                hall_name = normalize_hall_name(raw_hall_name)

                showtimes = []
                time_items = hall.find_elements(By.CSS_SELECTOR, "ol > li")
                for item in time_items:
                    booking_id = item.get_attribute("data-id") or ""
                    link_el = item.find_elements(By.CSS_SELECTOR, "a.showtime")
                    if not link_el:
                        continue
                    time_text = link_el[0].text.strip()
                    link = link_el[0].get_attribute("href")
                    if booking_id and time_text and link:
                        showtimes.append({
                            "booking_id": booking_id,
                            "time": time_text,
                            "link": link,
                        })

                if showtimes:
                    halls.append({"name": hall_name, "showtimes": showtimes})

            if halls:
                movies.append({
                    "title": title,
                    "rating": rating,
                    "language": language,
                    "duration": duration,
                    "info_link": info_link,
                    "halls": halls,
                })
        except Exception as e:
            print(f"Error parsing a movie article: {e}")
            continue

    return movies


def format_vox_message(movie, day_label, new_showtimes_by_hall):
    escape = html.escape
    meta = ' | '.join(escape(p) for p in
                      [movie['rating'], movie['language'], movie['duration']] if p)
    lines = [f"🎬 <b>{escape(movie['title'][:300])}</b>",
             '📍 VOX City Centre Almaza']
    if meta:
        lines.append(f'🏷 {meta}')
    lines.extend([f'📅 <b>{escape(day_label)}</b>', ''])
    for hall_name, showtimes in new_showtimes_by_hall.items():
        links = ' | '.join(
            f"<a href='{escape(s['link'], quote=True)}'>{escape(s['time'])}</a>"
            for s in showtimes)
        lines.append(f'🍿 <b>{escape(hall_name)}:</b> {links}')
    if movie['info_link']:
        lines.extend(['', f"ℹ️ <a href='{escape(movie['info_link'], quote=True)}'>Movie Info</a>"])
    return '\n'.join(lines)


def process_movies_for_date(movies, target_date, sent_items):
    day_label = f'{date_label(target_date)} ({target_date.isoformat()})'
    current_time = now_egypt()
    total_new = 0
    for movie in movies:
        pending = []
        seen = set(sent_items)
        for hall in movie['halls']:
            for show in hall['showtimes']:
                if target_date < current_time.date():
                    continue
                if target_date == current_time.date():
                    start = parse_showtime_dt(target_date, show['time'])
                    if start and start <= current_time:
                        continue
                key = f"vox:{target_date.isoformat()}:{show['booking_id']}"
                if key not in seen:
                    seen.add(key)
                    pending.append((hall['name'], show, key))
        # Bound each Telegram message and persist each accepted batch separately.
        for offset in range(0, len(pending), 12):
            batch = pending[offset:offset + 12]
            by_hall = {}
            for hall, show, key in batch:
                by_hall.setdefault(hall, []).append(show)
            message = format_vox_message(movie, day_label, by_hall)
            if len(message) > 4000:
                batches = [[item] for item in batch]
            else:
                batches = [batch]
            for items in batches:
                halls = {}
                for hall, show, key in items:
                    halls.setdefault(hall, []).append(show)
                if send_telegram_message(format_vox_message(movie, day_label, halls)):
                    for hall, show, key in items:
                        save_sent_item(key)
                        sent_items.add(key)
                        total_new += 1
    return total_new


def check_vox_cinemas(sent_items):
    print("Checking Vox Cinemas Almaza...")
    total_new = 0

    driver = new_browser()
    try:
        ok = load_vox_page(driver, VOX_BASE_URL)
        if not ok:
            save_debug_snapshot(driver, "vox_today")
            MONITOR_ERRORS.append("VOX initial page did not load")
            return
        dates_to_check = discover_open_dates(driver)
        print(f"  Dates currently open for booking: {[d.isoformat() for d in dates_to_check]}")

        today = today_egypt()
        movies_today = extract_movies(driver)
        total_new += process_movies_for_date(movies_today, today, sent_items)
    finally:
        driver.quit()

    for target_date in dates_to_check:
        if target_date == today:
            continue

        url = vox_url_for_date(target_date)
        print(f"  -> Checking {date_label(target_date)} ({url})")

        driver = new_browser()
        try:
            ok = load_vox_page(driver, url)
            if not ok:
                save_debug_snapshot(driver, f"vox_{target_date.isoformat()}")
                MONITOR_ERRORS.append(f"VOX page failed: {target_date}")
                continue
            movies = extract_movies(driver)
            total_new += process_movies_for_date(movies, target_date, sent_items)
        finally:
            driver.quit()

        time.sleep(2)

    print(f"Found and processed {total_new} new showtimes in Vox.")


def clean_tazkarti_line(value):
    return re.sub(r"\s+", " ", value or "").strip(" -")


def normalize_match_identity(value):
    value = (value or "").lower().translate(str.maketrans({
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ى": "ي",
    }))
    value = re.sub(r"[\u064b-\u065f\u0670]", "", value)
    return re.sub(r"[^a-z0-9\u0600-\u06ff]+", "", value)


def is_al_ahly_match(title):
    compact_title = normalize_match_identity(title)
    return "alahly" in compact_title or "الاهلي" in compact_title


def tazkarti_booking_is_open(text):
    normalized = clean_tazkarti_line(text).lower()
    closed_phrases = (
        "booking closed",
        "match ended",
        "sold out",
        "unavailable",
        "not available",
        "الحجز مغلق",
        "انتهت المباراة",
        "نفدت التذاكر",
    )
    if any(phrase in normalized for phrase in closed_phrases):
        return False

    open_phrases = (
        "book ticket",
        "احجز تذكرة",
        "احجز الآن",
        "احجز الان",
    )
    return any(phrase in normalized for phrase in open_phrases)


def tazkarti_field(lines, pattern):
    for line in lines:
        match = re.match(pattern, line, flags=re.IGNORECASE)
        if match:
            return clean_tazkarti_line(match.group(1))
    return ""


def tazkarti_element_text(container, selector):
    elements = container.find_elements(By.CSS_SELECTOR, selector)
    if not elements:
        return ""
    return clean_tazkarti_line(elements[0].text)


def tazkarti_card_field(card, label):
    wanted_label = normalize_match_identity(label)
    for block in card.find_elements(By.CSS_SELECTOR, ".bottom .one"):
        field_label = tazkarti_element_text(block, ".first")
        if normalize_match_identity(field_label).startswith(wanted_label):
            return tazkarti_element_text(block, ".second")
    return ""


def tazkarti_booking_url(card):
    site_root = "https://www.tazkarti.com/"
    controls = card.find_elements(By.CSS_SELECTOR, "a, button")

    for control in controls:
        label = clean_tazkarti_line(control.text).lower()
        attributes = [
            control.get_attribute("href"),
            control.get_attribute("routerlink"),
            control.get_attribute("ng-reflect-router-link"),
        ]
        searchable = " ".join([label] + [a or "" for a in attributes]).lower()
        if not any(word in searchable for word in ("book", "ticket", "حجز", "match")):
            continue

        for raw_url in attributes:
            raw_url = (raw_url or "").strip()
            if not raw_url or raw_url.lower().startswith(("javascript:", "mailto:")):
                continue
            if raw_url.startswith("#"):
                return f"{site_root}{raw_url}"
            return urljoin(site_root, raw_url)

    return TAZKARTI_URL


def parse_tazkarti_card(card):
    raw_text = (card.text or "").strip()
    if not raw_text or not tazkarti_booking_is_open(raw_text):
        return None

    lines = [clean_tazkarti_line(line) for line in raw_text.splitlines()]
    lines = [line for line in lines if line]

    first_team = tazkarti_element_text(card, ".team-name.first")
    second_team = tazkarti_element_text(card, ".team-name.second")
    title = f"{first_team} vs {second_team}" if first_team and second_team else ""

    if not title:
        title = next(
            (
                line for line in lines
                if (
                    re.search(r"\bvs\.?\b", line, flags=re.IGNORECASE)
                    and line.lower().rstrip(".") != "vs"
                ) or " ضد " in line
            ),
            "",
        )

    if not title:
        vs_index = next(
            (
                index for index, line in enumerate(lines)
                if line.lower().rstrip(".") == "vs"
            ),
            -1,
        )
        if 0 < vs_index < len(lines) - 1:
            title = f"{lines[vs_index - 1]} vs {lines[vs_index + 1]}"

    if not title:
        return None

    stadium = tazkarti_element_text(card, ".one-block.stadium .info")
    if not stadium:
        stadium = next(
            (
                line for line in lines
                if any(word in line.lower() for word in ("stadium", "استاد", "ملعب"))
            ),
            "",
        )

    match_date = tazkarti_element_text(card, ".one-block.when .info .first")
    if not match_date:
        match_date = next(
            (
                line for line in lines
                if re.search(
                    r"\b(?:mon|tue|wed|thu|fri|sat|sun)\b.*\b\d{4}\b",
                    line,
                    flags=re.IGNORECASE,
                )
            ),
            "",
        )

    time_line = tazkarti_element_text(card, ".one-block.when .info .second")
    match_time = tazkarti_field([time_line] if time_line else lines, r"^time\s*:\s*(.+)$")
    match_time = re.sub(r"\s*:\s*", ":", match_time)

    tournament = tazkarti_card_field(card, "Tournament")
    if not tournament:
        tournament = tazkarti_field(lines, r"^tournament\s*:?[ ]*(.+)$")

    match_number = tazkarti_card_field(card, "Match No")
    if not match_number:
        match_number = tazkarti_field(lines, r"^match\s*no\.?\s*:?[ ]*(.+)$")

    group = tazkarti_card_field(card, "Group")
    if not group:
        group = tazkarti_field(lines, r"^group\s*:\s*(.+)$")

    return {
        "title": title,
        "stadium": stadium,
        "date": match_date,
        "time": match_time,
        "tournament": tournament,
        "match_number": match_number,
        "group": group,
        "booking_url": tazkarti_booking_url(card),
        "_source_length": len(raw_text),
    }


def tazkarti_dedupe_key(match):
    if match["tournament"] and match["match_number"]:
        identity = f"{match['tournament']}|{match['match_number']}"
    else:
        identity = "|".join([
            match["title"],
            match["date"],
            match["time"],
        ])
    return f"tazkarti:al-ahly:{normalize_match_identity(identity)}"


def extract_open_al_ahly_matches(driver):
    cards = driver.find_elements(By.CSS_SELECTOR, TAZKARTI_MATCH_SELECTOR)
    if not cards:
        cards = driver.find_elements(By.TAG_NAME, "mat-card")
    if not cards:
        cards = driver.find_elements(
            By.CSS_SELECTOR,
            "[class*='match-card'], [class*='match_card'], [class*='matchCard']",
        )

    matches_by_key = {}
    for card in cards:
        try:
            match = parse_tazkarti_card(card)
            if not match or not is_al_ahly_match(match["title"]):
                continue

            key = tazkarti_dedupe_key(match)
            match["dedupe_key"] = key
            previous = matches_by_key.get(key)

            candidate_score = (
                match["booking_url"] != TAZKARTI_URL,
                -match["_source_length"],
            )
            previous_score = (
                previous["booking_url"] != TAZKARTI_URL,
                -previous["_source_length"],
            ) if previous else None

            if previous is None or candidate_score > previous_score:
                matches_by_key[key] = match
        except Exception as e:
            print(f"Could not parse a Tazkarti card: {e}")

    return list(matches_by_key.values())


def expand_all_tazkarti_matches(driver, max_clicks=10):
    for _ in range(max_clicks):
        view_more = None
        for control in driver.find_elements(By.CSS_SELECTOR, "button, a"):
            label = clean_tazkarti_line(
                control.text or control.get_attribute("aria-label") or ""
            ).lower()
            if ("view more" in label or "عرض المزيد" in label) and control.is_displayed():
                view_more = control
                break

        if view_more is None or not view_more.is_enabled():
            return

        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});",
            view_more,
        )
        driver.execute_script("arguments[0].click();", view_more)
        time.sleep(1.5)


def format_tazkarti_message(match):
    lines = [
        "🚨 <b>حجز مباراة الأهلي متاح الآن!</b>",
        "",
        f"⚽ <b>{html.escape(match['title'])}</b>",
    ]
    if match["tournament"]:
        lines.append(f"🏆 البطولة: {html.escape(match['tournament'])}")
    if match["date"]:
        lines.append(f"📅 التاريخ: {html.escape(match['date'])}")
    if match["time"]:
        lines.append(f"🕗 الساعة: {html.escape(match['time'])}")
    if match["stadium"]:
        lines.append(f"🏟 الملعب: {html.escape(match['stadium'])}")
    if match["group"]:
        lines.append(f"📌 الجولة: {html.escape(match['group'])}")

    lines.extend(["", "🎟 اضغط على الزر بالأسفل للحجز من تذكرتي."])
    return "\n".join(lines)


def check_tazkarti(sent_items):
    print("Checking Tazkarti for open Al Ahly bookings...")
    driver = new_browser()
    try:
        driver.get(TAZKARTI_URL)
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, TAZKARTI_MATCH_SELECTOR)
                )
            )
        except Exception:
            print("Timed out waiting for Tazkarti match cards.")
            save_debug_snapshot(driver, "tazkarti")
            return

        expand_all_tazkarti_matches(driver)
        matches = extract_open_al_ahly_matches(driver)
        count = 0

        for match in matches:
            dedupe_key = match["dedupe_key"]
            if dedupe_key in sent_items:
                continue

            message = format_tazkarti_message(match)
            sent_ok = send_telegram_message(
                message,
                button_text="🎟 احجز من تذكرتي",
                button_url=match["booking_url"],
            )
            if sent_ok:
                save_sent_item(dedupe_key)
                sent_items.add(dedupe_key)
                count += 1

        print(
            f"Found {len(matches)} open Al Ahly match(es); "
            f"sent {count} new alert(s)."
        )
    except Exception as e:
        print(f"Tazkarti Error: {e}")
        save_debug_snapshot(driver, "tazkarti")
    finally:
        driver.quit()


if __name__ == "__main__":
    print(f"Starting Bot. Trigger: {os.environ.get('RUN_EVENT', 'local')}")
    if os.environ.get("TELEGRAM_TEST", "").lower() == "true":
        if not send_telegram_message("✅ اختبار VOX Almaza: اتصال Telegram ناجح، ومراقبة الأفلام والحفلات الجديدة تعمل على نفس البوت."):
            raise SystemExit("Telegram delivery test failed")
    sent_items = load_sent_items()

    check_vox_cinemas(sent_items)
    check_tazkarti(sent_items)

    if MONITOR_ERRORS:
        print(f"Monitoring finished with {len(MONITOR_ERRORS)} error(s). Successful alerts are saved; failed alerts will be retried.")
        raise SystemExit(1)
    print("Done. Monitoring succeeded; only Telegram-confirmed alerts were saved.")
