#!/usr/bin/env bash
# report/report.md -> report/report.pdf (pandoc + xelatex, macOS fonts)
set -euo pipefail
cd "$(dirname "$0")/.."
pandoc report/title.md report/report.md -o report/report.pdf --pdf-engine=xelatex --resource-path=.
echo "report/report.pdf"
