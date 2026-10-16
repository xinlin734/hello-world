import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

import iphone_duo_checker as checker

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
