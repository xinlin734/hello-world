import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from unittest import mock

import iphone_duo_checker as checker

_spec = importlib.util.spec_from_file_location(
    "notify_delivery", os.path.join(os.path.dirname(__file__), ".github", "scripts", "notify_delivery.py"))
notify_delivery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notify_delivery)

BUY_PAGE = """
<html><script>window.PRODUCT_SELECTION_BOOTSTRAP = {"productSelectionData": {"products": [
  {"partNumber": "MK1Q4LL/A", "seoUrlToken": "7.6-inch-display-2tb-star-white-unlocked",
   "dimensions": {"dimensionCapacity": "2tb", "dimensionColor": "star_white", "carrierModel": "UNLOCKED/US"}},
  {"partNumber": "MAAA1LL/A", "seoUrlToken": "7.6-inch-display-256gb-night-sky-verizon",
   "dimensions": {"dimensionCapacity": "256gb", "dimensionColor": "night_sky", "carrierModel": "VERIZON_IPHONE"}},
  {"partNumber": "MAAA2LL/A", "seoUrlToken": "7.6-inch-display-512gb-night-sky-t-mobile",
   "dimensions": {"dimensionCapacity": "512gb", "dimensionColor": "night_sky", "carrierModel": "TMOBILE_IPHONE"}}
]}, "selectedPart": "MK1Q4LL/A"};</script>
<script type="application/json" id="metrics">{"data": {"products": [{"sku": "MAAA3LL/A", "name": "iPhone Duo 1TB Star White AT&T"}]}}</script>
</html>
"""


def store(name, number, distance, parts):
    return {
        "storeName": name, "storeNumber": number, "city": name, "state": "CA",
        "storeDistanceWithUnit": f"{distance} mi",
        "partsAvailability": {
            part: {
                "pickupDisplay": status,
                "pickupSearchQuote": "Available<br/>Today" if status == "available" else "Unavailable for Pickup",
                "messageTypes": {"regular": {"storePickupProductTitle": title}},
            }
            for part, (status, title) in parts.items()
        },
    }


def closed_store(name, number, distance, parts):
    """Pre-launch shape: pickup switched off for every part, whatever the stock."""
    return {
        "storeName": name, "storeNumber": number, "city": name, "state": "CA",
        "storeDistanceWithUnit": f"{distance} mi",
        "partsAvailability": {
            part: {
                "pickupDisplay": "default", "storePickEligible": True,
                "pickupSearchQuote": "Check back for availability on 10.24",
                "messageTypes": {"regular": {"storePickupProductTitle": title, "storeSelectionEnabled": False,
                                             "storeSearchEnabled": False}},
            }
            for part, title in parts.items()
        },
    }


CLOSED_RESPONSE = {"body": {"stores": [
    closed_store("Pasadena", "R034", 12.09, {"MK1Q4LL/A": "iPhone Duo 2TB Star White",
                                             "MK1E4LL/A": "iPhone Duo 256GB Star White"}),
    closed_store("Glendale Galleria", "R052", 17.68, {"MK1Q4LL/A": "iPhone Duo 2TB Star White"}),
]}}


def delivery_response(dates):
    """dates: {part: ("Mon, Jan 25 — Free", "20270125") or None for not buyable}."""
    message = {"geoLocated": False, "dudeLocated": True}
    for part, value in dates.items():
        if value is None:
            message[part] = {"regular": {"buyability": {"isBuyable": False, "reason": "NOT_AVAILABLE"},
                                         "deliveryOptionMessages": []}}
        else:
            text, date = value
            message[part] = {"regular": {
                "buyability": {"isBuyable": True, "reason": "OK"},
                "deliveryOptionMessages": [{"displayName": text, "encodedUpperDateString": date}],
            }}
    return {"head": {"status": "200"}, "body": {"content": {"deliveryMessage": message}}}


