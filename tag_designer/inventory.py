"""Product data for Tag Designer: read an inventory list (CSV, TSV, JSON or Excel .xlsx, from a file
or a web link) so text on a tag can use {title}, {price}, {barcode} ... of the product the tag is
linked to. Everything is kept in inventory.json next to this file (not in git)."""
import csv
import io
import json
import os
import re
import time
import zipfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
INVENTORY = os.path.join(HERE, "inventory.json")
MAX_ROWS = 20000
MAX_BYTES = 20 << 20
STANDARD = ("title", "name", "price", "summary", "barcode")  # the fields the app offers by name

# column names that usually hold each thing (lower case, spaces and _ ignored)
GUESS = {
    "key": ["sku", "itemid", "productid", "id", "itemcode", "productcode", "code", "item", "partnumber", "part"],
    "title": ["title", "producttitle", "productname", "itemname", "name", "description"],
    "name": ["name", "productname", "itemname", "shortname", "title"],
    "price": ["price", "saleprice", "retailprice", "unitprice", "sellprice", "sellingprice", "listprice", "msrp", "cost"],
    "summary": ["summary", "shortdescription", "description", "details", "subtitle", "notes"],
    "barcode": ["barcode", "upc", "ean", "gtin", "ean13", "upca", "upccode", "barcodenumber"],
    "tag": ["tag", "tagname", "esl", "eslname", "label", "labelid", "eslid", "shelftag"],
}


def empty():
    return {"source": None, "columns": [], "items": [], "key": "", "tag_column": "", "map": {},
            "currency": "$", "links": {}, "refresh_before_send": True}


def load():
    try:
        with open(INVENTORY) as f:
            inv = json.load(f)
        base = empty()
        base.update({k: v for k, v in inv.items() if k in base})
        return base
    except (OSError, ValueError):
        return empty()


def save(inv):
    tmp = INVENTORY + ".tmp"
    with open(tmp, "w") as f:
        json.dump(inv, f, indent=1)
    os.replace(tmp, INVENTORY)
    return inv


def _norm(s):
    return re.sub(r"[\s_\-.]+", "", str(s).lower())


def guess_column(columns, what, taken=()):
    by = {_norm(c): c for c in columns}
    for want in GUESS[what]:
        c = by.get(want)
        if c and c not in taken:
            return c
    # "Retail Price (USD)" and the like: a whole word of the heading, or two words together;
    # headings about dates or changes are passed over ("Price Change Date" is not the price)
    for want in GUESS[what]:
        for c in columns:
            words = re.findall(r"[a-z0-9]+", str(c).lower())
            if c in taken or {"date", "time", "change", "changed", "updated"} & set(words):
                continue
            pairs = {a + b for a, b in zip(words, words[1:])}
            if want in words or want in pairs:
                return c
    return ""


def unique_headings(head):
    """Name empty headings and make repeated ones unique (Price, Price -> Price, Price_2)."""
    out, used = [], set()
    for i, h in enumerate(head):
        h = str(h).strip() or "column_%d" % (i + 1)
        base, n = h, 2
        while h in used:
            h = "%s_%d" % (base, n)
            n += 1
        used.add(h)
        out.append(h)
    return out


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v).strip()


def _table(rows):
    """list of dicts -> (columns in first-seen order, rows of strings), at most MAX_ROWS."""
    cols, seen, out = [], set(), []
    for r in rows[:MAX_ROWS]:
        if not isinstance(r, dict):
            continue
        row = {}
        for k, v in r.items():
            k = str(k).strip()
            if not k:
                continue
            if k not in seen:
                seen.add(k)
                cols.append(k)
            row[k] = _cell(v)[:2000]
        if any(row.values()):
            out.append(row)
    return cols, out


def _flatten(d, pre=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict) and not pre:  # one level: {"price": {"amount": 4}} -> price.amount
            for k2, v2 in v.items():
                out["%s.%s" % (k, k2)] = v2
        else:
            out[pre + str(k)] = v
    return out


