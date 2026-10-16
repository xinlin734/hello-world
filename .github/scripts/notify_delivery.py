#!/usr/bin/env python3
"""Sync the delivery-date issue with the JSON written by iphone_duo_checker.py --delivery-file.

The issue body always shows the current dates; edits don't notify anyone.
A comment that @-mentions the owner is added only when a model's date beats
the earliest date seen so far, so slips and back-and-forth stay quiet.
Needs gh plus GH_TOKEN, OWNER, ZIP and DELIVERY_ISSUE_TITLE in the environment.
"""

import json
import os
import re
import subprocess
import sys

MARKER = re.compile(r"<!-- best:(\{.*?\}) -->")


def short(date):
    return f"{int(date[4:6])}/{int(date[6:8])}" if date else "没有日期"


def render(current, best, zip_code, mention=None):
    lines = [f"@{mention} 开始跟踪 iPhone Duo 寄送日期。", ""] if mention else []
    lines += [f"寄送到 {zip_code} 的最早日期（自动更新）：", "", "| 型号 | 现在 | 见过最早 |", "|---|---|---|"]
    for part, d in sorted(current.items(), key=lambda kv: kv[1]["label"]):
        lines.append(f"| {d['label']} | {d['text'] or '没有日期'} | {short(best.get(part))} |")
    lines += ["", "哪个型号的日期比「见过最早」还早，会在下面评论并 @ 你。", "",
              f"<!-- best:{json.dumps(best, sort_keys=True)} -->"]
    return "\n".join(lines)


def plan(current, old_body, owner, zip_code):
    """Return (body, comment or None); old_body is None when the issue doesn't exist yet."""
    m = MARKER.search(old_body or "")
    best = json.loads(m.group(1)) if m else {}
    improved = [(part, best.get(part), d) for part, d in sorted(current.items(), key=lambda kv: kv[1]["label"])
                if d["date"] and (part not in best or d["date"] < best[part])]
    for part, _, d in improved:
        best[part] = d["date"]

    if old_body is None:
        return render(current, best, zip_code, mention=owner), None
    comment = None
    if improved:
        lines = [f"@{owner} 寄送日期提前了：", ""]
        lines += [f"- {d['label']}：之前最早 {short(old)} → **{d['text']}**" for _, old, d in improved]
        lines += ["", "下单：https://www.apple.com/shop/buy-iphone/iphone-duo"]
        comment = "\n".join(lines)
    return render(current, best, zip_code), comment


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def main(path):
    with open(path, encoding="utf-8") as f:
        current = json.load(f)
    if not current:
        return
    title = os.environ["DELIVERY_ISSUE_TITLE"]
    issues = json.loads(gh("issue", "list", "--state", "open", "--limit", "100", "--json", "number,title,body"))
    issue = next((i for i in issues if i["title"] == title), None)
    body, comment = plan(current, issue["body"] if issue else None, os.environ["OWNER"], os.environ["ZIP"])
    if issue is None:
        gh("issue", "create", "--title", title, "--body", body)
        return
    if comment:
        gh("issue", "comment", str(issue["number"]), "--body", comment)
    if body.strip() != issue["body"].replace("\r\n", "\n").strip():
        gh("issue", "edit", str(issue["number"]), "--body", body)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "delivery.json")