STORES = [
    store("Santa Anita", "R100", 4.1, {"MK1Q4LL/A": ("unavailable", "iPhone Duo 2TB Star White")}),
    store("Pasadena", "R200", 3.2, {
        "MK1Q4LL/A": ("available", "iPhone Duo 2TB Star White"),
        "MAAA1LL/A": ("unavailable", "iPhone Duo 256GB Night Sky"),
    }),
    store("Glendale Galleria", "R300", 12.0, {"MAAA9LL/A": ("available", "MagSafe Charger")}),
]
RESPONSE = {"body": {"stores": STORES}}  # retail/pickup-message
LEGACY_RESPONSE = {"body": {"content": {"pickupMessage": {"stores": STORES}}}}  # fulfillment-messages


class FakeClient:
    """Answers per endpoint; an Exception value is raised instead of returned."""

    def __init__(self, response=None, by_endpoint=None):
        self.responses = by_endpoint or {checker.PICKUP_URL: response}
        self.urls = []

    def warm_up(self):
        pass

    def get_json(self, url, referer=None):
        self.urls.append(url)
        result = self.responses.get(url.split("?")[0], {})
        if isinstance(result, Exception):
            raise result
        return result


class DiscoveryTest(unittest.TestCase):
    def test_parts_from_bootstrap_and_metrics(self):
        parts = checker.parts_from_page(BUY_PAGE)
        self.assertEqual(parts["MK1Q4LL/A"], {"capacity": "2TB", "color": "Star White", "carrier": "Unlocked"})
        self.assertEqual(parts["MAAA1LL/A"], {"capacity": "256GB", "color": "Night Sky", "carrier": "Verizon"})
        self.assertEqual(parts["MAAA2LL/A"], {"capacity": "512GB", "color": "Night Sky", "carrier": "T-Mobile"})
        self.assertEqual(parts["MAAA3LL/A"], {"capacity": "1TB", "color": "Star White", "carrier": "AT&T"})

    def test_regex_fallback_with_escaped_slashes(self):
        parts = checker.parts_from_page('<div data-x="MAAA4LL\\u002FA MAAA5LL\\/A MX123ZM/A"></div>')
        self.assertEqual(sorted(parts), ["MAAA4LL/A", "MAAA5LL/A"])

    def test_discovery_stops_on_network_error_but_skips_404(self):
        calls = []

        class Client:
            def get(self, url, accept=None):
                calls.append(url)
                if len(calls) == 1:
                    raise checker.urllib.error.HTTPError(url, 404, "nf", None, None)
                raise checker.urllib.error.URLError("timed out")

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            parts = checker.discover_parts(Client())
        self.assertEqual(len(calls), 2)
        self.assertEqual(sorted(parts), ["MK1Q4LL/A"])

    def test_infer_meta_leaves_ambiguous_fields_empty(self):
        meta = checker.infer_meta("Star White | Night Sky | 256GB | unlocked")
        self.assertEqual(meta, {"capacity": "256GB", "color": None, "carrier": "Unlocked"})

    def test_att_does_not_match_inside_words(self):
        self.assertIsNone(checker.infer_meta("pattern attribute")["carrier"])