def parse_json(data):
    obj = json.loads(_text(data))
    if isinstance(obj, dict):  # {"products": [...]} or {"data": {"items": [...]}}
        lists = [v for v in obj.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
        if not lists:
            for v in obj.values():
                if isinstance(v, dict):
                    lists += [w for w in v.values() if isinstance(w, list) and w and isinstance(w[0], dict)]
        if not lists:
            raise ValueError("No list of products found in this JSON.")
        obj = max(lists, key=len)
    if not isinstance(obj, list):
        raise ValueError("Expected a list of products in this JSON.")
    return _table([_flatten(r) for r in obj if isinstance(r, dict)])


def _text(data):
    """UTF-8 (with or without a BOM), else Windows Excel's own encoding."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def parse_csv(data):
    text = _text(data)
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if sample.count("\t") > sample.count(",") else csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("The file is empty.")
    head = unique_headings(rows[0])
    return _table([dict(zip(head, r)) for r in rows[1:]])


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _kids(el, name):
    return [c for c in el if _local(c.tag) == name]


def _first(el, name):
    k = _kids(el, name)
    return k[0] if k else None


def _col_index(ref):
    m = re.match(r"[A-Z]+", ref or "")
    if not m:
        return None
    n = 0
    for ch in m.group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _rich_text(el):
    """The text of a string item: plain <t>, or the <t> of each formatted run; not the phonetic guides (<rPh>)."""
    parts = []
    for c in el:
        if _local(c.tag) == "t":
            parts.append(c.text or "")
        elif _local(c.tag) == "r":
            parts += [t.text or "" for t in _kids(c, "t")]
    return "".join(parts)


DATE_FORMATS = set(range(14, 23)) | {45, 46, 47}


def _date_styles(z):
    """Which cell styles show a date or time (so 46346 becomes 2026-11-20)."""
    try:
        root = ET.fromstring(z.read("xl/styles.xml"))
    except (KeyError, ET.ParseError):
        return set()
    custom = {}
    for nf in root.iter():
        if _local(nf.tag) == "numFmt":
            code = re.sub(r'"[^"]*"|\[[^]]*\]|\\.', "", nf.get("formatCode", "")).lower()
            custom[int(nf.get("numFmtId", "0"))] = bool(re.search(r"[dmyhs]", code))
    out = set()
    for xfs in root.iter():
        if _local(xfs.tag) == "cellXfs":
            for i, xf in enumerate(_kids(xfs, "xf")):
                fid = int(xf.get("numFmtId", "0"))
                if fid in DATE_FORMATS or custom.get(fid):
                    out.add(i)
    return out


def _excel_date(v):
    import datetime
    f = float(v)
    d = datetime.datetime(1899, 12, 30) + datetime.timedelta(days=f)
    if f == int(f):
        return d.strftime("%Y-%m-%d")
    if f < 1:
        return d.strftime("%H:%M")
    return d.strftime("%Y-%m-%d %H:%M")


def _first_sheet(z):
    """The first sheet as Excel lists it (workbook order), not the first file name."""
    names = z.namelist()
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        sheets = [e for e in wb.iter() if _local(e.tag) == "sheet"]
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels.iter() if _local(r.tag) == "Relationship"}
        if sheets:
            rid = next((v for k, v in sheets[0].attrib.items() if _local(k) == "id"), None)
            t = targets.get(rid, "")
            t = t.lstrip("/") if t.startswith("/") else "xl/" + t
            if t in names:
                return t
    except (KeyError, ET.ParseError):
        pass
    sheets = sorted((n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                    key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
    if not sheets:
        raise ValueError("No sheet found in this Excel file.")
    return sheets[0]


def parse_xlsx(data):
    """The first sheet of an Excel file (no extra libraries needed)."""
    z = zipfile.ZipFile(io.BytesIO(data))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        shared = [_rich_text(si) for si in _kids(ET.fromstring(z.read("xl/sharedStrings.xml")), "si")]
    dates = _date_styles(z)
    grid = []
    for row in ET.fromstring(z.read(_first_sheet(z))).iter():
        if _local(row.tag) != "row":
            continue
        cells, pos = {}, 0
        for c in _kids(row, "c"):
            i = _col_index(c.get("r"))
            pos = pos if i is None else i  # cells may leave out their reference: then they follow on
            t, v = c.get("t"), _first(c, "v")
            if t == "s" and v is not None:
                k = int(v.text)
                val = shared[k] if 0 <= k < len(shared) else ""
            elif t == "inlineStr":
                isel = _first(c, "is")
                val = _rich_text(isel) if isel is not None else ""
            elif t in ("str", "e") and v is not None:
                val = v.text or ""
            elif t == "b" and v is not None:
                val = "yes" if v.text == "1" else "no"
            elif v is not None and v.text is not None:
                val = v.text
                try:
                    if c.get("s") is not None and int(c.get("s")) in dates:
                        val = _excel_date(val)
                    else:
                        val = _cell(round(float(val), 10))
                except ValueError:
                    pass
            else:
                val = ""
            cells[pos] = val
            pos += 1
        if cells:
            grid.append([cells.get(i, "") for i in range(max(cells) + 1)])
        if len(grid) > MAX_ROWS + 1:
            break
    grid = [r for r in grid if any(str(c).strip() for c in r)]
    if not grid:
        raise ValueError("The sheet is empty.")
    head = unique_headings(grid[0])
    return _table([dict(zip(head, r)) for r in grid[1:]])


def parse(data, name=""):
    name = (name or "").lower()
    if data[:4] == b"PK\x03\x04" or name.endswith(".xlsx"):
        return parse_xlsx(data)
    head = data.lstrip(b"\xef\xbb\xbf \t\r\n")[:1]
    if name.endswith(".json") or head in (b"[", b"{"):
        return parse_json(data)
    return parse_csv(data)


def fetch(url, timeout=20):
    """Download a list from a web link (Google Sheets 'publish as CSV', a database export, an API)."""
    import urllib.request
    if not re.match(r"https?://", url or "", re.I):
        raise ValueError("Use a link that starts with https:// or http://")
    m = re.match(r"(https://docs\.google\.com/spreadsheets/d/[\w-]+)(/(edit|view)?[^#]*)?(#.*)?$", url)
    if m and "/export" not in url and "/pub" not in url:
        # a normal Google Sheets link: ask for it as CSV (the sheet must be shared "anyone with the link")
        g = re.search(r"[?&#]gid=(\d+)", url)
        url = m.group(1) + "/export?format=csv" + ("&gid=" + g.group(1) if g else "")
    req = urllib.request.Request(url, headers={"User-Agent": "TagDesigner/1", "Accept": "application/json, text/csv, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(MAX_BYTES + 1)
        ctype = r.headers.get("Content-Type", "")
        want = r.headers.get("Content-Length")
    if want and want.isdigit() and len(data) < int(want) and len(data) <= MAX_BYTES:
        raise ValueError("The download stopped part way. Try again.")
    if len(data) > MAX_BYTES:
        raise ValueError("That list is larger than 20 MB.")
    if b"<html" in data[:2000].lower():
        raise ValueError("That link gives a web page, not a list. For Google Sheets use File > Share > Publish to web > CSV, "
                         "or share the sheet with 'anyone with the link'.")
    name = url.split("?")[0].rsplit("/", 1)[-1]
    if "json" in ctype:
        name += ".json"
    return data, name


def apply_import(inv, columns, items, source):
    """Take a new list, keeping your choices where the columns still exist."""
    if not items:
        raise ValueError("No products found: the list needs a heading row and at least one product.")
    inv["columns"], inv["items"], inv["source"] = columns, items, source
    if inv.get("key") not in columns:
        inv["key"] = guess_column(columns, "key") or columns[0]
    m = {k: v for k, v in (inv.get("map") or {}).items() if v in columns or v is None}  # None = you chose "not used"
    for f in STANDARD:
        if f not in m:
            m[f] = guess_column(columns, f)
    inv["map"] = m
    if inv.get("tag_column") is not None and inv.get("tag_column") not in columns:
        inv["tag_column"] = guess_column(columns, "tag")
    return inv


def import_bytes(inv, data, name, url=None):
    cols, items = parse(data, name)
    src = {"kind": "url" if url else "file", "name": name if not url else url, "url": url,
           "updated": int(time.time()), "rows": len(items)}
    return apply_import(inv, cols, items, src)


def refresh(inv):
    src = inv.get("source") or {}
    if src.get("kind") != "url":
        raise ValueError("This list came from a file. Load the file again to update it.")
    data, name = fetch(src["url"])
    return import_bytes(inv, data, name, url=src["url"])


def clean_settings(inv, req):
    cols = inv.get("columns") or []
    if "key" in req and req["key"] in cols:
        inv["key"] = req["key"]
    if "tag_column" in req and (req["tag_column"] in cols or not req["tag_column"]):
        inv["tag_column"] = req["tag_column"] or None  # None: you chose none, so a new list won't guess one
    if isinstance(req.get("map"), dict):
        inv["map"] = {f: (v if v in cols else None) for f, v in req["map"].items() if f in STANDARD and (isinstance(v, str) or v is None)}
    if isinstance(req.get("currency"), str):
        inv["currency"] = req["currency"][:4]
    if "refresh_before_send" in req:
        inv["refresh_before_send"] = bool(req["refresh_before_send"])
    if isinstance(req.get("links"), dict):
        inv["links"] = {str(t)[:40]: str(k)[:200] for t, k in req["links"].items() if isinstance(k, str) and k}
    return inv
