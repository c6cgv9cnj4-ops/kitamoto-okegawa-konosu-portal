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
    """X（旧Twitter）公式埋め込みウィジェットの設置。
    ※ サーバー側では中身を取得・検証できない（X公式の埋め込みJSが
    閲覧者のブラウザ上で都度読み込む方式のため）。表示される投稿の
    信頼性・最新性はX側の仕様に依存し、当方では保証できない旨を明記する。"""
    cards = "".join(f"""
      <div class="x-widget-card">
        <h4>#{tag}</h4>
        <a class="twitter-timeline" data-height="400" data-theme="dark"
           href="https://twitter.com/search?q=%23{urllib.parse.quote(tag)}&src=typed_query">
           #{esc_x(tag)} のポストを読み込み中…
        </a>
      </div>""" for tag in X_HASHTAGS.values())
    return f"""
  <section class="block">
    <h2>③ X（旧Twitter）速報ウィジェット <span class="unverified-tag">動作保証外</span></h2>
    <p class="disclaimer">X公式の埋め込み機能をそのまま設置しています。表示内容はX側が
      その都度返すものであり、当方のスクリプトでは中身を取得・検証していません。
      デマ・不確実な投稿が含まれる可能性がある点をご了承のうえご覧ください。
      うまく表示されない場合はX側の仕様変更が原因の可能性があります。</p>
    <div class="x-widget-grid">{cards}</div>
  </section>
  <script async src="https://platform.twitter.com/widgets.js" charset="utf-8"></script>"""


def esc_x(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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

    alerts.sort(key=lambda x: x["sort_key"], reverse=True)
    topics.sort(key=lambda x: x["sort_key"], reverse=True)
    return alerts, topics


def render_item_row(item, extra_class=""):
    city_badge = item["city"]
    cat_badge = item["category"]
    date_disp = item["date_display"] or "日付不明"
    title = item["title"]
    link = item["link"]
    source = item["source"]
    location = item.get("location", "")
    cat_class = "cat-store" if cat_badge == "新店舗・開閉店" else ("cat-topic" if cat_badge == "地域トピック" else "cat-alert")
    return f"""
    <a class="item {extra_class}" data-city="{city_badge}" href="{link}" target="_blank" rel="noopener">
      <span class="badge city-badge">{city_badge}</span>
      <span class="badge {cat_class}">{cat_badge}</span>
      <span class="item-title">{title}</span>
      <span class="loc-badge">{location}</span>
      <span class="item-meta">{date_disp}｜{source}</span>
    </a>"""


def render_html(alerts, topics, skip_log):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

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

  .unverified-tag {{ font-size: 11px; font-weight: 700; color: var(--alert); border: 1px solid var(--alert); border-radius: 6px; padding: 2px 8px; vertical-align: middle; margin-left: 8px; }}
  .disclaimer {{ font-size: 12.5px; color: var(--ink-soft); background: var(--bg-raised); border: 1px solid var(--rule); border-radius: 8px; padding: 10px 14px; }}
  .x-widget-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; margin-top: 12px; }}
  .x-widget-card {{ background: var(--bg-raised); border: 1px solid var(--rule); border-radius: 8px; padding: 12px; }}
  .x-widget-card h4 {{ margin: 0 0 8px; font-size: 14px; color: var(--accent); }}

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

  <div class="tabs">
    <button class="tab-btn active" data-target="全体">全体</button>
    <button class="tab-btn" data-target="北本市">北本市</button>
    <button class="tab-btn" data-target="桶川市">桶川市</button>
    <button class="tab-btn" data-target="鴻巣市">鴻巣市</button>
  </div>

  <section class="block">
    <h2>① 緊急・防犯防災アラート</h2>
    <div class="item-list" id="alert-list">{alert_rows}</div>
  </section>

  <section class="block" id="topics-block">
    <h2>② エリア新店舗・地域トピック</h2>
    {topics_html}
  </section>
{render_x_widget_section()}

  <footer>
    データ取得状況（スキップログ）:
    <ul>{skip_html}</ul>
    <p>※ 消防出動情報はライブ配信元がJavaScript動的読み込みのため、静的スクレイピングでは取得できず、消防本部の公式お知らせを代替表示しています。</p>
    <p>※ 北本市・桶川市・鴻巣市の公式サイトはJSタブ構造のため新着情報を直接取得できず、Google Newsの検索結果（直近{GOOGLE_NEWS_RECENT_DAYS}日以内のみ）で代替しています。NHK埼玉のRSS配信は廃止済みのため対象外です。</p>
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

    alerts, topics = build_dataset()
    html = render_html(alerts, topics, skip_log)

    temp_file = OUTPUT_HTML + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(html)
        os.replace(temp_file, OUTPUT_HTML)
        print(f"✅ 地域ポータル生成完了: {OUTPUT_HTML}")
        print(f"   防犯・防災アラート: {len(alerts)}件 / 新店舗・地域トピック: {len(topics)}件")
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
