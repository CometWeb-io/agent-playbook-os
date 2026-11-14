#!/usr/bin/env bash
# Assemble the GitHub Pages landing page into a single directory.
# Everything the page needs (fonts, favicon) lives in site/, so the build is
# a copy plus .nojekyll; it reads nothing else from the repository.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$root/_site}"

rm -rf "$out"
mkdir -p "$out"
cp -R "$root/site/." "$out/"
touch "$out/.nojekyll"
echo "Site assembled in $out"
