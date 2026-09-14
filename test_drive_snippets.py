"""The transport half of Stage 0's bootstrap.

These pin the one thing that could silently corrupt state: the reversal of
Drive's markdown escaping. A snippet that comes back subtly wrong would fold
into a journal or a fills cache that parses cleanly and means something else,
which is exactly the class of failure `journal_fully_readable` cannot catch.
"""

import json

import drive_snippets as ds
import ledger


def test_markdown_punctuation_escapes_are_stripped():
    assert ds.unescape_snippet(r"\_") == "_"
    assert ds.unescape_snippet(r"\[1, 2\]") == "[1, 2]"
    assert ds.unescape_snippet(r"a \- b \+ c \* d \# e") == "a - b + c * d # e"
    assert ds.unescape_snippet(r"\$19.93") == "$19.93"


def test_genuine_json_escapes_survive():
    """The dangerous case: stripping the backslash from \\" turns one JSON
    string into two and changes what the document means."""
    for keep in (r'\"', r"\\", r"\n", r"\t", r"\r", r"\/", r"\b", r"\f",
                 r"é"):
        assert ds.unescape_snippet(keep) == keep


def test_a_string_value_containing_a_quote_round_trips():
    original = json.dumps({"note": 'he said "no" \\ and left', "k": "a_b"})
    # what the escaper does to it: punctuation gets a backslash, JSON escapes
    # are already backslashed and must not be touched twice
    escaped = original.replace("_", r"\_")
    assert json.loads(ds.unescape_snippet(escaped)) == json.loads(original)


def test_real_journal_shape_survives_escaping():
    doc = {"entries": [{"run_id": "manual-2026-09-01-opening-balance-backfill",
                        "kind": "opening_balance",
                        "payload": {"symbol": "MBGL", "quantity": 4.316287,
                                    "reason": "sold at $19.93 -- no matching buy "
                                              "in the account's history [see 10e]"}}]}
    original = json.dumps(doc, indent=1)
    escaped = (original.replace("_", r"\_").replace("[", r"\[")
                       .replace("]", r"\]").replace("$", r"\$")
                       .replace("\n", "  \n "))
    assert json.loads(ds.unescape_snippet(escaped)) == doc


def test_files_from_listing_produces_the_fold_shape():
    listing = {"files": [
        {"title": "journal-2026-09-02.json",
         "contentSnippet": r'{"entries": \[\]}'},
        {"title": "journal-2026-09-01.json",
         "contentSnippet": r'{"entries": \[\]}'},
    ]}
    files = ds.files_from_listing(listing)
    assert [f["title"] for f in files] == ["journal-2026-09-01.json",
                                           "journal-2026-09-02.json"]
    assert ledger.fold_journal(files).unreadable == []


def test_a_file_with_no_snippet_is_skipped_not_yielded_empty():
    """An empty `content` folds as unreadable, which aborts the next run --
    a transport gap must not masquerade as a corrupt journal."""
    listing = {"files": [{"title": "journal-2026-09-02.json"},
                         {"title": "journal-2026-09-03.json",
                          "contentSnippet": ""}]}
    assert ds.files_from_listing(listing) == []


def test_files_from_listing_accepts_the_auto_saved_path(tmp_path):
    p = tmp_path / "listing.json"
    p.write_text(json.dumps({"files": [
        {"title": "fills-cache-2026-09-09.json", "contentSnippet": r"\[\]"}]}))
    files = ds.files_from_listing(str(p))
    assert files[0]["title"] == "fills-cache-2026-09-09.json"
    assert files[0]["content"] == "[]"


def test_titles_matching_splits_a_mixed_listing():
    files = [{"title": "fills-cache-2026-09-09.json", "content": "[]"},
             {"title": "splits-cache-2026-09-08.json", "content": "[]"},
             {"title": "journal-2026-09-09.json", "content": "{}"}]
    assert len(ds.titles_matching(files, "fills-cache-")) == 1
    assert len(ds.titles_matching(files, "splits-cache-")) == 1
    assert len(ds.titles_matching(files, "journal-")) == 1


def test_fills_cache_survives_the_round_trip_with_accounts_intact():
    """`fold_fills_cache` rejects any row without an account, and a dropped
    account is the specific corruption that reconciles against neither
    account (9 September 2026)."""
    rows = [{"symbol": "SGOV", "side": "buy", "quantity": 4.421802,
             "price": 100.64, "on": "2026-08-27",
             "order_id": "a-b-c_d", "account": "688021013"}]
    escaped = json.dumps(rows).replace("_", r"\_")
    files = [{"title": "fills-cache-2026-09-09.json",
              "content": ds.unescape_snippet(escaped)}]
    fills, bad = ledger.fold_fills_cache(files)
    assert bad == []
    assert fills[0].account == "688021013"
    assert fills[0].order_id == "a-b-c_d"
