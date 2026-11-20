#!/usr/bin/env python3
"""Product list (inventory) tests: reading CSV/JSON/Excel, guessing columns, links, the web API.
Run: python3 tests/test_inventory.py"""
import base64
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "..", "atc_port")]
import inventory  # noqa: E402

CSV = ("SKU,Product Name,Short Description,Retail Price,UPC,Shelf Tag\n"
       "1001,Fresh Coffee Beans,250 g · Medium roast,8.99,036000291452,ESL_A7E16A\n"
       "1002,Green Tea,20 bags,4.5,012345678905,\n").encode()


def make_xlsx(rows):
    """A minimal Excel file: shared strings for text, numbers as numbers."""
    strings, sheet = [], []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, v in enumerate(row):
            ref = chr(65 + c) + str(r)
            if isinstance(v, str):
                if v not in strings:
                    strings.append(v)
                cells.append('<c r="%s" t="s"><v>%d</v></c>' % (ref, strings.index(v)))
            else:
                cells.append('<c r="%s"><v>%s</v></c>' % (ref, v))
        sheet.append('<row r="%d">%s</row>' % (r, "".join(cells)))
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("xl/sharedStrings.xml", '<sst %s>%s</sst>' % (ns, "".join("<si><t>%s</t></si>" % s for s in strings)))
        z.writestr("xl/worksheets/sheet1.xml", '<worksheet %s><sheetData>%s</sheetData></worksheet>' % (ns, "".join(sheet)))
    return b.getvalue()


class ParseTests(unittest.TestCase):
    def test_csv_and_guesses(self):
        inv = inventory.import_bytes(inventory.empty(), CSV, "products.csv")
        self.assertEqual(len(inv["items"]), 2)
        self.assertEqual(inv["key"], "SKU")
        self.assertEqual(inv["map"]["price"], "Retail Price")
        self.assertEqual(inv["map"]["barcode"], "UPC")
        self.assertEqual(inv["map"]["title"], "Product Name")
        self.assertEqual(inv["map"]["summary"], "Short Description")
        self.assertEqual(inv["tag_column"], "Shelf Tag")
        self.assertEqual(inv["items"][0]["UPC"], "036000291452")  # leading zero kept

    def test_semicolon_and_tab_csv(self):
        cols, items = inventory.parse(b"sku;name;price\nA1;Tea;1,50\n", "x.csv")
        self.assertEqual(cols, ["sku", "name", "price"])
        self.assertEqual(items[0]["price"], "1,50")
        cols, items = inventory.parse(b"sku\tname\nA1\tTea\n", "x.tsv")
        self.assertEqual(items[0]["name"], "Tea")

    def test_json_shapes(self):
        cols, items = inventory.parse(json.dumps({"products": [{"sku": "A", "price": {"amount": 4.5}, "upc": 36000291452}]}).encode(), "p.json")
        self.assertIn("price.amount", cols)
        self.assertEqual(items[0]["price.amount"], "4.5")
        self.assertEqual(items[0]["upc"], "36000291452")
        cols, items = inventory.parse(b'[{"id": 1, "title": "x"}]', "")
        self.assertEqual(items, [{"id": "1", "title": "x"}])
        with self.assertRaises(ValueError):
            inventory.parse(b'{"count": 3}', "p.json")

    def test_xlsx(self):
        data = make_xlsx([["Item Code", "Description", "Price", "Barcode"], ["B-7", "Oat milk", 3.25, "012345678905"], [8, "Bread", 2, 4006381333931]])
        inv = inventory.import_bytes(inventory.empty(), data, "stock.xlsx")
        self.assertEqual(inv["key"], "Item Code")
        self.assertEqual(inv["items"][1], {"Item Code": "8", "Description": "Bread", "Price": "2", "Barcode": "4006381333931"})
        self.assertEqual(inv["items"][0]["Price"], "3.25")

    def test_keeps_choices_on_reload(self):
        inv = inventory.import_bytes(inventory.empty(), CSV, "a.csv")
        inventory.clean_settings(inv, {"map": {"title": "Short Description", "price": "Retail Price", "bogus": "SKU"},
                                       "links": {"ESL_8079EB": "1002", "ESL_X": ""}, "currency": "€"})
        self.assertNotIn("bogus", inv["map"])
        self.assertEqual(inv["links"], {"ESL_8079EB": "1002"})
        inventory.import_bytes(inv, CSV, "a.csv")
        self.assertEqual(inv["map"]["title"], "Short Description")
        self.assertEqual(inv["links"], {"ESL_8079EB": "1002"})
        self.assertEqual(inv["currency"], "€")

    def test_empty_and_unknown_columns(self):
        with self.assertRaises(ValueError):
            inventory.import_bytes(inventory.empty(), b"sku,name\n", "a.csv")
        inv = inventory.import_bytes(inventory.empty(), CSV, "a.csv")
        inventory.clean_settings(inv, {"key": "Nope", "tag_column": "Nope"})
        self.assertEqual(inv["key"], "SKU")
        self.assertEqual(inv["tag_column"], "Shelf Tag")
        inventory.clean_settings(inv, {"tag_column": ""})
        self.assertIsNone(inv["tag_column"])


