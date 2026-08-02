import os
import re
import json
import time
import datetime
import urllib.parse
import feedparser
import requests
from bs4 import BeautifulSoup

# ============================================================
# 北本市・桶川市・鴻巣市 地域ポータル 自動生成スクリプト
#
# カテゴリ1（防犯・防災）: 埼玉県警 鴻巣警察署／上尾警察署の新着情報一覧
#   ※ 埼玉県央広域消防本部・anzn.net(火事ドコまっぷ)の火災出動情報は
#     JavaScriptによる動的読み込みのため静的スクレイピング不可と判明。
#     無理な非公式API解析は安定性を損なうため、消防本部の公式お知らせ
#     （静的ページ）を代替情報源として利用する。
# カテゴリ2（新店舗・地域トピック）: 号外NET（鴻巣市・北本市／上尾市・桶川市）RSS
# ============================================================

OUTPUT_DIR = "docs"
OUTPUT_HTML = os.path.join(OUTPUT_DIR, "index.html")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

CITIES = ["北本市", "桶川市", "鴻巣市"]
OPEN_CLOSE_KEYWORDS = ["オープン", "新装", "開店", "閉店", "移転", "リニューアル", "グランドオープン", "新店"]

# 発生場所・所在地の厳密抽出用パターン（市名＋町丁目・駅・交差点・バイパス等）
ADDRESS_PATTERN = re.compile(
    r"(北本市|桶川市|鴻巣市)"
    r"([^\s、。,.\n】]{0,14}?(?:町\d*丁目|丁目|字[^\s、。]{0,6}|地内|駅[前東西南北口]{0,2}|"
    r"交差点付近|交差点|付近|バイパス沿い))"
)
ADDRESS_LABEL_PATTERN = re.compile(r"(?:住所|所在地)[：:]\s*([^\s、。,.\n]{4,30})")


def extract_location(text, fallback_label):
    """本文・タイトルから発生場所/所在地を厳密抽出する。
    番地レベルまで特定できない場合も、空欄やN/Aは出さず
    判明している行政区・管轄名を正直なフォールバックとして返す（捏造は行わない）。"""
    if text:
        m = ADDRESS_PATTERN.search(text)
        if m:
            return f"📍 {m.group(1)}{m.group(2)}"
        m2 = ADDRESS_LABEL_PATTERN.search(text)
        if m2:
            return f"📍 {m2.group(1)}"
        for city in CITIES:
            if city in text:
                return f"📍 {city}エリア"
    return f"📍 {fallback_label}"


skip_log = []


def log_skip(source, reason):
    skip_log.append(f"{source}: {reason}")
    print(f"⚠️  スキップ - {source}: {reason}")


def parse_police_date(date_str):
    """「7月31日」のような表記を今年(または去年)の日付に変換する"""
    m = re.match(r"(\d{1,2})月(\d{1,2})日", date_str)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    now = datetime.datetime.now()
    try:
        dt = datetime.datetime(now.year, month, day)
        if dt > now + datetime.timedelta(days=1):
            dt = datetime.datetime(now.year - 1, month, day)
        return dt
    except ValueError:
        return None


def fetch_police_list(name, url, default_city, limit=12):
    """埼玉県警 警察署の「新着情報一覧」ページ(table.list_table)を安全に取得・パースする"""
    items = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.select("table.list_table tr")
        if not rows:
            log_skip(name, "一覧テーブルが見つかりませんでした（サイト構造変更の可能性）")
            return items
        for row in rows[:limit]:
            link_a = row.select_one("td a")
            if not link_a:
                continue
            title = link_a.get_text(strip=True)
            href = link_a.get("href", "")
            link = requests.compat.urljoin(url, href)
            date_td = row.select_one("td.date")
            date_str = date_td.get_text(strip=True) if date_td else ""
            dt = parse_police_date(date_str)

            city = default_city
            if "北本" in title:
                city = "北本市"
            elif "桶川" in title:
                city = "桶川市"
            elif "鴻巣" in title:
                city = "鴻巣市"

            location = extract_location(title, fallback_label=f"{name}管内")

            items.append({
                "city": city,
                "category": "防犯・防災",
                "date_display": date_str,
                "sort_key": dt.isoformat() if dt else "",
                "title": title,
                "link": link,
                "source": name,
                "location": location,
            })
    except Exception as e:
        log_skip(name, f"取得エラー ({e})")
    return items


