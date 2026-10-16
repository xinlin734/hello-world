#!/usr/bin/env python3
"""Check in-store pickup availability for iPhone Duo at Apple Stores near a US ZIP code.

Calls the same (undocumented) fulfillment-messages endpoint that the apple.com
"Check availability" dialog uses. Every color and capacity is checked; the
model list is scraped from the iPhone Duo buy page at startup.

Standard library only: python3 iphone_duo_checker.py
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import platform
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

BUY_URL = "https://www.apple.com/shop/buy-iphone/iphone-duo"
BAG_URL = "https://www.apple.com/shop/bag"
# Current pickup endpoint; the legacy one is only a fallback since Apple now
# often answers it with HTTP 541 or an empty store list.
PICKUP_URL = "https://www.apple.com/shop/retail/pickup-message"
FULFILLMENT_URL = "https://www.apple.com/shop/fulfillment-messages"

# Seed list; the full list is scraped from BUY_URL at startup.
KNOWN_PARTS = {
    "MK1Q4LL/A": {"capacity": "2TB", "color": "Star White", "carrier": "Unlocked"},
}

# Used only if the main buy page yields no part numbers.
VARIANT_CAPACITIES = ["256gb", "512gb", "1tb", "2tb"]
VARIANT_COLORS = ["star-white", "night-sky"]
VARIANT_CARRIERS = ["unlocked", "att", "boost-mobile", "t-mobile", "verizon"]

PART_RE = re.compile(r"M[A-Z0-9]{4}LL/A")
CAPACITY_RE = re.compile(r"\b(\d+)\s?(gb|tb)\b")
COLOR_PATTERNS = [
    (re.compile(r"\bstar ?white\b"), "Star White"),
    (re.compile(r"\bnight ?sky\b"), "Night Sky"),
]
CARRIER_PATTERNS = [
    (re.compile(r"\bunlocked\b"), "Unlocked"),
    (re.compile(r"\b(att|at&t)\b"), "AT&T"),
    (re.compile(r"\bboost\b"), "Boost Mobile"),
    (re.compile(r"\bt ?mobile\b"), "T-Mobile"),
    (re.compile(r"\bverizon\b"), "Verizon"),
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
RETRY_STATUSES = {500, 502, 503, 504}
# Apple's refusals. Retrying them right away only extends the block.
BLOCKED_STATUSES = {403, 429, 541}
MIN_WATCH_SECONDS = 30
BLOCKED_COOLDOWN_SECONDS = 300


class Blocked(Exception):
    """Apple refused the request; this says nothing about stock."""


@dataclass
class Row:
    store: str
    store_number: str
    city: str
    state: str
    distance: float | None
    part: str
    title: str
    status: str
    quote: str

    @property
    def available(self):
        return self.status == "available"


class AppleClient:
    def __init__(self, timeout=20):
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, url, accept="*/*", referer=None, retries=3, extra_headers=None):
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
            **(extra_headers or {}),
        }
        if referer:
            headers["Referer"] = referer
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers=headers)
                with self.opener.open(req, timeout=self.timeout) as resp:
                    return resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                if e.code in BLOCKED_STATUSES:
                    raise Blocked(f"HTTP {e.code}") from e
                if e.code not in RETRY_STATUSES or attempt == retries - 1:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == retries - 1:
                    raise
            time.sleep(3 * 2**attempt + random.random() * 2)
        raise RuntimeError("unreachable")

    def warm_up(self):
        """Apple's edge rejects pickup queries that carry no store cookies."""
        if len(self.jar):
            return
        try:
            self.get(BAG_URL, accept="text/html,application/xhtml+xml,*/*;q=0.8")
        except (Blocked, urllib.error.URLError, TimeoutError):
            pass  # The query itself decides; a failed warm-up is not an answer.

    def get_json(self, url, referer=None):
        text = self.get(url, accept="application/json, text/javascript, */*; q=0.01", referer=referer,
                        extra_headers={"X-Requested-With": "XMLHttpRequest"})
        try:
            return json.loads(text)
        except ValueError:
            # Blocks sometimes come back as a 200 HTML page.
            raise Blocked("返回的不是 JSON") from None


# ---------- part number discovery ----------

def _normalize(text):
    return re.sub(r"[_\-/]+", " ", text.lower())


def _only(values):
    values = set(values)
    return values.pop() if len(values) == 1 else None


def infer_meta(text):
    """Guess capacity / color / carrier from free text; None where ambiguous."""
    text = _normalize(text)
    capacity = _only(f"{n}{u.upper()}" for n, u in CAPACITY_RE.findall(text))
    color = _only(name for pat, name in COLOR_PATTERNS if pat.search(text))
    carrier = _only(name for pat, name in CARRIER_PATTERNS if pat.search(text))
    return {"capacity": capacity, "color": color, "carrier": carrier}