class ReviewFixTests(unittest.TestCase):
    def test_odd_files(self):
        self.assertEqual(inventory.parse(b"PK,Name\n1,Tea\n", "x.csv")[1], [{"PK": "1", "Name": "Tea"}])
        self.assertEqual(inventory.parse(b'\xef\xbb\xbf[{"sku": 1}]', "list")[1], [{"sku": "1"}])
        self.assertEqual(inventory.parse("Name,Price\nCaf\u00e9 \u00a3,1\n".encode("cp1252"), "x.csv")[1][0]["Name"], "Caf\u00e9 \u00a3")
        cols, items = inventory.parse(b"Price,Price,Price_2\n1,2,3\n", "x.csv")
        self.assertEqual(sorted(items[0].values()), ["1", "2", "3"])

    def test_xlsx_details(self):
        ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        rns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("xl/workbook.xml", '<workbook %s %s><sheets><sheet name="Products" r:id="rId2"/><sheet name="Notes" r:id="rId1"/></sheets></workbook>' % (ns, rns))
            z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
            z.writestr("xl/styles.xml", '<styleSheet %s><cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>' % ns)
            z.writestr("xl/sharedStrings.xml", '<sst %s><si><t>Name</t></si><si><t>\u5546\u54c1</t><rPh sb="0" eb="2"><t>\u30b7\u30e7\u30a6</t></rPh></si></sst>' % ns)
            z.writestr("xl/worksheets/sheet1.xml", '<worksheet %s><sheetData><row><c t="inlineStr"><is><t>Notes</t></is></c></row><row><c t="inlineStr"><is><t>x</t></is></c></row></sheetData></worksheet>' % ns)
            z.writestr("xl/worksheets/sheet2.xml", '<worksheet %s><sheetData>'
                       '<row><c t="s"><v>0</v></c><c t="inlineStr"><is><t>UPC</t></is></c><c t="inlineStr"><is><t>Ends</t></is></c></row>'
                       '<row><c t="s"><v>1</v></c><c t="str"><f>TEXT(A1)</f><v>036000291452</v></c><c s="1"><v>46346</v></c></row>'
                       '</sheetData></worksheet>' % ns)
        cols, items = inventory.parse(b.getvalue(), "s.xlsx")
        self.assertEqual(cols, ["Name", "UPC", "Ends"])
        self.assertEqual(items, [{"Name": "\u5546\u54c1", "UPC": "036000291452", "Ends": "2026-11-20"}])

    def test_guesses_and_cleared_choices(self):
        g = inventory.guess_column
        self.assertEqual(g(["Department", "Price Change Date", "Our Price"], "price"), "Our Price")
        self.assertEqual(g(["Bean Type", "UPC Code"], "barcode"), "UPC Code")
        inv = inventory.import_bytes(inventory.empty(), CSV, "a.csv")
        inventory.clean_settings(inv, {"map": dict(inv["map"], summary=None), "tag_column": ""})
        inventory.import_bytes(inv, CSV, "a.csv")
        self.assertIsNone(inv["map"]["summary"])
        self.assertIsNone(inv["tag_column"])


class Feed(BaseHTTPRequestHandler):
    body, ctype, claim = CSV, "text/csv", None

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", Feed.ctype)
        if Feed.claim:
            self.send_header("Content-Length", str(Feed.claim))
        self.end_headers()
        self.wfile.write(Feed.body)