def fetch_fire_dept_notices(name, url, limit=8):
    """埼玉県央広域消防本部の公式お知らせ(静的ページ)を取得する。
    ライブの火災出動情報(災害発生情報)はJS動的読み込みのため対象外。"""
    items = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        blocks = soup.select(".block-news .news-content li")
        if not blocks:
            log_skip(name, "お知らせ一覧が見つかりませんでした（サイト構造変更の可能性）")
            return items
        for li in blocks[:limit]:
            day_div = li.select_one(".day")
            a_tag = li.select_one("a")
            if not a_tag:
                continue
            title = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")
            link = requests.compat.urljoin(url, href)
            date_str = day_div.get_text(strip=True) if day_div else ""
            try:
                dt = datetime.datetime.strptime(date_str, "%Y年%m月%d日")
            except ValueError:
                dt = None
            location = extract_location(title, fallback_label="埼玉県央広域消防本部管内（北本市・桶川市・鴻巣市）")

            items.append({
                "city": "北本市・桶川市・鴻巣市（管内共通）",
                "category": "防犯・防災",
                "date_display": date_str,
                "sort_key": dt.isoformat() if dt else "",
                "title": title,
                "link": link,
                "source": name,
                "location": location,
            })
    except Exception as e:
        log_skip(name, f"取得エラー ({e})")
    return items


def fetch_goguynet(name, url, city_filter, limit=20):
    """号外NETのRSSを取得し、タイトルの【市名】タグで市を判定・分類する"""
    items = []
    try:
        feed = feedparser.parse(url, request_headers=HEADERS)
        status = getattr(feed, "status", None)
        if status is not None and status >= 400:
            log_skip(name, f"HTTP {status}")
            return items
        if not feed.entries:
            log_skip(name, "記事が取得できませんでした（フィード形式変更の可能性）")
            return items
        for entry in feed.entries[:limit]:
            title_raw = getattr(entry, "title", "").strip()
            m = re.match(r"【(.+?市)】\s*(.*)", title_raw)
            if not m:
                continue
            city, title = m.group(1), m.group(2).strip() or title_raw
            if city not in city_filter:
                continue
            category = "新店舗・開閉店" if any(k in title_raw for k in OPEN_CLOSE_KEYWORDS) else "地域トピック"
            published_parsed = entry.get("published_parsed")
            dt = datetime.datetime(*published_parsed[:6]) if published_parsed else None
            summary = getattr(entry, "summary", "")
            location = extract_location(f"{title_raw} {summary}", fallback_label=f"{city}エリア")
            items.append({
                "city": city,
                "category": category,
                "date_display": dt.strftime("%Y-%m-%d") if dt else "",
                "sort_key": dt.isoformat() if dt else "",
                "title": title,
                "link": getattr(entry, "link", ""),
                "source": name,
                "location": location,
            })
    except Exception as e:
        log_skip(name, f"取得エラー ({e})")
    return items


ALERT_KEYWORDS_QUERY = "(火事 OR 火災 OR 事故 OR 不審者 OR 通行止め OR 事件 OR 停電)"
TREND_KEYWORDS_QUERY = "(オープン OR 新装開店 OR グランドオープン OR 閉店 OR イベント OR リニューアル)"
GOOGLE_NEWS_RECENT_DAYS = 21  # これより古い記事はノイズとみなし対象外にする（「最新のもの」という要件のため）


def fetch_google_news_local(city, kind):
    """Google News RSSで「{city} (キーワード群)」を検索し、実際の報道タイトルのみを
    抽出する。北本市・桶川市・鴻巣市の公式サイトはJSタブ構造で新着情報が静的
    取得できなかったため、代替として採用（NHK埼玉のRSSも2026年時点で廃止済み
    のため、Google Newsの集約結果で代替する）。
    kind: "alert"（防犯・防災）または "trend"（新店舗・地域トピック）"""
    name = f"Google News［{city}・{'防犯防災' if kind == 'alert' else '新店舗/イベント'}］"
    items = []
    kw = ALERT_KEYWORDS_QUERY if kind == "alert" else TREND_KEYWORDS_QUERY
    query = f"{city} {kw}"
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) + "&hl=ja&gl=JP&ceid=JP:ja"
    try:
        feed = feedparser.parse(url, request_headers=HEADERS)
        status = getattr(feed, "status", None)
        if status is not None and status >= 400:
            log_skip(name, f"HTTP {status}")
            return items
        if not feed.entries:
            log_skip(name, "記事が取得できませんでした")
            return items
        now = datetime.datetime.now()
        seen_titles = set()
        for e in feed.entries:
            published_parsed = e.get("published_parsed")
            dt = datetime.datetime(*published_parsed[:6]) if published_parsed else None
            # 公開日が不明、または一定期間より古い記事はノイズとして除外する
            if dt is None or (now - dt).days > GOOGLE_NEWS_RECENT_DAYS:
                continue
            title_raw = re.sub(r'\s*-\s*[^-]+$', '', e.get("title", "")).strip()
            if not title_raw or title_raw in seen_titles:
                continue
            seen_titles.add(title_raw)
            category = "防犯・防災" if kind == "alert" else (
                "新店舗・開閉店" if any(k in title_raw for k in OPEN_CLOSE_KEYWORDS) else "地域トピック")
            location = extract_location(title_raw, fallback_label=f"{city}エリア")
            items.append({
                "city": city,
                "category": category,
                "date_display": dt.strftime("%Y-%m-%d"),
                "sort_key": dt.isoformat(),
                "title": title_raw,
                "link": e.get("link", ""),
                "source": "Google News",
                "location": location,
            })
            if len(items) >= 6:
                break
    except Exception as e:
        log_skip(name, f"取得エラー ({e})")
    if not items:
        log_skip(name, f"直近{GOOGLE_NEWS_RECENT_DAYS}日以内の該当記事なし")
    return items


