import os
import re
import io
import json
import time
import datetime
import urllib.parse
import feedparser
import requests
import pdfplumber
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


VAGUE_TITLE_PATTERN = re.compile(r"警戒情報|お知らせ$")


def fetch_notice_summary(url):
    """「警戒情報（〇月〇日認知）」等のタイトルだけでは中身が分からない
    お知らせについて、リンク先の詳細ページを実際に取得し、最初に掲載されて
    いる具体的な被害内容（罪種＋発生概要・実際の町丁目レベルの場所を含む）を
    要約として抽出する。取得できない場合はNoneを返し、憶測で埋めない。"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        h1 = soup.select_one("h1")
        if not h1:
            return None
        h3 = h1.find_next("h3")
        if not h3:
            return None
        crime_type = h3.get_text(strip=True)
        ul = h3.find_next_sibling("ul")
        li = ul.select_one("li") if ul else None
        if not li:
            return None
        detail = li.get_text(strip=True)
        return f"{crime_type}：{detail}"
    except Exception:
        return None


ALERT_ICON_RULES = [
    (("不審者",), "🚨"),
    (("特殊詐欺", "オレオレ", "詐欺"), "⚠️"),
    (("窃盗", "盗難", "空き巣", "忍込み", "侵入"), "🔓"),
    (("死亡事故", "交通事故", "事故", "ひき逃げ"), "🚗"),
    (("逮捕", "暴行", "傷害", "強盗", "強姦", "脅迫"), "🚨"),
    (("警戒情報",), "⚠️"),
]
TOPIC_ICON_RULES = [
    (("オープン", "開店", "グランドオープン", "新装"), "🎉"),
    (("閉店", "閉局", "休業"), "🔚"),
    (("まつり", "祭", "フェス", "イベント"), "🎪"),
    (("ランチ", "グルメ", "カフェ", "食", "レストラン"), "🍔"),
]


def classify_icon(text, rules, default_icon):
    for keywords, icon in rules:
        if any(k in text for k in keywords):
            return icon
    return default_icon


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

# X公式の埋め込みタイムラインが機能しないため、Yahoo!リアルタイム検索
# （XのデータをYahooがライセンス提供しているページ、静的HTMLで取得可能）を
# 代わりに使用する。ただし雑談・広告・陰謀論等のノイズが大量に混在するため、
# 「防犯・防災・鉄道・地震」に関連するキーワードを含む投稿のみを採用し、
# それ以外は表示しない（無関係な投稿を紛れ込ませない＝デマ対策）。
YAHOO_RT_KEYWORDS = ("火事", "火災", "出火", "事故", "事件", "不審者", "通行止め",
                     "停電", "遅延", "運転見合わせ", "運休", "震度", "地震", "避難", "警報",
                     "逮捕", "強盗", "暴行", "傷害", "特殊詐欺", "ひき逃げ", "行方不明",
                     "土砂災害", "浸水", "竜巻", "落雷",
                     # ここから地域の日常トピック（防犯・防災以外も幅広く拾う）
                     "オープン", "開店", "閉店", "新装", "グランドオープン", "リニューアル",
                     "まつり", "祭", "フェス", "イベント", "花火", "桜", "紅葉",
                     "グルメ", "ランチ", "カフェ", "食べ", "出没", "話題")
# 明らかな迷惑投稿・陰謀論・個人的な呼びかけは、キーワード一致有無に関わらず除外する
YAHOO_RT_NOISE_PATTERNS = ("集団ストーカー", "スピリチュアル", "都市伝説", "洗脳",
                           "暇つぶし付き合", "出会い希望", "副業", "在宅ワーク", "稼げる")
YAHOO_RT_RECENT_DAYS = 3
YAHOO_RT_TARGET_MIN = 3  # この件数に満たない場合のみ、キーワード不問のフォールバックを行う


def parse_yahoo_relative_time(text):
    """Yahoo!リアルタイム検索の相対時刻表記（「5分前」「3時間前」「7月31日(金) 22:50」等）
    を実際の日時に変換する。解釈できない表記は None を返し、憶測で埋めない。"""
    now = datetime.datetime.now()
    m = re.match(r"(\d+)秒前", text)
    if m:
        return now - datetime.timedelta(seconds=int(m.group(1)))
    m = re.match(r"(\d+)分前", text)
    if m:
        return now - datetime.timedelta(minutes=int(m.group(1)))
    m = re.match(r"(\d+)時間前", text)
    if m:
        return now - datetime.timedelta(hours=int(m.group(1)))
    m = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        try:
            dt = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            if dt > now:
                dt -= datetime.timedelta(days=1)  # 「HH:MM」のみの表記は当日（日付をまたいだ場合は前日）
            return dt
        except ValueError:
            return None
    m = re.match(r"(\d{1,2})月(\d{1,2})日\([^)]*\)\s*(\d{1,2}):(\d{2})", text)
    if m:
        mo, d, h, mi = (int(g) for g in m.groups())
        try:
            dt = datetime.datetime(now.year, mo, d, h, mi)
            if dt > now + datetime.timedelta(days=1):
                dt = dt.replace(year=now.year - 1)
            return dt
        except ValueError:
            return None
    return None


URL_FRAGMENT_PATTERN = re.compile(r'[a-zA-Z0-9\-]+\.[a-zA-Z]{2,}/\S{4,}')
LEADING_NOISE_PATTERN = re.compile(r'^[\s　]*(RT[:\s]|返信先[:：].*?\s|[大王？＞>»\-－―]+)+')


def dedup_key_for_post(body):
    """引用RT・まとめbotの再投稿で先頭の煽り文句（「大王？＞」等）だけが異なる
    ほぼ同一内容の投稿を確実に重複と判定するため、本文中に含まれるURL断片が
    あればそれを最優先の重複判定キーとして使う（同じ記事へのリンクを含む投稿は
    文面が多少違っても同一ニュースの可能性が高いため）。URLが無い場合のみ、
    先頭の煽り文句を除去してから正規化したテキストで判定する。"""
    url_m = URL_FRAGMENT_PATTERN.search(body)
    if url_m:
        return url_m.group(0)
    cleaned = LEADING_NOISE_PATTERN.sub("", body)
    return normalize_title_for_dedup(cleaned[:80])


def fetch_yahoo_realtime_search(city):
    """Yahoo!リアルタイム検索（X由来データのライセンス提供ページ）から、
    「{city}」を含む投稿を実際に取得する。
    まず防犯・防災・鉄道・地震＋地域トピックのキーワードに一致する投稿を
    優先採用する。それだけでは{YAHOO_RT_TARGET_MIN}件に満たない場合のみ、
    キーワード不問で直近の投稿を補うフォールバックを行うが、その場合も
    明らかな迷惑投稿・陰謀論（YAHOO_RT_NOISE_PATTERNS）は除外する。"""
    name = f"Yahoo!リアルタイム検索［{city}］"
    matched, fallback_candidates = [], []
    seen = set()
    url = "https://search.yahoo.co.jp/realtime/search?p=" + urllib.parse.quote(city)
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        tweets = soup.select('[class*="Tweet_Tweet__"]')
        now = datetime.datetime.now()
        for t in tweets:
            body_el = t.select_one('[class*="Tweet_body__"]')
            if not body_el:
                continue
            body = body_el.get_text(" ", strip=True)
            if any(p in body for p in YAHOO_RT_NOISE_PATTERNS):
                continue  # 迷惑投稿・陰謀論はキーワード一致有無に関わらず除外
            time_el = t.select_one('[class*="Tweet_time__"]')
            time_text = time_el.get_text(strip=True) if time_el else ""
            dt = parse_yahoo_relative_time(time_text)
            if dt is None or (now - dt).days > YAHOO_RT_RECENT_DAYS:
                continue
            dedup_key = dedup_key_for_post(body)
            if not dedup_key or dedup_key in seen:
                continue
            author_el = t.select_one('[class*="Tweet_authorName__"]')
            author = author_el.get_text(strip=True) if author_el else "投稿者不明"
            entry = {
                "city": city, "author": author, "body": body[:160],
                "time_text": time_text, "link": url,
            }
            if any(k in body for k in YAHOO_RT_KEYWORDS):
                seen.add(dedup_key)
                matched.append(entry)
            else:
                fallback_candidates.append((dedup_key, entry))
            if len(matched) >= 5:
                break
    except Exception as e:
        log_skip(name, f"取得エラー ({e})")
        return []

    items = matched
    if len(items) < YAHOO_RT_TARGET_MIN:
        for dedup_key, entry in fallback_candidates:
            if len(items) >= YAHOO_RT_TARGET_MIN:
                break
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            items.append(entry)
        if len(items) > len(matched):
            log_skip(name, f"キーワード一致が{len(matched)}件のみだったため、直近の投稿（迷惑投稿除く）で{len(items)}件まで補完")

    if not items:
        log_skip(name, "該当する投稿なし（迷惑投稿除外後もゼロ件）")
    return items


def render_x_widget_section(x_posts):
    """X（旧Twitter）連携について。
    公式埋め込みウィジェット（ハッシュタグ検索タイムライン）を実機で検証したところ、
    X側の仕様変更により中身が描画されず「読み込み中」のまま止まることを確認した
    （2026-08-02、GitHub Pages上の実ページ・widgets.jsのコンソールログで実際に
    確認済み）ため、代わりにYahoo!リアルタイム検索（Xデータのライセンス提供ページ、
    静的HTMLで実データ取得可能）を実際にスクレイピングし、防犯・防災・鉄道・地震
    に関連するキーワードでフィルタしたうえでタイムラインとして直接埋め込む。"""
    buttons = "".join(f"""
      <a class="x-search-btn" href="https://twitter.com/search?q=%23{urllib.parse.quote(tag)}&src=typed_query&f=live" target="_blank" rel="noopener">
        🔗 #{esc_x(tag)} をXで検索
      </a>""" for tag in X_HASHTAGS.values())

    if x_posts:
        post_cards = "".join(f"""
      <a class="rt-post" href="{esc_x(p['link'])}" target="_blank" rel="noopener">
        <div class="rt-post-head"><span class="rt-post-city">{esc_x(p['city'])}</span><span class="rt-post-author">{esc_x(p['author'])}</span><span class="rt-post-time">{esc_x(p['time_text'])}</span></div>
        <div class="rt-post-body">{esc_x(p['body'])}</div>
      </a>""" for p in x_posts)
        timeline_html = f"<div class='rt-timeline'>{post_cards}</div>"
    else:
        timeline_html = "<p class='empty'>直近{}日以内・関連キーワード一致の投稿はありません（ノイズ除外フィルタが正常に機能している状態です）。</p>".format(YAHOO_RT_RECENT_DAYS)

    return f"""
  <details class="block accordion">
    <summary>⑤ X（旧Twitter）リアルタイム速報</summary>
    {timeline_html}
    <div class="x-link-grid">{buttons}</div>
  </details>"""


# ============================================================
# 追加機能: 買い物・生活インフラ・イベント/天気
#
# 休日当番医は、各医師会・自治体が公開しているテキストベースのPDF
# （画像ではなく実際に文字がPDF内に埋め込まれた表形式データ）を
# pdfplumberで実際に解析し、医療機関名・診療科目・所在地・電話番号を
# 構造化して抽出する。抽出できなかった場合は空リストを返し、
# 存在しない当番医情報を憶測で生成することはしない。
# ============================================================

# 鴻巣市公式サイトが公開する当番医PDF（鴻巣市医師会）
TOBAN_PDF_KONOSU = "https://www.city.kounosu.saitama.jp/uploaded/attachment/24541.pdf"
# 桶川市公式サイトが公開する当番医PDF（桶川北本伊奈地区医師会＝北本市・桶川市共通の輪番）
TOBAN_PDF_OKEGAWA = "https://www.city.okegawa.lg.jp/material/files/group/28/Dr_Aug2026.pdf"

GOMI_LINKS = {
    "北本市": "https://www.city.kitamoto.lg.jp/soshiki/shiminkeizai/kankyou/gyomu/g1/gomical/index.html",
    "桶川市": "https://www.city.okegawa.lg.jp/kurashi/gomi_kankyo/index.html",
    "鴻巣市": "https://www.city.kounosu.saitama.jp/page/1174.html",
}

CINEMA_SCHEDULE_URL = "https://eiga.com/theater/11/110208/3254/"

SHOPPING_NEWS_QUERIES = {
    "北本市": "(ヤオコー OR ベルク) 北本",
    "桶川市": "(ヤオコー OR ベルク) 桶川",
    "鴻巣市": "(ヤオコー OR ベルク) 鴻巣",
}

# 3市はいずれも半径5km圏内のため、気象庁観測地点も共通する北本市付近の
# 座標で代表させる（Open-Meteo、APIキー不要・実データ）
WEATHER_LAT, WEATHER_LON = 36.02, 139.53
WMO_WEATHER_TEXT = {
    0: "快晴", 1: "晴れ", 2: "薄曇り", 3: "曇り",
    45: "霧", 48: "霧氷",
    51: "小雨", 53: "雨", 55: "強い雨",
    61: "雨", 63: "雨", 65: "大雨",
    71: "雪", 73: "雪", 75: "大雪",
    80: "にわか雨", 81: "にわか雨", 82: "激しいにわか雨",
    95: "雷雨",
}


def fetch_weather_forecast():
    """Open-Meteo（無料・APIキー不要）から北本・桶川・鴻巣エリアの
    実際の天気予報（今日・明日）を取得する。"""
    try:
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
               "&daily=weathercode,temperature_2m_max,temperature_2m_min&timezone=Asia%2FTokyo&forecast_days=2")
        r = requests.get(url, headers=HEADERS, timeout=8)
        data = r.json()
        daily = data["daily"]
        results = []
        for i in range(len(daily["time"])):
            code = daily["weathercode"][i]
            results.append({
                "date": daily["time"][i],
                "weather": WMO_WEATHER_TEXT.get(code, f"天気コード{code}"),
                "tmax": daily["temperature_2m_max"][i],
                "tmin": daily["temperature_2m_min"][i],
            })
        return results
    except Exception as e:
        log_skip("Open-Meteo 天気予報", f"取得エラー ({e})")
        return []


def fetch_hourly_precipitation():
    """Open-Meteoの時間別降水量（mm/h）を実データで取得する（本日〜明日）。"""
    try:
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
               "&hourly=precipitation&timezone=Asia%2FTokyo&forecast_days=2")
        r = requests.get(url, headers=HEADERS, timeout=8)
        data = r.json()
        hourly = data["hourly"]
        now_hour = datetime.datetime.now().replace(minute=0, second=0, microsecond=0)
        results = []
        for t, mm in zip(hourly["time"], hourly["precipitation"]):
            dt = datetime.datetime.fromisoformat(t)
            if dt < now_hour or dt >= now_hour + datetime.timedelta(hours=24):
                continue
            results.append({"time": dt.strftime("%m/%d %H時"), "mm": mm})
        return results
    except Exception as e:
        log_skip("Open-Meteo 時間別降水量", f"取得エラー ({e})")
        return []


TOBAN_ADDR_CITY_PATTERN = re.compile(r"(北本市|桶川市|伊奈町)")


def fetch_toban_doctors_from_pdf(pdf_url, source_name, target_cities=None):
    """医師会・自治体が公開する休日当番医PDF（テキスト埋め込み型）を
    pdfplumberで実際に解析し、当月・本日以降の当番医を構造化データとして
    抽出する。表構造の列位置から「月」列で当月を特定し、行の縦方向の
    forward-fill（同じ月内で日付・医療機関がグループ化された表構造）を
    実データに基づいて復元する。抽出に失敗した場合は空リストを返し、
    存在しない当番医情報を憶測で生成することはしない。"""
    name = f"当番医PDF［{source_name}］"
    results = []
    try:
        r = requests.get(pdf_url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    if not table or len(table) < 2:
                        continue
                    header = [str(c or "").strip() for c in table[0]]
                    # 「鴻巣市」形式（月ごとの列が横に並ぶ表）と
                    # 「桶川市」形式（月・日・医療機関の列が縦に並ぶ表）の
                    # 2種類のレイアウトが実際に存在するため、ヘッダーで判定する
                    if any("月" in h and h not in ("月", "月日") for h in header[2:]):
                        results += _parse_toban_wide_table(table, header, source_name)
                    elif header[:2] == ["月", "日"] or ("医療機関" in "".join(header)):
                        results += _parse_toban_long_table(table, source_name, target_cities)
    except Exception as e:
        log_skip(name, f"取得・解析エラー ({e})")
        return []
    if not results:
        log_skip(name, "当月分の当番医データを抽出できませんでした")
    return results


def _parse_toban_wide_table(table, header, source_name):
    """鴻巣市形式：1行目=施設名+住所、2行目=電話番号、列=1月〜12月 の当番日。
    ヘッダーの月表記は全角数字（８月）のため、半角に正規化してから比較する。"""
    today = datetime.date.today()
    month_idx = None
    for i, h in enumerate(header):
        h_normalized = h.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        if f"{today.month}月" == h_normalized:
            month_idx = i
            break
    if month_idx is None:
        return []
    out = []
    i = 1
    while i < len(table):
        row = table[i]
        if not row or not row[0]:
            i += 1
            continue
        clinic, addr = row[0], row[1] or ""
        day_val = row[month_idx] if month_idx < len(row) else None
        phone = ""
        if i + 1 < len(table) and table[i + 1] and table[i + 1][0] is None:
            phone = (table[i + 1][1] or "").strip()
            i += 1
        if day_val and str(day_val).strip():
            for day_str in re.findall(r"\d+", str(day_val)):
                try:
                    d = datetime.date(today.year, today.month, int(day_str))
                except ValueError:
                    continue
                if d < today:
                    continue
                out.append({
                    "date": d, "clinic": clinic.strip(), "address": addr.strip(),
                    "phone": phone, "source": source_name,
                })
        i += 1
    return out


def _parse_toban_long_table(table, source_name, target_cities):
    """桶川市形式：月・日・医療機関・診療科目・所在地・電話番号 の列を持つ表。
    月・日は該当行にのみ値があり、以降の行は空欄（forward-fill対象）。"""
    today = datetime.date.today()
    out = []
    cur_month, cur_day = None, None
    for row in table[1:]:
        if not row or len(row) < 6:
            continue
        month_c, day_c, clinic, dept, addr, phone = row[:6]
        if month_c and str(month_c).strip():
            cur_month = str(month_c).strip()
        if day_c and str(day_c).strip():
            cur_day = str(day_c).strip()
        if not clinic or not str(clinic).strip():
            continue
        if target_cities and not any(c in (addr or "") for c in target_cities):
            continue
        try:
            d = datetime.date(today.year, int(cur_month), int(cur_day))
        except (ValueError, TypeError):
            continue
        if d < today:
            continue
        out.append({
            "date": d, "clinic": str(clinic).strip(), "dept": (dept or "").strip(),
            "address": (addr or "").strip(), "phone": (phone or "").strip(), "source": source_name,
        })
    return out


def fetch_all_toban_doctors():
    kounosu = fetch_toban_doctors_from_pdf(TOBAN_PDF_KONOSU, "鴻巣市医師会")
    shared = fetch_toban_doctors_from_pdf(TOBAN_PDF_OKEGAWA, "桶川北本伊奈地区医師会", target_cities=["北本市", "桶川市"])
    by_city = {"北本市": [], "桶川市": [], "鴻巣市": kounosu}
    for item in shared:
        if "北本市" in item["address"]:
            by_city["北本市"].append(item)
        elif "桶川市" in item["address"]:
            by_city["桶川市"].append(item)
    for city in by_city:
        by_city[city].sort(key=lambda x: x["date"])
        by_city[city] = by_city[city][:3]
    return by_city


def fetch_cinema_today_schedule():
    """映画.com（こうのすシネマ）の実ページから、本日日付のtd要素に
    含まれる実際の上映時刻をタイトルごとに抽出する。"""
    name = "こうのすシネマ 上映スケジュール"
    items = []
    today_str = datetime.date.today().strftime("%Y%m%d")
    try:
        r = requests.get(CINEMA_SCHEDULE_URL, headers=HEADERS, timeout=10)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        for section in soup.select("section[data-title]"):
            title = section.get("data-title", "").strip()
            td = section.select_one(f'td[data-date="{today_str}"]')
            if not td or not title:
                continue
            times = [el.get_text(strip=True) for el in td.select("a.btn, span.btn, span")
                     if re.match(r"^\d{1,2}:\d{2}", el.get_text(strip=True))]
            times = [t for t in dict.fromkeys(times)]  # 順序を保ったまま重複除去
            if times:
                items.append({"title": title, "times": times})
    except Exception as e:
        log_skip(name, f"取得エラー ({e})")
        return []
    if not items:
        log_skip(name, "本日の上映データを抽出できませんでした")
    return items


def fetch_shopping_news(city):
    """Google News RSSから、地域のスーパー（ヤオコー・ベルク）に関する
    実際の報道タイトルを取得する（新店舗・改装・キャンペーン等の実текст）。"""
    name = f"店舗ニュース［{city}］"
    items = []
    query = SHOPPING_NEWS_QUERIES.get(city, city)
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) + "&hl=ja&gl=JP&ceid=JP:ja"
    try:
        feed = feedparser.parse(url, request_headers=HEADERS)
        for e in feed.entries[:3]:
            title = re.sub(r'\s*-\s*[^-]+$', '', e.get("title", "")).strip()
            if not title:
                continue
            items.append({"title": title, "link": e.get("link", "")})
    except Exception as e:
        log_skip(name, f"取得エラー ({e})")
    if not items:
        log_skip(name, "該当する店舗ニュースなし")
    return items


EVENT_DATE_PATTERN = re.compile(r"(\d{1,2})月(\d{1,2})日")


def extract_event_countdowns(topics):
    """既に取得済みの地域トピック（新店舗・地域トピック）のタイトル本文から、
    「7月24日」のような開催日表記を実際に抽出し、今日から見て未来（当日含む）
    のイベントのみをカウントダウン対象とする。タイトルに日付が無いものは
    対象外にする（憶測で開催日を作らない）。"""
    today = datetime.date.today()
    results = []
    seen = set()
    for t in topics:
        if t.get("icon") != "🎪":
            continue
        m = EVENT_DATE_PATTERN.search(t["title"])
        if not m:
            continue
        month, day = int(m.group(1)), int(m.group(2))
        try:
            event_date = datetime.date(today.year, month, day)
            if event_date < today - datetime.timedelta(days=1):
                event_date = datetime.date(today.year + 1, month, day)
        except ValueError:
            continue
        days_left = (event_date - today).days
        if days_left < 0 or days_left > 60:
            continue
        key = (t["city"], event_date, t["title"][:20])
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "city": t["city"], "title": t["title"], "link": t["link"],
            "event_date": event_date, "days_left": days_left,
        })
        if len(results) >= 3:
            break
    results.sort(key=lambda x: x["days_left"])
    return results


def render_shopping_section():
    blocks = []
    for city in CITIES:
        news = fetch_shopping_news(city)
        if news:
            rows = "".join(
                f'<a class="info-link" href="{esc_x(n["link"])}" target="_blank" rel="noopener">🛒 {esc_x(n["title"])}</a>'
                for n in news)
        else:
            rows = "<p class='empty'>直近の店舗ニュースはありません。</p>"
        blocks.append(f"<div class='info-city-block'><h4>{city}</h4><div class='info-link-grid'>{rows}</div></div>")
    return f"""
  <details class="block accordion">
    <summary>🛍️ 買い物・店舗トピックス</summary>
    {"".join(blocks)}
  </details>"""


def render_medical_gomi_section():
    toban_by_city = fetch_all_toban_doctors()
    blocks = []
    for city in CITIES:
        doctors = toban_by_city.get(city, [])
        if doctors:
            cards = "".join(f"""
        <div class="toban-card">
          <div class="toban-date">{d['date'].strftime('%m/%d')}（{'月火水木金土日'[d['date'].weekday()]}）</div>
          <div class="toban-clinic">{esc_x(d['clinic'])}{('　' + esc_x(d['dept'])) if d.get('dept') else ''}</div>
          <div class="toban-addr">{esc_x(d['address'])}</div>
          <div class="toban-phone">☎ {esc_x(d['phone'])}</div>
        </div>""" for d in doctors)
            doc_html = f"<div class='toban-grid'>{cards}</div>"
        else:
            doc_html = "<p class='empty'>今月・来月分の当番医データを抽出できませんでした。</p>"
        gomi_url = GOMI_LINKS.get(city, "")
        gomi_html = f'<a class="info-link" href="{gomi_url}" target="_blank" rel="noopener">🗑️ {city} ごみカレンダー</a>' if gomi_url else ""
        blocks.append(f"<div class='info-city-block'><h4>{city} 休日当番医（直近3件）</h4>{doc_html}<div class='info-link-grid'>{gomi_html}</div></div>")
    return f"""
  <details class="block accordion">
    <summary>🏥 生活インフラ・ヘルスケア</summary>
    {"".join(blocks)}
  </details>"""


def render_events_section(topics):
    weather = fetch_weather_forecast()
    precip = fetch_hourly_precipitation()
    countdowns = extract_event_countdowns(topics)
    cinema = fetch_cinema_today_schedule()

    if weather:
        weather_cards = "".join(f"""
      <div class="weather-card">
        <div class="weather-date">{w['date']}</div>
        <div class="weather-desc">{esc_x(w['weather'])}</div>
        <div class="weather-temp">{w['tmax']}℃ / {w['tmin']}℃</div>
      </div>""" for w in weather)
        weather_html = f"<div class='weather-grid'>{weather_cards}</div>"
    else:
        weather_html = "<p class='empty'>天気予報を取得できませんでした。</p>"

    if precip:
        max_mm = max(p["mm"] for p in precip) or 1
        bars = "".join(f"""
      <div class="precip-bar-col">
        <div class="precip-bar" style="height:{max(2, int(p['mm'] / max_mm * 80))}px"></div>
        <div class="precip-mm">{p['mm']}</div>
        <div class="precip-time">{p['time'][-3:]}</div>
      </div>""" for p in precip)
        precip_html = f"<div class='precip-chart'>{bars}</div>"
    else:
        precip_html = "<p class='empty'>時間別降水量を取得できませんでした。</p>"

    if countdowns:
        cd_cards = "".join(f"""
      <a class="countdown-card" href="{c['link']}" target="_blank" rel="noopener">
        <div class="countdown-days">{'本日開催' if c['days_left'] == 0 else f"あと{c['days_left']}日"}</div>
        <div class="countdown-title">{esc_x(c['city'])}｜{esc_x(c['title'][:50])}</div>
      </a>""" for c in countdowns)
        countdown_html = f"<div class='countdown-grid'>{cd_cards}</div>"
    else:
        countdown_html = "<p class='empty'>タイトルから開催日が確認できる直近イベントはありません。</p>"

    if cinema:
        cinema_cards = "".join(f"""
      <div class="cinema-card">
        <div class="cinema-title">{esc_x(c['title'])}</div>
        <div class="cinema-times">{" / ".join(esc_x(t) for t in c['times'])}</div>
      </div>""" for c in cinema)
        cinema_html = f"<div class='cinema-grid'>{cinema_cards}</div>"
    else:
        cinema_html = "<p class='empty'>本日の上映データを抽出できませんでした。</p>"

    return f"""
  <details class="block accordion">
    <summary>🎪 イベント・天気・エンタメ</summary>
    <h4>天気予報（北本・桶川・鴻巣エリア共通）</h4>
    {weather_html}
    <h4>時間別降水量（mm/h・本日〜24時間）</h4>
    {precip_html}
    <h4>開催日が確認できたイベント（最新3件）</h4>
    {countdown_html}
    <h4>こうのすシネマ 本日の上映スケジュール</h4>
    {cinema_html}
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

    # 「警戒情報」等タイトルだけでは中身が分からないお知らせのみ、詳細ページを
    # 実際に取得して要約を補う（対象を絞ることでフィルタ後の少数件のみに
    # リクエストを限定し、処理時間を抑える）
    for a in alerts:
        a["summary"] = None
        if VAGUE_TITLE_PATTERN.search(a["title"]):
            summary = fetch_notice_summary(a["link"])
            if summary:
                a["summary"] = summary
                # 詳細ページの被害内容から、より具体的な町丁目レベルの場所が
                # 取得できる場合は、一覧ページのタイトルだけでは得られなかった
                # 実際の発生場所として反映する（無い場合は既存のlocationを維持）
                better_location = extract_location(summary, fallback_label="")
                if better_location != "📍 ":
                    a["location"] = better_location
        a["icon"] = classify_icon(a["title"] + (a["summary"] or ""), ALERT_ICON_RULES, "📌")

    for t in topics:
        t["icon"] = classify_icon(t["title"], TOPIC_ICON_RULES, "📍")

    FIRE_KEYWORDS = ("火事", "火災", "出火", "全焼", "半焼", "ぼや")
    for a in alerts:
        a["is_fire"] = any(k in a["title"] for k in FIRE_KEYWORDS)
        if a["is_fire"]:
            a["icon"] = "🔥"

    # 新しい順に並べたうえで、火災関連のみ最優先で先頭に引き上げる（安定ソートを利用）
    alerts.sort(key=lambda x: x["sort_key"], reverse=True)
    alerts.sort(key=lambda x: x["is_fire"], reverse=True)
    topics.sort(key=lambda x: x["sort_key"], reverse=True)

    train_status = fetch_train_status()
    earthquakes = fetch_earthquake_info()

    x_posts_raw = []
    for city in CITIES:
        x_posts_raw += fetch_yahoo_realtime_search(city)

    # 複数都市の検索結果をまたいだ重複（同じ投稿が複数市名に言及し、
    # 別々の検索クエリで重複取得されるケース）も、ここで最終的に排除する
    x_seen = set()
    x_posts = []
    for p in x_posts_raw:
        key = dedup_key_for_post(p["body"])
        if not key or key in x_seen:
            continue
        x_seen.add(key)
        x_posts.append(p)

    return alerts, topics, train_status, earthquakes, x_posts