def _strings(obj, depth=0):
    if depth > 4:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v, depth + 1)


def _add(found, part, meta):
    current = found.setdefault(part, {"capacity": None, "color": None, "carrier": None})
    for key, value in meta.items():
        if value and not current.get(key):
            current[key] = value


def _walk_products(obj, found):
    """Collect {part: meta} from any dict that looks like a product record.

    Children are visited first so the most specific record wins; outer dicts
    only fill fields that are still unknown.
    """
    if isinstance(obj, dict):
        for value in obj.values():
            _walk_products(value, found)
        for key, value in obj.items():
            # {"MK1Q4LL/A": {...}} style maps.
            if PART_RE.fullmatch(key) and isinstance(value, dict):
                _add(found, key, infer_meta(" | ".join(_strings(value))))
        parts = {v for v in obj.values() if isinstance(v, str) and PART_RE.fullmatch(v)}
        if len(parts) == 1:
            _add(found, parts.pop(), infer_meta(" | ".join(_strings(obj))))
    elif isinstance(obj, list):
        for value in obj:
            _walk_products(value, found)


def _json_blobs(html):
    decoder = json.JSONDecoder()
    for body in re.findall(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
        body = body.strip()
        starts = [0] if body[:1] in "{[" else []
        starts += [m.end() - 1 for m in re.finditer(r"=\s*\{", body)]
        for start in starts:
            try:
                yield decoder.raw_decode(body, start)[0]
            except ValueError:
                continue


def parts_from_page(html):
    found = {}
    for blob in _json_blobs(html):
        _walk_products(blob, found)
    if not found:
        flat = html.replace("\\u002F", "/").replace("\\/", "/")
        for part in PART_RE.findall(flat):
            found.setdefault(part, {"capacity": None, "color": None, "carrier": None})
    return found


def _discovery_pages():
    yield BUY_URL
    # Each variant page carries the whole lineup; only needed if BUY_URL yields nothing.
    for carrier in VARIANT_CARRIERS:
        for capacity in VARIANT_CAPACITIES:
            for color in VARIANT_COLORS:
                yield f"{BUY_URL}/7.6-inch-display-{capacity}-{color}-{carrier}"


def discover_parts(client, verbose=True):
    parts = {}
    for url in _discovery_pages():
        try:
            parts.update(parts_from_page(client.get(url, accept="text/html")))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            print(f"⚠️  打开 {url} 失败：{e}", file=sys.stderr)
            break
        except (Blocked, urllib.error.URLError, TimeoutError) as e:
            # Blocked or network trouble: don't hammer the remaining pages.
            print(f"⚠️  打开 {url} 失败：{e}", file=sys.stderr)
            break
        if parts:
            break

    for part, meta in KNOWN_PARTS.items():
        merged = dict(parts.get(part) or {})
        merged.update({k: v for k, v in meta.items() if v})
        parts[part] = merged

    if verbose:
        print(f"共找到 {len(parts)} 个 iPhone Duo 型号")
    return parts


# ---------- availability ----------

def _distance(store):
    value = store.get("storedistance") or store.get("storeDistance")
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"[\d.]+", str(store.get("storeDistanceWithUnit") or value or ""))
    return float(m.group()) if m else None