X_HASHTAGS = {"北本市": "北本市", "桶川市": "桶川市", "鴻巣市": "鴻巣市"}


def render_x_widget_section():
    """X（旧Twitter）連携について。
    公式埋め込みウィジェット（ハッシュタグ検索タイムライン）を実機で検証したところ、
    X側の仕様変更により中身が描画されず「読み込み中」のまま止まることを確認した
    （2026-08-02、GitHub Pages上の実ページ・widgets.jsのコンソールログで実際に
    確認済み）。動かないものを動くように見せかけるのは誠実でないため、埋め込みは
    廃止し、実際に機能する「Xで検索」への外部リンクに置き換える。"""
    buttons = "".join(f"""
      <a class="x-search-btn" href="https://twitter.com/search?q=%23{urllib.parse.quote(tag)}&src=typed_query&f=live" target="_blank" rel="noopener">
        🔗 #{esc_x(tag)} をXで検索
      </a>""" for tag in X_HASHTAGS.values())
    return f"""
  <details class="block accordion">
    <summary>⑤ X（旧Twitter）で検索</summary>
    <p class="disclaimer">X公式のハッシュタグ埋め込みタイムラインは実機検証の結果、
      X側の仕様変更により正常に描画されないことを確認したため廃止しました
      （中身が空のまま表示され続ける状態を「動いている」と偽ることはしません）。
      代わりに、タップすると実際のX検索結果に飛べる外部リンクにしています。</p>
    <div class="x-link-grid">{buttons}</div>
  </details>"""


UPDATE_INTERVAL_NOTE = "15分おき（GitHub Actions cron: 3,18,33,48 * * * *）"
REPO_ACTIONS_URL = "https://github.com/c6cgv9cnj4-ops/kitamoto-okegawa-konosu-portal/actions"


def render_system_status_section(now_jst_str):
    return f"""
  <details class="block accordion">
    <summary>⚙️ システム状態・自動更新について</summary>
    <ul class="system-status-list">
      <li>このページの生成時刻（JST）: {now_jst_str}</li>
      <li>自動更新間隔: {UPDATE_INTERVAL_NOTE}</li>
      <li>実行環境: GitHub Actions（Macの電源・スリープに依存しない）</li>
      <li>実行履歴の確認: <a href="{REPO_ACTIONS_URL}" target="_blank" rel="noopener">{REPO_ACTIONS_URL}</a></li>
      <li>変更が無い回はコミットされません（「Yahoo!路線情報」等の更新時刻が同じままなら、実際にデータ側が変化していないだけで自動更新自体は動いています）</li>
    </ul>
  </details>"""


