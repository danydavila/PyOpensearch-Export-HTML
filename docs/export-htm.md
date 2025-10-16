python export_opensearch_url.py \
  --index-pattern "pyspider-mk-gpn-2025-*" \
  --start-date 2025-03-19 --end-date 2025-03-21

python src/export-raw-html-pages.py \
  --index-pattern "pyspider-mk-gpn-2025-*" \
  --start-date 2025-08-04 \
  --end-date 2025-08-07 \
  --hostname "www.heartland.us" \
  --html-dir "outputs/raw_html_2025-10-16" \
  --skip-existing