def _plain(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def parse_pickup(data):
    """Turn a pickup response into Rows; also return Apple's notice, if any.

    retail/pickup-message puts stores at body.stores; fulfillment-messages
    nests them under body.content.pickupMessage.
    """
    body = (data or {}).get("body") or {}
    pickup = (body.get("content") or {}).get("pickupMessage") or body
    rows = []
    for store in pickup.get("stores") or []:
        for part, info in (store.get("partsAvailability") or {}).items():
            messages = info.get("messageTypes") or {}
            message = messages.get("regular") or messages.get("compact") or {}
            rows.append(Row(
                store=store.get("storeName") or "",
                store_number=store.get("storeNumber") or "",
                city=store.get("city") or "",
                state=store.get("state") or "",
                distance=_distance(store),
                part=part,
                title=message.get("storePickupProductTitle") or "",
                status=(info.get("pickupDisplay") or "").lower(),
                quote=_plain(info.get("pickupSearchQuote") or message.get("storePickupQuote")),
            ))
    notice = _plain(pickup.get("errorMessage") or pickup.get("pickupLocatorErrorMessage"))
    return rows, notice


def pickup_url(base, parts, zip_code):
    params = [("pl", "true"), ("mts.0", "regular")]
    if base == FULFILLMENT_URL:
        params = [("fae", "true")] + params + [("mts.1", "compact")]
    params += [(f"parts.{i}", part) for i, part in enumerate(parts)]
    params += [("searchNearby", "true"), ("location", zip_code)]
    return f"{base}?{urllib.parse.urlencode(params)}"


def query_pickup(client, parts, zip_code):
    """Ask the current endpoint, falling back to the legacy one if it fails or finds no stores."""
    result, error = None, None
    for base in (PICKUP_URL, FULFILLMENT_URL):
        try:
            result = parse_pickup(client.get_json(pickup_url(base, parts, zip_code), referer=BUY_URL))
        except (Blocked, urllib.error.HTTPError) as e:
            error = error or e
            continue
        if result[0]:
            return result
    if result is not None:
        return result
    raise error


def check_once(client, parts, zip_code, batch_size=8, max_miles=None):
    """Raises Blocked if Apple refuses the queries; that is not the same as no stock."""
    client.warm_up()
    part_list = sorted(parts)
    rows, notices, failed = [], set(), []
    for i in range(0, len(part_list), batch_size):
        batch = part_list[i:i + batch_size]
        try:
            batch_rows, notice = query_pickup(client, batch, zip_code)
        except urllib.error.HTTPError:
            if len(batch) == 1:
                raise
            # A multi-part query was rejected outright; try one part at a time.
            batch_rows, notice = [], ""
            for part in batch:
                try:
                    r, n = query_pickup(client, [part], zip_code)
                except urllib.error.HTTPError as e:
                    print(f"⚠️  {part} 查询失败，跳过：{e}", file=sys.stderr)
                    failed.append(e)
                    continue
                batch_rows += r
                notice = notice or n
        rows += batch_rows
        if notice:
            notices.add(notice)
        time.sleep(0.5 + random.random())

    if failed and not rows:
        raise failed[-1]
    rows = [r for r in rows if not r.title or "duo" in r.title.lower()]
    if max_miles is not None:
        rows = [r for r in rows if r.distance is None or r.distance <= max_miles]
    rows.sort(key=lambda r: (r.distance if r.distance is not None else 1e9, r.store, r.part))
    return rows, notices


# ---------- output ----------

def label(part, parts, title=""):
    meta = parts.get(part) or {}
    if meta.get("capacity") and meta.get("color"):
        name = f"iPhone Duo {meta['capacity']} {meta['color']}"
    else:
        name = title or "iPhone Duo"
    return f"{name} · {meta['carrier']}" if meta.get("carrier") else name


def _store_line(r):
    where = ", ".join(x for x in (r.city, r.state) if x)
    dist = f" · {r.distance:g} mi" if r.distance is not None else ""
    return f"{r.store} ({where}{dist})"


def print_report(rows, notices, parts, zip_code, show_all=False):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stores = {r.store_number for r in rows}
    hits = [r for r in rows if r.available]
    print(f"\n[{stamp}] {zip_code} 附近 {len(stores)} 家店 × {len(parts)} 个型号 → 可自提 {len(hits)} 个")
    for notice in notices:
        print(f"  Apple 提示：{notice}")

    by_store = {}
    for r in rows:
        by_store.setdefault(r.store_number, []).append(r)
    for store_rows in by_store.values():
        avail = [r for r in store_rows if r.available]
        if not avail and not show_all:
            continue
        mark = "✅" if avail else "❌"
        print(f"{mark} {_store_line(store_rows[0])}  有货 {len(avail)}/{len(store_rows)}")
        for r in store_rows if show_all else avail:
            flag = "  ✔" if r.available else "   "
            print(f"   {flag} {label(r.part, parts, r.title):<42} {r.part:<10} {r.quote}")
    if not hits and not show_all:
        print("  暂时没有任何门店可自提。")
    return hits


def notify(hits, parts, ntfy_topic=None):
    lines = [f"{label(r.part, parts, r.title)} @ {r.store}" for r in hits]
    title = "iPhone Duo 有货可自提"
    body = "\n".join(lines[:8]) + (f"\n…另有 {len(lines) - 8} 个" if len(lines) > 8 else "")
    print("\a", end="", flush=True)
    try:
        if platform.system() == "Darwin":
            script = (f"display notification {json.dumps(body, ensure_ascii=False)} "
                      f"with title {json.dumps(title, ensure_ascii=False)} sound name \"Glass\"")
            subprocess.run(["osascript", "-e", script], check=False, timeout=10)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, body], check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
    if ntfy_topic:
        req = urllib.request.Request(
            f"https://ntfy.sh/{urllib.parse.quote(ntfy_topic)}",
            data=body.encode("utf-8"),
            headers={"Title": "iPhone Duo in stock for pickup", "Priority": "high", "Click": BUY_URL},
        )
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"⚠️  ntfy 推送失败：{e}", file=sys.stderr)