class PickupTest(unittest.TestCase):
    def test_parse_pickup(self):
        rows, notice = checker.parse_pickup(RESPONSE)
        self.assertEqual(notice, "")
        self.assertEqual(rows, checker.parse_pickup(LEGACY_RESPONSE)[0])
        pasadena = [r for r in rows if r.store == "Pasadena" and r.part == "MK1Q4LL/A"][0]
        self.assertTrue(pasadena.available)
        self.assertEqual(pasadena.distance, 3.2)
        self.assertEqual(pasadena.quote, "Available Today")

    def test_parse_pickup_handles_empty_and_error(self):
        self.assertEqual(checker.parse_pickup({}), ([], ""))
        data = {"body": {"content": {"pickupMessage": {"errorMessage": "Check back <b>later</b>"}}}}
        self.assertEqual(checker.parse_pickup(data), ([], "Check back later"))

    def test_check_once_filters_sorts_and_batches(self):
        client = FakeClient(RESPONSE)
        parts = {f"MAAB{i}LL/A": {} for i in range(10)}
        with mock.patch.object(checker.time, "sleep"):
            rows, _ = checker.check_once(client, parts, "91016", batch_size=4, max_miles=10)
        self.assertEqual(len(client.urls), 3)
        self.assertTrue(all(u.startswith(checker.PICKUP_URL + "?") for u in client.urls))
        self.assertIn("location=91016", client.urls[0])
        self.assertIn("parts.3=MAAB3LL%2FA", client.urls[0])
        # Same canned response per batch: non-Duo and >10 mi rows dropped, nearest store first.
        self.assertEqual([r.store for r in rows], ["Pasadena"] * 6 + ["Santa Anita"] * 3)

    def test_pickup_url(self):
        url = checker.pickup_url(checker.PICKUP_URL, ["MK1Q4LL/A"], "91016")
        self.assertEqual(url, checker.PICKUP_URL + "?pl=true&mts.0=regular&parts.0=MK1Q4LL%2FA"
                              "&searchNearby=true&location=91016")
        legacy = checker.pickup_url(checker.FULFILLMENT_URL, ["MK1Q4LL/A"], "91016")
        self.assertIn("fae=true", legacy)

    def test_falls_back_to_legacy_endpoint_when_no_stores(self):
        client = FakeClient(by_endpoint={
            checker.PICKUP_URL: {"body": {"stores": []}},
            checker.FULFILLMENT_URL: LEGACY_RESPONSE,
        })
        with mock.patch.object(checker.time, "sleep"):
            rows, _ = checker.check_once(client, {"MK1Q4LL/A": {}}, "91016")
        self.assertEqual(len(client.urls), 2)
        self.assertTrue(any(r.available for r in rows))

    def test_falls_back_to_legacy_endpoint_when_blocked(self):
        client = FakeClient(by_endpoint={
            checker.PICKUP_URL: checker.Blocked("HTTP 541"),
            checker.FULFILLMENT_URL: LEGACY_RESPONSE,
        })
        with mock.patch.object(checker.time, "sleep"):
            rows, _ = checker.check_once(client, {"MK1Q4LL/A": {}}, "91016")
        self.assertTrue(rows)

    def test_blocked_everywhere_is_an_error_not_out_of_stock(self):
        blocked = checker.Blocked("HTTP 541")
        client = FakeClient(by_endpoint={checker.PICKUP_URL: blocked, checker.FULFILLMENT_URL: blocked})
        with mock.patch.object(checker.time, "sleep"), self.assertRaises(checker.Blocked):
            checker.check_once(client, {"MK1Q4LL/A": {}, "MAAA1LL/A": {}}, "91016")
        self.assertEqual(len(client.urls), 2)  # no per-part retries once blocked

        err = io.StringIO()
        with mock.patch.object(checker, "AppleClient", lambda: client), mock.patch.object(checker.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = checker.main(["--parts", "MK1Q4LL/A"])
        self.assertEqual(code, 2)
        self.assertIn("不代表没货", err.getvalue())

    def test_all_parts_failing_is_an_error(self):
        boom = checker.urllib.error.HTTPError("u", 400, "bad", None, None)
        client = FakeClient(by_endpoint={checker.PICKUP_URL: boom, checker.FULFILLMENT_URL: boom})
        with mock.patch.object(checker.time, "sleep"), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(checker.urllib.error.HTTPError):
            checker.check_once(client, {"MK1Q4LL/A": {}, "MAAA1LL/A": {}}, "91016")


class WatchTest(unittest.TestCase):
    """--watch with a fake clock: every sleep advances time by its own length."""

    def run_watch(self, responses, argv):
        clock = [0.0]
        responses = iter(responses)

        class Client(FakeClient):
            def get_json(self, url, referer=None):
                result = next(responses)
                if isinstance(result, Exception):
                    raise result
                return result

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch.object(checker, "AppleClient", lambda: Client()), \
                mock.patch.object(checker.time, "sleep", sleep), \
                mock.patch.object(checker.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(checker.subprocess, "run") as run, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = checker.main(["--parts", "MK1Q4LL/A"] + argv)
        return code, run, clock[0]

    def test_on_change_fires_first_round_and_on_changes_only(self):
        empty = {"body": {"stores": [store("Pasadena", "R034", 12.1, {"MK1Q4LL/A": ("default", "iPhone Duo 2TB Star White")})]}}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "hits.txt")
            code, run, elapsed = self.run_watch(
                [empty, empty, RESPONSE, RESPONSE, empty],
                ["--watch", "60", "--duration", "290", "--hits-file", path, "--on-change", "notify"])
            with open(path, encoding="utf-8") as f:
                last_hits = f.read()
        self.assertEqual(code, 0)
        # Rounds: empty (first) → empty → stock → stock → empty: fires on 1st, 3rd and 5th.
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args.args, ("notify",))
        self.assertEqual(last_hits, "")
        self.assertLess(elapsed, 290)

    def test_stop_on_block(self):
        code, run, _ = self.run_watch(
            [checker.Blocked("HTTP 541"), checker.Blocked("HTTP 541")],
            ["--watch", "60", "--duration", "600", "--stop-on-block", "--on-change", "notify"])
        self.assertEqual(code, 2)
        run.assert_not_called()


