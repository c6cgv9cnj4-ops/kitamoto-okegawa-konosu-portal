import os
import re
import time
import datetime
import requests
import feedparser
from bs4 import BeautifulSoup

# ============================================================
# 主要4紙（日経・読売・朝日・毎日）当日見出し比較ポータル
#
# 【スコープについて（合意済み）】
# 「社説・論調・スタンスの分析」は無料で読める範囲では十分な情報量が
# 得られず、憶測での性格付けになるリスクがあるため対象外とする。
# 実装するのは「同一トピックをどの紙がどう報じているか」という
# 事実ベースの比較（見出し・切り口・掲載順の違い）のみ。
# ============================================================

OUTPUT_DIR = "00_Knowledge"
OUTPUT_HTML = os.path.join(OUTPUT_DIR, "newspaper_portal_index.html")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

NIKKEI_RSS = "https://assets.wor.jp/rss/rdf/nikkei/news.rdf"
YAHOO_MEDIA = {"読売新聞": "yom", "朝日新聞": "asahi", "毎日新聞": "mai"}

skip_log = []


def log_skip(source, reason):
    skip_log.append(f"{source}: {reason}")
    print(f"⚠️  スキップ - {source}: {reason}")


# ------------------------------------------------------------
# 1. 当日見出しの取得（4紙とも実データ・厳密に本日の日付のみ）
# ------------------------------------------------------------
RECENT_WINDOW_HOURS = 20


def fetch_nikkei_today(limit=25):
    items = []
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=RECENT_WINDOW_HOURS)
    try:
        feed = feedparser.parse(NIKKEI_RSS, request_headers=HEADERS)
        status = getattr(feed, "status", None)
        if status is not None and status >= 400:
            log_skip("日経新聞", f"HTTP {status}")
            return items
        for e in feed.entries:
            date_str = e.get("date") or e.get("published")
            try:
                dt = datetime.datetime.fromisoformat(date_str) if date_str else None
            except ValueError:
                dt = None
            if not dt or dt.replace(tzinfo=None) < cutoff:
                continue
            items.append({"title": e.get("title", "").strip(), "link": e.get("link", ""), "time": dt.strftime("%m/%d %H:%M")})
            if len(items) >= limit:
                break
    except Exception as e:
        log_skip("日経新聞", f"取得エラー ({e})")
    if not items:
        log_skip("日経新聞", f"直近{RECENT_WINDOW_HOURS}時間以内の記事が見つかりませんでした")
    return items