# ---------- CLI ----------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="查 Apple Store 附近门店的 iPhone Duo 自提库存（全部颜色、全部容量）")
    p.add_argument("--zip", default="91016", help="邮编（默认 91016）")
    p.add_argument("--parts", help="手动指定型号，逗号分隔，例如 MK1Q4LL/A,...（跳过自动抓取）")
    p.add_argument("--carrier", action="append", metavar="NAME",
                   help="只看某些运营商版本，可重复：unlocked / att / verizon / t-mobile / boost")
    p.add_argument("--max-miles", type=float, help="只显示这个距离（英里）以内的门店")
    p.add_argument("--watch", type=int, metavar="SECONDS",
                   help=f"持续监控，每隔 N 秒查一次，新出现库存时提醒（最少 {MIN_WATCH_SECONDS} 秒）")
    p.add_argument("--ntfy", metavar="TOPIC", help="有货时推送到 ntfy.sh/TOPIC（手机装 ntfy App 订阅即可）")
    p.add_argument("--all", action="store_true", help="显示每家店每个型号的状态（包括没货的）")
    p.add_argument("--list-parts", action="store_true", help="只列出找到的型号，不查库存")
    p.add_argument("--hits-file", metavar="PATH", help="查一次时把有货的结果写进这个文件（给 GitHub Actions 用）")
    p.add_argument("--batch-size", type=int, default=8, help=argparse.SUPPRESS)
    return p.parse_args(argv)


def resolve_parts(client, args):
    if args.parts:
        parts = {}
        for part in filter(None, (s.strip().upper() for s in args.parts.split(","))):
            parts[part] = dict(KNOWN_PARTS.get(part) or {"capacity": None, "color": None, "carrier": None})
    else:
        parts = discover_parts(client)
    if args.carrier:
        wanted = {infer_meta(c)["carrier"] for c in args.carrier} - {None}
        if not wanted:
            sys.exit(f"不认识的运营商：{', '.join(args.carrier)}")
        # Keep parts whose carrier we couldn't infer rather than silently dropping them.
        parts = {p: m for p, m in parts.items() if not m.get("carrier") or m["carrier"] in wanted}
    return parts


def main(argv=None):
    args = parse_args(argv)
    client = AppleClient()
    parts = resolve_parts(client, args)
    if not parts:
        print("没找到任何 iPhone Duo 型号。可以用 --parts 手动指定。", file=sys.stderr)
        return 2

    if args.list_parts:
        for part in sorted(parts, key=lambda p: label(p, parts)):
            print(f"{part:<10} {label(part, parts)}")
        return 0

    if not args.watch:
        try:
            rows, notices = check_once(client, parts, args.zip, args.batch_size, args.max_miles)
        except Blocked as e:
            print(f"被 Apple 拦截了（{e}）：这不代表没货。过 10 分钟以上再试，或换个网络。", file=sys.stderr)
            return 2
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"查询失败：{e}", file=sys.stderr)
            return 2
        hits = print_report(rows, notices, parts, args.zip, args.all)
        if args.hits_file:
            with open(args.hits_file, "w", encoding="utf-8") as f:
                for r in hits:
                    f.write(f"- {_store_line(r)}：{label(r.part, parts, r.title)}（{r.part}）{r.quote}\n")
        return 0 if hits else 1

    interval = max(args.watch, MIN_WATCH_SECONDS)
    print(f"开始监控 {args.zip} 附近，每 {interval} 秒左右查一次，Ctrl+C 退出。")
    seen = set()
    cooldown = 0
    try:
        while True:
            pause = interval
            try:
                rows, notices = check_once(client, parts, args.zip, args.batch_size, args.max_miles)
                hits = print_report(rows, notices, parts, args.zip, args.all)
                current = {(r.store_number, r.part) for r in hits}
                new = [r for r in hits if (r.store_number, r.part) not in seen]
                if new:
                    notify(new, parts, args.ntfy)
                seen = current
                cooldown = 0
            except Blocked as e:
                # Back off 5 → 10 → 20 → 30 min while Apple keeps refusing.
                cooldown = min(cooldown * 2 or BLOCKED_COOLDOWN_SECONDS, 1800)
                pause = max(interval, cooldown)
                print(f"[{datetime.now():%H:%M:%S}] 被 Apple 拦截（{e}），不代表没货；"
                      f"{pause // 60} 分钟后再试", file=sys.stderr)
            except (urllib.error.URLError, TimeoutError) as e:
                print(f"[{datetime.now():%H:%M:%S}] 查询失败：{e}（下次继续）", file=sys.stderr)
            time.sleep(pause + random.uniform(0, pause * 0.2))
    except KeyboardInterrupt:
        print("\n已停止。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
