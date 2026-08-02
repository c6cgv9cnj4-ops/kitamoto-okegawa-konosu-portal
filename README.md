# 近隣3市 地域ポータル（北本・桶川・鴻巣）

北本市・桶川市・鴻巣市の防犯・防災情報／地域トピックを自動収集して表示するポータルサイトです。

- データ取得元: 埼玉県警（鴻巣警察署・上尾警察署）新着情報、埼玉県央広域消防本部お知らせ、号外NET RSS、Google Newsローカル検索
- `fetch.py` を実行すると `docs/index.html` が再生成されます
- GitHub Actions（`.github/workflows/update.yml`）が毎時自動実行し、変更があればコミット・pushします
- GitHub Pagesが `docs/` を自動配信します

すべて無料枠（GitHub Actions・GitHub Pages）の範囲で運用しています。
このリポジトリは公開用の最小構成のみを含み、個人的なメモ等は含まれていません。