class ClosedPickupTest(unittest.TestCase):
    def test_prelaunch_response_reads_as_closed(self):
        rows, _ = checker.parse_pickup(CLOSED_RESPONSE)
        self.assertTrue(checker.pickup_closed(rows))
        self.assertFalse(any(r.available for r in rows))

    def test_anything_else_reads_as_open(self):
        rows, _ = checker.parse_pickup(RESPONSE)
        self.assertFalse(checker.pickup_closed(rows))
        self.assertFalse(checker.pickup_closed([]))
        # One store already taking reservations is enough.
        mixed = {"body": {"stores": CLOSED_RESPONSE["body"]["stores"] + [
            store("Brea Mall", "R100", 22.4, {"MK1Q4LL/A": ("unavailable", "iPhone Duo 2TB Star White")})]}}
        self.assertFalse(checker.pickup_closed(checker.parse_pickup(mixed)[0]))

    def test_learn_titles_fills_labels(self):
        parts = {"MK1E4LL/A": {"capacity": None, "color": None, "carrier": None}}
        checker.learn_titles(parts, checker.parse_pickup(CLOSED_RESPONSE)[0])
        self.assertEqual(checker.label("MK1E4LL/A", parts), "iPhone Duo 256GB Star White")


class DeliveryTest(unittest.TestCase):
    def test_delivery_url_uses_postal_code(self):
        url = checker.delivery_url(["MK1Q4LL/A", "MK1E4LL/A"], "91016")
        self.assertEqual(url, checker.DELIVERY_URL + "?parts.0=MK1Q4LL%2FA&parts.1=MK1E4LL%2FA"
                              "&mts.0=regular&postalCode=91016")

    def test_parse_delivery(self):
        data = delivery_response({"MK1Q4LL/A": ("Mon, Jan 25 — Free", "20270125"), "MK1E4LL/A": None})
        self.assertEqual(checker.parse_delivery(data), {
            "MK1Q4LL/A": {"date": "20270125", "text": "Mon, Jan 25 — Free"},
            "MK1E4LL/A": {"date": None, "text": "暂时买不了"},
        })
        self.assertEqual(checker.parse_delivery({}), {})

    def test_parse_delivery_takes_earliest_option(self):
        data = delivery_response({"MK1Q4LL/A": ("Fri, Jan 29 — Free", "20270129")})
        data["body"]["content"]["deliveryMessage"]["MK1Q4LL/A"]["regular"]["deliveryOptionMessages"].append(
            {"displayName": "Tue, Jan 26 — $19.95", "encodedUpperDateString": "20270126"})
        self.assertEqual(checker.parse_delivery(data)["MK1Q4LL/A"]["date"], "20270126")


