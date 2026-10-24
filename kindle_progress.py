#!/usr/bin/env python3
"""Kindle reading-progress exporter (personal use only).

Reads YOUR OWN Kindle library through Amazon's Cloud Reader endpoints
using YOUR OWN browser session cookies, and reports the books you have
read over 90%.

Why this exists: Amazon offers no public Kindle API and blocks automated
browsers, so this script runs on your own computer with your own session
instead of through an AI browser.

What it does NOT do:
  - It never sends your cookies anywhere except to Amazon.
  - It never prints your cookies.
  - It downloads no book content, buys nothing, and changes nothing.
  - The output file contains only book titles, authors, progress and
    dates - no credentials of any kind.

Requirements:
  - Python 3.9+
  - pip install curl_cffi   (mimics a real browser's TLS fingerprint so
    Amazon does not reject the requests as automated traffic; plain
    Python requests are often blocked. If curl_cffi is unavailable the
    script falls back to urllib with a warning.)

Usage:
  1. In your own browser, sign in to Amazon and open read.amazon.com
     (or read.amazon.ca if that is where your library loads).
  2. Export cookies for that site to a Netscape-format cookies.txt file
     (e.g. with the "Get cookies.txt LOCALLY" browser extension).
  3. Run:
       python3 kindle_progress.py --cookies cookies.txt
  4. Send the resulting kindle-progress.json (or the printed summary)
     to Muse - that file contains no credentials.

  Useful options:
    --domain read.amazon.ca     use the Canadian Cloud Reader domain
    --max-books 5               only check details for the first 5 books
                                (good for a first test run)
    --min-progress 90           progress threshold (default 90)
    --year 2026                 year used for the "read this year" list
    --delay 5                   seconds between per-book requests
                                (default 5; keeps Amazon happy)
    --device-token TOKEN        optional registered-device token; only
                                needed if the progress calls fail without
                                it (see the setup guide)

Security: keep cookies.txt private. It grants access to your Amazon
account. NEVER paste it into a chat or send it to anyone, including Muse.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "en-US,en;q=0.9"

# Cookies the Cloud Reader session needs (names as Amazon sets them).
WANTED_COOKIES = ("at-main", "session-id", "ubid-main", "x-main")

JSONP_RE = re.compile(r"\(({.*})\)", re.DOTALL)


def parse_cookies_file(path):
    """Parse a Netscape-format cookies.txt file into {name: value}."""
    cookies = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            name, value = parts[5], parts[6]
            if name in WANTED_COOKIES:
                cookies[name] = value
    return cookies


def parse_cookie_string(raw):
    """Parse a pasted 'name=value; name=value' cookie header string."""
    cookies = {}
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name, value = name.strip(), value.strip()
        if name in WANTED_COOKIES:
            cookies[name] = value
    return cookies


def cookie_header(cookies):
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def make_session():
    """Return (request_fn, name). Prefers curl_cffi (browser TLS)."""
    try:
        from curl_cffi import requests as crequests

        session = crequests.Session(impersonate="chrome")

        def do_get(url, headers):
            resp = session.get(url, headers=headers, timeout=60)
            return resp.status_code, dict(resp.headers), resp.text

        return do_get, "curl_cffi (browser TLS impersonation)"
    except ImportError:
        import urllib.request

        def do_get(url, headers):
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                return exc.code, dict(exc.headers), exc.read().decode("utf-8", "replace")

        print(
            "WARNING: curl_cffi is not installed; falling back to urllib.\n"
            "Amazon may reject these requests as automated traffic.\n"
            "Install it with: pip install curl_cffi",
            file=sys.stderr,
        )
        return do_get, "urllib (fallback)"


def extract_set_cookie(headers, name):
    """Pull one cookie value out of Set-Cookie header(s)."""
    raw = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
    if isinstance(raw, (list, tuple)):
        raw = "; ".join(raw)
    match = re.search(rf"(?:^|,)\s*{re.escape(name)}=([^;,]+)", raw)
    return match.group(1) if match else None


def parse_json_body(text):
    """Cloud Reader responses wrap the payload in a 'body' string."""
    data = json.loads(text)
    body = data.get("body") if isinstance(data, dict) else None
    if isinstance(body, str):
        return json.loads(body)
    if isinstance(data, dict) and "itemsList" in data:
        return data
    return body if body is not None else data


def parse_jsonp(text):
    match = JSONP_RE.search(text)
    if not match:
        return None
    return json.loads(match.group(1))


def parse_details_response(text):
    """Parse a startReading response, tolerating both shapes.

    Legacy format: {"body": "<json string>"} (or a dict body).
    Current format: fields at the top level, like getDeviceToken.
    Raises ValueError on genuinely non-JSON payloads.
    """
    outer = json.loads(text)
    if not isinstance(outer, dict):
        return outer
    if "body" in outer:
        body = outer["body"]
        if isinstance(body, str):
            return json.loads(body)
        return body
    return outer


def extract_device_session(payload):
    """Split a getDeviceToken response into (shape, token_data).

    Current format: deviceSessionToken sits at the top level of the
    JSON response, alongside eid, clientHashId and deviceName. Older
    format: wrapped in a string 'body' field. Only the shape and key
    names are ever recorded; token values are never logged or saved.
    """
    if isinstance(payload, dict) and "deviceSessionToken" in payload:
        return "top-level", payload
    raw_body = payload.get("body") if isinstance(payload, dict) else None
    if isinstance(raw_body, str):
        try:
            return "body-string", json.loads(raw_body)
        except ValueError:
            return "unparsable", {}
    if isinstance(raw_body, dict):
        return "body-object", raw_body
    return "no-body-field", {}


def parse_sync_time(value):
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser(description="Export Kindle reading progress.")
    parser.add_argument("--cookies", help="Path to Netscape-format cookies.txt")
    parser.add_argument("--cookie-string", help="Raw 'name=value; ...' cookie header")
    parser.add_argument("--domain", default="read.amazon.com",
                        help="Cloud Reader domain (default: read.amazon.com)")
    parser.add_argument("--library-types", default="BOOKS,DOCS",
                        help="Comma-separated library types to search "
                             "(default: BOOKS,DOCS; DOCS covers personal "
                             "documents such as PDFs sent to Kindle)")
    parser.add_argument("--page-size", type=int, default=200,
                        help="Books to fetch in the library call (default: 200)")
    parser.add_argument("--max-books", type=int, default=0,
                        help="Only check details for the first N books (0 = all)")
    parser.add_argument("--min-progress", type=float, default=90.0,
                        help="Progress %% counted as read (default: 90)")
    parser.add_argument("--year", type=int, default=2026,
                        help="Year for the 'read this year' list (default: 2026)")
    parser.add_argument("--delay", type=float, default=5.0,
                        help="Seconds between per-book requests (default: 5)")
    parser.add_argument("--device-token", default="",
                        help="Optional registered-device token for the ADP session")
    parser.add_argument("--out", default="kindle-progress.json",
                        help="Output JSON file (default: kindle-progress.json)")
    args = parser.parse_args()

    if bool(args.cookies) == bool(args.cookie_string):
        print("ERROR: pass exactly one of --cookies or --cookie-string.",
              file=sys.stderr)
        return 2

    cookies = (parse_cookies_file(args.cookies) if args.cookies
               else parse_cookie_string(args.cookie_string))
    missing = [name for name in WANTED_COOKIES if name not in cookies]
    if missing:
        print(f"ERROR: cookies file is missing: {', '.join(missing)}.\n"
              "Export cookies while signed in to Amazon on the same domain "
              "you passed with --domain, then retry.",
              file=sys.stderr)
        return 2

    do_get, transport = make_session()
    print(f"Using transport: {transport}")
    base_headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": ACCEPT_LANGUAGE,
        "Cookie": cookie_header(cookies),
    }

    # Diagnostics recorded into the output file so a failed run can be
    # understood from the JSON alone, without seeing terminal output.
    # Only key names / shapes are recorded here, never secret values.
    diagnostics = {
        "deviceTokenProvided": bool(args.device_token),
        "deviceTokenHttpStatus": None,
        "deviceTokenResponseShape": None,
        "deviceTokenBodyKeys": None,
        "adpSessionObtained": False,
        "sessionIdRefreshed": False,
        "libraryItemKeys": None,
        "libraryTypeCounts": {},
        "startReadingInfoKeys": None,
    }

    # 1. Library list -------------------------------------------------
    # Books (purchased/borrowed) and Docs (personal documents such as
    # PDFs sent to Kindle) live in separate catalogs, so query each type
    # and merge. Unknown or rejected types are skipped, never fatal.
    lib_types = [t.strip() for t in args.library_types.split(",") if t.strip()]
    items = []
    headers = {}
    for lib_type in lib_types:
        books_url = (
            f"https://{args.domain}/kindle-library/search"
            f"?query=&libraryType={lib_type}&sortType=recency"
            f"&querySize={args.page_size}"
        )
        print(f"Fetching library ({lib_type})...")
        status, resp_headers, text = do_get(books_url, base_headers)
        if lib_type == lib_types[0]:
            headers = resp_headers
        if status != 200:
            if lib_type == lib_types[0]:
                print(f"ERROR: library request failed (HTTP {status}).\n"
                      "Your session may have expired - re-export cookies "
                      "and retry.\n"
                      "If Amazon shows a bot/refusal page, the session is "
                      "not usable from a script either.", file=sys.stderr)
                return 1
            print(f"WARNING: library type {lib_type} returned HTTP "
                  f"{status}; skipping it.", file=sys.stderr)
            diagnostics["libraryTypeCounts"][lib_type] = f"http-{status}"
            continue
        try:
            library = parse_json_body(text)
            type_items = (library.get("itemsList", [])
                          if isinstance(library, dict) else [])
        except (ValueError, AttributeError):
            if lib_type == lib_types[0]:
                print("ERROR: library response was not JSON - session may "
                      "be invalid or Amazon returned a login/bot page. "
                      "Re-export cookies and retry.", file=sys.stderr)
                return 1
            print(f"WARNING: library type {lib_type} returned an "
                  f"unparsable response; skipping it.", file=sys.stderr)
            diagnostics["libraryTypeCounts"][lib_type] = "unparsable"
            continue
        diagnostics["libraryTypeCounts"][lib_type] = len(type_items)
        items.extend(type_items)

    if items:
        keys = set()
        for entry in items:
            if isinstance(entry, dict):
                keys.update(entry.keys())
        diagnostics["libraryItemKeys"] = sorted(keys)

    if not items:
        print("No books returned. Check that the cookies came from the same "
              "Amazon account/domain as your Kindle library.", file=sys.stderr)
        return 1

    print(f"Found {len(items)} item(s) across library types "
          f"{diagnostics['libraryTypeCounts']}.")
    if len(items) >= args.page_size * len(lib_types):
        print("NOTE: a response hit the page-size limit; there may be more "
              "items. Re-run with a larger --page-size if your library is "
              "bigger.")

    # Refresh session id from the library response, if Amazon rotated it.
    # Mirror the reference library exactly: the rotated id goes ONLY in
    # the x-amzn-sessionid header. The Cookie header keeps the original
    # login session; rewriting it diverged from the known-working flow
    # and may break the per-book endpoints.
    new_session = extract_set_cookie(headers, "session-id")
    if new_session:
        diagnostics["sessionIdRefreshed"] = True
        base_headers["x-amzn-sessionid"] = new_session

    # 2. Optional ADP device session ----------------------------------
    if args.device_token:
        token_url = (
            f"https://{args.domain}/service/web/register/getDeviceToken"
            f"?serialNumber={args.device_token}&deviceType={args.device_token}"
        )
        status, _, text = do_get(token_url, base_headers)
        diagnostics["deviceTokenHttpStatus"] = status
        try:
            token_body = json.loads(text)
            shape, token_data = extract_device_session(token_body)
            diagnostics["deviceTokenResponseShape"] = shape
            if isinstance(token_data, dict):
                diagnostics["deviceTokenBodyKeys"] = sorted(token_data.keys())
            adp = token_data.get("deviceSessionToken") if isinstance(token_data, dict) else None
            if adp:
                diagnostics["adpSessionObtained"] = True
                base_headers["x-adp-session-token"] = adp
                print("Device session token obtained.")
            else:
                print("WARNING: device token call succeeded but returned no "
                      "session token; continuing without it.", file=sys.stderr)
        except (ValueError, KeyError, TypeError):
            diagnostics["deviceTokenResponseShape"] = "unparsable"
            print("WARNING: could not obtain a device session token; "
                  "continuing without it.", file=sys.stderr)
    else:
        print("NOTE: no --device-token given; progress lookups may be "
              "forbidden (HTTP 403). See the setup guide for how to find "
              "your device token.", file=sys.stderr)

    # 3. Per-book progress --------------------------------------------
    results = []
    books = items if not args.max_books else items[: args.max_books]
    for index, book in enumerate(books, start=1):
        asin = book.get("asin")
        title = book.get("title", "Unknown title")
        authors_raw = book.get("authors") or []
        authors = authors_raw[0].split(":") if authors_raw else []
        authors = [a.strip() for a in authors if a.strip()]
        print(f"[{index}/{len(books)}] {title} ...", flush=True)
        record = {
            "title": title,
            "authors": authors,
            "asin": asin,
            "originType": book.get("originType"),
            "percentageRead": None,
            "syncDate": None,
            "reportedOnDevice": None,
            "status": "unknown",
        }
        # Progress from the library list, when present. This is only
        # used on its own when we have no device session: the per-book
        # sync position is authoritative, and the library value can sit
        # at 0 for books never opened in the Cloud Reader.
        lib_pct_value = None
        lib_pct = book.get("percentageRead")
        if lib_pct is not None:
            try:
                lib_pct_value = round(float(lib_pct), 1)
            except (TypeError, ValueError):
                lib_pct_value = None
        if lib_pct_value is not None and not diagnostics["adpSessionObtained"]:
            record["percentageRead"] = lib_pct_value
            record["status"] = "library"
            results.append(record)
            continue
        if not asin:
            if lib_pct_value is not None:
                record["percentageRead"] = lib_pct_value
                record["status"] = "library"
            else:
                record["status"] = "no-asin"
            results.append(record)
            continue
        try:
            status, _, text = do_get(
                f"https://{args.domain}/service/mobile/reader/startReading"
                f"?asin={asin}&clientVersion=2000010",
                base_headers,
            )
            if status != 200:
                if lib_pct_value is not None:
                    record["percentageRead"] = lib_pct_value
                    record["status"] = f"library-fallback-after-http-{status}"
                else:
                    record["status"] = f"details-http-{status}"
                results.append(record)
                continue
            info = parse_details_response(text)
            if not isinstance(info, dict):
                raise ValueError("details response is not a JSON object")
            if diagnostics.get("startReadingInfoKeys") is None:
                diagnostics["startReadingInfoKeys"] = sorted(info.keys())
            last = info.get("lastPageReadData")
            if not isinstance(last, dict):
                # Response shape changed (no usable lastPageReadData):
                # record it honestly instead of mislabeling the book
                # never-opened. The keys above reveal the real fields.
                record["status"] = "details-shape-unknown"
                results.append(record)
                continue
            position = last.get("position")
            sync = parse_sync_time(last.get("syncTime"))
            record["reportedOnDevice"] = last.get("deviceName")
            record["syncDate"] = sync.isoformat() if sync else None
            meta_url = info.get("metadataUrl")
            if position is None or not meta_url:
                record["status"] = "never-opened"
                record["percentageRead"] = 0.0
                results.append(record)
                continue
            status, _, meta_text = do_get(meta_url, base_headers)
            meta = parse_jsonp(meta_text) if status == 200 else None
            if not meta or not meta.get("endPosition"):
                record["status"] = "metadata-unavailable"
                results.append(record)
                continue
            start = meta.get("startPosition") or 0
            end = meta["endPosition"]
            pct = round(((start + position) / end) * 100, 1)
            record["percentageRead"] = pct
            record["status"] = "ok"
        except (ValueError, KeyError, TypeError) as exc:
            if lib_pct_value is not None:
                record["percentageRead"] = lib_pct_value
                record["status"] = "library-fallback-after-parse-error"
            else:
                record["status"] = f"parse-error: {exc}"
        results.append(record)
        if index < len(books):
            time.sleep(args.delay)

    # 4. Classify ------------------------------------------------------
    # The library feed carries no last-read date, so books whose progress
    # came from the library list land in a separate "no date" bucket for
    # a one-time year confirmation instead of being silently counted.
    read_this_year = []
    over_threshold_older = []
    over_threshold_no_date = []
    for record in results:
        pct = record.get("percentageRead")
        if pct is None or pct <= args.min_progress:
            continue
        sync = parse_sync_time(record.get("syncDate"))
        if sync and sync.year == args.year:
            read_this_year.append(record)
        elif sync:
            over_threshold_older.append(record)
        else:
            over_threshold_no_date.append(record)

    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "diagnostics": diagnostics,
        "threshold": args.min_progress,
        "year": args.year,
        "totalBooksChecked": len(results),
        "readThisYear": read_this_year,
        "overThresholdOlderSync": over_threshold_older,
        "overThresholdNoDate": over_threshold_no_date,
        "all": results,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)

    print()
    print(f"== Read in {args.year} (over {args.min_progress}%) ==")
    if read_this_year:
        for record in read_this_year:
            by = f" by {', '.join(record['authors'])}" if record["authors"] else ""
            print(f"  - {record['title']}{by} ({record['percentageRead']}%)")
    else:
        print("  (none found)")
    print(f"== Over {args.min_progress}% but last synced before {args.year} ==")
    for record in over_threshold_older:
        by = f" by {', '.join(record['authors'])}" if record["authors"] else ""
        print(f"  - {record['title']}{by} ({record['percentageRead']}%, "
              f"last sync {record['syncDate']})")
    print(f"== Over {args.min_progress}% but no last-read date from Amazon ==")
    if over_threshold_no_date:
        for record in over_threshold_no_date:
            by = f" by {', '.join(record['authors'])}" if record["authors"] else ""
            print(f"  - {record['title']}{by} ({record['percentageRead']}%) "
                  f"[confirm read in {args.year}]")
    else:
        print("  (none)")
    forbidden = [r for r in results if "http-403" in r["status"]]
    if forbidden:
        print()
        print("NOTE: progress lookups were forbidden (HTTP 403). Amazon's "
              "progress endpoint usually requires a device session token. "
              "In your browser, open read.amazon.com with Developer Tools "
              "-> Network open, find the getDeviceToken request, and "
              "re-run with --device-token <its serialNumber value>.")
    print()
    print(f"Full results (no credentials) written to {args.out}.")
    print("Send that file, or the summary above, to Muse to update your "
          "reading tracker.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
