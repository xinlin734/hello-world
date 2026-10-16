import contextlib
import io
import json
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


RESPONSE = {"body": {"content": {"pickupMessage": {"stores": [
    store("Santa Anita", "R100", 4.1, {"MK1Q4LL/A": ("unavailable", "iPhone Duo 2TB Star White")}),
    store("Pasadena", "R200", 3.2, {
        "MK1Q4LL/A": ("available", "iPhone Duo 2TB Star White"),
        "MAAA1LL/A": ("unavailable", "iPhone Duo 256GB Night Sky"),
    }),
    store("Glendale Galleria", "R300", 12.0, {"MAAA9LL/A": ("available", "MagSafe Charger")}),
]}}}}


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.urls = []

    def get_json(self, url, referer=None):
        self.urls.append(url)
        return self.response


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

    def test_infer_meta_leaves_ambiguous_fields_empty(self):
        meta = checker.infer_meta("Star White | Night Sky | 256GB | unlocked")
        self.assertEqual(meta, {"capacity": "256GB", "color": None, "carrier": "Unlocked"})

    def test_att_does_not_match_inside_words(self):
        self.assertIsNone(checker.infer_meta("pattern attribute")["carrier"])


class PickupTest(unittest.TestCase):
    def test_parse_pickup(self):
        rows, notice = checker.parse_pickup(RESPONSE)
        self.assertEqual(notice, "")
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
        self.assertIn("location=91016", client.urls[0])
        self.assertIn("parts.3=MAAB3LL%2FA", client.urls[0])
        # Same canned response per batch: non-Duo and >10 mi rows dropped, nearest store first.
        self.assertEqual([r.store for r in rows], ["Pasadena"] * 6 + ["Santa Anita"] * 3)

    def test_fulfillment_url(self):
        url = checker.fulfillment_url(["MK1Q4LL/A"], "91016")
        self.assertTrue(url.startswith(checker.FULFILLMENT_URL + "?"))
        self.assertIn("parts.0=MK1Q4LL%2FA", url)
        self.assertIn("searchNearby=true", url)

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


if __name__ == "__main__":
    unittest.main()