class AdaptiveWatchTest(unittest.TestCase):
    """--watch with a fake clock, answering per endpoint from queues of responses."""

    def run_watch(self, pickups, deliveries, argv):
        clock = [0.0]
        queues = {checker.PICKUP_URL: iter(pickups), checker.DELIVERY_URL: iter(deliveries)}

        class Client(FakeClient):
            def get_json(self, url, referer=None):
                return next(queues[url.split("?")[0]])

        def sleep(seconds):
            clock[0] += seconds

        out = io.StringIO()
        with mock.patch.object(checker, "AppleClient", lambda: Client()), \
                mock.patch.object(checker.time, "sleep", sleep), \
                mock.patch.object(checker.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(checker.subprocess, "run") as run, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = checker.main(["--parts", "MK1Q4LL/A"] + argv)
        return code, run, out.getvalue()

    def test_stop_if_closed_after_one_round(self):
        code, run, out = self.run_watch(
            [CLOSED_RESPONSE], [delivery_response({"MK1Q4LL/A": ("Mon, Jan 25 — Free", "20270125")})],
            ["--watch", "60", "--duration", "1200", "--stop-if-closed", "--delivery",
             "--on-delivery-change", "deliv"])
        self.assertEqual(code, 3)
        self.assertIn("Apple 还没开放自提", out)
        self.assertIn("Mon, Jan 25 — Free", out)
        run.assert_called_once()  # the delivery hook still ran before exiting

    def test_open_pickup_keeps_watching(self):
        code, _, _ = self.run_watch([RESPONSE] * 3, [], ["--watch", "60", "--duration", "150", "--stop-if-closed"])
        self.assertEqual(code, 0)

    def test_delivery_checked_every_five_minutes_and_hook_on_change(self):
        jan25 = delivery_response({"MK1Q4LL/A": ("Mon, Jan 25 — Free", "20270125")})
        nov6 = delivery_response({"MK1Q4LL/A": ("Fri, Nov 6 — Free", "20261106")})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "delivery.json")
            # ~12 one-minute rounds; delivery is asked at 0, ~5 and ~10 minutes.
            code, run, _ = self.run_watch(
                [RESPONSE] * 12, [jan25, jan25, nov6],
                ["--watch", "60", "--duration", "700", "--delivery", "--delivery-file", path,
                 "--on-delivery-change", "deliv"])
            with open(path, encoding="utf-8") as f:
                written = checker.json.load(f)
        self.assertEqual(code, 0)
        self.assertEqual([c.args for c in run.call_args_list], [("deliv",), ("deliv",)])  # first, then Nov 6
        self.assertEqual(written["MK1Q4LL/A"]["date"], "20261106")
        self.assertEqual(written["MK1Q4LL/A"]["label"], "iPhone Duo 2TB Star White · Unlocked")