class ApiTests(unittest.TestCase):
    def setUp(self):
        import server
        server.LOG = os.devnull  # keep test errors out of the real log
        self.tmp = tempfile.mkdtemp()
        self.old = inventory.INVENTORY
        inventory.INVENTORY = os.path.join(self.tmp, "inventory.json")
        self.httpd = server.make_server(18350)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.feed = ThreadingHTTPServer(("127.0.0.1", 0), Feed)
        self.feed_url = "http://127.0.0.1:%d/products.csv" % self.feed.server_address[1]
        threading.Thread(target=self.feed.serve_forever, daemon=True).start()
        Feed.body, Feed.ctype, Feed.claim = CSV, "text/csv", None

    def test_cut_off_download(self):
        Feed.claim = len(CSV) + 50
        st, err = self.req("/api/inventory/import", "POST", {"url": self.feed_url})
        self.assertEqual(st, 400)
        self.assertIn("part way", err["error"])

    def tearDown(self):
        for s in (self.httpd, self.feed):
            s.shutdown()
            s.server_close()
        inventory.INVENTORY = self.old

    def req(self, path, method="GET", body=None):
        import urllib.request
        import urllib.error
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r) as f:
                return f.status, json.loads(f.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_file_import_settings_and_remove(self):
        st, inv = self.req("/api/inventory")
        self.assertEqual((st, inv["items"]), (200, []))
        st, inv = self.req("/api/inventory/import", "POST", {"name": "p.csv", "data": base64.b64encode(CSV).decode()})
        self.assertEqual(st, 200)
        self.assertEqual(len(inv["items"]), 2)
        self.assertEqual(inv["source"]["kind"], "file")
        st, inv = self.req("/api/inventory/settings", "POST", {"links": {"ESL_8079EB": "1002"}})
        self.assertEqual(self.req("/api/inventory")[1]["links"], {"ESL_8079EB": "1002"})
        st, err = self.req("/api/inventory/refresh", "POST", {})
        self.assertEqual(st, 400)  # a file can't be refreshed
        st, err = self.req("/api/inventory/import", "POST", {"name": "bad.xlsx", "data": base64.b64encode(b"PK not a zip").decode()})
        self.assertEqual(st, 400)
        self.assertEqual(len(self.req("/api/inventory")[1]["items"]), 2)  # a bad file leaves the old list
        st, inv = self.req("/api/inventory", "DELETE")
        self.assertEqual(inv["items"], [])

    def test_link_import_and_refresh(self):
        st, inv = self.req("/api/inventory/import", "POST", {"url": self.feed_url})
        self.assertEqual(st, 200, inv)
        self.assertEqual(inv["source"]["kind"], "url")
        Feed.body = CSV.replace(b"8.99", b"9.49")
        st, inv = self.req("/api/inventory/refresh", "POST", {})
        self.assertEqual(st, 200)
        self.assertEqual(inv["items"][0]["Retail Price"], "9.49")
        Feed.body, Feed.ctype = b"<html><body>Sign in</body></html>", "text/html"
        st, err = self.req("/api/inventory/refresh", "POST", {})
        self.assertEqual(st, 400)
        self.assertIn("web page", err["error"])
        self.assertEqual(self.req("/api/inventory")[1]["items"][0]["Retail Price"], "9.49")  # kept
        Feed.body, Feed.ctype = json.dumps({"data": {"items": [{"sku": "Z", "price": 1}]}}).encode(), "application/json"
        st, inv = self.req("/api/inventory/refresh", "POST", {})
        self.assertEqual(st, 200)
        self.assertEqual(inv["items"], [{"sku": "Z", "price": "1"}])

    def test_bad_links(self):
        self.assertEqual(self.req("/api/inventory/import", "POST", {"url": "file:///etc/passwd"})[0], 400)
        self.assertEqual(self.req("/api/inventory/import", "POST", {"url": "http://127.0.0.1:1/x.csv"})[0], 400)

    def test_google_sheets_link(self):
        seen = []
        import urllib.request
        real = urllib.request.urlopen

        def fake(req, timeout=0):
            seen.append(req.full_url)
            raise OSError("offline")
        urllib.request.urlopen = fake
        try:
            with self.assertRaises(OSError):
                inventory.fetch("https://docs.google.com/spreadsheets/d/abc_DEF-1/edit#gid=42")
            with self.assertRaises(OSError):
                inventory.fetch("https://docs.google.com/spreadsheets/d/abc/edit?usp=sharing&gid=7")
        finally:
            urllib.request.urlopen = real
        self.assertEqual(seen, ["https://docs.google.com/spreadsheets/d/abc_DEF-1/export?format=csv&gid=42",
                                "https://docs.google.com/spreadsheets/d/abc/export?format=csv&gid=7"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
