#!/usr/bin/env bash
# Sync the stock-alert issue with the hits file written by iphone_duo_checker.py.
# One open issue while stock exists: created on first sighting, commented when
# the in-stock list changes, closed when everything is gone.
# Needs GH_TOKEN, OWNER, ZIP and ISSUE_TITLE in the environment.
set -euo pipefail
hits=${1:-hits.txt}

num=$(gh issue list --state open --limit 100 --json number,title \
      --jq 'map(select(.title == env.ISSUE_TITLE)) | .[0].number // empty')

if [ -s "$hits" ]; then
  sig=$(sha256sum "$hits" | cut -c1-12)
  body="$(printf '@%s %s 附近有 iPhone Duo 可以自提了！\n\n%s\n\n下单：https://www.apple.com/shop/buy-iphone/iphone-duo\n\n<!-- sig:%s -->' \
          "$OWNER" "$ZIP" "$(cat "$hits")" "$sig")"
  if [ -z "$num" ]; then
    gh issue create --title "$ISSUE_TITLE" --body "$body"
  else
    last=$(gh issue view "$num" --json body,comments --jq '([.body] + [.comments[].body]) | last')
    case "$last" in
      *"sig:$sig"*) echo "库存没变化，不重复提醒" ;;
      *) gh issue comment "$num" --body "$body" ;;
    esac
  fi
elif [ -n "$num" ]; then
  gh issue close "$num" --comment "暂时都没货了。下次有货会再开一个新的提醒。"
fi
