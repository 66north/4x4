#!/bin/bash
# Build the site and deploy to gh-pages.
# Run from the project root after output/ is present.
set -euo pipefail

REPO="https://github.com/66north/4x4.git"
SITE_DIR="site"
BRANCH="gh-pages"
BUILD_DIR=$(mktemp -d)

echo "→ Building site..."
python build_site.py

echo "→ Building search index..."
~/.local/bin/pagefind --site "$SITE_DIR"

echo "→ Preparing gh-pages branch..."
cd "$BUILD_DIR"
git init -b "$BRANCH"
git remote add origin "$REPO"

cp -r "$(cd - > /dev/null && pwd)/$SITE_DIR/." .

git add -A
git commit -m "Deploy $(date -u '+%Y-%m-%d %H:%M UTC')"

echo "→ Pushing to $BRANCH..."
git push --force origin "$BRANCH"

cd - > /dev/null
rm -rf "$BUILD_DIR"
echo "✓ Deployed to https://66north.github.io/4x4/"