def fetch_yahoo_media_today(name, media_code, limit=25):
    items = []
    now = datetime.datetime.now()
    cutoff = now - datetime.timedelta(hours=RECENT_WINDOW_HOURS)
    try:
        r = requests.get(f"https://news.yahoo.co.jp/media/{media_code}", headers=HEADERS, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        pattern = re.compile(r"^(.*?)\s*(\d{1,2})/(\d{1,2})\(.\)\s*(\d{1,2}):(\d{2})$")
        links = soup.select("a[href*='/articles/']")
        seen = set()
        for a in links:
            text = a.get_text(" ", strip=True)
            m = pattern.match(text)
            if not m:
                continue
            title, month, day, hh, mm = m.group(1).strip(), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
            try:
                dt = datetime.datetime(now.year, month, day, hh, mm)
                if dt > now + datetime.timedelta(hours=1):
                    dt = datetime.datetime(now.year - 1, month, day, hh, mm)
            except ValueError:
                continue
            href = a.get("href", "")
            if dt < cutoff or not title or href in seen:
                continue
            seen.add(href)
            items.append({"title": title, "link": href, "time": dt.strftime("%m/%d %H:%M")})
            if len(items) >= limit:
                break
    except Exception as e:
        log_skip(name, f"取得エラー ({e})")
        return items
    if not items:
        log_skip(name, f"直近{RECENT_WINDOW_HOURS}時間以内の記事が見つかりませんでした")
    return items


def build_all_headlines():
    return {
        "日経新聞": fetch_nikkei_today(),
        **{name: fetch_yahoo_media_today(name, code) for name, code in YAHOO_MEDIA.items()},
    }


# ------------------------------------------------------------
# 2. 同一トピックのクラスタリング（キーワード一致による事実ベースの突合）
# ------------------------------------------------------------
KATAKANA_PATTERN = re.compile(r"[ァ-ヴー]{3,}")
KANJI_ENTITY_PATTERN = re.compile(r"[一-龥]{2,8}(?:省|庁|市|県|銀行|証券|自動車|グループ|ホールディングス|大学|病院)")
STOPWORDS = {"ニュース", "速報", "詳報"}


def extract_topic_keywords(title):
    candidates = set()
    for pattern in (KATAKANA_PATTERN, KANJI_ENTITY_PATTERN):
        for m in pattern.finditer(title):
            token = m.group(0)
            if token not in STOPWORDS and len(token) >= 3:
                candidates.add(token)
    return candidates


def cluster_shared_topics(all_headlines):
    """4紙のうち2紙以上が同じキーワードを含む記事を報じている場合、同一トピックとしてまとめる"""
    tagged = []
    for paper, items in all_headlines.items():
        for item in items:
            kws = extract_topic_keywords(item["title"])
            tagged.append({"paper": paper, "item": item, "keywords": kws})

    clusters = []
    used = set()
    for i, t1 in enumerate(tagged):
        if i in used or not t1["keywords"]:
            continue
        group = [t1]
        used.add(i)
        for j, t2 in enumerate(tagged):
            if j in used or j == i or t2["paper"] == t1["paper"]:
                continue
            if t1["keywords"] & t2["keywords"]:
                group.append(t2)
                used.add(j)
        papers_in_group = {g["paper"] for g in group}
        if len(papers_in_group) >= 2:
            shared_kw = set.intersection(*[g["keywords"] for g in group]) or group[0]["keywords"]
            clusters.append({"keyword": "・".join(list(shared_kw)[:2]), "entries": group})

    # 単独トピック（他紙と一致しなかったもの）
    solo = [t for i, t in enumerate(tagged) if i not in used]
    return clusters, solo


def fetch_article_summary(url):
    """記事本文は転載せず、各社が共有用に公開しているog:description
    （通常1〜2文の短い要約）のみを取得する。全文取得・転載は行わない。
    取得できない場合はNoneを返し、憶測で埋めない。"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        soup = BeautifulSoup(r.text, "html.parser")
        tag = soup.select_one('meta[property="og:description"]') or soup.select_one('meta[name="description"]')
        if not tag:
            return None
        desc = tag.get("content", "").strip()
        if not desc:
            return None
        return desc[:85] + ("…" if len(desc) > 85 else "")
    except Exception:
        return None


TOPIC_DISPLAY_LIMIT = 7


def build_dataset():
    all_headlines = build_all_headlines()
    clusters, solo = cluster_shared_topics(all_headlines)

    # 「同一トピックは1カードに集約」の方針のため、クラスタは代表1紙のみを
    # 採用する（掲載時刻が最も新しいものを代表とする）。単独トピックは
    # クラスタで採用されなかった分から新しい順に補充し、合計を
    # TOPIC_DISPLAY_LIMIT件までに絞る。
    topics = []
    for c in clusters:
        rep = max(c["entries"], key=lambda g: g["item"]["time"])
        topics.append({
            "title": rep["item"]["title"], "link": rep["item"]["link"],
            "time": rep["item"]["time"], "paper": rep["paper"],
            "shared_with": sorted({g["paper"] for g in c["entries"]} - {rep["paper"]}),
        })
    solo_sorted = sorted(solo, key=lambda t: t["item"]["time"], reverse=True)
    for t in solo_sorted:
        if len(topics) >= TOPIC_DISPLAY_LIMIT:
            break
        topics.append({
            "title": t["item"]["title"], "link": t["item"]["link"],
            "time": t["item"]["time"], "paper": t["paper"], "shared_with": [],
        })
    topics = topics[:TOPIC_DISPLAY_LIMIT]

    for t in topics:
        t["summary"] = fetch_article_summary(t["link"])

    return all_headlines, topics


# ------------------------------------------------------------
# 3. HTML生成
# ------------------------------------------------------------
PAPER_COLORS = {"日経新聞": "#3b82f6", "読売新聞": "#f59e0b", "朝日新聞": "#ef4444", "毎日新聞": "#10b981"}


def render_topic_card(t):
    shared_note = f"（{'・'.join(t['shared_with'])}も同時報道）" if t["shared_with"] else ""
    summary_html = f"<div class='topic-summary'>{t['summary']}</div>" if t.get("summary") else ""
    return f"""
    <div class="card topic-card">
      <div class="topic-head">
        <span class="paper-name" style="color:{PAPER_COLORS.get(t['paper'],'#666')}">{t['paper']}{shared_note}</span>
        <span class="paper-time">{t['time']}</span>
      </div>
      <div class="topic-title">{t['title']}</div>
      {summary_html}
      <a class="topic-link" href="{t['link']}" target="_blank" rel="noopener">全文を読む（元記事へ）→</a>
    </div>"""


def render_html(all_headlines, topics):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S JST")
    total = sum(len(v) for v in all_headlines.values())
    topics_html = "".join(render_topic_card(t) for t in topics) if topics else "<p class='empty'>直近20時間で、掲載できるトピックが見つかりませんでした。</p>"
    skip_html = "".join(f"<li>{s}</li>" for s in skip_log) if skip_log else "<li>なし（すべての情報源から正常に取得できました）</li>"
    counts_html = "".join(f"<li>{p}: {len(v)}件</li>" for p, v in all_headlines.items())

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>4紙見出し比較ポータル</title>
<style>
  :root {{ --bg:#0f172a; --bg-raised:#1e293b; --ink:#f8fafc; --ink-soft:#94a3b8; --rule:#334155; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink); font-family:"Hiragino Kaku Gothic ProN","Hiragino Sans","Yu Gothic",sans-serif; line-height:1.7; }}
  .page {{ max-width: 880px; margin:0 auto; padding:28px 20px 64px; }}
  header h1 {{ font-size:22px; margin:0 0 4px; }}
  header .meta {{ font-size:12.5px; color:var(--ink-soft); margin-bottom:8px; }}
  .summary {{ font-size:13px; color:var(--ink-soft); margin-bottom:20px; }}
  .summary ul {{ margin:4px 0 0; padding-left:18px; }}

  section {{ margin-bottom:32px; }}
  section > h2 {{ font-size:17px; border-bottom:2px solid var(--rule); padding-bottom:8px; margin-bottom:14px; }}

  .card {{ background:var(--bg-raised); border:1px solid var(--rule); border-radius:10px; padding:16px 18px; margin-bottom:12px; }}
  .cluster-head {{ font-size:13.5px; font-weight:700; margin-bottom:10px; }}
  .paper-list {{ display:flex; flex-direction:column; gap:6px; }}
  .paper-row {{ display:grid; grid-template-columns: 90px 1fr auto; gap:10px; align-items:center; background:#161b26; border-left:3px solid #666; border-radius:6px; padding:6px 10px; text-decoration:none; color:var(--ink); font-size:12.5px; }}
  .paper-name {{ font-weight:700; font-size:11.5px; }}
  .paper-time {{ color:var(--ink-soft); font-size:11px; }}
  .disclaimer {{ font-size:10.5px; color:var(--ink-soft); margin-top:8px; }}
  .empty {{ color:var(--ink-soft); font-size:13px; }}

  .np-grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap:14px; }}
  .np-col {{ background:var(--bg-raised); border:1px solid var(--rule); border-radius:10px; padding:14px 16px; }}
  .np-col h3 {{ font-size:14px; margin:0 0 10px; display:flex; justify-content:space-between; }}
  .np-count {{ color:var(--ink-soft); font-size:11px; font-weight:400; }}
  .np-item {{ display:flex; gap:8px; text-decoration:none; color:var(--ink); font-size:12px; margin-bottom:8px; }}
  .np-time {{ color:var(--ink-soft); flex-shrink:0; }}

  .topic-card {{ display:flex; flex-direction:column; gap:6px; }}
  .topic-head {{ display:flex; justify-content:space-between; align-items:baseline; gap:10px; }}
  .topic-title {{ font-size:15.5px; font-weight:700; line-height:1.5; }}
  .topic-summary {{ font-size:13px; color:var(--ink-soft); }}
  .topic-link {{ align-self:flex-start; margin-top:4px; font-size:12.5px; color:#60a5fa; text-decoration:none; }}
  .topic-link:hover {{ text-decoration:underline; }}

  footer {{ border-top:1px solid var(--rule); padding-top:14px; font-size:12px; color:var(--ink-soft); }}
  footer ul {{ margin:6px 0 14px; padding-left:18px; }}
</style>
</head>
<body>
<div class="page">
  <header>
    <h1>4紙見出し比較ポータル</h1>
    <div class="meta">最終生成: {now_str}</div>
  </header>
  <div class="summary">
    日経・読売・朝日・毎日、直近20時間分の見出し合計{total}件を取得。
    ※本ポータルは「どの紙がどう報じているか」という事実ベースの比較のみを行い、論調・スタンスの分析（憶測になりかねないため）は行いません。
    <ul>{counts_html}</ul>
  </div>

  <section>
    <h2>本日のトピック（重複排除・厳選{len(topics)}件）</h2>
    {topics_html}
  </section>

  <footer>
    データ取得状況（スキップログ）:
    <ul>{skip_html}</ul>
  </footer>
</div>
</body>
</html>
"""


def self_test(all_headlines, topics):
    failures = []
    total = sum(len(v) for v in all_headlines.values())
    if total == 0:
        failures.append("4紙すべてで直近20時間の見出しが0件")
    if len(topics) > TOPIC_DISPLAY_LIMIT:
        failures.append(f"表示トピックが上限{TOPIC_DISPLAY_LIMIT}件を超過（{len(topics)}件）")
    seen_titles = set()
    for t in topics:
        if not t.get("title") or not t.get("link"):
            failures.append("タイトルまたはリンクが空のトピックが存在する")
        if t["title"] in seen_titles:
            failures.append(f"重複トピックが残存: {t['title']}")
        seen_titles.add(t["title"])
    if failures:
        print("❌ 自己検証: 不合格")
        for f in failures:
            print(f"   - {f}")
    else:
        print("✅ 自己検証: 合格")
    return len(failures) == 0


def main():
    start_time = time.time()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    all_headlines, topics = build_dataset()
    passed = self_test(all_headlines, topics)
    html = render_html(all_headlines, topics)

    temp_file = OUTPUT_HTML + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(html)
        os.replace(temp_file, OUTPUT_HTML)
        print(f"✅ 生成完了: {OUTPUT_HTML}")
    except Exception as e:
        print(f"❌ ファイル保存エラー: {e}")
        if os.path.exists(temp_file):
            os.remove(temp_file)

    with_summary = sum(1 for t in topics if t.get("summary"))
    shared_count = sum(1 for t in topics if t.get("shared_with"))
    print(f"📊 掲載トピック: {len(topics)}件（うち複数紙報道: {shared_count}件／要約取得済み: {with_summary}件）")
    elapsed = time.time() - start_time
    print(f"⏱️ トータル処理時間: {int(elapsed // 60)}分 {elapsed % 60:.2f}秒")
    print(f"🔎 自己検証結果: {'PASS' if passed else 'FAIL'}")


if __name__ == "__main__":
    main()