class NotifyDeliveryTest(unittest.TestCase):
    CURRENT = {
        "MK1E4LL/A": {"date": "20270125", "text": "Mon, Jan 25 — Free", "label": "iPhone Duo 256GB Star White"},
        "MK1Q4LL/A": {"date": "20270125", "text": "Mon, Jan 25 — Free", "label": "iPhone Duo 2TB Star White"},
    }

    def plan(self, current, body):
        return notify_delivery.plan(current, body, "xinlin734", "91016")

    def with_date(self, part, date, text):
        current = {p: dict(d) for p, d in self.CURRENT.items()}
        current[part].update(date=date, text=text)
        return current

    def test_new_issue_mentions_owner_without_comment(self):
        body, comment = self.plan(self.CURRENT, None)
        self.assertIsNone(comment)
        self.assertTrue(body.startswith("@xinlin734 "))
        self.assertIn("| iPhone Duo 256GB Star White | Mon, Jan 25 — Free | 1/25 |", body)

    def test_unchanged_dates_are_quiet(self):
        body, _ = self.plan(self.CURRENT, None)
        body2, comment = self.plan(self.CURRENT, body)
        self.assertIsNone(comment)
        self.assertNotIn("@xinlin734", body2)  # edits never re-mention

    def test_earlier_date_comments(self):
        body, _ = self.plan(self.CURRENT, None)
        body2, comment = self.plan(self.with_date("MK1Q4LL/A", "20261106", "Fri, Nov 6 — Free"), body)
        self.assertEqual(comment.splitlines()[2], "- iPhone Duo 2TB Star White：之前最早 1/25 → **Fri, Nov 6 — Free**")
        self.assertIn("| iPhone Duo 2TB Star White | Fri, Nov 6 — Free | 11/6 |", body2)

    def test_slip_and_recovery_stay_quiet(self):
        body, _ = self.plan(self.CURRENT, None)
        body2, comment = self.plan(self.with_date("MK1Q4LL/A", "20270205", "Fri, Feb 5 — Free"), body)
        self.assertIsNone(comment)
        self.assertIn("| iPhone Duo 2TB Star White | Fri, Feb 5 — Free | 1/25 |", body2)
        _, comment = self.plan(self.CURRENT, body2)  # back to Jan 25: not earlier than seen before
        self.assertIsNone(comment)

    def test_unbuyable_model_is_not_an_improvement(self):
        body, _ = self.plan(self.CURRENT, None)
        _, comment = self.plan(self.with_date("MK1E4LL/A", None, "暂时买不了"), body)
        self.assertIsNone(comment)


class ClientTest(unittest.TestCase):
    def test_541_raises_blocked_without_retry(self):
        client = checker.AppleClient()
        err = checker.urllib.error.HTTPError("u", 541, "Unknown", None, None)
        with mock.patch.object(client.opener, "open", side_effect=err) as opened, \
                mock.patch.object(checker.time, "sleep"), self.assertRaises(checker.Blocked):
            client.get("https://www.apple.com/x")
        self.assertEqual(opened.call_count, 1)

    def test_html_instead_of_json_is_blocked(self):
        client = checker.AppleClient()
        with mock.patch.object(client, "get", return_value="<html>nope</html>"), \
                self.assertRaises(checker.Blocked):
            client.get_json("https://www.apple.com/x")

    def test_report_lists_only_available_by_default(self):
        rows, _ = checker.parse_pickup(RESPONSE)
        rows = [r for r in rows if "Duo" in r.title]
        parts = checker.parts_from_page(BUY_PAGE)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hits = checker.print_report(rows, set(), parts, "91016")
        self.assertEqual(len(hits), 1)
        text = out.getvalue()
        self.assertIn("Pasadena", text)
        self.assertIn("iPhone Duo 2TB Star White · Unlocked", text)
        self.assertNotIn("Santa Anita", text)


class CliTest(unittest.TestCase):
    def test_manual_parts_and_carrier_filter(self):
        args = checker.parse_args(["--parts", "mk1q4ll/a, MAAA1LL/A", "--carrier", "verizon"])
        parts = checker.resolve_parts(None, args)
        # MK1Q4LL/A is known to be unlocked; MAAA1LL/A has no carrier info so it is kept.
        self.assertEqual(sorted(parts), ["MAAA1LL/A"])

    def test_list_parts(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = checker.main(["--parts", "MK1Q4LL/A", "--list-parts"])
        self.assertEqual(code, 0)
        self.assertIn("MK1Q4LL/A  iPhone Duo 2TB Star White · Unlocked", out.getvalue())

    def test_hits_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "hits.txt")
            with mock.patch.object(checker, "AppleClient", lambda: FakeClient(RESPONSE)), \
                    mock.patch.object(checker.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                code = checker.main(["--parts", "MK1Q4LL/A", "--hits-file", path])
            with open(path, encoding="utf-8") as f:
                hits = f.read()
        self.assertEqual(code, 0)
        self.assertEqual(hits, "- Pasadena (Pasadena, CA · 3.2 mi)：iPhone Duo 2TB Star White · Unlocked（MK1Q4LL/A）Available Today\n")


if __name__ == "__main__":
    unittest.main()