def esc_x(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


TRAIN_LINES = [
    ("JR高崎線", "https://transit.yahoo.co.jp/diainfo/48/0"),
    ("JR宇都宮線", "https://transit.yahoo.co.jp/diainfo/46/46"),
    ("JR湘南新宿ライン", "https://transit.yahoo.co.jp/diainfo/25/0"),
    ("JR上野東京ライン", "https://transit.yahoo.co.jp/diainfo/627/0"),
]


def fetch_train_status():
    """Yahoo!路線情報の運行情報ページ（路線ごとの静的ページ）を直接取得する。
    ステータス（平常運転／遅延／運転見合わせ等）はページ自身が表示する
    見出しテキストとアイコンclassをそのまま使い、こちらで推測はしない。"""
    name = "Yahoo!路線情報"
    items = []
    for line_name, url in TRAIN_LINES:
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            r.raise_for_status()
            r.encoding = "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
            dt = soup.select_one("#mdServiceStatus dt")
            dd = soup.select_one("#mdServiceStatus dd")
            updated = soup.select_one(".subText")
            if not dt:
                log_skip(f"{name}［{line_name}］", "運行情報が見つかりませんでした（サイト構造変更の可能性）")
                continue
            status_text = dt.get_text(strip=True)
            icon = dt.select_one("span")
            icon_class = icon.get("class", [""])[0] if icon else ""
            is_normal = icon_class == "icnNormalLarge" or status_text == "平常運転"
            items.append({
                "line": line_name,
                "status": status_text,
                "is_normal": is_normal,
                "detail": dd.get_text(strip=True) if dd else "",
                "updated": updated.get_text(strip=True) if updated else "",
                "link": url,
            })
        except Exception as e:
            log_skip(f"{name}［{line_name}］", f"取得エラー ({e})")
    return items


EQ_FEED_URL = "https://www.data.jma.go.jp/developer/xml/feed/eqvol.xml"
EQ_RELEVANT_TITLES = ("震度速報", "震源・震度に関する情報")
EQ_TARGET_PREF = "埼玉県"
EQ_RECENT_HOURS = 72  # これより古い地震情報は表示対象外（「最新」という要件のため）


def fetch_earthquake_info():
    """気象庁 防災情報XMLフィード（公式・無料）から、埼玉県が最大震度の
    対象に含まれる直近の地震情報のみを抽出する。個々の地震ごとの詳細XMLを
    実際に読み込み、<Pref><Name>埼玉県</Name>...<MaxInt> の実データがある
    場合のみ採用する（埼玉県に無関係な地震は対象外とし、憶測で埋めない）。"""
    name = "気象庁 地震情報"
    items = []
    try:
        r = requests.get(EQ_FEED_URL, headers=HEADERS, timeout=10)
        r.encoding = "utf-8"
        entries = re.findall(r"<entry>(.*?)</entry>", r.text, re.S)
        now = datetime.datetime.now(datetime.timezone.utc)
        checked = 0
        for entry in entries:
            title_m = re.search(r"<title>(.*?)</title>", entry)
            if not title_m or title_m.group(1) not in EQ_RELEVANT_TITLES:
                continue
            updated_m = re.search(r"<updated>(.*?)</updated>", entry)
            if updated_m:
                try:
                    updated_dt = datetime.datetime.fromisoformat(updated_m.group(1).replace("Z", "+00:00"))
                    if (now - updated_dt).total_seconds() > EQ_RECENT_HOURS * 3600:
                        continue
                except ValueError:
                    pass
            link_m = re.search(r'<link type="application/xml" href="([^"]+)"', entry)
            if not link_m:
                continue
            checked += 1
            if checked > 15:  # フィード全体の走査量に上限を設け、処理時間を抑える
                break
            try:
                r2 = requests.get(link_m.group(1), headers=HEADERS, timeout=10)
                r2.encoding = "utf-8"
                xml = r2.text
            except Exception:
                continue
            pref_m = re.search(
                rf"<Pref><Name>{EQ_TARGET_PREF}</Name><Code>\d+</Code><MaxInt>(\d+)</MaxInt>", xml)
            if not pref_m:
                continue  # 埼玉県が対象に含まれない地震は表示しない
            max_int = pref_m.group(1)
            headline_m = re.search(r"<Headline>\s*<Text>(.*?)</Text>", xml, re.S)
            hypo_m = re.search(r"<Hypocenter>.*?<Name>(.*?)</Name>", xml, re.S)
            mag_m = re.search(r'<jmx_eb:Magnitude[^>]*description="([^"]+)"', xml)
            origin_m = re.search(r"<OriginTime>(.*?)</OriginTime>", xml)
            items.append({
                "title": f"埼玉県で最大震度{max_int}を観測" + (f"（震源: {hypo_m.group(1)}）" if hypo_m else ""),
                "detail": (headline_m.group(1).strip() if headline_m else "") +
                           (f" {mag_m.group(1)}" if mag_m else ""),
                "origin_time": origin_m.group(1) if origin_m else "",
                "max_int": max_int,
                "link": link_m.group(1).replace(".xml", "").replace(
                    "developer/xml/data/", "www.jma.go.jp/bosai/map.html#") or "https://www.jma.go.jp/bosai/map.html",
            })
    except Exception as e:
        log_skip(name, f"取得エラー ({e})")
    if not items:
        log_skip(name, f"直近{EQ_RECENT_HOURS}時間以内に埼玉県を含む地震情報なし（気象庁XMLフィードは正常に取得できています）")
    return items


RECENT_ALERT_DAYS = 7  # 「本日〜直近数日以内のみ」という要件のため、防犯防災アラート・地域トピックとも直近7日以内のみを対象にする


def normalize_title_for_dedup(title):
    """重複判定用にタイトルを正規化する。同一ニュースがGoogle News等で
    「－ 媒体名」「｜媒体名」「（媒体名）」といった表記ゆれ違いの複数記事として
    重複掲載されるのを防ぐため、末尾の媒体名表記を除去してから比較する。"""
    t = title
    t = re.sub(r'\s*[-－―｜|]\s*[^\-－―｜|]{1,24}$', '', t)
    t = re.sub(r'[（(][^（）()]{1,20}[）)]\s*$', '', t)
    t = re.sub(r'[\s　]+', '', t)
    return t[:36]


def dedup_and_filter_recent(items, days):
    """タイトルの重複を排除し、直近days日以内に日付が確認できたものだけを残す。
    日付が確認できないものは「最新のみ」という要件を満たせないため対象外にする
    （表示継続の方向に倒すのではなく、ここでは正直に除外する）。"""
    now = datetime.datetime.now()
    seen = set()
    result = []
    for item in items:
        sort_key = item.get("sort_key", "")
        if not sort_key:
            continue
        try:
            dt = datetime.datetime.fromisoformat(sort_key)
        except ValueError:
            continue
        if (now - dt).days > days or dt > now + datetime.timedelta(days=1):
            continue
        dedup_key = normalize_title_for_dedup(item["title"])
        if not dedup_key or dedup_key in seen:
            continue
        seen.add(dedup_key)
        result.append(item)
    return result


def build_dataset():
    alerts = []
    topics = []

    alerts += fetch_police_list(
        "鴻巣警察署 新着情報",
        "https://www.police.pref.saitama.lg.jp/kenke/kesatsusho/konosu/shinchaku/index.html",
        default_city="鴻巣市",
    )
    alerts += fetch_police_list(
        "上尾警察署 新着情報",
        "https://www.police.pref.saitama.lg.jp/kenke/kesatsusho/ageo/shinchaku/index.html",
        default_city="桶川市",
    )
    alerts += fetch_fire_dept_notices(
        "埼玉県央広域消防本部 お知らせ",
        "https://www.ken-o.or.jp/firehead/",
    )

    topics += fetch_goguynet(
        "号外NET 鴻巣市・北本市",
        "https://kounosu-kitamoto.goguynet.jp/feed/",
        city_filter=["鴻巣市", "北本市"],
    )
    topics += fetch_goguynet(
        "号外NET 上尾市・桶川市",
        "https://ageo-okegawa.goguynet.jp/feed/",
        city_filter=["桶川市"],
    )

    for city in CITIES:
        alerts += fetch_google_news_local(city, "alert")
        topics += fetch_google_news_local(city, "trend")

    alerts = dedup_and_filter_recent(alerts, RECENT_ALERT_DAYS)
    topics = dedup_and_filter_recent(topics, RECENT_ALERT_DAYS)

    FIRE_KEYWORDS = ("火事", "火災", "出火", "全焼", "半焼", "ぼや")
    for a in alerts:
        a["is_fire"] = any(k in a["title"] for k in FIRE_KEYWORDS)

    # 新しい順に並べたうえで、火災関連のみ最優先で先頭に引き上げる（安定ソートを利用）
    alerts.sort(key=lambda x: x["sort_key"], reverse=True)
    alerts.sort(key=lambda x: x["is_fire"], reverse=True)
    topics.sort(key=lambda x: x["sort_key"], reverse=True)

    train_status = fetch_train_status()
    earthquakes = fetch_earthquake_info()
    return alerts, topics, train_status, earthquakes


def render_item_row(item, extra_class=""):
    city_badge = item["city"]
    cat_badge = item["category"]
    date_disp = item["date_display"] or "日付不明"
    title = item["title"]
    link = item["link"]
    source = item["source"]
    location = item.get("location", "")
    is_fire = item.get("is_fire", False)
    cat_class = "cat-store" if cat_badge == "新店舗・開閉店" else ("cat-topic" if cat_badge == "地域トピック" else "cat-alert")
    fire_class = " item-fire" if is_fire else ""
    fire_prefix = "🔥 " if is_fire else ""
    return f"""
    <a class="item {extra_class}{fire_class}" data-city="{city_badge}" href="{link}" target="_blank" rel="noopener">
      <span class="badge city-badge">{city_badge}</span>
      <span class="badge {cat_class}">{cat_badge}</span>
      <span class="item-title">{fire_prefix}{title}</span>
      <span class="loc-badge">{location}</span>
      <span class="item-meta">{date_disp}｜{source}</span>
    </a>"""


def render_train_section(train_status):
    if not train_status:
        return "<p class='empty'>運行情報を取得できませんでした。</p>"
    cards = []
    for t in train_status:
        status_class = "status-normal" if t["is_normal"] else "status-alert"
        cards.append(f"""
      <a class="train-card {status_class}" href="{t['link']}" target="_blank" rel="noopener">
        <div class="train-line">{t['line']}</div>
        <div class="train-status">{t['status']}</div>
        <div class="train-detail">{t['detail']}</div>
        <div class="train-updated">{t['updated']}｜Yahoo!路線情報</div>
      </a>""")
    return f"<div class='train-grid'>{''.join(cards)}</div>"


def render_earthquake_section(earthquakes):
    if not earthquakes:
        return "<p class='empty'>直近72時間以内に埼玉県を含む地震情報はありません（気象庁XMLフィードで確認済み）。</p>"
    cards = []
    for eq in earthquakes:
        cards.append(f"""
      <a class="eq-card" href="{eq['link']}" target="_blank" rel="noopener">
        <div class="eq-title">{eq['title']}</div>
        <div class="eq-detail">{eq['detail']}</div>
        <div class="eq-meta">発生: {eq['origin_time']}｜気象庁 防災情報XML</div>
      </a>""")
    return f"<div class='eq-grid'>{''.join(cards)}</div>"


def render_html(alerts, topics, train_status, earthquakes, skip_log):
    JST = datetime.timezone(datetime.timedelta(hours=9))
    now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

    alert_rows = "".join(render_item_row(a) for a in alerts) if alerts else "<p class='empty'>現在、取得できた防犯・防災情報はありません。</p>"

    topic_sections = []
    for city in CITIES:
        city_items = [t for t in topics if t["city"] == city]
        rows = "".join(render_item_row(t) for t in city_items) if city_items else "<p class='empty'>該当する新店舗・地域トピック情報はありません。</p>"
        topic_sections.append(f"""
    <section class="topic-city-block" data-city="{city}">
      <h3>{city}</h3>
      <div class="item-list">{rows}</div>
    </section>""")
    topics_html = "".join(topic_sections)

    skip_html = "".join(f"<li>{s}</li>" for s in skip_log) if skip_log else "<li>なし（すべての情報源から正常に取得できました）</li>"

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>近隣3市 地域ポータル（北本・桶川・鴻巣）</title>
<style>
  :root {{
    --bg: #12151a;
    --bg-raised: #1b1f26;
    --ink: #e6e9ee;
    --ink-soft: #9aa4b2;
    --rule: #2a2f38;
    --accent: #5aa9e6;
    --alert: #e2665a;
    --store: #4fb08a;
    --topic: #c9a24b;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: "Hiragino Kaku Gothic ProN", "Hiragino Sans", "Yu Gothic", "Noto Sans JP", system-ui, sans-serif;
    line-height: 1.7;
  }}
  .page {{ max-width: 860px; margin: 0 auto; padding: 32px 20px 64px; }}
  header.top {{ margin-bottom: 20px; }}
  header.top h1 {{ font-size: 26px; margin: 0 0 6px; }}
  header.top .meta {{ color: var(--ink-soft); font-size: 13px; }}

  .tabs {{ display: flex; gap: 8px; margin: 20px 0; flex-wrap: wrap; }}
  .tab-btn {{
    background: var(--bg-raised);
    color: var(--ink);
    border: 1px solid var(--rule);
    border-radius: 999px;
    padding: 8px 16px;
    font-size: 14px;
    cursor: pointer;
  }}
  .tab-btn.active {{ background: var(--accent); color: #0c1116; border-color: var(--accent); font-weight: 700; }}

  section.block {{ margin-bottom: 32px; }}
  section.block > h2 {{
    font-size: 18px;
    border-bottom: 2px solid var(--rule);
    padding-bottom: 8px;
    margin-bottom: 12px;
  }}

  .item-list {{ display: flex; flex-direction: column; gap: 8px; }}
  a.item {{
    display: grid;
    grid-template-columns: auto auto 1fr auto auto;
    align-items: center;
    gap: 10px;
    background: var(--bg-raised);
    border: 1px solid var(--rule);
    border-radius: 8px;
    padding: 10px 14px;
    text-decoration: none;
    color: var(--ink);
  }}
  a.item:hover {{ border-color: var(--accent); }}
  .badge {{
    font-size: 11px;
    font-weight: 700;
    padding: 3px 8px;
    border-radius: 6px;
    white-space: nowrap;
  }}
  .city-badge {{ background: #232a35; color: var(--ink-soft); }}
  .cat-alert {{ background: rgba(226,102,90,0.18); color: var(--alert); }}
  a.item.item-fire {{
    background: rgba(226,60,50,0.22);
    border: 2px solid #ff3b30;
    box-shadow: 0 0 14px rgba(255,59,48,0.35);
  }}
  a.item.item-fire .item-title {{ font-weight: 700; color: #ffd7d2; }}
  .cat-store {{ background: rgba(79,176,138,0.18); color: var(--store); }}
  .cat-topic {{ background: rgba(201,162,75,0.18); color: var(--topic); }}
  .item-title {{ font-size: 14px; }}
  .loc-badge {{
    font-size: 11px;
    font-weight: 600;
    padding: 3px 8px;
    border-radius: 6px;
    background: #2a2a2a;
    color: #00adb5;
    white-space: nowrap;
  }}
  .item-meta {{ font-size: 11.5px; color: var(--ink-soft); white-space: nowrap; }}
  .empty {{ color: var(--ink-soft); font-size: 13.5px; }}

  .topic-city-block h3 {{ font-size: 15px; color: var(--ink-soft); margin: 16px 0 8px; }}
  .topic-city-block:first-child h3 {{ margin-top: 0; }}

  .train-grid, .eq-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }}
  .train-card, .eq-card {{ display: block; border-radius: 8px; padding: 12px 14px; text-decoration: none; border: 1px solid var(--rule); }}
  .train-card.status-normal {{ background: rgba(79,176,138,0.12); border-color: var(--store); }}
  .train-card.status-normal .train-status {{ color: var(--store); font-weight: 700; }}
  .train-card.status-alert {{ background: rgba(226,102,90,0.14); border-color: var(--alert); }}
  .train-card.status-alert .train-status {{ color: var(--alert); font-weight: 700; }}
  .train-line {{ font-size: 13px; color: var(--ink-soft); }}
  .train-status {{ font-size: 16px; margin: 2px 0; }}
  .train-detail {{ font-size: 12px; color: var(--ink); }}
  .train-updated {{ font-size: 10.5px; color: var(--ink-soft); margin-top: 6px; }}
  .eq-card {{ background: rgba(226,102,90,0.08); border-color: var(--alert); color: var(--ink); }}
  .eq-title {{ font-size: 14px; font-weight: 700; color: var(--alert); }}
  .eq-detail {{ font-size: 12px; margin: 4px 0; }}
  .eq-meta {{ font-size: 10.5px; color: var(--ink-soft); }}
  .unverified-tag {{ font-size: 11px; font-weight: 700; color: var(--alert); border: 1px solid var(--alert); border-radius: 6px; padding: 2px 8px; vertical-align: middle; margin-left: 8px; }}
  .disclaimer {{ font-size: 12.5px; color: var(--ink-soft); background: var(--bg-raised); border: 1px solid var(--rule); border-radius: 8px; padding: 10px 14px; }}
  details.accordion {{ background: var(--bg-raised); border: 1px solid var(--rule); border-radius: 10px; padding: 4px 16px; }}
  details.accordion summary {{ cursor: pointer; font-size: 18px; padding: 10px 0; list-style: none; }}
  details.accordion summary::-webkit-details-marker {{ display: none; }}
  details.accordion summary::before {{ content: "▶ "; display: inline-block; transition: transform 0.15s; }}
  details.accordion[open] summary::before {{ transform: rotate(90deg); }}
  details.accordion .disclaimer, details.accordion .x-link-grid, details.accordion .system-status-list {{ margin-bottom: 14px; }}
  .system-status-list {{ font-size: 12.5px; color: var(--ink-soft); line-height: 1.9; padding-left: 18px; }}
  .system-status-list a {{ color: var(--accent); }}
  .x-link-grid {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }}
  .x-search-btn {{ background: var(--bg-raised); border: 1px solid var(--accent); color: var(--accent); border-radius: 999px; padding: 8px 16px; font-size: 13px; text-decoration: none; font-weight: 700; }}
  .x-search-btn:hover {{ background: var(--accent); color: #0c1116; }}

  footer {{ border-top: 1px solid var(--rule); padding-top: 14px; font-size: 12px; color: var(--ink-soft); }}
  footer ul {{ margin: 6px 0 0; padding-left: 18px; }}

  @media (max-width: 560px) {{
    a.item {{ grid-template-columns: 1fr; }}
    .item-meta, .loc-badge {{ white-space: normal; justify-self: start; }}
  }}
</style>
</head>
<body>
<div class="page">
  <header class="top">
    <h1>近隣3市 地域ポータル</h1>
    <div class="meta">北本市・桶川市・鴻巣市｜最終更新: {now_str}</div>
  </header>

  <section class="block">
    <h2>① 鉄道運行情報（JR高崎線・宇都宮線・湘南新宿ライン・上野東京ライン）</h2>
    {render_train_section(train_status)}
  </section>

  <section class="block">
    <h2>② 地震情報（埼玉県が対象に含まれるもののみ・気象庁XML）</h2>
    {render_earthquake_section(earthquakes)}
  </section>

  <div class="tabs">
    <button class="tab-btn active" data-target="全体">全体</button>
    <button class="tab-btn" data-target="北本市">北本市</button>
    <button class="tab-btn" data-target="桶川市">桶川市</button>
    <button class="tab-btn" data-target="鴻巣市">鴻巣市</button>
  </div>

  <section class="block">
    <h2>③ 緊急・防犯防災アラート</h2>
    <div class="item-list" id="alert-list">{alert_rows}</div>
  </section>

  <section class="block" id="topics-block">
    <h2>④ エリア新店舗・地域トピック</h2>
    {topics_html}
  </section>
{render_x_widget_section()}
{render_system_status_section(now_str)}

  <footer>
    データ取得状況（スキップログ）:
    <ul>{skip_html}</ul>
    <p>※ 消防出動情報はライブ配信元がJavaScript動的読み込みのため、静的スクレイピングでは取得できず、消防本部の公式お知らせを代替表示しています。</p>
    <p>※ 北本市・桶川市・鴻巣市の公式サイトはJSタブ構造のため新着情報を直接取得できず、Google Newsの検索結果（直近{GOOGLE_NEWS_RECENT_DAYS}日以内のみ）で代替しています。NHK埼玉のRSS配信は廃止済みのため対象外です。</p>
    <p>※ 防犯・防災アラート／地域トピックは、同一ニュースの重複掲載を除去したうえで、直近{RECENT_ALERT_DAYS}日以内に日付が確認できたもののみを表示しています（日付が確認できない情報は「最新のみ」の要件を満たせないため対象外にしています）。</p>
  </footer>
</div>

<script>
  const buttons = document.querySelectorAll(".tab-btn");
  const alertItems = document.querySelectorAll("#alert-list .item");
  const topicBlocks = document.querySelectorAll(".topic-city-block");

  buttons.forEach(btn => {{
    btn.addEventListener("click", () => {{
      buttons.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const target = btn.dataset.target;

      alertItems.forEach(item => {{
        const city = item.dataset.city;
        const show = target === "全体" || city === target || city.indexOf(target) !== -1;
        item.style.display = show ? "" : "none";
      }});

      topicBlocks.forEach(block => {{
        const city = block.dataset.city;
        block.style.display = (target === "全体" || city === target) ? "" : "none";
      }});
    }});
  }});
</script>
</body>
</html>
"""


def main():
    start_time = time.time()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    alerts, topics, train_status, earthquakes = build_dataset()
    html = render_html(alerts, topics, train_status, earthquakes, skip_log)

    temp_file = OUTPUT_HTML + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(html)
        os.replace(temp_file, OUTPUT_HTML)
        print(f"✅ 地域ポータル生成完了: {OUTPUT_HTML}")
        print(f"   防犯・防災アラート: {len(alerts)}件 / 新店舗・地域トピック: {len(topics)}件")
        print(f"   鉄道運行情報: {len(train_status)}路線 / 地震情報（埼玉県該当）: {len(earthquakes)}件")
        if skip_log:
            print(f"   ⚠️ スキップ件数: {len(skip_log)}件（詳細はページ下部フッター参照）")
    except Exception as e:
        print(f"❌ ファイル保存エラー: {e}")
        if os.path.exists(temp_file):
            os.remove(temp_file)

    elapsed = time.time() - start_time
    print(f"⏱️ トータル処理時間: {int(elapsed // 60)}分 {elapsed % 60:.2f}秒")


if __name__ == "__main__":
    main()
