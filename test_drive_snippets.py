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


def markdown_escape(text):
    """What Drive actually does to a file's text on the way into a snippet.

    Verified against the real connector on 18 September 2026: every backslash
    already in the file is DOUBLED, and a backslash is inserted before
    punctuation. Both halves matter -- a model that escapes only punctuation
    (which is what this test file assumed until that date) makes the broken
    case unrepresentable.
    """
    text = text.replace("\\", "\\\\")
    for ch in "_[]$-+*#":
        text = text.replace(ch, "\\" + ch)
    return text.replace("\n", "  \n ")


def test_genuine_json_escapes_survive():
    """The dangerous case: a JSON escape reaches us DOUBLED, and must come
    back out single. Losing the backslash from \\" turns one JSON string into
    two and changes what the document means; keeping both backslashes closes
    the string two characters early, which is how 18 September 2026 aborted."""
    for keep in (r'\"', r"\\", r"\n", r"\t", r"\r", r"\/", r"\b", r"\f",
                 r"\u00e9"):
        assert ds.unescape_snippet(markdown_escape(keep)) == keep


def test_a_string_value_containing_a_quote_round_trips():
    original = json.dumps({"note": 'he said "no" \\ and left', "k": "a_b"})
    assert json.loads(ds.unescape_snippet(markdown_escape(original))) \
        == json.loads(original)


def test_a_journal_entry_quoting_json_round_trips():
    """The exact shape that aborted the 2026-09-18 run: a journal note whose
    text quotes a fragment of JSON, so the stored file legitimately contains
    backslash-escaped quotes inside a string value.

    Both journal-2026-09-15-1.json and journal-2026-09-16-1.json were written
    by watchdog review passes that quoted JSON in their own prose, and both
    landed in `unreadable_files` for this reason -- a blocking Stage 0 check
    firing on a transport defect, not on a corrupt journal.
    """
    doc = {"entries": [{"kind": "note", "payload": {
        "detail": 'the extra entry is one duplicated '
                  '{"service":"alpha_vantage","operation":"SPLITS"} record'}}]}
    original = json.dumps(doc)
    assert json.loads(ds.unescape_snippet(markdown_escape(original))) == doc


def test_the_2026_09_16_snippet_verbatim():
    """Not a synthesised escape -- the literal snippet the Drive connector
    returned for journal-2026-09-16-1.json, byte for byte, next to the file's
    own downloaded content. Pinned so no future change to `unescape_snippet`
    can be validated against a model of the escaper instead of the escaper."""
    snippet = (
        '{"entries":\\[{"run\\_id":"2026-09-16-0620","kind":"note","payload":'
        '{"topic":"manifest\\_call\\_count\\_correction","detail":"one duplicated '
        '{\\\\"service\\\\":\\\\"alpha\\_vantage\\\\",\\\\"operation\\\\":\\\\"SPLITS\\\\"} record"}}\\]}'
    )
    downloaded = (
        '{"entries":[{"run_id":"2026-09-16-0620","kind":"note","payload":'
        '{"topic":"manifest_call_count_correction","detail":"one duplicated '
        '{\\"service\\":\\"alpha_vantage\\",\\"operation\\":\\"SPLITS\\"} record"}}]}'
    )
    assert ds.unescape_snippet(snippet) == downloaded
    listing = {"files": [{"title": "journal-2026-09-16-1.json",
                          "contentSnippet": snippet}]}
    assert ledger.fold_journal(ds.files_from_listing(listing)).unreadable == []


def test_real_journal_shape_survives_escaping():
    doc = {"entries": [{"run_id": "manual-2026-09-01-opening-balance-backfill",
                        "kind": "opening_balance",
                        "payload": {"symbol": "MBGL", "quantity": 4.316287,
                                    "reason": "sold at $19.93 -- no matching buy "
                                              "in the account's history [see 10e]"}}]}
    original = json.dumps(doc, indent=1)
    assert json.loads(ds.unescape_snippet(markdown_escape(original))) == doc


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