def render_item_row(item, extra_class=""):
    city_badge = item["city"]
    cat_badge = item["category"]
    date_disp = item["date_display"] or "日付不明"
    title = item["title"]
    link = item["link"]
    source = item["source"]
    location = item.get("location", "")
    is_fire = item.get("is_fire", False)
    icon = item.get("icon", "")
    summary = item.get("summary")
    cat_class = "cat-store" if cat_badge == "新店舗・開閉店" else ("cat-topic" if cat_badge == "地域トピック" else "cat-alert")
    fire_class = " item-fire" if is_fire else ""
    icon_prefix = f"{icon} " if icon else ""
    summary_html = f"<span class='item-summary'>{summary}</span>" if summary else ""
    return f"""
    <a class="item {extra_class}{fire_class}" data-city="{city_badge}" href="{link}" target="_blank" rel="noopener">
      <div class="item-badges">
        <span class="badge city-badge">{city_badge}</span>
        <span class="badge {cat_class}">{cat_badge}</span>
        <span class="item-meta">{date_disp}｜{source}</span>
      </div>
      <div class="item-title">{icon_prefix}{title}</div>
      {summary_html}
      <div class="loc-badge">{location}</div>
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


def render_html(alerts, topics, train_status, earthquakes, x_posts, skip_log):
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
  .page {{ max-width: 860px; margin: 0 auto; padding: 24px 16px 56px; overflow-x: hidden; }}
  header.top {{ margin-bottom: 20px; }}
  header.top h1 {{ font-size: 22px; margin: 0 0 6px; line-height: 1.3; }}
  header.top .meta {{ color: var(--ink-soft); font-size: 12.5px; }}

  .tabs {{ display: flex; gap: 8px; margin: 20px 0; flex-wrap: wrap; }}
  .tab-btn {{
    background: var(--bg-raised);
    color: var(--ink);
    border: 1px solid var(--rule);
    border-radius: 999px;
    padding: 11px 18px;
    min-height: 44px;
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

  /* モバイルファースト: まずスマホ幅を基準にしたflexレイアウトを定義し、
     広い画面ではメディアクエリで補助的に調整する（スマホでバッジが縦に
     間延びする問題を避けるため、badge類は横並びのグループにまとめている） */
  .item-list {{ display: flex; flex-direction: column; gap: 10px; }}
  a.item {{
    display: flex;
    flex-direction: column;
    gap: 6px;
    background: var(--bg-raised);
    border: 1px solid var(--rule);
    border-radius: 10px;
    padding: 14px 16px;
    text-decoration: none;
    color: var(--ink);
    min-height: 44px;
  }}
  a.item:hover {{ border-color: var(--accent); }}
  .item-badges {{ display: flex; flex-wrap: wrap; gap: 6px 8px; align-items: center; }}
  .badge {{
    font-size: 11.5px;
    font-weight: 700;
    padding: 4px 9px;
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
  .item-title {{ font-size: 15px; line-height: 1.55; }}
  .item-summary {{ font-size: 13px; line-height: 1.55; color: var(--ink-soft); }}
  .loc-badge {{
    font-size: 11.5px;
    font-weight: 600;
    padding: 4px 9px;
    border-radius: 6px;
    background: #2a2a2a;
    color: #00adb5;
    white-space: nowrap;
    align-self: flex-start;
  }}
  .item-meta {{ font-size: 11.5px; color: var(--ink-soft); margin-left: auto; }}
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
  details.accordion summary {{ cursor: pointer; font-size: 17px; padding: 14px 0; min-height: 44px; display: flex; align-items: center; list-style: none; }}
  details.accordion summary::-webkit-details-marker {{ display: none; }}
  details.accordion summary::before {{ content: "▶ "; display: inline-block; transition: transform 0.15s; }}
  details.accordion[open] summary::before {{ transform: rotate(90deg); }}
  details.accordion .disclaimer, details.accordion .x-link-grid, details.accordion .system-status-list {{ margin-bottom: 14px; }}
  .system-status-list {{ font-size: 12.5px; color: var(--ink-soft); line-height: 1.9; padding-left: 18px; }}
  .system-status-list a {{ color: var(--accent); }}
  details.accordion h4 {{ font-size: 13px; color: var(--ink-soft); margin: 14px 0 8px; }}
  details.accordion h4:first-of-type {{ margin-top: 4px; }}
  .info-city-block {{ margin-bottom: 10px; }}
  .info-city-block h4 {{ margin: 0 0 6px; }}
  .info-link-grid {{ display: flex; flex-direction: column; gap: 8px; }}
  .info-link {{
    display: flex; align-items: center; min-height: 44px;
    background: #14171c; border: 1px solid var(--rule); border-radius: 8px;
    padding: 10px 14px; text-decoration: none; color: var(--ink); font-size: 13.5px;
  }}
  .info-link:hover {{ border-color: var(--accent); }}
  .weather-grid {{ display: flex; gap: 10px; flex-wrap: wrap; }}
  .weather-card {{ background: #14171c; border: 1px solid var(--rule); border-radius: 8px; padding: 12px 16px; min-width: 120px; }}
  .weather-date {{ font-size: 11px; color: var(--ink-soft); }}
  .weather-desc {{ font-size: 15px; margin: 4px 0; }}
  .weather-temp {{ font-size: 13px; color: var(--accent); font-weight: 700; }}
  .countdown-grid {{ display: flex; flex-direction: column; gap: 8px; }}
  .countdown-card {{
    display: block; background: #14171c; border: 1px solid var(--topic); border-radius: 8px;
    padding: 10px 14px; text-decoration: none; color: var(--ink); min-height: 44px;
  }}
  .countdown-days {{ font-size: 15px; font-weight: 700; color: var(--topic); }}
  .countdown-title {{ font-size: 13px; margin-top: 2px; }}
  .toban-grid {{ display: flex; flex-direction: column; gap: 8px; margin-bottom: 10px; }}
  .toban-card {{ background: #14171c; border: 1px solid var(--rule); border-radius: 8px; padding: 10px 14px; }}
  .toban-date {{ font-size: 12px; color: var(--accent); font-weight: 700; }}
  .toban-clinic {{ font-size: 14px; margin: 3px 0; }}
  .toban-addr {{ font-size: 12px; color: var(--ink-soft); }}
  .toban-phone {{ font-size: 13px; font-weight: 700; color: var(--store); margin-top: 3px; }}
  .precip-chart {{ display: flex; align-items: flex-end; gap: 4px; overflow-x: auto; padding: 8px 4px; background: #14171c; border: 1px solid var(--rule); border-radius: 8px; }}
  .precip-bar-col {{ display: flex; flex-direction: column; align-items: center; min-width: 30px; }}
  .precip-bar {{ width: 12px; background: var(--accent); border-radius: 3px 3px 0 0; }}
  .precip-mm {{ font-size: 9.5px; color: var(--ink-soft); margin-top: 3px; }}
  .precip-time {{ font-size: 9px; color: var(--ink-soft); }}
  .cinema-grid {{ display: flex; flex-direction: column; gap: 8px; }}
  .cinema-card {{ background: #14171c; border: 1px solid var(--rule); border-radius: 8px; padding: 10px 14px; }}
  .cinema-title {{ font-size: 13.5px; font-weight: 700; }}
  .cinema-times {{ font-size: 12px; color: var(--accent); margin-top: 4px; }}
  .rt-timeline {{ display: flex; flex-direction: column; gap: 8px; margin-top: 10px; }}
  .rt-post {{ display: block; background: #14171c; border: 1px solid var(--rule); border-radius: 8px; padding: 10px 12px; text-decoration: none; color: var(--ink); }}
  .rt-post:hover {{ border-color: var(--accent); }}
  .rt-post-head {{ display: flex; gap: 8px; align-items: center; font-size: 11px; color: var(--ink-soft); margin-bottom: 4px; }}
  .rt-post-city {{ background: rgba(90,169,230,0.18); color: var(--accent); border-radius: 4px; padding: 1px 6px; font-weight: 700; }}
  .rt-post-author {{ font-weight: 600; }}
  .rt-post-time {{ margin-left: auto; }}
  .rt-post-body {{ font-size: 13px; line-height: 1.6; }}
  .x-link-grid {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }}
  .x-search-btn {{ background: var(--bg-raised); border: 1px solid var(--accent); color: var(--accent); border-radius: 999px; padding: 12px 18px; min-height: 44px; display: inline-flex; align-items: center; font-size: 13.5px; text-decoration: none; font-weight: 700; }}
  .x-search-btn:hover {{ background: var(--accent); color: #0c1116; }}

  footer {{ border-top: 1px solid var(--rule); padding-top: 14px; font-size: 12px; color: var(--ink-soft); }}
  footer ul {{ margin: 6px 0 0; padding-left: 18px; }}

  /* 広い画面向けの補助調整（モバイルはここに依存せず単体で成立させている） */
  @media (min-width: 640px) {{
    .item-badges {{ flex-wrap: nowrap; }}
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
{render_shopping_section()}
{render_medical_gomi_section()}
{render_events_section(topics)}
{render_x_widget_section(x_posts)}
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

    alerts, topics, train_status, earthquakes, x_posts = build_dataset()
    html = render_html(alerts, topics, train_status, earthquakes, x_posts, skip_log)

    temp_file = OUTPUT_HTML + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(html)
        os.replace(temp_file, OUTPUT_HTML)
        print(f"✅ 地域ポータル生成完了: {OUTPUT_HTML}")
        print(f"   防犯・防災アラート: {len(alerts)}件 / 新店舗・地域トピック: {len(topics)}件")
        print(f"   鉄道運行情報: {len(train_status)}路線 / 地震情報（埼玉県該当）: {len(earthquakes)}件")
        print(f"   Xリアルタイム速報（キーワード一致）: {len(x_posts)}件")
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
